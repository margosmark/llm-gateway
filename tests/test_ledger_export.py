"""Metadata-only bounded ledger export regression tests."""

from __future__ import annotations

import csv
import io
import json
import unittest

from gateway.ledger_export import export_records, render_csv, render_json
from gateway.observability import RequestEvent
from tests.test_end_to_end import GatewayHarness


class LedgerExportUnitTests(unittest.TestCase):
    def test_export_allowlists_console_columns_and_redacts_values(self) -> None:
        secret = "sk-" + "A" * 20
        records = export_records([{
            "timestamp": "2026-01-02T03:04:05Z",
            "requested_alias": "coding",
            "serving_provider": "provider",
            "serving_model": "model",
            "status": 200,
            "error_class": None,
            "fallback_used": False,
            "degradations": [{"reason": secret}],
            "request_id": "internal-only",
            "token_counters": {"secret": 999},
            "prompt": "never include this",
        }])
        self.assertEqual(set(records[0]), {"timestamp", "alias", "serving_provider", "serving_model", "outcome", "note"})
        serialized = render_json(records).decode()
        self.assertNotIn(secret, serialized)
        self.assertIn("[redacted]", serialized)
        self.assertNotIn("internal-only", serialized)
        self.assertNotIn("token_counters", serialized)
        self.assertNotIn("never include this", serialized)

    def test_csv_encodes_console_columns_with_quoting(self) -> None:
        payload = render_csv(export_records([{
            "timestamp": "now", "requested_alias": "alias,one", "serving_provider": "p",
            "serving_model": "m", "status": 200, "fallback_used": False,
            "degradations": [{"reason": "reason;with, punctuation"}],
        }])).decode()
        rows = list(csv.DictReader(io.StringIO(payload)))
        self.assertEqual(list(rows[0]), ["timestamp", "alias", "serving_provider", "serving_model", "outcome", "note"])
        self.assertEqual(rows[0]["alias"], "alias,one")
        self.assertEqual(rows[0]["outcome"], "status 200")


class LedgerExportEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = GatewayHarness()
        self.addCleanup(self.harness.close)

    def _add(self, count: int) -> None:
        for index in range(count):
            self.harness.router._ledger.record(RequestEvent(
                request_id=f"request-{index}", timestamp=f"time-{index}", requested_alias="coding",
                serving_provider="fake", serving_model="model", status=200,
            ))

    def test_json_export_is_bounded_read_only_and_downloadable(self) -> None:
        self._add(7)
        before = self.harness.router._ledger.totals()
        status, payload, headers = self.harness.request("GET", "/owner/api/ledger/export?format=json&limit=3")
        self.assertEqual(status, 200)
        self.assertEqual(len(payload), 3)
        self.assertEqual(payload[0]["timestamp"], "time-6")
        self.assertEqual(headers["Content-Type"], "application/json; charset=utf-8")
        self.assertIn('filename="ledger.json"', headers["Content-Disposition"])
        self.assertEqual(self.harness.router._ledger.totals(), before)
        self.assertEqual(len(self.harness.router._ledger.recent()), 7)

    def test_csv_export_and_empty_ledger(self) -> None:
        self._add(1)
        status, raw, headers = self.harness.request("GET", "/owner/api/ledger/export", raw=True)
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/csv; charset=utf-8")
        self.assertIn(b"timestamp,alias,serving_provider,serving_model,outcome,note", raw)
        status, raw, _ = self.harness.request("GET", "/owner/api/ledger/export?format=json", raw=True)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw), [
            {"timestamp": "time-0", "alias": "coding", "serving_provider": "fake", "serving_model": "model", "outcome": "status 200", "note": ""}
        ])

    def test_invalid_format_and_row_limits_are_refused(self) -> None:
        self._add(1)
        for query in ("format=xml", "limit=nope", "limit=0", "limit=1001"):
            with self.subTest(query=query):
                status, payload, _ = self.harness.request("GET", f"/owner/api/ledger/export?{query}")
                self.assertEqual(status, 400)
                self.assertIn("invalid_export_", payload["error"]["type"])
        self.assertEqual(self.harness.router._ledger.totals()["requests"], 1)


if __name__ == "__main__":
    unittest.main()
