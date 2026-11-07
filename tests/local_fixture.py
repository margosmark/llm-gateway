"""An isolated loopback stand-in for a local model server.

Nothing here touches the network beyond ``127.0.0.1``, and nothing here is a
real server: it is a scripted double that records every request it receives,
so a test can assert on the exact bytes the gateway sent - including the
absence of an ``Authorization`` header, of ``max_completion_tokens`` and of
another provider's credential.

It speaks the surface all three local servers publish (``GET /v1/models``,
``POST /v1/chat/completions``, streamed and buffered) plus Ollama's
``POST /api/show``, and it can be told to behave badly: stall before the
head, redirect, require a token, answer with any status.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

#: A deliberately obvious placeholder. It is never a real credential and it
#: is never printed by a test - only its presence or absence is asserted.
SYNTHETIC_TOKEN = "fixture-token-not-a-secret"

DEFAULT_MODELS: dict[str, Any] = {"data": [{"id": "local-test-model"}]}

DEFAULT_CHAT: dict[str, Any] = {
    "id": "chatcmpl-fixture",
    "object": "chat.completion",
    "choices": [
        {"index": 0, "message": {"role": "assistant", "content": "hello"}, "finish_reason": "stop"}
    ],
    "usage": {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8},
}

DEFAULT_SHOW: dict[str, Any] = {
    "capabilities": ["completion", "thinking", "vision", "tools"],
    "model_info": {"llama.context_length": 8192, "llama.embedding_length": 4096},
    "details": {"family": "fixture"},
}


class LocalFixture:
    """A scripted local server on loopback, with a request log."""

    def __init__(
        self,
        *,
        models: Any = DEFAULT_MODELS,
        chat: Any = DEFAULT_CHAT,
        show: Any = DEFAULT_SHOW,
        models_status: int = 200,
        chat_status: int = 200,
        show_status: int | None = None,
        chat_error: dict[str, Any] | None = None,
        stream_frames: list[dict[str, Any]] | None = None,
        sse_comments: bool = False,
        head_delay: float = 0.0,
        frame_delay: float = 0.0,
        redirect_to: str | None = None,
        require_bearer: str | None = None,
        close_after: int | None = None,
    ) -> None:
        self.models = models
        self.chat = chat
        self.show = show
        self.models_status = models_status
        self.chat_status = chat_status
        self.show_status = show_status
        self.chat_error = chat_error
        self.stream_frames = stream_frames
        self.sse_comments = sse_comments
        self.head_delay = head_delay
        #: Per-frame pacing, so a stream can be made to outlive any short
        #: budget without a fixture that hangs.
        self.frame_delay = frame_delay
        self.redirect_to = redirect_to
        self.require_bearer = require_bearer
        self.close_after = close_after
        self.requests: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any, **kwargs: Any) -> None:  # noqa: D102 - silence the stdlib logger
                return

            def _record(self, body: bytes) -> None:
                with fixture._lock:
                    fixture.requests.append(
                        {
                            "method": self.command,
                            "path": self.path,
                            "headers": dict(self.headers.items()),
                            "body": body,
                        }
                    )

            def _reply(self, status: int, payload: bytes, content_type: str = "application/json") -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                try:
                    self.wfile.write(payload)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def _read_body(self) -> bytes:
                length = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(length) if length else b""

            def _authorised(self) -> bool:
                if fixture.require_bearer is None:
                    return True
                return self.headers.get("Authorization") == f"Bearer {fixture.require_bearer}"

            def do_GET(self) -> None:  # noqa: N802 - stdlib naming
                self._record(b"")
                if not self._authorised():
                    # llama-server's documented error shape: an integer code.
                    self._reply(
                        401,
                        json.dumps(
                            {"error": {"code": 401, "message": "Invalid API Key", "type": "authentication_error"}}
                        ).encode(),
                    )
                    return
                if self.path.startswith("/v1/models"):
                    if fixture.head_delay:
                        time.sleep(fixture.head_delay)
                    body = json.dumps(fixture.models).encode()
                    self._reply(fixture.models_status, body)
                    return
                self._reply(404, b'{"error":{"message":"not found"}}')

            def do_POST(self) -> None:  # noqa: N802 - stdlib naming
                body = self._read_body()
                self._record(body)
                if not self._authorised():
                    self._reply(
                        401,
                        json.dumps(
                            {"error": {"code": 401, "message": "Invalid API Key", "type": "authentication_error"}}
                        ).encode(),
                    )
                    return
                if self.path.endswith("/api/show"):
                    status = fixture.show_status if fixture.show_status is not None else 200
                    if status >= 400:
                        self._reply(status, b'{"error":{"message":"no model found"}}')
                        return
                    self._reply(status, json.dumps(fixture.show).encode())
                    return
                if self.path.endswith("/v1/chat/completions"):
                    if fixture.head_delay:
                        time.sleep(fixture.head_delay)
                    if fixture.redirect_to is not None:
                        self.send_response(302)
                        self.send_header("Location", fixture.redirect_to)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    if fixture.chat_status >= 400:
                        payload = fixture.chat_error or {"error": {"message": "upstream refused"}}
                        self._reply(fixture.chat_status, json.dumps(payload).encode())
                        return
                    request_payload = json.loads(body.decode("utf-8") or "{}")
                    if request_payload.get("stream"):
                        self._stream(request_payload)
                        return
                    self._reply(200, json.dumps(fixture.chat).encode())
                    return
                self._reply(404, b'{"error":{"message":"not found"}}')

            def _stream(self, request_payload: dict[str, Any]) -> None:
                del request_payload
                frames = fixture.stream_frames
                if frames is None:
                    frames = [
                        {
                            "id": "chatcmpl-fixture",
                            "object": "chat.completion.chunk",
                            "choices": [{"index": 0, "delta": {"content": "hel"}, "finish_reason": None}],
                        },
                        {
                            "id": "chatcmpl-fixture",
                            "object": "chat.completion.chunk",
                            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                            "usage": {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8},
                        },
                    ]
                paced = bool(fixture.frame_delay)
                payload = (
                    b"" if paced
                    else b"".join(_sse_frame(chunk, fixture.sse_comments) for chunk in frames)
                    + b"data: [DONE]\n\n"
                )
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                if paced:
                    # No Content-Length: the stream ends at the socket, so
                    # the connection has to be closed for the reader to see
                    # the end instead of waiting for a byte that never comes.
                    self.send_header("Connection", "close")
                    self.close_connection = True
                else:
                    self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                try:
                    if paced:
                        # A progressing stream, frame by frame: the head and
                        # each later frame are separate waits, so a read
                        # budget has to be the one that judges them.
                        for chunk in frames:
                            time.sleep(fixture.frame_delay)
                            self.wfile.write(_sse_frame(chunk, fixture.sse_comments))
                            self.wfile.flush()
                        self.wfile.write(b"data: [DONE]\n\n")
                        self.wfile.flush()
                    else:
                        self.wfile.write(payload)
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    @property
    def base_url(self) -> str:
        """The server root, which is what an owner would paste."""
        return f"http://127.0.0.1:{self.port}"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def paths(self) -> list[str]:
        with self._lock:
            return [entry["path"] for entry in self.requests]

    def last(self) -> dict[str, Any]:
        with self._lock:
            return dict(self.requests[-1])


def _sse_frame(chunk: dict[str, Any], comment: bool) -> bytes:
    """One complete SSE frame: the data line(s) plus their blank terminator."""
    out = b""
    if comment:
        # llama-server's keep-alive. A comment-only frame carries no event.
        out += b": ping\n\n"
    out += b"data: " + json.dumps(chunk).encode() + b"\n\n"
    return out


class ClosedPort:
    """A loopback port nothing is listening on - the connection-refused case."""

    def __init__(self) -> None:
        import socket

        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        self.port = int(probe.getsockname()[1])
        probe.close()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"
