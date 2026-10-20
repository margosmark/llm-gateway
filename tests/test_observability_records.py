"""Every request outcome is recorded, with a real timestamp and latency.

Regression cover for **R5**: the request ledger recorded only the
successful response path, with an empty timestamp and a `None` latency, so
the Status totals could not support the error, rate-limit and exhaustion
counts they appeared to offer. A provider failure raised before the ledger
was written and left no trace at all.

The record is metadata only. It never holds the request or the response.
"""

from __future__ import annotations

import json
import unittest

from gateway.adapters.base import AdapterError
from tests.test_end_to_end import FAKE_PROVIDER, FakeAdapter, GatewayHarness


class OutcomeRecordingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = GatewayHarness()
        self.addCleanup(self.harness.close)
        with self.harness.config.mutate() as config:
            config.aliases["coding"] = _alias()

    def _send(self, body=None):
        return self.harness.request(
            "POST",
            "/v1/chat/completions",
            body=body
            or {"model": "coding", "messages": [{"role": "user", "content": "hello"}]},
        )

    def test_a_successful_request_is_recorded_with_a_timestamp_and_latency(self) -> None:
        self._send()
        events = self.harness.router._ledger.recent()
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertTrue(event["timestamp"], "the timestamp must not be empty")
        self.assertNotEqual(event["timestamp"], "")
        self.assertIsNotNone(event["latency_ms"])
        self.assertGreaterEqual(event["latency_ms"], 0)
        self.assertEqual(event["error_class"], None)
        self.assertEqual(event["serving_provider"], FAKE_PROVIDER)

    def test_a_provider_failure_is_recorded(self) -> None:
        failing = FakeAdapter(fail_with=AdapterError("rate_limited", provider_id=FAKE_PROVIDER))
        self.harness.registry.register(failing)
        status, _, _ = self._send()
        self.assertEqual(status, 502)

        events = self.harness.router._ledger.recent()
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["error_class"], "rate_limited")
        self.assertIsNotNone(event["latency_ms"])
        self.assertTrue(event["timestamp"])

    def test_a_failure_updates_the_error_counters(self) -> None:
        failing = FakeAdapter(fail_with=AdapterError("exhausted", provider_id=FAKE_PROVIDER))
        self.harness.registry.register(failing)
        self._send()
        totals = self.harness.router._ledger.totals()
        self.assertEqual(totals["requests"], 1)
        self.assertEqual(totals["errors"], 1)
        self.assertEqual(totals["exhausted"], 1)

    def test_the_record_never_carries_request_or_response_content(self) -> None:
        self._send(
            {
                "model": "coding",
                "messages": [{"role": "user", "content": "a very secret prompt"}],
            }
        )
        events = self.harness.router._ledger.recent()
        serialised = json.dumps(events)
        self.assertNotIn("very secret prompt", serialised)
        for key in ("prompt", "messages", "content", "body", "completion"):
            self.assertNotIn(key, events[0])

    def test_the_status_view_reports_the_failure(self) -> None:
        failing = FakeAdapter(fail_with=AdapterError("transient", provider_id=FAKE_PROVIDER))
        self.harness.registry.register(failing)
        self._send()
        totals = self.harness.router._ledger.totals()
        self.assertEqual(totals["errors"], 1)


def _alias():
    from gateway.config import Alias, RouteRef

    return Alias(
        name="coding",
        enabled=True,
        primary=RouteRef(FAKE_PROVIDER, "gpt-6-luna"),
    )


if __name__ == "__main__":
    unittest.main()
