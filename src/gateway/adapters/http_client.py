"""The upstream HTTP client.

Uses :mod:`urllib.request` from the standard library. It is deliberately
small and explicit:

* **Streaming is incremental.** SSE frames are yielded as they arrive, using
  line reads rather than a blocking buffered read, so a response is not held
  in memory and a slow upstream is not stalled.
* **Credentials never appear in an exception.** A URLError carries the URL
  and the reason, not the headers, so the API key cannot leak through a
  traceback.
* **Redirects are not followed** for upstream calls, so a redirect cannot
  quietly move a credential to another host.
"""

from __future__ import annotations

import json
import socket
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Iterator

from gateway.adapters.base import AdapterError
from gateway.config import ERROR_TRANSIENT, ERROR_UNKNOWN

DEFAULT_TIMEOUT = 120.0
DEFAULT_CONNECT_TIMEOUT = 10.0


def _sse_frame(buffer: str) -> bytes:
    """Render one complete SSE frame: its data lines and its terminator.

    A frame is terminated by a blank line. Without it, a client that
    parses SSE strictly reads the whole stream as a single event.
    """
    text = buffer if buffer.endswith("\n") else buffer + "\n"
    return (text + "\n").encode("utf-8")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102, ANN001
        return None


def _build_opener() -> urllib.request.OpenerDirector:
    context = ssl.create_default_context()
    return urllib.request.build_opener(
        _NoRedirect(),
        urllib.request.HTTPSHandler(context=context),
    )


@dataclass
class UpstreamResponse:
    status: int
    headers: dict[str, str]
    body: bytes | None
    stream: Iterator[bytes] | None
    content_type: str


class UpstreamClient:
    """A small, streaming-aware HTTP client for provider calls."""

    def __init__(self, timeout: float = DEFAULT_TIMEOUT, user_agent: str = "personal-llm-gateway/2.0") -> None:
        self.timeout = timeout
        self.user_agent = user_agent
        self._opener = _build_opener()

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

    def _request_once(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None,
        stream: bool,
        provider_id: str,
    ) -> UpstreamResponse:
        request = urllib.request.Request(url=url, data=body, method=method.upper())
        for key, value in headers.items():
            request.add_header(key, value)
        if self.user_agent:
            request.add_header("User-Agent", self.user_agent)

        try:
            raw = self._opener.open(request, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            payload_body = b""
            try:
                payload_body = exc.read()
            except Exception:  # pragma: no cover - defensive
                pass
            headers_map = {key.lower(): value for key, value in exc.headers.items()}
            content_type = headers_map.get("content-type", "")
            return UpstreamResponse(
                status=exc.code,
                headers=headers_map,
                body=payload_body,
                stream=None,
                content_type=content_type,
            )
        except urllib.error.URLError as exc:
            raise AdapterError(
                ERROR_TRANSIENT,
                f"could not reach the provider ({exc.reason.__class__.__name__})",
                provider_id=provider_id,
            ) from None
        except (socket.timeout, TimeoutError):
            raise AdapterError(
                ERROR_TRANSIENT,
                "the provider did not respond within the configured timeout",
                provider_id=provider_id,
            ) from None
        except OSError as exc:
            raise AdapterError(
                ERROR_TRANSIENT,
                f"the provider connection failed ({exc.__class__.__name__})",
                provider_id=provider_id,
            ) from None

        headers_map = {key.lower(): value for key, value in raw.headers.items()}
        content_type = headers_map.get("content-type", "application/json")

        if stream:
            return UpstreamResponse(
                status=raw.status,
                headers=headers_map,
                body=None,
                stream=self._iter_sse(raw),
                content_type=content_type,
            )

        try:
            payload = raw.read()
        except (socket.timeout, TimeoutError):
            raise AdapterError(
                ERROR_TRANSIENT,
                "the provider did not send the full response within the configured timeout",
                provider_id=provider_id,
            ) from None
        except OSError:
            raise AdapterError(
                ERROR_TRANSIENT,
                "the provider connection was interrupted",
                provider_id=provider_id,
            ) from None
        finally:
            raw.close()

        return UpstreamResponse(
            status=raw.status,
            headers=headers_map,
            body=payload,
            stream=None,
            content_type=content_type,
        )

    def _iter_sse(self, raw: Any) -> Iterator[bytes]:
        """Yield SSE frames as they arrive.

        Each yielded frame is a **complete** SSE frame: the data lines plus
        the blank line that terminates it. The terminator matters - a
        client that parses SSE strictly reads a stream without it as a
        single event, and reports an empty stream with no finish reason.
        """
        buffer = ""
        try:
            while True:
                line = raw.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace")
                if not text.strip():
                    if buffer.strip():
                        # The blank line ends the frame, so it belongs to it.
                        yield _sse_frame(buffer)
                        buffer = ""
                    continue
                if text.startswith("data:") or text.startswith("event:") or text.startswith(":"):
                    buffer += text
        except (socket.timeout, TimeoutError):
            raise AdapterError(
                ERROR_TRANSIENT,
                "the provider stream stalled before the response finished",
            ) from None
        except OSError:
            # The client hung up. The partial stream is what the caller got.
            return
        finally:
            try:
                raw.close()
            except Exception:  # pragma: no cover - defensive
                pass
        if buffer.strip():
            yield _sse_frame(buffer)


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
