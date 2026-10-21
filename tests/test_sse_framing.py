"""The gateway must emit well-formed SSE.

Regression cover: the upstream reader consumed the blank line that
terminates an SSE frame and did not include it in the frame it yielded, so
every **passthroughed** stream reached the client as single-newline
terminated lines with no frame separator at all.

A lenient reader still sees the lines. A client that parses SSE strictly
reads the whole thing as one event, finds no ``finish_reason``, and reports
"provider returned an empty stream with no finish_reason".

The frames must be separated by a blank line, and the stream must end with
the terminator the provider sent.
"""

from __future__ import annotations

import io
import json
import unittest

from gateway.adapters.http_client import UpstreamClient


class FakeResponse:
    """A minimal stand-in for an http.client response."""

    def __init__(self, payload: bytes) -> None:
        self._stream = io.BytesIO(payload)
        self.closed = False

    def readline(self) -> bytes:
        return self._stream.readline()

    def close(self) -> None:
        self.closed = True


UPSTREAM_STREAM = (
    b'data: {"id":"c1","choices":[{"index":0,"delta":{"content":"He"},"finish_reason":null}]}\n\n'
    b'data: {"id":"c1","choices":[{"index":0,"delta":{"content":"llo"},"finish_reason":null}]}\n\n'
    b'data: {"id":"c1","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
    b"data: [DONE]\n\n"
)


def parse_sse(payload: bytes) -> list[dict]:
    """Parse SSE the way a strict client does: split on blank lines."""
    events: list[dict] = []
    for frame in payload.split(b"\n\n"):
        text = frame.strip()
        if not text:
            continue
        for line in text.split(b"\n"):
            if not line.startswith(b"data:"):
                continue
            data = line[5:].strip()
            if data == b"[DONE]":
                events.append({"done": True})
                continue
            try:
                events.append(json.loads(data))
            except ValueError:
                events.append({"unparseable": data.decode("utf-8", "replace")})
    return events


class SseFramingTests(unittest.TestCase):
    def _frames(self, payload: bytes = UPSTREAM_STREAM) -> list[bytes]:
        client = UpstreamClient()
        return list(client._iter_sse(FakeResponse(payload)))

    def test_each_frame_is_terminated_by_a_blank_line(self) -> None:
        for frame in self._frames():
            self.assertTrue(
                frame.endswith(b"\n\n"),
                f"a frame must end with a blank line, got {frame!r}",
            )

    def test_the_frames_separate_a_strict_parser(self) -> None:
        client = UpstreamClient()
        raw = b"".join(client._iter_sse(FakeResponse(UPSTREAM_STREAM)))
        events = parse_sse(raw)
        self.assertEqual(len(events), 4, f"expected four events, got {len(events)}")
        self.assertNotIn("unparseable", events[0])
        self.assertEqual(events[0]["choices"][0]["delta"]["content"], "He")
        self.assertEqual(events[2]["choices"][0]["finish_reason"], "stop")
        self.assertTrue(events[3].get("done"))

    def test_the_terminator_is_present_in_the_raw_bytes(self) -> None:
        client = UpstreamClient()
        raw = b"".join(client._iter_sse(FakeResponse(UPSTREAM_STREAM)))
        self.assertEqual(raw.count(b"\n\n"), 4, "every frame needs its own separator")

    def test_a_stream_without_a_trailing_blank_line_is_still_terminated(self) -> None:
        client = UpstreamClient()
        raw = b"".join(client._iter_sse(FakeResponse(b'data: {"a":1}\n')))
        self.assertTrue(raw.endswith(b"\n\n"))
        self.assertEqual(len(parse_sse(raw)), 1)

    def test_a_partial_frame_at_the_end_is_still_terminated(self) -> None:
        client = UpstreamClient()
        raw = b"".join(
            client._iter_sse(FakeResponse(b'data: {"a":1}\n\ndata: {"b":2}'))
        )
        self.assertTrue(raw.endswith(b"\n\n"))
        self.assertEqual(len(parse_sse(raw)), 2)


if __name__ == "__main__":
    unittest.main()
