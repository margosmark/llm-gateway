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


if __name__ == "__main__":
    unittest.main()
