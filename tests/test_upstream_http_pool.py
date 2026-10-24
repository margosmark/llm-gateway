"""Upstream connections are reused, bounded, and still refused a redirect.

The upstream client used to open a **new connection for every request** and
never keep one alive, so every provider call paid a fresh TCP handshake -
and, over TLS, a fresh handshake (~55ms on this network) - before the
provider did any work. For a short non-streaming request that was pure added
latency.

Pooling is the fix, and it buys three properties that must be held together:

* a connection is **reused** across sequential requests to the same origin;
* the idle pool is **bounded**, so it cannot grow without limit;
* a redirect is still **refused**, so a redirect cannot quietly move a
  credential to another host.

Pooling also introduces a failure a connection-per-request client cannot
have: the peer may close an idle connection while the pool still holds it.
That case is covered here too - the stale connection is discarded *before*
the request is written, so a request is never replayed and a side effect is
never duplicated.
"""

from __future__ import annotations

import socket
import threading
import unittest
from typing import ClassVar
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from gateway.adapters.base import AdapterError
from gateway.adapters.http_client import (
    UpstreamClient,
    _ConnectionPool,
    close_all_clients,
)
from gateway.adapters.registry import AdapterRegistry


class _Handler(BaseHTTPRequestHandler):
    """Answers plainly, and can pretend to be several servers at once."""

    protocol_version = "HTTP/1.1"
    connections: ClassVar[set[int]] = set()
    requests = 0
    redirect = False
    drop_after = False
    error_status = 0
    error_body = b""
    sse_body = b""

    def do_GET(self):
        type(self).requests += 1
        type(self).connections.add(self.client_address[1])
        if type(self).redirect:
            self.send_response(302)
            self.send_header("Location", "/secret-target")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        status = type(self).error_status or 200
        if type(self).sse_body:
            body = type(self).sse_body
        elif type(self).error_status:
            body = type(self).error_body
        else:
            body = b'{"ok":true}'
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        if type(self).drop_after:
            # Answer, then hang up without saying so: the client sees a
            # keep-alive response and pools the connection, which the peer
            # has already closed. This is the stale-connection case.
            self.close_connection = True
            self.connection.close()

    def handle_one_request(self):
        """Answer one request, ignoring the peer hanging up.

        The stale-connection test has the client drop a connection the
        server still holds, which surfaces here as a reset. That is the
        scenario under test, not a failure.
        """
        try:
            super().handle_one_request()
        except (ConnectionResetError, BrokenPipeError):
            self.close_connection = True

    def log_message(self, *_args):
        pass


class _FakeConnection:
    """A connection that only records whether it was closed."""

    def __init__(self, sock=None, peer=None):
        self.closed = False
        self.sock = sock
        self.peer = peer

    def close(self):
        self.closed = True
        if self.sock is not None:
            self.sock.close()
            self.sock = None
        if self.peer is not None:
            self.peer.close()
            self.peer = None


def _fake_live_connection():
    """A reusable connection backed by a genuinely live socket."""
    sock, peer = socket.socketpair()
    return _FakeConnection(sock=sock, peer=peer)


class UpstreamConnectionPoolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}/"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        _Handler.connections = set()
        _Handler.requests = 0
        _Handler.redirect = False
        _Handler.drop_after = False
        _Handler.error_status = 0
        _Handler.error_body = b""
        _Handler.sse_body = b""

    # --- reuse ---------------------------------------------------------

    def test_sequential_requests_reuse_one_connection(self):
        """The whole point: the second request opens no new connection."""
        client = UpstreamClient(timeout=5)
        try:
            first = client.request("GET", self.url, {})
            second = client.request("GET", self.url, {})
            third = client.request("GET", self.url, {})
            self.assertEqual([r.body for r in (first, second, third)], [b'{"ok":true}'] * 3)
            self.assertEqual(len(_Handler.connections), 1, "one TCP connection must serve all three")
        finally:
            client.close()

    def test_two_clients_do_not_share_a_pool(self):
        """The pool is per client, so one provider cannot take another's."""
        first, second = UpstreamClient(timeout=5), UpstreamClient(timeout=5)
        try:
            first.request("GET", self.url, {})
            first.request("GET", self.url, {})
            second.request("GET", self.url, {})
            self.assertEqual(len(_Handler.connections), 2, "each client owns its own connection")
        finally:
            first.close()
            second.close()

    def test_a_streamed_response_returns_its_connection_to_the_pool(self):
        """A fully consumed stream leaves the connection reusable."""
        _Handler.sse_body = b'data: {"ok":true}\n\ndata: [DONE]\n\n'
        client = UpstreamClient(timeout=5)
        try:
            response = client.request("GET", self.url, {}, stream=True)
            self.assertIsNotNone(response.stream)
            self.assertEqual(b"".join(response.stream), b'data: {"ok":true}\n\ndata: [DONE]\n\n')
            self.assertEqual(len(client._pool), 1, "a completed stream must give the connection back")
            _Handler.sse_body = b""
            again = client.request("GET", self.url, {})
            self.assertEqual(again.body, b'{"ok":true}')
            self.assertEqual(len(_Handler.connections), 1, "the streamed connection must be reused")
        finally:
            client.close()

    def test_an_unconsumed_stream_is_never_reused(self):
        """Bytes still in flight mean the connection is not clean."""
        _Handler.sse_body = b'data: {"a":1}\n\ndata: {"b":2}\n\ndata: [DONE]\n\n'
        client = UpstreamClient(timeout=5)
        try:
            response = client.request("GET", self.url, {}, stream=True)
            # Start the stream, read one frame, then abandon it - a caller
            # that hung up mid-stream. The connection has bytes in flight
            # and must not be handed to another request.
            iterator = response.stream
            self.assertTrue(next(iterator).startswith(b"data:"))
            iterator.close()
            self.assertEqual(len(client._pool), 0, "a half-read stream must not be pooled")
        finally:
            client.close()

    # --- exhaustion ----------------------------------------------------

    def test_the_idle_pool_is_bounded_and_overflow_is_closed(self):
        pool = _ConnectionPool(max_idle=1)
        key = ("http", "example.test", 80)
        kept, discarded = _fake_live_connection(), _fake_live_connection()
        pool.release(key, kept, True)
        pool.release(key, discarded, True)
        self.assertEqual(len(pool), 1, "the pool must hold no more than its bound")
        self.assertFalse(kept.closed, "the connection inside the bound is kept")
        self.assertTrue(discarded.closed, "overflow is closed at once, not accumulated")
        pool.close()

    def test_a_non_reusable_connection_is_closed_not_pooled(self):
        pool = _ConnectionPool()
        key = ("http", "example.test", 80)
        connection = _FakeConnection()
        pool.release(key, connection, False)
        self.assertEqual(len(pool), 0)
        self.assertTrue(connection.closed)
        pool.close()

    # --- a stale pooled connection -------------------------------------

    def test_a_connection_the_peer_closed_is_discarded_before_the_request(self):
        """The peer may drop an idle connection at any moment.

        The next request must still succeed, and must be written once - to a
        fresh connection - rather than replayed on the dead one.
        """
        _Handler.drop_after = True
        client = UpstreamClient(timeout=5)
        try:
            first = client.request("GET", self.url, {})
            self.assertEqual(first.body, b'{"ok":true}')
            self.assertEqual(len(client._pool), 1, "the client pooled a keep-alive connection")

            _Handler.drop_after = False
            second = client.request("GET", self.url, {})
            self.assertEqual(second.body, b'{"ok":true}', "the stale connection must not surface")
            self.assertEqual(_Handler.requests, 2, "the request must be answered exactly once")
        finally:
            client.close()

    def test_a_closed_pool_refuses_the_next_request(self):
        """Shutdown closes the pool; a later request says so, not a crash."""
        client = UpstreamClient(timeout=5)
        client.request("GET", self.url, {})
        client.close()
        with self.assertRaises(AdapterError) as caught:
            client.request("GET", self.url, {})
        self.assertIn("shutting down", str(caught.exception))
        self.assertTrue(client.closed)

    def test_close_is_idempotent(self):
        client = UpstreamClient(timeout=5)
        client.request("GET", self.url, {})
        client.close()
        client.close()
        self.assertTrue(client.closed)

    # --- shutdown ------------------------------------------------------

    def test_the_registry_closes_every_client_it_reached(self):
        """A stop that returns with connections open is not a stop."""
        client = UpstreamClient(timeout=5)
        client.request("GET", self.url, {})
        self.assertEqual(len(client._pool), 1)

        registry = AdapterRegistry()
        registry.close()

        self.assertTrue(client.closed, "the registry must release pooled connections")
        self.assertEqual(len(client._pool), 0)

    def test_close_all_clients_reaches_every_live_client(self):
        clients = [UpstreamClient(timeout=5) for _ in range(3)]
        for client in clients:
            client.request("GET", self.url, {})
        close_all_clients()
        self.assertTrue(all(client.closed for client in clients))
        self.assertEqual(len(_Handler.connections), 3, "each client still had its own connection")

    # --- redirects stay refused ----------------------------------------

    def test_a_redirect_is_returned_not_followed(self):
        _Handler.redirect = True
        client = UpstreamClient(timeout=5)
        try:
            response = client.request(
                "GET", self.url, {"Authorization": "Bearer synthetic-secret"}
            )
            self.assertEqual(response.status, 302)
            self.assertEqual(_Handler.requests, 1, "the redirect target must never be fetched")
            self.assertNotIn("synthetic-secret", str(response), "the credential must not be echoed")
            self.assertIn("location", response.headers, "the caller still needs to see where it pointed")
        finally:
            client.close()

    def test_a_redirect_body_is_not_echoed_and_its_connection_is_dropped(self):
        _Handler.redirect = True
        client = UpstreamClient(timeout=5)
        try:
            response = client.request("GET", self.url, {"Authorization": "Bearer synthetic-secret"})
            self.assertNotIn("synthetic-secret", str(response))
            self.assertEqual(len(client._pool), 0, "a redirect's connection is not reused")
        finally:
            client.close()

    # --- error bodies survive a streaming request -----------------------

    def test_a_streaming_request_still_buffers_an_error_body(self):
        """An error body is what the adapter turns into the caller's error.

        A streaming request that comes back non-2xx must carry that body as
        bytes, not as a stream the adapter would have to consume.
        """
        _Handler.error_status = 429
        _Handler.error_body = b'{"error":"rate limited"}'
        client = UpstreamClient(timeout=5)
        try:
            response = client.request("GET", self.url, {}, stream=True)
            self.assertEqual(response.status, 429)
            self.assertIsNone(response.stream, "a non-2xx answer is not a stream")
            self.assertEqual(response.body, b'{"error":"rate limited"}')
        finally:
            client.close()

    # --- timeouts and credentials ---------------------------------------

    def test_the_timeout_is_applied_to_a_new_connection(self):
        client = UpstreamClient(timeout=0.125)
        try:
            key = ("http", "127.0.0.1", self.server.server_port)
            connection, reused = client._pool.acquire(key, client.timeout)
            self.assertFalse(reused, "an empty pool must open a fresh connection")
            self.assertEqual(connection.timeout, 0.125)
            connection.connect()
            self.assertEqual(connection.sock.gettimeout(), 0.125)
            client._pool.release(key, connection, True)
        finally:
            client.close()

    def test_the_timeout_is_reapplied_to_a_reused_connection(self):
        """A reused socket keeps the timeout of the request using it."""
        client = UpstreamClient(timeout=5)
        try:
            client.request("GET", self.url, {})
            key = ("http", "127.0.0.1", self.server.server_port)
            connection, reused = client._pool.acquire(key, 0.25)
            self.assertTrue(reused)
            self.assertEqual(connection.timeout, 0.25)
            self.assertEqual(connection.sock.gettimeout(), 0.25, "the socket must be re-armed, not just the attribute")
            client._pool.release(key, connection, True)
        finally:
            client.close()

    def test_a_refused_connection_names_no_credential(self):
        """A pooled client must not leak the header into a traceback."""
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        dead_port = probe.getsockname()[1]
        probe.close()
        client = UpstreamClient(timeout=2)
        try:
            with self.assertRaises(AdapterError) as caught:
                client.request(
                    "GET",
                    f"http://127.0.0.1:{dead_port}/",
                    {"Authorization": "Bearer synthetic-secret"},
                )
            self.assertNotIn("synthetic-secret", str(caught.exception))
            self.assertEqual(len(client._pool), 0, "a failed connection is never pooled")
        finally:
            client.close()


if __name__ == "__main__":
    unittest.main()
