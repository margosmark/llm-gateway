"""A very small HTTP layer built on the standard library.

It exists because the project takes no third-party runtime dependency. The
pieces are deliberately boring:

* :class:`Request` - parsed request with case-insensitive headers.
* :class:`Response` - a buffered response.
* :class:`StreamResponse` - a response whose body is produced incrementally,
  used for server-sent events.
* :class:`Router` - exact and ``{parameter}`` path patterns.
* :class:`Server` - a threading HTTP/1.1 server that speaks chunked transfer
  encoding for streamed bodies.

Security-relevant behaviour (host, origin, CSRF, forwarded-header rejection)
lives in :mod:`gateway.http.security`, not here.
"""

from __future__ import annotations

import json
import re
import socket
import socketserver
import sys
import threading
import traceback
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from http.server import BaseHTTPRequestHandler
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from gateway.http.keepalive import with_sse_keepalives

MAX_BODY_BYTES = 4 * 1024 * 1024
DEFAULT_MAX_WORKERS = 32
DEFAULT_RATE_LIMIT_REQUESTS = 120
DEFAULT_RATE_LIMIT_WINDOW_SECONDS = 60
# CPython's JSON decoder is recursive. Reject pathological nesting before it
# enters the decoder (and before any application surface can route the body).
MAX_JSON_NESTING = 3000
"""Maximum JSON container nesting accepted by the request parser."""


def _check_json_nesting(text: str) -> None:
    """Bound structural JSON nesting without interpreting brackets in strings."""
    depth = 0
    in_string = False
    escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
            if depth > MAX_JSON_NESTING:
                raise ValueError("JSON nesting exceeds the maximum allowed depth")
        elif char in "]}":
            depth = max(0, depth - 1)


def escape_text(value: str) -> str:
    """Rewrite lone surrogates to UTF-8-safe escape text.

    A clean string comes back byte-identical, so this is safe to apply
    unconditionally at a capture boundary (F9).
    """
    return value.encode("utf-8", "backslashreplace").decode("utf-8")


class InvalidBodyText(Exception):
    """A parsed request body carries lone-surrogate text the gateway refuses.

    Raised only by :meth:`Request.json`, so every JSON ingress - the model
    API and the owner-console forms alike - passes through the same choke.

    Deliberately **not** a ``ValueError``: handlers catch ``ValueError`` to
    fall back to an empty payload, and a refused body must be *refused* with
    a 400 at the dispatch boundary rather than silently rewritten to ``{}``.
    Lone surrogates are never legitimate client input.
    """

    def __init__(self, path: str, excerpt: str, model: str | None) -> None:
        super().__init__(f"lone surrogate in request body at {path}")
        #: Where in the body the unencodable text sits (already escaped).
        self.path = path
        #: A bounded, escaped JSON rendering of the offending string, for
        #: the ledger's forensic reason - never the whole body.
        self.excerpt = excerpt
        #: The request's ``model`` field when it has one, already escaped,
        #: so a refusal can still be attributed to an alias.
        self.model = model


#: Longest field path kept on a refusal; a hostile body can nest long keys.
_MAX_FIELD_PATH = 160


def find_unencodable(value: Any) -> tuple[str, str] | None:
    """The first string in *value* that cannot be UTF-8 encoded, with its path.

    ``json.loads`` accepts the ``\\ud800`` escape and yields a lone
    surrogate, which every later strict ``encode("utf-8")`` raises on.
    Encoding here in C is both the exact predicate and cheap enough to run
    over a whole body.

    The walk is iterative (an explicit stack, document order preserved):
    the parser accepts up to :data:`MAX_JSON_NESTING` levels - far past
    Python's recursion limit - so a recursive walk raised ``RecursionError``
    on a deep-but-legal body and handed the client the very bare 500 this
    choke exists to remove (caught by the F6 deep-nesting probe).
    """
    stack: list[tuple[str, Any]] = [("$", value)]
    while stack:
        path, node = stack.pop()
        if isinstance(node, str):
            try:
                node.encode("utf-8")
            except UnicodeEncodeError:
                return path, node
            continue
        children: list[tuple[str, Any]] = []
        if isinstance(node, dict):
            for key, item in node.items():
                if isinstance(key, str):
                    try:
                        key.encode("utf-8")
                    except UnicodeEncodeError:
                        return f"{path}.<key>", key
                    child = f"{path}.{escape_text(key)}"
                else:
                    child = f"{path}[key]"
                if len(child) > _MAX_FIELD_PATH:
                    child = child[:_MAX_FIELD_PATH] + "..."
                children.append((child, item))
        elif isinstance(node, list):
            for index, item in enumerate(node):
                child = f"{path}[{index}]"
                if len(child) > _MAX_FIELD_PATH:
                    child = child[:_MAX_FIELD_PATH] + "..."
                children.append((child, item))
        # Pushed in reverse so pop() yields the original document order.
        stack.extend(reversed(children))
    return None


class Headers:
    """Case-insensitive header mapping that keeps repeated headers."""

    __slots__ = ("_items",)

    def __init__(self, items: Iterable[tuple[str, str]] = ()) -> None:
        self._items: list[tuple[str, str]] = [(str(k), str(v)) for k, v in items]

    def get(self, name: str, default: str | None = None) -> str | None:
        lowered = name.lower()
        for key, value in self._items:
            if key.lower() == lowered:
                return value
        return default

    def get_all(self, name: str) -> list[str]:
        lowered = name.lower()
        return [value for key, value in self._items if key.lower() == lowered]

    def __contains__(self, name: object) -> bool:
        if not isinstance(name, str):
            return False
        return self.get(name) is not None

    def items(self) -> list[tuple[str, str]]:
        return list(self._items)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Headers({self._items!r})"


class Request:
    """An inbound HTTP request."""

    __slots__ = (
        "method",
        "path",
        "raw_query",
        "headers",
        "body",
        "client_address",
        "server_port",
        "params",
        "context",
    )

    def __init__(
        self,
        method: str,
        path: str,
        raw_query: str,
        headers: Headers,
        body: bytes,
        client_address: tuple[str, int],
        server_port: int,
    ) -> None:
        self.method = method.upper()
        self.path = path
        self.raw_query = raw_query
        self.headers = headers
        self.body = body
        self.client_address = client_address
        self.server_port = server_port
        self.params: dict[str, str] = {}
        self.context: dict[str, Any] = {}

    @property
    def query(self) -> dict[str, list[str]]:
        return parse_qs(self.raw_query, keep_blank_values=True)

    def query_one(self, name: str, default: str | None = None) -> str | None:
        values = self.query.get(name)
        return values[0] if values else default

    def json(self) -> Any:
        """Parse the body as JSON.

        Raises ``ValueError`` for a body that is not valid JSON, and
        :class:`InvalidBodyText` for a body whose strings cannot be UTF-8
        encoded (a lone ``\\ud800``-style surrogate, which ``json.loads``
        accepts but every later strict encode raises on). The surrogate
        refusal is the one choke that closes every JSON ingress at once:
        the model API, the owner forms, passwords and machine-key names
        never receive such a string.
        """
        if not self.body:
            return None
        try:
            text = self.body.decode("utf-8")
            _check_json_nesting(text)
            payload = json.loads(text)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("request body is not valid JSON") from exc
        hit = find_unencodable(payload)
        if hit is not None:
            path, offender = hit
            excerpt = json.dumps(offender, ensure_ascii=True)
            if len(excerpt) > 120:
                excerpt = excerpt[:120] + "..."
            model = payload.get("model") if isinstance(payload, dict) else None
            raise InvalidBodyText(
                escape_text(path),
                excerpt,
                escape_text(model) if isinstance(model, str) else None,
            )
        return payload

    @property
    def origin(self) -> str | None:
        return self.headers.get("origin")

    @property
    def host(self) -> str | None:
        return self.headers.get("host")


class Response:
    """A buffered response."""

    __slots__ = ("status", "headers", "body")

    def __init__(self, status: int = 200, headers: Sequence[tuple[str, str]] = (), body: bytes = b"") -> None:
        self.status = status
        self.headers = list(headers)
        self.body = body


class StreamResponse:
    """A response whose body is produced incrementally.

    Used for server-sent events. The iterator yields byte chunks; the server
    forwards each chunk as it arrives instead of buffering the whole body.
    """

    __slots__ = ("status", "headers", "iterator")

    def __init__(self, iterator: Iterator[bytes], status: int = 200, headers: Sequence[tuple[str, str]] = ()) -> None:
        self.status = status
        self.headers = list(headers)
        self.iterator = iterator


def json_response(payload: Any, status: int = 200, headers: Sequence[tuple[str, str]] = ()) -> Response:
    # backslashreplace: a lone surrogate that survived every earlier
    # boundary cannot be UTF-8 encoded, so it would raise here and turn a
    # clean response into a bare 500. The escape text keeps the payload
    # buildable; encodable characters are byte-identical (F9).
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8", "backslashreplace"
    )
    base = [("Content-Type", "application/json; charset=utf-8")]
    return Response(status, [*base, *headers], body)


def no_content(status: int = 204) -> Response:
    return Response(status, [], b"")


class Route:
    __slots__ = ("methods", "regex", "handler", "name")

    def __init__(self, methods: frozenset[str], regex: re.Pattern[str], handler: Callable[[Request], Any], name: str) -> None:
        self.methods = methods
        self.regex = regex
        self.handler = handler
        self.name = name


class Router:
    """Path router with ``{parameter}`` segments."""

    def __init__(self) -> None:
        self._routes: list[Route] = []

    def add(self, methods: Sequence[str], pattern: str, handler: Callable[[Request], Any], name: str = "") -> None:
        regex = self._compile(pattern)
        self._routes.append(Route(frozenset(m.upper() for m in methods), regex, handler, name or pattern))

    def route(self, methods: Sequence[str], pattern: str, name: str = "") -> Callable[[Callable[[Request], Any]], Callable[[Request], Any]]:
        def decorator(handler: Callable[[Request], Any]) -> Callable[[Request], Any]:
            self.add(methods, pattern, handler, name)
            return handler

        return decorator

    @staticmethod
    def _compile(pattern: str) -> re.Pattern[str]:
        segments: list[str] = []
        for part in pattern.strip("/").split("/"):
            if not part:
                continue
            if part.startswith("{") and part.endswith("}"):
                segments.append(r"(?P<%s>[^/]+)" % re.escape(part[1:-1]).replace(r"\-", "_"))
            else:
                segments.append(re.escape(part))
        return re.compile(r"^/" + "/".join(segments) + r"/?$")

    def match(self, method: str, path: str) -> tuple[Route | None, dict[str, str], bool]:
        """Return ``(route, params, path_matched_but_method_did_not)``."""
        path_matched = False
        for route in self._routes:
            match = route.regex.match(path)
            if not match:
                continue
            path_matched = True
            if method.upper() in route.methods:
                params = {k: unquote(v) for k, v in match.groupdict().items()}
                return route, params, True
        return None, {}, path_matched

    def allowed_methods(self, path: str) -> list[str]:
        allowed: set[str] = set()
        for route in self._routes:
            if route.regex.match(path):
                allowed.update(route.methods)
        return sorted(allowed)


Handler = Callable[[Request], Any]


class _RequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "personal-llm-gateway"
    sys_version = ""

    #: Send without waiting for an acknowledgement.
    #:
    #: ``socketserver.StreamRequestHandler`` defaults this to False, so
    #: TCP_NODELAY is not set on the accepted socket and Nagle's algorithm
    #: applies. The handler writes a response in two segments - the header
    #: block, then the body - so Nagle holds the body until the header
    #: block is acknowledged, and the client's TCP delays that
    #: acknowledgement. Every response of about one MSS or less then paid
    #: a 41ms stall, on every request after the first on a connection.
    #: See ``docs/REVIEW.md`` R28.
    disable_nagle_algorithm = True

    # Silence the default stderr access log: it would echo request targets,
    # and request targets may carry data we do not want in logs.
    def log_message(self, fmt: str, *args: Any) -> None:  # pragma: no cover
        return

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def do_PUT(self) -> None:
        self._handle("PUT")

    def do_PATCH(self) -> None:
        self._handle("PATCH")

    def do_DELETE(self) -> None:
        self._handle("DELETE")

    def do_HEAD(self) -> None:
        self._handle("HEAD")

    def do_OPTIONS(self) -> None:
        self._handle("OPTIONS")

    def _handle(self, method: str) -> None:
        limiter = self.server.rate_limiter  # type: ignore[attr-defined]
        if not limiter.allow(self.client_address[0]):
            self.close_connection = True
            self.server._reject(  # type: ignore[attr-defined]
                self.connection,
                b'{"error":{"message":"local request rate limit exceeded","type":"rate_limited"}}',
            )
            return
        parts = urlsplit(self.path)
        path = unquote(parts.path) or "/"

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._send(Response(400, [("Content-Type", "text/plain; charset=utf-8")], b"bad content-length"))
            return
        if length < 0 or length > MAX_BODY_BYTES:
            self._send(Response(413, [("Content-Type", "text/plain; charset=utf-8")], b"request body too large"))
            return

        body = self.rfile.read(length) if length else b""
        headers = Headers(self.headers.items())
        server_port = int(self.server.server_address[1]) if self.server.server_address else 0
        request = Request(method, path, parts.query, headers, body, self.client_address, server_port)

        try:
            result = self.server.application(request)  # type: ignore[attr-defined]
        except Exception:  # pragma: no cover - defensive
            traceback.print_exc(file=sys.stderr)
            result = Response(500, [("Content-Type", "application/json; charset=utf-8")],
                              b'{"error":{"message":"internal error","type":"gateway_error"}}')

        self._send(result, head_only=(method == "HEAD"))

    def _send(self, result: Any, head_only: bool = False) -> None:
        try:
            if isinstance(result, StreamResponse):
                self._send_stream(result, head_only)
            else:
                self._send_buffered(result, head_only)
        except (BrokenPipeError, ConnectionResetError):  # pragma: no cover - client hung up
            self.close_connection = True

    def _send_buffered(self, response: Response, head_only: bool) -> None:
        self.send_response(response.status)
        seen_length = False
        for key, value in response.headers:
            if key.lower() == "content-length":
                seen_length = True
            self.send_header(key, value)
        if not seen_length:
            self.send_header("Content-Length", str(len(response.body)))
        if self.request_version == "HTTP/0.9":
            # An HTTP/0.9 request has no header block at all.
            if not head_only and response.body:
                self.wfile.write(response.body)
            return
        # The status line, the header block and the body are written as
        # **one** segment. Writing the body separately lets Nagle's
        # algorithm hold it until the header block is acknowledged, which
        # cost 41ms on every response of about one MSS or less.
        # See ``docs/REVIEW.md`` R28.
        head = b"".join(getattr(self, "_headers_buffer", [])) + b"\r\n"
        self._headers_buffer = []
        self.wfile.write(head if head_only else head + response.body)

    def _send_stream(self, response: StreamResponse, head_only: bool) -> None:
        self.send_response(response.status)
        for key, value in response.headers:
            self.send_header(key, value)
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        if head_only:
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
            return
        for chunk in with_sse_keepalives(response.iterator):
            if not chunk:
                continue
            # One write per frame: the length line, the frame and the
            # terminator belong to the same chunk on the wire.
            self.wfile.write(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
            self.wfile.flush()
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()


class _ThreadingServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64

    def __init__(self, address: tuple[str, int], handler: type[BaseHTTPRequestHandler],
                 max_workers: int, rate_limit_requests: int, rate_limit_window_seconds: float) -> None:
        self._workers = threading.BoundedSemaphore(max_workers)
        from gateway.http.rate_limit import RateLimiter

        self.rate_limiter = RateLimiter(rate_limit_requests, rate_limit_window_seconds)
        super().__init__(address, handler)

    @staticmethod
    def _reject(request: Any, message: bytes) -> None:
        response = (
            b"HTTP/1.1 429 Too Many Requests\r\n"
            b"Content-Type: application/json; charset=utf-8\r\n"
            b"Connection: close\r\n"
            b"Content-Length: " + str(len(message)).encode("ascii") + b"\r\n\r\n" + message
        )
        try:
            request.sendall(response)
        except OSError:
            pass

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._workers.acquire(blocking=False):
            self._reject(request, b'{"error":{"message":"worker capacity exceeded","type":"rate_limited"}}')
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._workers.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._workers.release()


class _ThreadingIPv6Server(_ThreadingServer):
    address_family = socket.AF_INET6


class Server:
    """Threaded HTTP server bound to an explicit address."""

    def __init__(self, host: str, port: int, handler: Handler, *,
                 max_workers: int = DEFAULT_MAX_WORKERS,
                 rate_limit_requests: int = DEFAULT_RATE_LIMIT_REQUESTS,
                 rate_limit_window_seconds: float = DEFAULT_RATE_LIMIT_WINDOW_SECONDS) -> None:
        self.host = host
        self.port = port
        if max_workers < 1:
            raise ValueError("max_workers must be positive")
        self.max_workers = max_workers
        self.rate_limit_requests = rate_limit_requests
        self.rate_limit_window_seconds = rate_limit_window_seconds
        self._handler = handler
        self._httpd: _ThreadingServer | None = None
        self._thread: threading.Thread | None = None

    def serve_forever(self) -> None:  # pragma: no cover - exercised by deployment
        assert self._httpd is not None
        self._httpd.serve_forever(poll_interval=0.2)

    def start_background(self) -> None:
        server_class = _ThreadingIPv6Server if ":" in self.host else _ThreadingServer
        self._httpd = server_class(
            (self.host, self.port), _RequestHandler, self.max_workers,
            self.rate_limit_requests, self.rate_limit_window_seconds,
        )
        self._httpd.application = self._handler  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._httpd.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    @property
    def actual_port(self) -> int:
        if self._httpd is not None:
            return int(self._httpd.server_address[1])
        return self.port


def local_ip_for(host: str) -> str | None:  # pragma: no cover - diagnostics only
    """Best-effort local address used to populate a URL. Never used for auth."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(0.2)
            sock.connect(("192.0.2.1", 80))
            return str(sock.getsockname()[0])
    except OSError:
        return None


def quote_path(path: str) -> str:  # pragma: no cover - helper for redirects
    return re.sub(r"[^A-Za-z0-9._~/-]", lambda m: "%%%02X" % ord(m.group(0)), path)


def is_loopback(host: str) -> bool:
    """True when *host* names a loopback address."""
    lowered = host.strip().lower()
    if lowered in {"localhost", "localhost.localdomain"}:
        return True
    try:
        import ipaddress

        return ipaddress.ip_address(lowered).is_loopback
    except ValueError:
        return False


def is_private_lan(host: str) -> bool:
    """True when *host* names an RFC1918 / unique-local IPv4 or IPv6 address."""
    try:
        import ipaddress

        address = ipaddress.ip_address(host.strip())
    except ValueError:
        return False
    if address.is_loopback:
        return True
    if isinstance(address, ipaddress.IPv4Address):
        return address.is_private and not address.is_link_local
    return address.is_private


Mapping_ = Mapping
