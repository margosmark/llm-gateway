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
* **The connect budget and the read budget are different.** Establishing a
  connection - the TCP handshake and, over TLS, the handshake that follows
  it - is judged by ``DEFAULT_CONNECT_TIMEOUT``, so a provider that accepts
  the connection and then goes quiet is abandoned in seconds instead of
  holding a worker for the whole read timeout. Everything the provider
  *sends* is judged by the generous ``DEFAULT_TIMEOUT``, per read: the
  buffered body, and every frame of a stream, so a long response that is
  still arriving is never abandoned while it progresses. The one wait on
  the boundary is the head of a **streamed** response: an SSE provider
  sends its status line before it generates anything, so a stream whose
  head has not arrived within the connect budget has stalled and is
  judged by the connect budget too. The read budget for the frames that
  follow is armed on the socket *directly*, because ``getresponse()``
  hands the socket to the response for any ``will_close`` response -
  every stream has no Content-Length - and the connection no longer
  holds it. A *buffered* head may legitimately
  follow the whole generation, so silence there is judged by the read
  budget, never the connect budget.
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
import inspect
import json
import select
import socket
import ssl
import threading
import time
import urllib.parse
import weakref
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

from gateway.adapters.base import AdapterError
from gateway.config import ERROR_TRANSIENT, ERROR_UNKNOWN

#: The read budget: one wait for the provider's own bytes - the buffered
#: head and body, or one frame of a stream. Generous, because a generation
#: legitimately takes minutes and a progressing stream must never be cut.
DEFAULT_TIMEOUT = 120.0
#: The connect budget: establishing a connection (the TCP handshake and,
#: over TLS, the handshake that follows it), and the head of a streamed
#: response - an SSE provider answers before it generates anything, so
#: silence there is a stall and not a slow generation.
DEFAULT_CONNECT_TIMEOUT = 10.0
#: How many idle connections one client keeps per origin. Overflow is closed
#: immediately, so the pool is a bound and not a cache that grows without one.
DEFAULT_IDLE_CONNECTIONS = 4

#: Every live client, so a single shutdown hook can release their sockets.
#: A weak set: a client must never be kept alive by being closable.
_LIVE_CLIENTS: weakref.WeakSet[UpstreamClient] = weakref.WeakSet()


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

    def acquire(
        self,
        key: tuple[str, str, int],
        timeout: float,
        connect_timeout: float | None = None,
    ) -> tuple[http.client.HTTPConnection, bool]:
        """Return a connection for *key*, reusing a live idle one if present.

        *timeout* is the **read** budget, which a reused connection is
        re-armed with: it is already established, so every wait it makes
        from here is a wait for the provider's own bytes. A **new**
        connection is created with *connect_timeout* when one is given
        (else *timeout*), so establishing it - TCP and TLS - is judged by
        the **connect** budget; the caller re-arms the socket for the later
        phases itself.

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
        return self._new(key, connect_timeout if connect_timeout is not None else timeout), False

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

    def __init__(
        self,
        timeout: float = DEFAULT_TIMEOUT,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        user_agent: str = "personal-llm-gateway/2.0",
    ) -> None:
        #: The read budget: one wait for the provider's own bytes.
        self.timeout = timeout
        #: The connect budget: establishing a connection, and the head of a
        #: streamed response (see the module docstring for why the two are
        #: judged differently).
        self.connect_timeout = connect_timeout
        self.user_agent = user_agent
        self._pool = _ConnectionPool()
        self._active_lock = threading.Lock()
        self._active: set[http.client.HTTPConnection] = set()
        _LIVE_CLIENTS.add(self)

    @staticmethod
    def _arm(connection: http.client.HTTPConnection, timeout: float) -> None:
        """Give the connection's socket the budget of its next phase.

        A socket carries its own timeout from the moment it exists, and
        that is what every later read and write inherits, so each phase
        has to re-arm the socket itself - setting the attribute alone
        would not reach it (the same reason :meth:`_ConnectionPool.acquire`
        re-applies a timeout to a reused connection).
        """
        connection.timeout = timeout
        if connection.sock is not None:
            connection.sock.settimeout(timeout)

    def close(self) -> None:
        """Close pooled and active connections. Idempotent."""
        self._pool.close()
        with self._active_lock:
            active = list(self._active)
        for connection in active:
            connection.close()

    @property
    def closed(self) -> bool:
        return self._pool.closed

    def _discard(self, connection: http.client.HTTPConnection) -> None:
        """Drop a connection from the active set."""
        with self._active_lock:
            self._active.discard(connection)

    def _release(self, key: tuple[str, str, int], connection: http.client.HTTPConnection, reusable: bool) -> None:
        """Return a checked-out connection to the pool - and leave no trace.

        Every release point goes through here, so a connection can never
        outlive its request in ``_active``. That set exists so
        :meth:`close` can reach sockets still in flight; an entry left
        behind by a *failed* request would never be removed again, and a
        long-lived adapter client would accumulate one dead entry per
        failure.
        """
        self._discard(connection)
        self._pool.release(key, connection, reusable)

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
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None,
        provider_id: str,
        stream: bool = False,
        on_checkout: Callable[[http.client.HTTPConnection], None] | None = None,
    ) -> tuple[tuple[str, str, int], http.client.HTTPConnection, http.client.HTTPResponse]:
        key, target, host_header = self._connection_key(url)
        effective_timeout = self.timeout
        if hasattr(self, "_probe_deadline") and self._probe_deadline is not None:
            remaining = self._probe_deadline - time.monotonic()
            if remaining <= 0:
                raise AdapterError(ERROR_TRANSIENT, "probe deadline exceeded", provider_id=provider_id)
            effective_timeout = min(effective_timeout, remaining)
        try:
            connection, reused = self._pool.acquire(key, effective_timeout, connect_timeout=min(self.connect_timeout, effective_timeout))
        except RuntimeError as exc:  # the pool was closed by a shutdown
            raise AdapterError(ERROR_UNKNOWN, f"the gateway is shutting down ({exc})", provider_id=provider_id) from None
        with self._active_lock:
            self._active.add(connection)
        if on_checkout is not None:
            # Publish the connection before any phase can block on it. A
            # caller that watches this request (the probe's deadline and
            # cancellation watchdog) can then interrupt the connect, the
            # request write and the response head, not only the body read
            # that follows - all of which are places where a peer that
            # trickles bytes would otherwise keep every recv() succeeding.
            on_checkout(connection)
        request_headers = dict(headers)
        request_headers.setdefault("Host", host_header)
        if self.user_agent:
            request_headers.setdefault("User-Agent", self.user_agent)
        try:
            if not reused:
                # --- connect budget: the TCP handshake and, over TLS, the
                # handshake that follows it. The connection was created
                # with the connect timeout, so this is the phase it
                # already carries.
                try:
                    connection.connect()
                except TimeoutError:
                    raise AdapterError(
                        ERROR_TRANSIENT,
                        "the provider did not accept a connection within the configured timeout",
                        provider_id=provider_id,
                    ) from None
                self._arm(connection, effective_timeout)
            try:
                # --- read budget: writing the request. Sending is the
                # gateway's own bytes; a provider that never reads them is
                # not worth a separate budget, and a large body on a slow
                # uplink must not be cut short by the connect one.
                connection.request(method.upper(), target, body=body, headers=request_headers)
                if stream:
                    # --- connect budget: the head of a stream. An SSE
                    # provider sends its status line before it generates
                    # anything, so a head that has not arrived is a stall
                    # and not a slow generation.
                    self._arm(connection, self.connect_timeout)
                # Hold the socket itself across getresponse(): for a
                # response without a Content-Length - every SSE stream -
                # will_close is true and getresponse() passes the socket
                # to the response by closing the connection's reference
                # to it. The socket itself lives on inside the response's
                # file object; this reference is what the read budget is
                # armed on below.
                sock = connection.sock
                response = connection.getresponse()
            except TimeoutError:
                raise AdapterError(
                    ERROR_TRANSIENT,
                    (
                        "the provider did not start a response within the connect timeout"
                        if stream
                        else "the provider did not respond within the configured timeout"
                    ),
                    provider_id=provider_id,
                ) from None
            if stream and sock is not None:
                # --- read budget: every frame that follows. The head is
                # in, so the stream is generating, and each frame within
                # this budget counts as progress.
                #
                # The socket is armed directly, not through the
                # connection: getresponse() has already handed the socket
                # to the response on this path (connection.sock is None),
                # so re-arming only the connection would be a silent
                # no-op that leaves every frame judged by the connect
                # budget it was armed with for the head. The socket is
                # the reference held just above getresponse(); the guard
                # is defensive, since a request that got this far always
                # had one.
                sock.settimeout(self.timeout)
        except AdapterError:
            # The connection was published to the active set the moment it
            # was checked out; a failure here must not leave it there.
            self._discard(connection)
            connection.close()
            raise
        except (OSError, http.client.HTTPException):
            self._discard(connection)
            connection.close()
            raise
        return key, connection, response

    def _read_buffered_body(
        self,
        raw: http.client.HTTPResponse,
        *,
        max_body_bytes: int | None,
        deadline: float | None,
        cancel_event: threading.Event | None,
        provider_id: str,
    ) -> bytes:
        """Buffer a response body that can neither out-wait the run nor
        outgrow the byte cap.

        The body is read one underlying receive at a time
        (:meth:`http.client.HTTPResponse.read1`), so cancellation and the
        absolute deadline are re-checked after **every** chunk that arrives.
        Re-checking only between whole 64 KiB reads let a peer that trickled
        bytes faster than the socket timeout delay the next check without
        bound: the deadline held only while the upstream stayed silent.

        Silence is judged by the read budget re-armed with whatever time is
        left before the deadline, so a stalled peer is abandoned at the
        smaller of the two while a peer that keeps sending is never
        abandoned as it progresses. The watchdog that runs alongside this
        read additionally ``shutdown()``s the socket at the deadline and on
        a cancellation - that is what breaks a read blocked *inside*
        ``http.client`` (a response head, or a chunked-encoding line) out of
        its own internal loop, where no check of ours runs.

        Raises :class:`TimeoutError` for a deadline or a cancellation (the
        caller maps either to its transient failure) and
        :class:`AdapterError` when the byte cap would be exceeded - the cap
        is checked before the overflowing bytes are kept, so the allocation
        stays at the cap.
        """
        chunks: list[bytes] = []
        total = 0
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise TimeoutError("probe cancelled")
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                raise TimeoutError("probe deadline")
            holder = getattr(getattr(raw, "fp", None), "raw", None)
            sock = getattr(holder, "_sock", None)
            if sock is not None:
                wait_budget = self.timeout if remaining is None else min(self.timeout, remaining)
                sock.settimeout(wait_budget)
            want = 65_536 if max_body_bytes is None else min(65_536, max_body_bytes - total + 1)
            chunk = raw.read1(want)
            if not chunk:
                break
            total += len(chunk)
            if max_body_bytes is not None and total > max_body_bytes:
                raise AdapterError(
                    ERROR_TRANSIENT,
                    "the provider response exceeded the configured byte limit",
                    provider_id=provider_id,
                )
            chunks.append(chunk)
        # The watchdog ends an in-flight read by shutting the socket down,
        # which surfaces here as a short body. Those bytes stopped because
        # *we* stopped them, so the reason still has to be reported instead
        # of being handed back as if the body were complete.
        if cancel_event is not None and cancel_event.is_set():
            raise TimeoutError("probe cancelled")
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("probe deadline")
        return b"".join(chunks)

    def _request_once(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None,
        stream: bool,
        provider_id: str,
        *,
        max_body_bytes: int | None = None,
        deadline: float | None = None,
        cancel_event: threading.Event | None = None,
    ) -> UpstreamResponse:
        self._probe_deadline = deadline
        stop_watch = threading.Event()
        # The watchdog may outlive the call that started it - a streamed
        # response is consumed after this function returns - so it tracks
        # whether this request still *owns* the socket it may interrupt.
        # "Finished" is set before the connection is released, under the
        # same lock the watchdog checks, so a watch that outlives its
        # request can never shutdown() a socket that a later request has
        # since taken from the pool.
        watch_lock = threading.Lock()
        finished = False
        # The connection and the response are published here as soon as they
        # exist rather than read from the locals below: the watchdog starts
        # before _open runs, and a watchdog that touched a not-yet-assigned
        # local would die with NameError at exactly the wrong moment.
        checked_out: list[http.client.HTTPConnection] = []
        served: list[http.client.HTTPResponse] = []

        def _response_socket() -> socket.socket | None:
            """The socket behind the response, whichever end still holds it.

            ``getresponse()`` hands the socket to the response for a
            ``will_close`` response and drops the connection's own reference
            to it, so during a body read ``connection.sock`` can already be
            ``None`` while a read is still in flight on that socket.
            """
            if served:
                holder = getattr(getattr(served[0], "fp", None), "raw", None)
                found = getattr(holder, "_sock", None)
                if found is not None:
                    return found
            if checked_out:
                return checked_out[0].sock
            return None

        def _retire() -> None:
            """End this request's ownership of its socket, then stop the watch.

            Called by every exit that finishes a request - including the
            stream's end, which happens after this function returned. It
            must run *before* the connection is released, so the watchdog
            can never interrupt a socket this request no longer holds.
            """
            nonlocal finished
            with watch_lock:
                finished = True
                stop_watch.set()

        def _interrupt() -> None:
            # A plain ``close()`` does not wake a ``recv()`` that another
            # thread has already blocked in: on Linux the reader stayed
            # blocked long after the connection was closed, while
            # ``shutdown(SHUT_RDWR)`` woke the identical recv at once. The
            # shutdown is what makes the absolute deadline and a
            # cancellation able to interrupt a read that is under way - a
            # head or a body that trickles in faster than the socket timeout
            # never lets a read return to a check of ours otherwise.
            #
            # The connection is deliberately *not* closed here.
            # ``HTTPConnection.close()`` also closes the response object it
            # is serving, which would pull ``response.fp`` out from under the
            # thread that is reading it (the reader died inside
            # ``HTTPResponse.read1`` on exactly that race). The thread that
            # owns the read always disposes of the connection itself - a
            # failed read releases it as not reusable - and a connection
            # shut down by mistake is discarded by the pool before it is
            # ever handed out again, because a shutdown socket is readable
            # with a pending EOF.
            #
            # Ownership is re-checked under the watch's own lock, against
            # the same flag :meth:`_retire` sets before releasing: a watch
            # whose request already finished must not touch the socket
            # again, because by then it may belong to an unrelated request
            # that re-acquired this connection from the pool.
            with watch_lock:
                if finished:
                    return
                sock = _response_socket()
                if sock is not None:
                    try:
                        sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        # Not connected (yet), already gone, or already shut
                        # down: the watch keeps polling, so a connection that
                        # appears later in the same request is still covered.
                        pass

        def _remember(checked: http.client.HTTPConnection) -> None:
            checked_out[:] = [checked]

        if cancel_event is not None or deadline is not None:
            def interrupt_when_needed() -> None:
                # The watch runs until the request itself finishes, not
                # until the first interrupt: the connection may not exist
                # yet when the first deadline or cancellation lands, and a
                # phase that starts afterwards still has to be interruptible.
                while not stop_watch.is_set():
                    expired = deadline is not None and deadline - time.monotonic() <= 0
                    cancelled = cancel_event is not None and cancel_event.is_set()
                    if cancelled or expired:
                        _interrupt()
                    stop_watch.wait(0.05)
            threading.Thread(
                target=interrupt_when_needed,
                name=f"upstream-watch-{provider_id or 'client'}",
                daemon=True,
            ).start()

        try:
            key, connection, raw = self._open(
                method,
                url,
                headers,
                body,
                provider_id,
                stream,
                on_checkout=_remember,
            )
        except TimeoutError:
            stop_watch.set()
            # Defensive: _open maps its own phase timeouts to the wording
            # of the phase that stalled. This only sees a timeout that
            # escaped a phase neither mapping covered.
            raise AdapterError(
                ERROR_TRANSIENT,
                "the provider did not accept a connection within the configured timeout",
                provider_id=provider_id,
            ) from None
        except OSError as exc:
            stop_watch.set()
            raise AdapterError(
                ERROR_TRANSIENT,
                f"could not reach the provider ({exc.__class__.__name__})",
                provider_id=provider_id,
            ) from None
        except http.client.HTTPException as exc:
            stop_watch.set()
            raise AdapterError(
                ERROR_TRANSIENT,
                f"the provider sent an unparseable response ({exc.__class__.__name__})",
                provider_id=provider_id,
            ) from None
        except ValueError:
            stop_watch.set()
            raise AdapterError(ERROR_UNKNOWN, "the provider URL is invalid", provider_id=provider_id) from None
        except AdapterError:
            stop_watch.set()
            raise
        checked_out[:] = [connection]
        served[:] = [raw]

        try:
            headers_map = {name.lower(): value for name, value in raw.getheaders()}
            # A 2xx is assumed to be JSON unless it says otherwise. Anything
            # else is not assumed to carry a body of any particular kind at
            # all. All of it sits inside the try so that *any* failure -
            # including one no clause below anticipated - retires the watch
            # and releases the connection before it can propagate.
            content_type = (
                headers_map.get("content-type", "application/json")
                if 200 <= raw.status < 300
                else headers_map.get("content-type", "")
            )

            if cancel_event is not None and cancel_event.is_set():
                # The run was cancelled while the head of the response was
                # arriving: an answer to a run that no longer exists must not
                # be handed back as if nothing had happened.
                raise TimeoutError("probe cancelled")
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("probe deadline")

            if 300 <= raw.status < 400:
                # Deliberately refuse redirects; never replay credentials
                # elsewhere. The body is read so the response matches what the
                # client would have seen before, then the connection is dropped
                # rather than reused: a redirect is not an answer to the request
                # we made, so its body is not this request's body either.
                #
                # It is read with the same capped, deadline- and
                # cancellation-aware read as every other buffered body. The
                # body is discarded either way, so an unbounded read here was
                # pure risk: it let a hostile provider choose how much this
                # process allocates, and hold the run open by trickling, with
                # none of the bounds the 2xx path had.
                payload = b""
                try:
                    payload = self._read_buffered_body(
                        raw,
                        max_body_bytes=max_body_bytes,
                        deadline=deadline,
                        cancel_event=cancel_event,
                        provider_id=provider_id,
                    )
                except Exception:  # noqa: BLE001 - defensive: a truncated,
                    # capped or cancelled redirect body must not fail the
                    # refusal of a request that never used it.
                    payload = b""
                _retire()
                self._release(key, connection, False)
                return UpstreamResponse(raw.status, headers_map, payload, None, content_type)

            if stream and 200 <= raw.status < 300:
                # The watchdog deliberately stays armed here: a streamed
                # response outlives this call, and cancelling or reaching
                # the deadline must still be able to interrupt it. Its
                # retirement is the stream's own end, handed down as the
                # finish callback - left running, the watch would fire its
                # deadline long after the stream completed, against a socket
                # that by then serves some other request.
                return UpstreamResponse(
                    raw.status,
                    headers_map,
                    None,
                    self._iter_response(key, connection, raw, on_finish=_retire),
                    content_type,
                )

            # Everything not streamed above is buffered, and that includes a
            # non-2xx answer to a streaming request: an error body is what the
            # adapter turns into the error the caller sees, and a stream of an
            # error body is not a stream. This is the behaviour the client had
            # when every request opened its own connection, and the adapters
            # read ``response.body`` on this path.
            if max_body_bytes is None and deadline is None and cancel_event is None:
                payload = raw.read()
            else:
                payload = self._read_buffered_body(
                    raw,
                    max_body_bytes=max_body_bytes,
                    deadline=deadline,
                    cancel_event=cancel_event,
                    provider_id=provider_id,
                )
        except AdapterError:
            _retire()
            self._release(key, connection, False)
            raise
        except TimeoutError:
            _retire()
            self._release(key, connection, False)
            raise AdapterError(
                ERROR_TRANSIENT,
                "the provider did not send the full response within the configured timeout",
                provider_id=provider_id,
            ) from None
        except (OSError, http.client.HTTPException):
            _retire()
            self._release(key, connection, False)
            raise AdapterError(
                ERROR_TRANSIENT, "the provider connection was interrupted", provider_id=provider_id
            ) from None
        except Exception as exc:
            # A read raced by a watchdog shutdown can surface from inside
            # ``http.client`` as an error no clause above names - a bare
            # AttributeError from a half-closed response, for one. Anything
            # like that is a transport failure by definition: it is reported
            # as one, with the class preserved for the evidence trail,
            # instead of escaping to the caller as an unhandled error that
            # would take an owner-facing run down with it.
            _retire()
            self._release(key, connection, False)
            raise AdapterError(
                ERROR_TRANSIENT,
                f"the provider connection was interrupted ({exc.__class__.__name__})",
                provider_id=provider_id,
            ) from exc
        # The whole body has been read, so the connection is clean and may
        # serve the next request unless the peer asked us not to reuse it.
        _retire()
        self._release(key, connection, not raw.will_close)

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
        max_body_bytes: int | None = None,
        deadline: float | None = None,
        cancel_event: threading.Event | None = None,
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
            request_kwargs = {}
            if "max_body_bytes" in inspect.signature(self._request_once).parameters:
                request_kwargs = {
                    "max_body_bytes": max_body_bytes,
                    "deadline": deadline,
                    "cancel_event": cancel_event,
                }
            last = self._request_once(method, url, headers, body, stream, provider_id, **request_kwargs)
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
        except TimeoutError:
            raise AdapterError(
                ERROR_TRANSIENT,
                "the provider stream stalled before the response finished",
            ) from None
        except OSError:
            # The client hung up. The partial stream is what the caller got.
            return

    def _iter_response(
        self,
        key: tuple[str, str, int],
        connection: http.client.HTTPConnection,
        response: http.client.HTTPResponse,
        on_finish: Callable[[], None] | None = None,
    ) -> Iterator[bytes]:
        """Stream SSE frames, returning the connection when the stream ends.

        The connection is pooled only when the frame reader reached the end
        of the stream on its own. A stall, an interrupted read, or a caller
        that stops consuming all leave bytes in flight, and a connection
        with bytes in flight is never handed to another request.

        *on_finish* runs first on every exit - normal end, interruption, or
        the caller abandoning the iterator - because it is what retires the
        request's deadline/cancellation watch. Retiring before releasing
        keeps the order the watchdog depends on: by the time this connection
        can be handed to another request, the watch that may ``shutdown()``
        its socket has already been stopped.
        """
        reusable = False
        try:
            yield from self._iter_sse(response)
            reusable = not response.will_close
        finally:
            if on_finish is not None:
                on_finish()
            self._release(key, connection, reusable)


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
