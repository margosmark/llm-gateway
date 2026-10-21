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
from http.server import BaseHTTPRequestHandler
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence
from urllib.parse import parse_qs, unquote, urlsplit

MAX_BODY_BYTES = 4 * 1024 * 1024
"""Request bodies above this size are refused with 413."""


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
        if not self.body:
            return None
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("request body is not valid JSON") from exc

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
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
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
        self.end_headers()
        if not head_only and response.body:
            self.wfile.write(response.body)

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
        for chunk in response.iterator:
            if not chunk:
                continue
            self.wfile.write(b"%x\r\n" % len(chunk))
            self.wfile.write(chunk)
            self.wfile.write(b"\r\n")
            self.wfile.flush()
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()


class _ThreadingServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64


class _ThreadingIPv6Server(_ThreadingServer):
    address_family = socket.AF_INET6


class Server:
    """Threaded HTTP server bound to an explicit address."""

    def __init__(self, host: str, port: int, handler: Handler) -> None:
        self.host = host
        self.port = port
        self._handler = handler
        self._httpd: _ThreadingServer | None = None
        self._thread: threading.Thread | None = None

    def serve_forever(self) -> None:  # pragma: no cover - exercised by deployment
        assert self._httpd is not None
        self._httpd.serve_forever(poll_interval=0.2)

    def start_background(self) -> None:
        server_class = _ThreadingIPv6Server if ":" in self.host else _ThreadingServer
        self._httpd = server_class((self.host, self.port), _RequestHandler)
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
