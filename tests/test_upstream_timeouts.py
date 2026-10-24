"""The connect budget and the read budget are separate, and each judges its own phase.

One timeout used to serve both. ``DEFAULT_TIMEOUT = 120.0`` was handed to
the socket for the connect *and* the read, so a provider that **accepted
a connection and then went quiet** held a gateway worker for two minutes.
With T10's worker pool bounding concurrency, a handful of such providers
is enough to exhaust the process, so the stall has to be abandoned on the
*connect* budget - and the generous read budget has to be reserved for
what it is actually good at: a long response that is still arriving.

The properties under test, all driven against loopback servers with no
network access and no credentials:

* a provider that accepts and stalls is abandoned after the **connect**
  budget, in every phase where silence can only mean a stall - the TLS
  handshake of an accepted connection, and the head of a stream;
* a long but **progressing** stream is *not* abandoned: frames within the
  read budget count as progress, however long the stream runs;
* once the head of a stream is in, the **read** budget is the one armed
  on the socket: a single inter-frame gap that exceeds the connect
  budget but fits the read budget (a long reasoning pause) must not
  abandon the stream;
* a buffered body that dribbles in more slowly than the connect budget
  still arrives whole, because the read budget judges it;
* a silent **buffered** response is deliberately judged by the read
  budget, not the connect budget - its head may legitimately follow the
  whole generation, so silence there is not evidence of a stall;
* the budgets reach the socket in every phase, including a pooled
  connection reused for a later, slower request;
* neither budget leaks a credential into its error.
"""

from __future__ import annotations

import http.client
import socket
import threading
import time
import unittest
from typing import ClassVar
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from gateway.adapters.base import AdapterError
from gateway.adapters.http_client import (
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_TIMEOUT,
    UpstreamClient,
)
from gateway.config import ERROR_TRANSIENT


class _SilentListener(threading.Thread):
    """Accepts connections and then never says a word.

    The kernel completes the TCP handshake, so from the client's side the
    provider *accepted* the connection; nothing else ever arrives. Against
    an ``https://`` URL the TLS handshake is the first thing to stall -
    the classic "accepts and stalls" provider.
    """

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self._listener = socket.socket()
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(16)
        self.port = self._listener.getsockname()[1]
        self.accepted = 0
        self._held: list[socket.socket] = []

    def run(self) -> None:
        while True:
            try:
                connection, _address = self._listener.accept()
            except OSError:
                return
            self.accepted += 1
            self._held.append(connection)

    def close(self) -> None:
        try:
            self._listener.close()
        except OSError:  # pragma: no cover - defensive
            pass
        for connection in self._held:
            try:
                connection.close()
            except OSError:  # pragma: no cover - defensive
                pass


class _QuietHandler(BaseHTTPRequestHandler):
    """Reads the request, then answers exactly as slowly as it is told.

    ``stream_frames`` sends an SSE body in that many frames, one every
    ``frame_gap`` seconds, then hangs up. ``chunks`` sends a buffered body
    of ``(delay, bytes)`` pairs. With neither set, the request is read and
    never answered at all.
    """

    protocol_version = "HTTP/1.1"
    stream_frames = 0
    frame_gap = 0.0
    chunks: ClassVar[list[tuple[float, bytes]]] = []
    silent = False
    silence = threading.Event()

    def do_GET(self):
        if type(self).silent:
            # Read the request, then never answer it - a provider that
            # accepted the work and went quiet.
            type(self).silence.wait(timeout=30.0)
            return
        if type(self).stream_frames:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            for index in range(type(self).stream_frames):
                if index:
                    time.sleep(type(self).frame_gap)
                self.wfile.write(f"data: frame-{index}\n\n".encode())
                self.wfile.flush()
            # No content length: the client reads until the peer hangs up,
            # which is what ends the stream.
            self.close_connection = True
            return
        body = b"".join(chunk for _delay, chunk in type(self).chunks)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        for delay, chunk in type(self).chunks:
            if delay:
                time.sleep(delay)
            self.wfile.write(chunk)
            self.wfile.flush()

    def log_message(self, *_args):
        pass


class ConnectAndReadBudgetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.silent = _SilentListener()
        cls.silent.start()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _QuietHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}/"

    @classmethod
    def tearDownClass(cls) -> None:
        _QuietHandler.silence.set()
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.silent.close()
        cls.silent.join(timeout=5)

    def setUp(self) -> None:
        _QuietHandler.stream_frames = 0
        _QuietHandler.frame_gap = 0.0
        _QuietHandler.chunks = []
        _QuietHandler.silent = False

    # --- the connect budget judges a stall ------------------------------

    def test_a_provider_that_accepts_and_stalls_is_abandoned_after_the_connect_budget(self):
        """The kernel accepts, the TLS handshake never completes: connect budget.

        This is the two-minute worker hold from the TODO: the provider
        accepted the connection and went quiet, and the read timeout used
        to be the only thing bounding it.
        """
        client = UpstreamClient(timeout=30.0, connect_timeout=0.4)
        try:
            started = time.monotonic()
            with self.assertRaises(AdapterError) as caught:
                client.request(
                    "GET",
                    f"https://127.0.0.1:{self.silent.port}/",
                    {"Authorization": "Bearer synthetic-secret"},
                )
            elapsed = time.monotonic() - started
        finally:
            client.close()
        self.assertGreaterEqual(self.silent.accepted, 1, "the provider did accept the connection")
        self.assertEqual(caught.exception.error_class, ERROR_TRANSIENT)
        self.assertIn("did not accept a connection", caught.exception.message)
        self.assertGreaterEqual(elapsed, 0.3, "the connect budget must actually be waited out")
        self.assertLess(
            elapsed,
            5.0,
            f"abandoned after the {client.connect_timeout}s connect budget, not the 30s read budget (took {elapsed:.2f}s)",
        )
        self.assertEqual(len(client._pool), 0, "a connection that never completed is never pooled")

    def test_a_stalled_stream_head_is_judged_by_the_connect_budget(self):
        """An accepted connection whose stream never starts is a stall too.

        An SSE provider sends its status line before it generates
        anything, so a head that has not arrived within the connect
        budget cannot be a slow generation.
        """
        _QuietHandler.silent = True
        client = UpstreamClient(timeout=30.0, connect_timeout=0.4)
        try:
            started = time.monotonic()
            with self.assertRaises(AdapterError) as caught:
                client.request("GET", self.url, {}, stream=True)
            elapsed = time.monotonic() - started
        finally:
            client.close()
        self.assertIn("did not start a response", caught.exception.message)
        self.assertLess(
            elapsed,
            5.0,
            f"the stalled stream head must be abandoned on the connect budget (took {elapsed:.2f}s)",
        )

    # --- the read budget judges progress --------------------------------

    def test_a_long_progressing_stream_is_not_abandoned_at_the_connect_budget(self):
        """Frames keep arriving, each within the read budget: no abandonment.

        The whole stream runs several times longer than the connect
        budget; progress inside the read budget is all the evidence the
        client needs.
        """
        _QuietHandler.stream_frames = 6
        _QuietHandler.frame_gap = 0.35  # each gap is within the 5s read budget
        client = UpstreamClient(timeout=5.0, connect_timeout=0.4)
        try:
            started = time.monotonic()
            response = client.request("GET", self.url, {}, stream=True)
            self.assertIsNotNone(response.stream)
            frames = b"".join(response.stream)
            elapsed = time.monotonic() - started
        finally:
            client.close()
        for index in range(6):
            self.assertIn(f"data: frame-{index}".encode(), frames)
        self.assertGreaterEqual(
            elapsed,
            1.5,
            "the stream must outlive the connect budget several times over to prove anything",
        )

    def test_a_frame_gap_beyond_the_connect_budget_is_judged_by_the_read_budget(self):
        """One long gap - longer than the connect budget - does not kill a progressing stream.

        This is the long-reasoning pause of a real provider: the head is
        in, one frame has arrived, then the provider thinks for longer
        than the connect budget before the next frame. The read budget
        must be the one in force after the head, so the stream survives
        the gap. When the connect budget is still armed on the socket,
        the gap read fails at the connect budget and the stream is
        abandoned mid-progress.
        """
        _QuietHandler.stream_frames = 2
        _QuietHandler.frame_gap = 1.5  # above the 0.6s connect budget, within the 3s read budget
        client = UpstreamClient(timeout=3.0, connect_timeout=0.6)
        # Socket-level, observed from inside the response: every frame
        # read must find the read budget armed on the socket. The head is
        # read inside begin() through the file object directly, so the
        # spy sees only the frame reads that follow it.
        original_readline = http.client.HTTPResponse.readline
        timeout_in_force: list[float] = []

        def spy(response, *args, **kwargs):
            timeout_in_force.append(response.fp.raw._sock.gettimeout())
            return original_readline(response, *args, **kwargs)

        http.client.HTTPResponse.readline = spy  # type: ignore[method-assign]
        try:
            started = time.monotonic()
            response = client.request("GET", self.url, {}, stream=True)
            self.assertIsNotNone(response.stream)
            frames = b"".join(response.stream)
            elapsed = time.monotonic() - started
        finally:
            http.client.HTTPResponse.readline = original_readline
            client.close()
        for index in range(2):
            self.assertIn(f"data: frame-{index}".encode(), frames)
        self.assertTrue(timeout_in_force, "the stream must have been read line by line")
        for read_timeout in timeout_in_force:
            self.assertEqual(
                read_timeout,
                3.0,
                "every frame read must be judged by the read budget, not the connect budget",
            )
        self.assertGreaterEqual(
            elapsed,
            1.4,
            "the inter-frame gap must have been waited out under the read budget, not cut at the connect budget",
        )

    def test_a_slow_buffered_body_is_judged_by_the_read_budget(self):
        """A body that dribbles in past the connect budget still arrives whole."""
        _QuietHandler.chunks = [(0.0, b'{"answer"'), (0.6, b":1}")]
        client = UpstreamClient(timeout=5.0, connect_timeout=0.4)
        try:
            started = time.monotonic()
            response = client.request("GET", self.url, {})
            elapsed = time.monotonic() - started
        finally:
            client.close()
        self.assertEqual(response.body, b'{"answer":1}')
        self.assertGreaterEqual(
            elapsed,
            0.5,
            "the dribbling body must have been waited for past the connect budget",
        )

    def test_a_silent_buffered_response_is_judged_by_the_read_budget(self):
        """A buffered head may follow the whole generation, so silence is not a stall.

        The read budget is deliberately the judge here: a provider that
        is generating a long completion sends nothing until it is done,
        and cutting that at the connect budget would break every slow
        non-streaming request the gateway serves.
        """
        _QuietHandler.silent = True
        client = UpstreamClient(timeout=0.6, connect_timeout=0.1)
        try:
            started = time.monotonic()
            with self.assertRaises(AdapterError) as caught:
                client.request("GET", self.url, {})
            elapsed = time.monotonic() - started
        finally:
            client.close()
        self.assertIn("did not respond within the configured timeout", caught.exception.message)
        self.assertGreaterEqual(
            elapsed,
            0.5,
            "the read budget, not the 0.1s connect budget, must judge a silent buffered response",
        )

    # --- the budgets reach the socket in every phase --------------------

    def test_a_pooled_connection_is_rearmed_with_the_read_budget(self):
        """A connection opened under the connect budget serves a slow request too.

        The socket was created under the connect timeout when it was
        first opened; a later request reusing it must still get the read
        budget, not a stale connect budget.
        """
        _QuietHandler.chunks = [(0.0, b"{}")]
        client = UpstreamClient(timeout=5.0, connect_timeout=0.4)
        try:
            first = client.request("GET", self.url, {})
            self.assertEqual(first.body, b"{}")
            self.assertEqual(len(client._pool), 1, "the keep-alive connection must be pooled")

            _QuietHandler.chunks = [(0.8, b'{"slow":true}')]
            started = time.monotonic()
            second = client.request("GET", self.url, {})
            elapsed = time.monotonic() - started
            self.assertEqual(second.body, b'{"slow":true}')
            self.assertEqual(len(client._pool), 1, "the same connection must have served it")
            self.assertGreaterEqual(
                elapsed,
                0.7,
                "the reused socket must carry the read budget, not the connect budget it was opened under",
            )
        finally:
            client.close()

    # --- defaults and refusal -------------------------------------------

    def test_the_default_budgets_are_separate(self):
        """The constants wired into a default client are two, not one."""
        client = UpstreamClient()
        try:
            self.assertEqual(client.timeout, DEFAULT_TIMEOUT)
            self.assertEqual(client.connect_timeout, DEFAULT_CONNECT_TIMEOUT)
            self.assertLess(client.connect_timeout, client.timeout)
        finally:
            client.close()

    def test_a_connect_timeout_never_names_a_credential(self):
        """The connect-phase error is metadata: no header, no token."""
        client = UpstreamClient(timeout=30.0, connect_timeout=0.4)
        try:
            with self.assertRaises(AdapterError) as caught:
                client.request(
                    "GET",
                    f"https://127.0.0.1:{self.silent.port}/",
                    {"Authorization": "Bearer synthetic-secret"},
                )
        finally:
            client.close()
        self.assertNotIn("synthetic-secret", str(caught.exception))
        self.assertNotIn("synthetic-secret", repr(caught.exception))


if __name__ == "__main__":
    unittest.main()
