"""The upstream HTTP client.

Uses :mod:`http.client` from the standard library behind a small
per-origin connection pool. It is deliberately small and explicit:

* **Connections are reused.** Every adapter owns one client, and one client
  keeps a bounded pool of idle keep-alive connections per origin, so a short
  non-streaming request no longer pays a fresh TCP handshake (and, over TLS,
  a fresh handshake) before the provider does any work.
* **Streaming is incremental.** SSE frames are yielded as they arrive, using
  line reads rather than a blocking buffered read, so a response is not held
  in memory and a slow upstream is not stalled.
* **Credentials never appear in an exception.** The failures raised here are
  :class:`OSError` and :class:`http.client.HTTPException` subclasses, which
  name the host and the port and never carry request headers, so the API key
  cannot leak through a traceback.
* **Redirects are not followed** for upstream calls, so a redirect cannot
  quietly move a credential to another host.

Pooling introduces one failure mode a connection-per-request client cannot
have: the peer may close an idle connection while the pool still holds it.
That is detected **before** a request is written, never after, so a request
is never replayed on the grounds that a pooled connection turned out to be
dead. See :meth:`_ConnectionPool.acquire`.
"""

from __future__ import annotations

import atexit
import http.client
import json
import select
import socket
import ssl
import threading
import time
import urllib.parse
import weakref
from dataclasses import dataclass
from typing import Any, Iterator

from gateway.adapters.base import AdapterError
from gateway.config import ERROR_TRANSIENT, ERROR_UNKNOWN

DEFAULT_TIMEOUT = 120.0
DEFAULT_CONNECT_TIMEOUT = 10.0
#: How many idle connections one client keeps per origin. Overflow is closed
#: immediately, so the pool is a bound and not a cache that grows without one.
DEFAULT_IDLE_CONNECTIONS = 4

#: Every live client, so a single shutdown hook can release their sockets.
#: A weak set: a client must never be kept alive by being closable.
_LIVE_CLIENTS: "weakref.WeakSet[UpstreamClient]" = weakref.WeakSet()


def close_all_clients() -> None:
    """Release every pooled socket at interpreter exit or shutdown.

    The normal path is the explicit close on shutdown in
    :meth:`gateway.adapters.registry.AdapterRegistry.close`. This is also
    the backstop for a process that exits without it, so a keep-alive pool
    cannot leave sockets dangling behind a worker that never closed.
    """
    for client in list(_LIVE_CLIENTS):
        try:
            client.close()
        except Exception:  # pragma: no cover - defensive
            pass


atexit.register(close_all_clients)


class _ConnectionPool:
    """Bounded idle keep-alive connections, keyed by origin.

    A connection is checked out by exactly one thread, so an
    :class:`http.client.HTTPConnection` is never used concurrently. The pool
    itself is only touched under the lock.
    """

    def __init__(self, max_idle: int = DEFAULT_IDLE_CONNECTIONS) -> None:
        self.max_idle = max_idle
        self._idle: dict[tuple[str, str, int], list[http.client.HTTPConnection]] = {}
        self._lock = threading.Lock()
        self._closed = False
        self._ssl_context: ssl.SSLContext | None = None

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def _context(self) -> ssl.SSLContext:
        # Built once per pool. Loading the CA store for every new connection
        # would hand straight back the saving the handshake gives.
        if self._ssl_context is None:
            self._ssl_context = ssl.create_default_context()
        return self._ssl_context

    @staticmethod
    def _is_usable(connection: http.client.HTTPConnection) -> bool:
        """True when the peer has not already closed a pooled connection.

        An idle keep-alive connection is one the server is free to drop at
        any moment. If it has been dropped, the socket is readable with a
        pending EOF; anything readable on an idle connection is unexpected
        and is treated the same way. Either way the connection is discarded
        *before* a request is written, so the request is never replayed and
        a side effect is never duplicated.
        """
        sock = connection.sock
        if sock is None:
            return False
        try:
            readable, _, _ = select.select([sock], [], [], 0.0)
        except (OSError, ValueError):
            return False
        return not readable

    def acquire(self, key: tuple[str, str, int], timeout: float) -> tuple[http.client.HTTPConnection, bool]:
        """Return a connection for *key*, reusing a live idle one if present.

        Returns ``(connection, reused)``. Raises :class:`RuntimeError` if the
        pool has been closed by a shutdown.
        """
        with self._lock:
            if self._closed:
                raise RuntimeError("the upstream connection pool is closed")
            bucket = self._idle.get(key)
            while bucket:
                connection = bucket.pop()
                if self._is_usable(connection):
                    # A pooled connection was opened under whatever timeout
                    # was current then. The socket carries its own timeout,
                    # set when it was connected, so that is what is
                    # re-applied here - setting the attribute alone would
                    # not reach a socket that already exists.
                    connection.timeout = timeout
                    if connection.sock is not None:
                        connection.sock.settimeout(timeout)
                    return connection, True
                connection.close()
        return self._new(key, timeout), False

    def _new(self, key: tuple[str, str, int], timeout: float) -> http.client.HTTPConnection:
        scheme, host, port = key
        if scheme == "https":
            return http.client.HTTPSConnection(host, port, timeout=timeout, context=self._context())
        return http.client.HTTPConnection(host, port, timeout=timeout)

    def release(self, key: tuple[str, str, int], connection: http.client.HTTPConnection, reusable: bool) -> None:
        """Return *connection* to the pool, or close it."""
        with self._lock:
            keep = reusable and not self._closed and connection.sock is not None
            if keep:
                bucket = self._idle.setdefault(key, [])
                if len(bucket) < self.max_idle:
                    bucket.append(connection)
                    return
        connection.close()

    def close(self) -> None:
        """Close every idle connection. Further acquires raise."""
        with self._lock:
            self._closed = True
            connections = [connection for bucket in self._idle.values() for connection in bucket]
            self._idle.clear()
        for connection in connections:
            connection.close()

    def __len__(self) -> int:
        with self._lock:
            return sum(len(bucket) for bucket in self._idle.values())


def _sse_frame(buffer: str) -> bytes:
    """Render one complete SSE frame: its data lines and its terminator.

    A frame is terminated by a blank line. Without it, a client that
    parses SSE strictly reads the whole stream as a single event.
    """
    text = buffer if buffer.endswith("\n") else buffer + "\n"
    return (text + "\n").encode("utf-8")


def _sse_frames(raw: Any) -> Iterator[bytes]:
    """Yield complete SSE frames from a response-like object.

    Returns only when the upstream stream ends, which is what lets a caller
    tell a completed stream from an interrupted one.
    """
    buffer = ""
    try:
        while True:
            line = raw.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace")
            if not text.strip():
                # The blank line ends the frame, so it belongs to it.
                if buffer.strip():
                    yield _sse_frame(buffer)
                    buffer = ""
                continue
            if text.startswith(("data:", "event:", ":")):
                buffer += text
    finally:
        try:
            raw.close()
        except Exception:  # pragma: no cover - defensive
            pass
    if buffer.strip():
        yield _sse_frame(buffer)


@dataclass
class UpstreamResponse:
    status: int
    headers: dict[str, str]
    body: bytes | None
    stream: Iterator[bytes] | None
    content_type: str


class UpstreamClient:
    """A small, streaming-aware HTTP client for provider calls.

    Each adapter owns one instance, so the pool it keeps is per-provider.
    Connections are additionally keyed by origin, so one provider pointed at
    two hosts keeps two pools rather than mixing them.
    """

    def __init__(self, timeout: float = DEFAULT_TIMEOUT, user_agent: str = "personal-llm-gateway/2.0") -> None:
        self.timeout = timeout
        self.user_agent = user_agent
        self._pool = _ConnectionPool()
        _LIVE_CLIENTS.add(self)

    def close(self) -> None:
        """Close every idle connection this client holds. Idempotent."""
        self._pool.close()

    @property
    def closed(self) -> bool:
        return self._pool.closed

    def _connection_key(self, url: str) -> tuple[tuple[str, str, int], str, str]:
        parsed = urllib.parse.urlsplit(url)
        scheme = parsed.scheme.lower()
        if scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("unsupported upstream URL")
        default_port = 443 if scheme == "https" else 80
        port = parsed.port or default_port
        target = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
        # Only a non-default port belongs in Host, and an IPv6 literal needs
        # its brackets back.
        hostname = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
        host_header = hostname if port == default_port else f"{hostname}:{port}"
        return (scheme, parsed.hostname, port), target, host_header

    def _open(
        self, method: str, url: str, headers: dict[str, str], body: bytes | None, provider_id: str
    ) -> tuple[tuple[str, str, int], http.client.HTTPConnection, http.client.HTTPResponse]:
        key, target, host_header = self._connection_key(url)
        try:
            connection, _reused = self._pool.acquire(key, self.timeout)
        except RuntimeError as exc:  # the pool was closed by a shutdown
            raise AdapterError(ERROR_UNKNOWN, f"the gateway is shutting down ({exc})", provider_id=provider_id) from None
        request_headers = dict(headers)
        request_headers.setdefault("Host", host_header)
        if self.user_agent:
            request_headers.setdefault("User-Agent", self.user_agent)
        try:
            connection.request(method.upper(), target, body=body, headers=request_headers)
            response = connection.getresponse()
        except (OSError, http.client.HTTPException):
            connection.close()
            raise
        return key, connection, response

    def _request_once(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None,
        stream: bool,
        provider_id: str,
    ) -> UpstreamResponse:
        try:
            key, connection, raw = self._open(method, url, headers, body, provider_id)
        except (socket.timeout, TimeoutError):
            # Establishing the connection has its own wording, because
            # "did not respond" is not what happened.
            raise AdapterError(
                ERROR_TRANSIENT,
                "the provider did not accept a connection within the configured timeout",
                provider_id=provider_id,
            ) from None
        except OSError as exc:
            raise AdapterError(
                ERROR_TRANSIENT,
                f"could not reach the provider ({exc.__class__.__name__})",
                provider_id=provider_id,
            ) from None
        except http.client.HTTPException as exc:
            raise AdapterError(
                ERROR_TRANSIENT,
                f"the provider sent an unparseable response ({exc.__class__.__name__})",
                provider_id=provider_id,
            ) from None
        except ValueError:
            raise AdapterError(ERROR_UNKNOWN, "the provider URL is invalid", provider_id=provider_id) from None

        headers_map = {name.lower(): value for name, value in raw.getheaders()}
        # A 2xx is assumed to be JSON unless it says otherwise. Anything else
        # is not assumed to carry a body of any particular kind at all.
        content_type = (
            headers_map.get("content-type", "application/json")
            if 200 <= raw.status < 300
            else headers_map.get("content-type", "")
        )

        if 300 <= raw.status < 400:
            # Deliberately refuse redirects; never replay credentials
            # elsewhere. The body is read so the response matches what the
            # client would have seen before, then the connection is dropped
            # rather than reused: a redirect is not an answer to the request
            # we made, so its body is not this request's body either.
            payload = b""
            try:
                payload = raw.read()
            except Exception:  # pragma: no cover - defensive
                payload = b""
            self._pool.release(key, connection, False)
            return UpstreamResponse(raw.status, headers_map, payload, None, content_type)

        if stream and 200 <= raw.status < 300:
            return UpstreamResponse(
                raw.status, headers_map, None, self._iter_response(key, connection, raw), content_type
            )

        # Everything not streamed above is buffered, and that includes a
        # non-2xx answer to a streaming request: an error body is what the
        # adapter turns into the error the caller sees, and a stream of an
        # error body is not a stream. This is the behaviour the client had
        # when every request opened its own connection, and the adapters
        # read ``response.body`` on this path.
        try:
            payload = raw.read()
        except (socket.timeout, TimeoutError):
            self._pool.release(key, connection, False)
            raise AdapterError(
                ERROR_TRANSIENT,
                "the provider did not send the full response within the configured timeout",
                provider_id=provider_id,
            ) from None
        except (OSError, http.client.HTTPException):
            self._pool.release(key, connection, False)
            raise AdapterError(
                ERROR_TRANSIENT, "the provider connection was interrupted", provider_id=provider_id
            ) from None
        # The whole body has been read, so the connection is clean and may
        # serve the next request unless the peer asked us not to reuse it.
        self._pool.release(key, connection, not raw.will_close)

        return UpstreamResponse(raw.status, headers_map, payload, None, content_type)

    def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None = None,
        stream: bool = False,
        provider_id: str = "",
        retry_safe: bool = False,
        retries: int = 2,
    ) -> UpstreamResponse:
        """Perform one upstream request.

        *retry_safe* enables a bounded retry for a request that may be
        repeated without cost or effect: a catalogue read, a model list, a
        device-code issue or poll. It is **never** set for an inference
        call, because that would bill twice and could repeat a side effect
        after an ambiguous delivery.
        """
        attempts = retries + 1 if retry_safe else 1
        last: UpstreamResponse | None = None
        for attempt in range(attempts):
            last = self._request_once(method, url, headers, body, stream, provider_id)
            if not retry_safe or last.status < 500 or attempt == attempts - 1:
                return last
            # A short, bounded backoff. The upstream often answers a
            # transient origin error on the next attempt.
            time.sleep(min(2.0, 0.4 * (attempt + 1)))
        assert last is not None
        return last

    def _iter_sse(self, raw: Any) -> Iterator[bytes]:
        """Yield SSE frames as they arrive.

        Each yielded frame is a **complete** SSE frame: the data lines plus
        the blank line that terminates it. The terminator matters - a
        client that parses SSE strictly reads a stream without it as a
        single event, and reports an empty stream with no finish reason.
        """
        try:
            yield from _sse_frames(raw)
        except (socket.timeout, TimeoutError):
            raise AdapterError(
                ERROR_TRANSIENT,
                "the provider stream stalled before the response finished",
            ) from None
        except OSError:
            # The client hung up. The partial stream is what the caller got.
            return

    def _iter_response(
        self, key: tuple[str, str, int], connection: http.client.HTTPConnection, response: http.client.HTTPResponse
    ) -> Iterator[bytes]:
        """Stream SSE frames, returning the connection when the stream ends.

        The connection is pooled only when the frame reader reached the end
        of the stream on its own. A stall, an interrupted read, or a caller
        that stops consuming all leave bytes in flight, and a connection
        with bytes in flight is never handed to another request.
        """
        reusable = False
        try:
            yield from self._iter_sse(response)
            reusable = not response.will_close
        finally:
            self._pool.release(key, connection, reusable)


def parse_json_body(body: bytes | None) -> Any:
    """Best-effort JSON parse. Returns ``None`` for anything unparseable."""
    if not body:
        return None
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None


def network_error(message: str, provider_id: str = "") -> AdapterError:
    return AdapterError(ERROR_TRANSIENT, message, provider_id=provider_id)


def unknown_error(message: str, provider_id: str = "") -> AdapterError:
    return AdapterError(ERROR_UNKNOWN, message, provider_id=provider_id)
