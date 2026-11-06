"""Durable request ledger tests; no providers or network are used."""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from gateway.observability import RequestEvent, RequestLedger


def stamp(days_ago: float) -> str:
    """A ledger timestamp ``days_ago`` days before the wall clock.

    The ledger prunes rows older than its 30-day retention window against
    the real clock, so fixture rows must be seeded relative to it: a fixed
    calendar date eventually ages out of its own fixture (the 2026-11-20
    bomb wave, where the seeded row was pruned before the restart read it).
    """
    return (datetime.now(UTC) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


class DurableLedgerTests(unittest.TestCase):
    def test_record_survives_ledger_restart_and_is_metadata_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "request-ledger.jsonl"
            ledger = RequestLedger(capacity=10, persistence_path=path)
            ledger.record(RequestEvent("req-1", stamp(days_ago=1), "coding", status=200))
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
            event = RequestEvent("req", stamp(days_ago=1), "a", status=200)
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
            expired = {"request_id": "old", "timestamp": stamp(days_ago=31), "requested_alias": "a"}
            fresh = {"request_id": "new", "timestamp": stamp(days_ago=1), "requested_alias": "a"}
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
                "request_id": "legacy", "timestamp": stamp(days_ago=1),
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


class InvalidUtf8JournalTests(unittest.TestCase):
    """F7: invalid UTF-8 bytes in a journal file never take a boot down.

    ``load()`` is the one journal reader that used to catch only
    ``FileNotFoundError``: a ``UnicodeDecodeError`` from its line iterator
    escaped ``restore()`` straight into ``RequestLedger.__init__`` and the
    gateway failed to start (reproduced on a944023 and 3fc4b3a). Its two
    sibling readers - ``rebuild()`` and ``_import_journal()`` - already
    treat an unreadable file as a partial-coverage gap; this pins the same
    contract for ``load()``, including the no-rewrite rule: rewriting a
    file that could not be read to the end would silently truncate every
    line after the bad byte.
    """

    #: The gate's exact repro bytes: a rotation file that is not UTF-8.
    BAD_BYTES = b"\xff\xfe not utf-8\n"

    @staticmethod
    def _good_line(request_id: str) -> bytes:
        row = {"request_id": request_id, "timestamp": stamp(days_ago=1),
               "requested_alias": "a", "status": 200}
        return (json.dumps(row) + "\n").encode()

    def test_first_boot_survives_invalid_utf8_rotation_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "request-ledger.jsonl"
            # Readable content in the active file keeps importing...
            path.write_bytes(self._good_line("ok"))
            rotation = Path(directory) / "request-ledger.jsonl.1"
            # ...while a rotation carrying a readable line, the bad byte
            # run and another readable line is skipped whole: text mode
            # decodes the buffered chunk before it yields any line, so
            # nothing from that file can be trusted past the bad byte.
            # Pre-fix: UnicodeDecodeError out of load() -> restore() ->
            # RequestLedger.__init__ (gateway failed to start).
            original = self._good_line("tail") + self.BAD_BYTES + self._good_line("tail2")
            rotation.write_bytes(original)
            ledger = RequestLedger(persistence_path=path)
            try:
                payload = ledger.rollup()
                self.assertIsNotNone(payload)
                assert payload is not None
                coverage = payload["coverage"]
                # The unreadable file is reported as a gap, not counted
                # as a complete read.
                self.assertEqual(coverage["rebuild"]["status"], "partial")
                self.assertTrue(coverage["incomplete"])
                self.assertEqual(ledger.totals()["requests"], 1)
            finally:
                ledger.close()
            # load() must not rewrite a file it could not read to the
            # end: _rewrite would replace it with only the lines read
            # before the failure - here, silently nothing - and drop the
            # tail after the bad byte.
            self.assertEqual(rotation.read_bytes(), original)

    def test_restart_with_a_corrupt_rotation_boots_and_marks_coverage_partial(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "request-ledger.jsonl"
            path.write_text(self._good_line("kept").decode())
            ledger = RequestLedger(persistence_path=path)
            self.assertTrue(ledger.flush())
            ledger.close()
            # The store and sidecar now describe a complete history;
            # corrupt a rotation file afterwards (hand-crafted line or
            # media damage). This is the path where only load() reads the
            # journal: the sidecar load path closes coverage as "complete"
            # before restore() runs, so load() itself has to report the gap.
            rotation = Path(directory) / "request-ledger.jsonl.1"
            rotation.write_bytes(self.BAD_BYTES)
            restarted = RequestLedger(persistence_path=path)
            try:
                payload = restarted.rollup()
                self.assertIsNotNone(payload)
                assert payload is not None
                coverage = payload["coverage"]
                # Pre-fix this boot raised UnicodeDecodeError; catching
                # without note_source(partial=True) would leave coverage
                # claiming "complete" over a file it never finished reading.
                self.assertEqual(coverage["rebuild"]["status"], "partial")
                self.assertTrue(coverage["incomplete"])
                self.assertEqual(restarted.totals()["requests"], 1)
            finally:
                restarted.close()
            self.assertEqual(rotation.read_bytes(), self.BAD_BYTES)


if __name__ == "__main__":
    unittest.main()
