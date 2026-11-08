from __future__ import annotations

import threading
import unittest

from gateway.http.keepalive import with_sse_keepalives


class KeepaliveTests(unittest.TestCase):
    def test_ping_is_emitted_while_source_is_idle_then_frames_continue(self) -> None:
        release = threading.Event()

        def source():
            release.wait(timeout=1)
            yield b"data: {\"ok\":true}\n\n"

        stream = with_sse_keepalives(iter(source()), interval=0.02)
        try:
            self.assertEqual(next(stream), b": ping\n\n")
            release.set()
            self.assertEqual(next(stream), b'data: {"ok":true}\n\n')
            with self.assertRaises(StopIteration):
                next(stream)
        finally:
            release.set()
            stream.close()

    def test_fast_source_gets_no_unneeded_ping(self) -> None:
        self.assertEqual(
            list(with_sse_keepalives(iter([b"data: [DONE]\n\n"]), interval=1)),
            [b"data: [DONE]\n\n"],
        )

    def test_nonpositive_interval_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            next(with_sse_keepalives(iter(()), interval=0))

    def test_a_source_failure_is_raised_on_the_consumer_thread(self) -> None:
        """A dead source must not be swallowed by the producer thread.

        The consumer is the response writer - the request thread - so the
        exception reaches ``socketserver.handle_error`` and the traceback
        still lands on stderr (t_7198bfa3), instead of surfacing only from
        the daemon thread's excepthook after the stream ended cleanly over
        a truncated answer.
        """

        def source():
            yield b"data: first\\n\\n"
            raise RuntimeError("source exploded")

        stream = with_sse_keepalives(source(), interval=1)
        try:
            self.assertEqual(next(stream), b"data: first\\n\\n")
            with self.assertRaisesRegex(RuntimeError, "source exploded"):
                next(stream)
        finally:
            stream.close()


if __name__ == "__main__":
    unittest.main()
