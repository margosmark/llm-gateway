"""Boot sidecar/store parity: the reconcile and the shared age clock (t_240d5e8d).

Two deterministic reds that the forced-delay probe of kanban t_b08745dd
could only show under load (a boot window stretched past the 16s row
stride), so neither test here sleeps:

1. A loaded sidecar still counts rows the store no longer holds - they
   aged out on the retention window's *boundary* day, whose bucket the
   day bound cannot drop yet - so ``coverage.requests`` over-counts the
   store until the whole day leaves. The test removes the oldest row
   between two boots the way an age-prune would and requires the second
   boot's sidecar to match the store anyway; its prune count is zero, so
   only the boundary-day reconcile can make that hold.
2. The full rebuild aged rows against a clock of its own, evaluated
   later than the boot prune's, so a row the store kept could be dropped
   from the sidecar (the mirrored under-count). The test pins rows on
   and around the cutoff both decisions must share and requires the
   rebuild to keep exactly what the prune kept.
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from gateway.ledger_persistence import RETENTION_SECONDS, store_path_for
from gateway.ledger_store import LedgerStore
from gateway.observability import RequestLedger
from tests.test_ledger_store import _fill, _row, _stamp


def _count(path: Path) -> int:
    connection = sqlite3.connect(store_path_for(path), timeout=30)
    try:
        return int(connection.execute("SELECT COUNT(*) FROM ledger").fetchone()[0])
    finally:
        connection.close()


def _oldest_ts(path: Path) -> int:
    connection = sqlite3.connect(store_path_for(path), timeout=30)
    try:
        return int(connection.execute("SELECT MIN(ts) FROM ledger").fetchone()[0])
    finally:
        connection.close()


class BoundaryDayReconcileTests(unittest.TestCase):
    def test_a_loaded_boot_matches_the_store_after_rows_leave_between_boots(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "request-ledger.jsonl"
            store = LedgerStore(store_path_for(path))
            store.open()
            try:
                # 3000 rows spread over the full window: row 0 sits on the
                # retention window's boundary day, stride ~864s, so pruning
                # just past the oldest row removes exactly one.
                _fill(store, 3_000)
            finally:
                store.close()

            first = RequestLedger(persistence_path=path)
            try:
                first_payload = json.loads(
                    path.with_name("request-ledger-rollup.json").read_text())
                # The same-clock half: boot 1's rebuild must keep exactly
                # what boot 1's prune kept, at the exact boundary row.
                self.assertEqual(first_payload["coverage"]["requests"],
                                 first.totals()["requests"])
                rows_expired_before = first_payload["coverage"]["rows_expired"]
            finally:
                first.close()
            rows_before = _count(path)

            # The rows leave between boots the way the runtime age-prune
            # leaves them - no boot prune will ever see them, so the
            # expiry fact cannot be what heals the next boot's parity.
            store = LedgerStore(store_path_for(path))
            store.open()
            try:
                self.assertEqual(store.prune(_oldest_ts(path) + 1), 1)
            finally:
                store.close()
            self.assertEqual(_count(path), rows_before - 1)

            second = RequestLedger(persistence_path=path)
            try:
                payload = json.loads(
                    path.with_name("request-ledger-rollup.json").read_text())
                coverage = payload["coverage"]
                # The load path really ran (a full rebuild would make the
                # parity assertion below pass for the wrong reason).
                self.assertEqual(coverage["rebuild"]["mode"], "incremental")
                totals = second.totals()
                self.assertEqual(totals["requests"], rows_before - 1)
                self.assertEqual(coverage["requests"], totals["requests"])
                # The reconcile mirrors the store; it never invents an
                # expiry the prune did not report.
                self.assertEqual(coverage["rows_expired"], rows_expired_before)
                self.assertFalse(coverage["incomplete"])
            finally:
                second.close()


class SharedCutoffTests(unittest.TestCase):
    def test_full_rebuild_keeps_exactly_the_rows_the_boot_prune_kept(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "request-ledger.jsonl"
            # Rows on and around the cutoff. The boot's own cutoff lands
            # within a second of this one, and every integer in between is
            # covered: whichever second the boot starts in, some row sits
            # exactly on the shared boundary - a rebuild with its own
            # (later) clock drops that row while the store keeps it.
            cutoff = int(time.time() - RETENTION_SECONDS)
            store = LedgerStore(store_path_for(path))
            store.open()
            try:
                items = []
                for request_id, epoch in (
                    ("gone", cutoff - 1),
                    ("edge0", cutoff),
                    ("edge1", cutoff + 1),
                    ("edge2", cutoff + 2),
                    ("edge3", cutoff + 3),
                    ("fresh", cutoff + 60),
                ):
                    row = _row(request_id, epoch)
                    items.append((request_id, epoch, _stamp(epoch), None, 0,
                                  json.dumps(row, separators=(",", ":"),
                                             ensure_ascii=True)))
                store.insert_many(items)
                # No pre-boot prune here: the boot re-runs the age rule
                # against its own cutoff, and that prune - not this test's
                # - is the one whose count coverage must report.
            finally:
                store.close()

            ledger = RequestLedger(persistence_path=path)
            try:
                payload = json.loads(
                    path.with_name("request-ledger-rollup.json").read_text())
                coverage = payload["coverage"]
                self.assertEqual(coverage["rebuild"]["mode"], "full")
                totals = ledger.totals()
                self.assertEqual(coverage["requests"], totals["requests"])
                # Every row the store lost is reported as expired exactly
                # once - by the prune that removed it, never re-counted.
                self.assertEqual(coverage["rows_expired"],
                                 len(items) - totals["requests"])
                self.assertFalse(coverage["incomplete"])
            finally:
                ledger.close()


if __name__ == "__main__":
    unittest.main()
