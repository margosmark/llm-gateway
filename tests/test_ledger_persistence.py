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

    def test_restart_restores_eviction_coverage_and_all_retained_totals(self) -> None:
        from gateway.costs import aggregate_usage

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "request-ledger.jsonl"
            ledger = RequestLedger(capacity=3, persistence_path=path)
            for index in range(5):
                ledger.record(RequestEvent(
                    f"req-{index}", "2099-01-01T00:00:00Z", "coding",
                    serving_provider="opencode_go", serving_model="claude-haiku-5-5",
                    status=200, token_counters={"prompt_tokens": 100, "completion_tokens": 20, "cached_read_tokens": 0},
                ))
            self.assertTrue(ledger.flush())
            self.assertEqual(ledger.totals()["requests"], 5)
            ledger.close()

            restarted = RequestLedger(capacity=3, persistence_path=path)
            self.assertEqual(restarted.totals()["requests"], 5)
            costs = aggregate_usage(restarted.recent(limit=100), evicted_events=restarted.evicted)
            # The ring holds 3 of the 5 rows, so the window is incomplete -
            # that flag is what keeps the partial view honest. The published
            # price still applies to the rows the window does count, exactly
            # as it does to the token columns in the same payload.
            self.assertTrue(costs["coverage"]["incomplete"])
            self.assertTrue(costs["alias"])
            self.assertTrue(all(row["amount_usd"] is not None for group in (costs["alias"], costs["provider"], costs["model"]) for row in group.values()))
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

    def test_load_physically_prunes_expired_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.jsonl"
            expired = {"request_id": "old", "timestamp": "2026-09-01T00:00:00Z", "requested_alias": "a"}
            fresh = {"request_id": "new", "timestamp": "2026-10-22T00:00:00Z", "requested_alias": "a"}
            path.write_text(json.dumps(expired) + "\n" + json.dumps(fresh) + "\n")
            ledger = RequestLedger(persistence_path=path)
            self.assertEqual([event["request_id"] for event in ledger.recent()], ["new"])
            self.assertNotIn("old", path.read_text())
            self.assertIn("new", path.read_text())
            ledger.close()

    def test_load_redacts_legacy_attempt_reasons(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.jsonl"
            secret = "sk-" + "A" * 28
            row = {
                "request_id": "legacy", "timestamp": "2026-10-22T00:00:00Z",
                "requested_alias": "a", "error_class": "auth",
                "attempts": [{"provider": "p", "outcome": "error", "error_class": "auth", "reason": secret}],
            }
            path.write_text(json.dumps(row) + "\n")
            ledger = RequestLedger(persistence_path=path)
            self.assertNotIn(secret, path.read_text())
            self.assertNotIn(secret, json.dumps(ledger.recent()))
            ledger.close()

    def test_status_declares_retention_and_durability(self) -> None:
        ledger = RequestLedger()
        self.assertEqual(ledger.durability(), {"enabled": False, "retention_days": 30, "billing_system": False})
        ledger.close()


if __name__ == "__main__":
    unittest.main()
