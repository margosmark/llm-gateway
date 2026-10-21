"""The response must leave the process in one segment.

``socketserver.StreamRequestHandler`` does not set TCP_NODELAY by default,
and the handler used to write a response in two pieces: the header block,
then the body. Nagle's algorithm therefore held the body until the header
block was acknowledged, and the client's TCP delays that acknowledgement.
Every response of about one MSS or less then paid a **41ms stall**, on
every request after the first on a connection.

The symptom was easy to miss: the first request on a fresh connection is
fast, and a client that reconnects per request never sees it.

These tests assert the two properties that remove the stall:

1. the accepted socket has TCP_NODELAY set, and
2. a buffered response is written in a **single** write.

The second one is asserted against the real handler, over a real socket
pair, by counting the writes the handler makes.
"""

from __future__ import annotations

import socket
import unittest

from gateway.http.core import Response, StreamResponse, _RequestHandler


class _CountingSocket:
    """A real socket that records what the handler did to it."""

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        #: Every chunk the handler wrote.
        self.writes: list[bytes] = []
        #: Every socket option the handler set.
        self.options: list[tuple] = []

    # -- the socket API the handler uses ---------------------------------

    def sendall(self, data: bytes) -> None:
        self.writes.append(bytes(data))
        self._sock.sendall(data)

    def setsockopt(self, *args) -> None:
        self.options.append(args)
        self._sock.setsockopt(*args)

    def settimeout(self, *args) -> None:
        self._sock.settimeout(*args)

    def makefile(self, *args, **kwargs):
        return self._sock.makefile(*args, **kwargs)

    def close(self) -> None:
        self._sock.close()

    def fileno(self) -> int:
        return self._sock.fileno()


class _FakeServer:
    """The parts of the server object the handler reads."""

    server_address = ("127.0.0.1", 8765)

    def __init__(self, result: object) -> None:
        self.application = lambda request: result


def _serve(request_bytes: bytes, result: object = None) -> _CountingSocket:
    """Run the real handler over a TCP connection and return the socket.

    A real TCP socket pair is used rather than a socketpair, because
    TCP_NODELAY is not supported on AF_UNIX.
    """
    if result is None:
        result = Response(200, [("Content-Type", "application/json")], b"{}")
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    host, port = listener.getsockname()
    client = socket.create_connection((host, port))
    server_side, _ = listener.accept()
    counting = _CountingSocket(server_side)
    client.sendall(request_bytes)
    _RequestHandler(counting, ("127.0.0.1", 5555), _FakeServer(result))
    client.close()
    listener.close()
    counting.close()
    return counting


class ResponseWriteTests(unittest.TestCase):
    def test_the_accepted_socket_has_tcp_nodelay(self) -> None:
        counting = _serve(
            b"GET /health HTTP/1.1\r\nHost: 127.0.0.1:8765\r\nConnection: close\r\n\r\n",
            Response(200, [("Content-Type", "application/json")], b"{}"),
        )
        self.assertTrue(
            any(
                len(option) >= 3 and option[0] == socket.IPPROTO_TCP and option[1] == socket.TCP_NODELAY
                for option in counting.options
            ),
            "TCP_NODELAY must be set on the accepted socket, or Nagle's algorithm "
            "holds the response body until the header block is acknowledged",
        )

    def test_a_buffered_response_is_written_in_one_write(self) -> None:
        counting = _serve(
            b"GET /health HTTP/1.1\r\nHost: 127.0.0.1:8765\r\nConnection: close\r\n\r\n",
            Response(200, [("Content-Type", "application/json")], b'{"ok":true}'),
        )
        self.assertEqual(
            len(counting.writes),
            1,
            "the status line, the header block and the body belong to one segment; "
            f"the handler wrote {len(counting.writes)} times",
        )

    def test_the_single_write_carries_the_whole_response(self) -> None:
        counting = _serve(
            b"GET /health HTTP/1.1\r\nHost: 127.0.0.1:8765\r\nConnection: close\r\n\r\n",
            Response(200, [("Content-Type", "application/json")], b'{"ok":true}'),
        )
        written = counting.writes[0]
        self.assertTrue(written.startswith(b"HTTP/1.1 200 "), written[:40])
        self.assertIn(b"Content-Type: application/json", written)
        self.assertTrue(written.endswith(b'{"ok":true}'), written[-40:])

    def test_a_head_request_sends_the_headers_without_the_body(self) -> None:
        counting = _serve(
            b"HEAD /health HTTP/1.1\r\nHost: 127.0.0.1:8765\r\nConnection: close\r\n\r\n",
            Response(200, [("Content-Type", "application/json")], b'{"ok":true}'),
        )
        written = counting.writes[0]
        self.assertIn(b"Content-Length: 11", written)
        self.assertFalse(written.endswith(b'{"ok":true}'))

    def test_each_streamed_frame_is_one_write(self) -> None:
        def frames():
            yield b"data: one\n\n"
            yield b"data: two\n\n"

        counting = _serve(
            b"GET /stream HTTP/1.1\r\nHost: 127.0.0.1:8765\r\nConnection: close\r\n\r\n",
            StreamResponse(frames(), status=200, headers=[("Content-Type", "text/event-stream")]),
        )
        # The header block, one write per frame, and the terminating chunk.
        self.assertEqual(len(counting.writes), 4)
        self.assertEqual(counting.writes[1], b"b\r\ndata: one\n\n\r\n")
        self.assertEqual(counting.writes[2], b"b\r\ndata: two\n\n\r\n")
        self.assertEqual(counting.writes[3], b"0\r\n\r\n")


if __name__ == "__main__":
    unittest.main()
