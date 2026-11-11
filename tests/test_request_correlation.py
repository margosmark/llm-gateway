"""Correlation across ingress, streaming outcome and ledger (t_f9e5c2a9).

The gateway already honours a caller's ``X-Request-Id`` (T27): it is
validated at ingress and recorded as ``client_request_id`` beside the
gateway's own authoritative ``request_id``. Three gaps remained, and
each one is what made an incident on an anonymous LAN un-attributable:

1. **The access line carried neither id.** The line an operator greps
   first could not be joined to the ledger row it described, nor to the
   caller's own records - the gap reproduced here by
   ``test_access_line_joins_the_ledger_row_by_both_ids``.
2. **A request refused before routing had no client id on its row.**
   A body refused at the parse choke wrote a ledger row with no way to
   tie it to the caller who sent it.
3. **A streamed request read as a success the moment the headers went
   out.** "HTTP accepted" and "the answer arrived" are different facts;
   only the ledger's later amendment separated them, and nothing said so
   in the log.

The rules the tests hold: a missing id stays legal (old clients keep
working), an invalid id is refused and never echoed, no id can ever
select or amend another request's record, and no line carries a prompt,
a completion or a credential.
"""

from __future__ import annotations

import io
import json
import socket
import struct
import time
import unittest

from gateway.adapters.base import ProviderResponse
from gateway.config import Alias, RouteRef
from gateway.logging import RequestLogger
from gateway.observability import RequestLedger
from tests.test_end_to_end import FAKE_PROVIDER, FakeAdapter, GatewayHarness
from tests.test_streaming_only_upstream import responses_stream

#: A body with a lone surrogate: refused at the parse choke, *before*
#: the router runs, so it exercises the pre-routing error path.
SURROGATE_BODY = b'{"content": "msg\\ud800here"}'

STREAM_BODY = {"model": "correlate", "input": "hi", "stream": True}


def _records(log: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in log.getvalue().splitlines() if line.strip()]


def _by_event(records: list[dict], event: str | None) -> list[dict]:
    return [r for r in records if (r.get("event") or None) == event]


class CorrelationHarnessCase(unittest.TestCase):
    """A harness with the access log wired to a buffer, one alias."""

    def build(self, adapter: FakeAdapter | None = None) -> tuple[GatewayHarness, io.StringIO]:
        harness = GatewayHarness(adapter=adapter)
        self.addCleanup(harness.close)
        log = io.StringIO()
        harness.app.logger = RequestLogger(stream=log)
        with harness.config.mutate() as config:
            config.aliases["correlate"] = Alias(
                name="correlate",
                enabled=True,
                primary=RouteRef(FAKE_PROVIDER, "gpt-6-luna"),
            )
        return harness, log

    @staticmethod
    def ledger(harness: GatewayHarness) -> RequestLedger:
        ledger = harness.router._ledger
        assert isinstance(ledger, RequestLedger)
        return ledger


class AccessLogCorrelationTests(CorrelationHarnessCase):
    def test_access_line_joins_the_ledger_row_by_both_ids(self) -> None:
        harness, log = self.build()
        status, _, _ = harness.request(
            "POST",
            "/v1/chat/completions",
            body={"model": "correlate", "messages": [{"role": "user", "content": "hi"}]},
            headers={"X-Request-Id": "incident-2026-11-10"},
        )
        self.assertEqual(status, 200)
        event = self.ledger(harness).recent(limit=1)[0]
        records = _records(log)
        self.assertEqual(len(records), 1, records)
        line = records[0]
        self.assertEqual(line["request_id"], event["request_id"])
        self.assertEqual(line["client_request_id"], "incident-2026-11-10")
        self.assertEqual(line["client_request_id"], event["client_request_id"])
        self.assertEqual(line["status"], 200)

    def test_access_line_without_a_client_id_still_names_the_gateway_id(self) -> None:
        """Backward compatibility: a client that sends nothing keeps working."""
        harness, log = self.build()
        status, _, _ = harness.request(
            "POST",
            "/v1/chat/completions",
            body={"model": "correlate", "messages": [{"role": "user", "content": "hi"}]},
        )
        self.assertEqual(status, 200)
        event = self.ledger(harness).recent(limit=1)[0]
        self.assertIsNone(event["client_request_id"])
        records = _records(log)
        self.assertEqual(len(records), 1, records)
        self.assertEqual(records[0]["request_id"], event["request_id"])
        self.assertNotIn("client_request_id", records[0])

    def test_a_refused_client_id_is_neither_echoed_nor_logged(self) -> None:
        harness, log = self.build()
        status, payload, _ = harness.request(
            "POST",
            "/v1/chat/completions",
            body={"model": "correlate", "messages": [{"role": "user", "content": "hi"}]},
            headers={"X-Request-Id": "bad/id with space"},
        )
        self.assertEqual(status, 400)
        self.assertNotIn("bad/id", str(payload))
        self.assertEqual(self.ledger(harness).recent(limit=10), [])
        text = log.getvalue()
        self.assertNotIn("bad/id with space", text)
        for record in _records(log):
            self.assertNotIn("client_request_id", record)

    def test_an_oversized_client_id_is_refused_and_not_recorded(self) -> None:
        harness, log = self.build()
        oversized = "a" * 129
        status, _, _ = harness.request(
            "POST",
            "/v1/chat/completions",
            body={"model": "correlate", "messages": [{"role": "user", "content": "hi"}]},
            headers={"X-Request-Id": oversized},
        )
        self.assertEqual(status, 400)
        self.assertNotIn(oversized, log.getvalue())
        self.assertEqual(self.ledger(harness).recent(limit=10), [])


class PreRoutingErrorPathTests(CorrelationHarnessCase):
    def test_a_body_refused_before_routing_keeps_the_client_id(self) -> None:
        """The parse-choke refusal is attributable to the caller too.

        The row is written by the dispatch boundary, not the router, so
        it used to carry no client id at all: an operator could see the
        refusal but not who sent the body that caused it.
        """
        harness, log = self.build()
        status, payload, _ = harness.request(
            "POST",
            "/v1/chat/completions",
            body=SURROGATE_BODY,
            headers={"X-Request-Id": "refused-7"},
        )
        self.assertEqual(status, 400)
        assert isinstance(payload, dict)
        self.assertEqual(payload["error"]["type"], "invalid_request")
        rows = self.ledger(harness).recent(limit=10)
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0]["client_request_id"], "refused-7")
        self.assertEqual(rows[0]["status"], 400)
        lines = _records(log)
        self.assertEqual(len(lines), 1, lines)
        self.assertEqual(lines[0]["request_id"], rows[0]["request_id"])
        self.assertEqual(lines[0]["client_request_id"], "refused-7")
        # Metadata only: the refusal excerpt never reaches the log line.
        self.assertNotIn("ud800", json.dumps(lines))


class StreamingTerminalOutcomeTests(CorrelationHarnessCase):
    def test_a_completed_stream_states_complete_and_keeps_both_ids(self) -> None:
        harness, log = self.build()
        harness.adapter.stream_frames = responses_stream("ok")
        status, body, _ = harness.request(
            "POST",
            "/v1/responses",
            body=STREAM_BODY,
            headers={"X-Request-Id": "stream-ok"},
            raw=True,
        )
        self.assertEqual(status, 200)
        assert isinstance(body, bytes)
        self.assertIn(b"data:", body)

        ingress = _by_event(_records(log), None)
        ends = _by_event(_records(log), "stream_end")
        self.assertEqual(len(ingress), 1, ingress)
        self.assertEqual(len(ends), 1, ends)
        self.assertEqual(ingress[0]["client_request_id"], "stream-ok")
        self.assertEqual(ends[0]["outcome"], "complete")
        self.assertEqual(ends[0]["request_id"], ingress[0]["request_id"])
        self.assertEqual(ends[0]["client_request_id"], "stream-ok")
        self.assertNotIn("error_class", ends[0])

        row = self.ledger(harness).recent(limit=1)[0]
        self.assertEqual(row["request_id"], ingress[0]["request_id"])
        self.assertEqual(row["client_request_id"], "stream-ok")
        self.assertEqual(row["status"], 200)
        self.assertIsNone(row["error_class"])
        self.assertTrue(row["streamed"])

    def test_a_client_that_hangs_up_mid_stream_is_recorded_as_interrupted(self) -> None:
        """HTTP accepted is not an answer: the hang-up is its own outcome.

        The adapter holds the first frame back, so the caller really does
        leave mid-stream - the gateway writes, the socket is gone, and
        the ledger row that said 200 must end up ``client_abort`` with
        the caller's id still on it.
        """
        first = responses_stream("first")
        later = responses_stream("second")

        class _HeldBackAdapter(FakeAdapter):
            def invoke(self, request):  # a ProviderRequest; see FakeAdapter
                self.calls.append(request)

                def frames():
                    yield first[0]
                    time.sleep(0.5)
                    yield from later

                return ProviderResponse(
                    status=200,
                    headers={},
                    body=None,
                    stream=frames(),
                    content_type="text/event-stream",
                    dialect=self.dialect,
                )

        harness, log = self.build(adapter=_HeldBackAdapter())
        payload = json.dumps(STREAM_BODY).encode("utf-8")
        head = (
            f"POST /v1/responses HTTP/1.1\r\n"
            f"Host: {harness.authority}:{harness.port}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(payload)}\r\n"
            f"X-Request-Id: stream-hang\r\n\r\n"
        ).encode()
        sock = socket.create_connection((harness.host, harness.port), timeout=10)
        try:
            sock.sendall(head + payload)
            buffer = b""
            deadline = time.monotonic() + 10
            while b"data:" not in buffer and time.monotonic() < deadline:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buffer += chunk
            self.assertIn(b"data:", buffer)
            # SO_LINGER 0 turns the close into a reset, so the gateway's
            # next write fails instead of blocking into a dead peer.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        finally:
            sock.close()

        ledger = self.ledger(harness)
        deadline = time.monotonic() + 10
        row: dict | None = None
        while time.monotonic() < deadline:
            rows = ledger.recent(limit=5)
            if rows and rows[0].get("error_class") == "client_abort":
                row = rows[0]
                break
            time.sleep(0.05)
        self.assertIsNotNone(row, ledger.recent(limit=5))
        assert row is not None
        self.assertEqual(row["client_request_id"], "stream-hang")
        self.assertEqual(row["status"], 200)

        ends: list[dict] = []
        while time.monotonic() < deadline and not ends:
            ends = _by_event(_records(log), "stream_end")
            time.sleep(0.05)
        self.assertEqual(len(ends), 1, _records(log))
        self.assertEqual(ends[0]["outcome"], "interrupted")
        self.assertEqual(ends[0]["client_request_id"], "stream-hang")
        ingress = _by_event(_records(log), None)
        self.assertEqual(ingress[0]["request_id"], ends[0]["request_id"])





class ClientIdCannotAmendAnotherRowTests(CorrelationHarnessCase):
    def test_a_client_id_that_copies_a_gateway_id_is_only_a_label(self) -> None:
        """Spoofing a real gateway id changes nothing about that row.

        The gateway's own id is the only amend key. A caller that echoes
        a previously observed ``request_id`` back as its ``X-Request-Id``
        gets a label, not a handle.
        """
        harness, _ = self.build()
        status, _, _ = harness.request(
            "POST",
            "/v1/chat/completions",
            body={"model": "correlate", "messages": [{"role": "user", "content": "one"}]},
        )
        self.assertEqual(status, 200)
        victim = self.ledger(harness).recent(limit=1)[0]
        victim_id = victim["request_id"]

        status, _, _ = harness.request(
            "POST",
            "/v1/chat/completions",
            body={"model": "correlate", "messages": [{"role": "user", "content": "two"}]},
            headers={"X-Request-Id": victim_id},
        )
        self.assertEqual(status, 200)
        rows = self.ledger(harness).recent(limit=5)
        self.assertEqual(len(rows), 2, rows)
        second, first = rows[0], rows[1]
        self.assertEqual(first["request_id"], victim_id)
        self.assertIsNone(first["client_request_id"])
        self.assertEqual(second["client_request_id"], victim_id)
        self.assertNotEqual(second["request_id"], victim_id)
        # The victim row is untouched by the caller that copied its id.
        self.assertEqual(
            self.ledger(harness).recent(limit=5)[1]["request_id"],
            victim_id,
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
