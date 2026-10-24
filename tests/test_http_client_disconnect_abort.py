"""A client that hangs up mid-stream must cancel the upstream request.

The HTTP layer notices a disconnect as a broken pipe on the response
write (``src/gateway/http/core.py``, the ``_send`` except branch). Before
this wiring the write simply failed, the handler returned, and the
upstream adapter kept running: the provider kept charging for a body
nobody was reading, and the request was never remembered as *aborted*
rather than *failed*.

These tests run the real handler over a real TCP connection whose peer
is gone, and assert the whole disconnect path:

1. the upstream stream is closed, so the adapter's own cleanup runs;
2. the adapter's cancel hook is invoked - the seam named
   ``T11-ANCHOR: client-abort propagation`` in ``gateway.core.routing``,
   reached by request id out of the request context, exactly as the
   application stores it for the access log;
3. the request is classified as a client abort, so a late provider error
   cannot bench a healthy route;
4. nothing on the path raises or prints a traceback - the client has
   already gone, so a secondary failure is noise, not information.
"""

from __future__ import annotations

import itertools
import io
import socket
import sys
import unittest
from typing import Any, Callable

from gateway.core.abort import (
    RouteContext,
    abort_request,
    classify_late_error,
    find_route_context,
    register_route_context,
    release_route_context,
)
from gateway.http.core import Request, Response, StreamResponse, _RequestHandler

REQUEST_ID = "req_disconnect_0001"


class _DroppingSocket:
    """A real socket whose peer disappears after *budget* writes."""

    def __init__(self, sock: socket.socket, budget: int) -> None:
        self._sock = sock
        self._budget = budget
        self.writes = 0

    def sendall(self, data: bytes) -> None:
        if self.writes >= self._budget:
            # What the kernel returns once the peer is gone and the
            # receive queue has been reset.
            raise BrokenPipeError(32, "Broken pipe")
        self.writes += 1
        self._sock.sendall(data)

    def setsockopt(self, *args: Any) -> None:
        self._sock.setsockopt(*args)

    def settimeout(self, *args: Any) -> None:
        self._sock.settimeout(*args)

    def makefile(self, *args: Any, **kwargs: Any):
        return self._sock.makefile(*args, **kwargs)

    def close(self) -> None:
        self._sock.close()

    def fileno(self) -> int:
        return self._sock.fileno()


class _FakeAdapter:
    """An adapter that offers the cancellation hook the seam discovers."""

    def __init__(self) -> None:
        self.cancelled: list[str] = []

    def cancel(self, request: Any = None) -> None:
        self.cancelled.append(type(request).__name__ if request is not None else "")


class _AllowAllRateLimiter:
    def allow(self, client_address: str) -> bool:
        return True


class _FakeServer:
    """The parts of the server object the handler reads."""

    server_address = ("127.0.0.1", 8765)

    def __init__(self, application: Callable[[Request], Any]) -> None:
        self.application = application
        self.rate_limiter = _AllowAllRateLimiter()


def _serve(application: Callable[[Request], Any], budget: int) -> str:
    """Run the real handler against a peer that is gone after *budget* writes.

    Returns everything the handler wrote to stderr, so a test can assert
    that the disconnect path stayed silent.
    """
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    host, port = listener.getsockname()
    client = socket.create_connection((host, port))
    server_side, _ = listener.accept()
    dropping = _DroppingSocket(server_side, budget)
    client.sendall(b"GET /v1/stream HTTP/1.1\r\nHost: 127.0.0.1:8765\r\nConnection: close\r\n\r\n")

    captured = io.StringIO()
    original_stderr = sys.stderr
    sys.stderr = captured
    try:
        _RequestHandler(dropping, (host, port), _FakeServer(application))  # type: ignore[arg-type]
    finally:
        sys.stderr = original_stderr
        client.close()
        listener.close()
        dropping.close()
    return captured.getvalue()


class _Upstream:
    """The upstream answer: a generator that records its own cleanup."""

    def __init__(self) -> None:
        self.closed = False

    def frames(self):
        try:
            while True:
                yield b'data: {"chunk":1}\n\n'
        finally:
            # This is where an adapter closes the provider connection.
            self.closed = True


def _application(upstream: _Upstream, adapter: _FakeAdapter, stream: bool = True) -> Callable[[Request], Any]:
    """An application shaped like the one in ``gateway.http.app``.

    It stores the request id on the request context and registers the
    route context with the routing core, which is how the HTTP layer
    finds the upstream request it has to cancel.
    """

    def handle(request: Request) -> Any:
        upstream_stream = upstream.frames()
        register_route_context(
            RouteContext(
                request_id=REQUEST_ID,
                provider="fake_provider",
                model="fake-model",
                adapter=adapter,
                request=object(),
                stream=upstream_stream,
            )
        )
        request.context["request_id"] = REQUEST_ID
        if not stream:
            return Response(200, [("Content-Type", "application/json")], b"{}")
        return StreamResponse(
            upstream_stream,
            status=200,
            headers=[("Content-Type", "text/event-stream; charset=utf-8")],
        )

    return handle


class ClientDisconnectTests(unittest.TestCase):
    def tearDown(self) -> None:
        context = find_route_context(REQUEST_ID)
        if context is not None:
            release_route_context(context)

    def test_the_upstream_stream_is_closed_when_the_client_hangs_up(self) -> None:
        upstream = _Upstream()
        stderr = _serve(_application(upstream, _FakeAdapter()), budget=1)
        self.assertTrue(
            upstream.closed,
            "the upstream generator must be closed when the client goes, or its "
            "provider connection stays open until garbage collection",
        )
        self.assertNotIn("Traceback", stderr)

    def test_the_adapters_cancel_hook_is_invoked(self) -> None:
        upstream = _Upstream()
        adapter = _FakeAdapter()
        _serve(_application(upstream, adapter), budget=1)
        self.assertEqual(
            len(adapter.cancelled),
            1,
            "the disconnect must reach the adapter's cancellation path exactly once",
        )

    def test_the_request_is_classified_as_a_client_abort(self) -> None:
        upstream = _Upstream()
        adapter = _FakeAdapter()
        _serve(_application(upstream, adapter), budget=1)
        context = find_route_context(REQUEST_ID)
        self.assertIsNotNone(context)
        assert context is not None
        self.assertTrue(context.aborted)
        self.assertEqual(context.abort_reason, "client_disconnected")

        class _LateError:
            error_class = "rate_limited"

        # A provider error that arrives after the client left is a
        # consequence of the abort, not a provider failure.
        self.assertEqual(classify_late_error(context, _LateError()), "client_abort")

    def test_a_second_disconnect_does_not_signal_the_adapter_again(self) -> None:
        upstream = _Upstream()
        adapter = _FakeAdapter()
        _serve(_application(upstream, adapter), budget=1)
        outcome = abort_request(REQUEST_ID, "client_disconnected")
        self.assertTrue(outcome.already_aborted)
        self.assertEqual(len(adapter.cancelled), 1)

    def test_a_disconnect_without_a_route_context_is_silent(self) -> None:
        # A request that never reached a provider - the console, a refused
        # route - has nothing to cancel, and must not raise or complain.
        stderr = _serve(lambda request: Response(200, [], b"{}"), budget=0)
        self.assertNotIn("Traceback", stderr)

    def test_a_buffered_response_write_failure_stays_silent(self) -> None:
        upstream = _Upstream()
        adapter = _FakeAdapter()

        def handle(request: Request) -> Any:
            upstream_stream = upstream.frames()
            # The buffered path: the application drained the stream before
            # it built the response. ``frames()`` is endless, so drain a
            # bounded prefix - an unbounded ``list()`` would never return.
            list(itertools.islice(upstream_stream, 3))  # buffered path: the stream was consumed
            register_route_context(
                RouteContext(
                    request_id=REQUEST_ID,
                    provider="fake_provider",
                    model="fake-model",
                    adapter=adapter,
                    request=object(),
                    stream=upstream_stream,
                )
            )
            request.context["request_id"] = REQUEST_ID
            return Response(200, [("Content-Type", "application/json")], b'{"ok":true}')

        stderr = _serve(handle, budget=0)
        self.assertNotIn("Traceback", stderr)
        context = find_route_context(REQUEST_ID)
        self.assertIsNotNone(context)
        assert context is not None
        self.assertTrue(context.aborted)


if __name__ == "__main__":
    unittest.main()
