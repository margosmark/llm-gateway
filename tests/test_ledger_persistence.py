"""Durable request ledger tests; no providers or network are used."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from gateway.observability import RequestEvent, RequestLedger


class DurableLedgerTests(unittest.TestCase):
    def test_record_survives_ledger_restart_and_is_metadata_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "request-ledger.jsonl"
            ledger = RequestLedger(capacity=10, persistence_path=path)
            ledger.record(RequestEvent("req-1", "2026-10-21T00:00:00Z", "coding", status=200))
            self.assertTrue(ledger.flush())
            ledger.close()
            stored = path.read_text()
            self.assertNotIn("prompt", stored)
            self.assertNotIn("messages", stored)
            self.assertNotIn("content", stored)
            restarted = RequestLedger(capacity=10, persistence_path=path)
            self.assertEqual(restarted.recent()[0]["request_id"], "req-1")
            self.assertEqual(restarted.totals()["requests"], 1)
            restarted.close()

    def test_amendment_is_metadata_only_and_evicted_records_are_safe(self) -> None:
        ledger = RequestLedger(capacity=1)
        ledger.record(RequestEvent("first", "t", "a", status=200))
        self.assertTrue(ledger.amend("first", status=499, error_class="client_abort", token_counters={"input": 12}))
        self.assertEqual(ledger.recent()[0]["status"], 499)
        with self.assertRaises(ValueError):
            ledger.amend("first", prompt="must not be accepted")
        ledger.record(RequestEvent("second", "t", "b", status=200))
        self.assertFalse(ledger.amend("first", status=500))
        self.assertFalse(ledger.amend("unknown", status=500))
        ledger.close()

    def test_durable_row_whitelist_refuses_content_keys(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.jsonl"
            ledger = RequestLedger(persistence_path=path)
            event = RequestEvent("req", "2026-10-21T00:00:00Z", "a", status=200)
            row = event.to_dict()
            row["prompt"] = "private content"
            ledger._persistence.submit(row)
            self.assertTrue(ledger.flush())
            ledger.close()
            decoded = json.loads(path.read_text().splitlines()[0])
            self.assertNotIn("prompt", decoded)

    def test_status_declares_retention_and_durability(self) -> None:
        ledger = RequestLedger()
        self.assertEqual(ledger.durability(), {"enabled": False, "retention_days": 30, "billing_system": False})
        ledger.close()


if __name__ == "__main__":
    unittest.main()
