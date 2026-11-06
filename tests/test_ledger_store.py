"""Durable request-store tests: the published 30-day window made real.

The bug this file exists for: the JSONL design bounded history by size
(5 MiB active file x 2 rotations = 15 MiB total) while the console and
``docs/TODO.md`` T7 claimed 30-day retention - at the measured ~400 KiB/h
write rate that cap dropped the oldest history after ~1.3 days, so size
bound ~23x earlier than age. The SQLite store replaces it, and each test
here pins one commitment of that replacement:

* retention is by AGE only - a full measured-volume window survives, and
  nothing is dropped because a file grew (the old cap's 15 MiB is asserted
  as *exceeded* by the retained data, which is the proof);
* boot over a full history stays bounded and fast (load path, sub-2s, no
  whole-history materialisation - the R56 state bar);
* the schema stays metadata-only (six whitelisted columns, no content);
* the legacy journal migrates without losing a row and never re-imports;
* the status figures the owner reads (size / rows / oldest row) are real.
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import time
import tracemalloc
import unittest
from pathlib import Path

from gateway.ledger_export import export_records, render_csv, render_json
from gateway.ledger_persistence import (
    FIELDS,
    LedgerPersistence,
    RETENTION_SECONDS,
    store_path_for,
)
from gateway.ledger_store import LedgerStore
from gateway.observability import RequestLedger

#: The measured fleet volume the card names: ~400 KiB/h of ledger writes,
#: ~5,190 requests/day -> ~156,000 rows over the 30-day window.
VOLUME_ROWS = 156_000
#: The old design's hard ceiling: 5 MiB x (MAX_ROTATIONS + 1). Retained
#: data must exceed it, or this file is not testing the bug it says it is.
OLD_JSONL_CAP_BYTES = 15 * 1024 * 1024


def _stamp(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _row(request_id: str, epoch: float, **overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "request_id": request_id,
        "timestamp": _stamp(epoch),
        "requested_alias": "coding",
        "requested_model": "gpt-x",
        "serving_provider": "p",
        "serving_model": "m",
        "status": 200,
        "error_class": None,
        "latency_ms": 1200,
        "fallback_reason": None,
        "fallback_used": 0,
        "streamed": 1,
        "token_counters": {"prompt_tokens": 1500, "completion_tokens": 400,
                           "cached_read_tokens": 256},
        "profile_id": None,
        "skipped": [],
        "attempts": [],
        "degradations": [],
    }
    row.update(overrides)
    return row


def _fill(store: LedgerStore, count: int, *, now: float | None = None,
          spread_seconds: float = float(RETENTION_SECONDS)) -> int:
    """Insert ``count`` rows spread across the retention window; returns total body bytes."""
    now = time.time() if now is None else now
    window_start = int(now - spread_seconds)
    total_bytes = 0
    batch: list[tuple[str, int, str, str | None, int, str]] = []
    for index in range(count):
        epoch = window_start + int(index * spread_seconds / max(count - 1, 1))
        row = _row(f"r{index}", epoch)
        body = json.dumps(row, separators=(",", ":"), ensure_ascii=True)
        total_bytes += len(body)
        batch.append((f"r{index}", epoch, str(row["timestamp"]), None, 0, body))
        if len(batch) >= 10_000:
            store.insert_many(batch)
            batch = []
    if batch:
        store.insert_many(batch)
    return total_bytes


class StoreRetentionTests(unittest.TestCase):
    """AC1/AC2: age is the only bound; the old size cap is genuinely exceeded."""

    def test_full_window_at_measured_volume_survives_and_only_age_prunes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "request-ledger.jsonl"
            store = LedgerStore(store_path_for(path))
            store.open()
            try:
                # One reference clock: the window's first row sits exactly on
                # the prune cutoff, which is the boundary the age rule uses.
                now = time.time()
                total_bytes = _fill(store, VOLUME_ROWS, now=now)
                # Three rows past the window, as a stale migration would leave.
                stale = [
                    (f"stale{i}", int(now - RETENTION_SECONDS - (i + 1) * 86400),
                     _stamp(now - RETENTION_SECONDS - (i + 1) * 86400), None, 0,
                     json.dumps(_row(f"stale{i}", now - RETENTION_SECONDS - (i + 1) * 86400)))
                    for i in range(3)
                ]
                store.insert_many(stale)

                # The retained data is bigger than the entire old cap - the
                # old rotation would already have dropped most of it here.
                self.assertGreater(total_bytes, OLD_JSONL_CAP_BYTES)

                # Age-prune with the window's own cutoff: the in-window rows
                # must all survive (a size-based design keeps ~1.3 days of
                # this), and only the three stale rows leave.
                deleted = store.prune(int(now - RETENTION_SECONDS))
                self.assertEqual(deleted, 3)
                stats = store.stats()
                self.assertEqual(stats["rows"], VOLUME_ROWS)
                self.assertEqual(store.totals()["requests"], VOLUME_ROWS)

                # Nothing further leaves while the rows are inside the
                # window: retention is not implemented as a hidden cap.
                self.assertEqual(store.prune(int(now - RETENTION_SECONDS)), 0)
                self.assertEqual(store.stats()["rows"], VOLUME_ROWS)

                # The whole window stays readable, one row at a time.
                self.assertEqual(sum(1 for _ in store.iter_bodies()), VOLUME_ROWS)
            finally:
                store.close()


class StoreSchemaTests(unittest.TestCase):
    """AC4: metadata-only - the schema itself cannot hold content."""

    def test_schema_is_exactly_the_six_metadata_columns(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "request-ledger.jsonl"
            store = LedgerStore(store_path_for(path))
            store.open()
            try:
                columns = [row[1] for row in
                           store._reader().execute("PRAGMA table_info(ledger)").fetchall()]
            finally:
                store.close()
            self.assertEqual(columns,
                             ["request_id", "ts", "stamp", "error_class",
                              "fallback_used", "body"])
            # No column anywhere near a prompt, response, credential or token
            # payload: the schema cannot be extended into content by adding
            # a "helpful" field without failing here.
            forbidden = ("prompt", "response", "content", "credential", "secret",
                         "message", "input", "output_text")
            self.assertFalse([name for name in columns
                              if any(word in name for word in forbidden)])

    def test_content_keys_never_reach_the_store_or_the_journal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "request-ledger.jsonl"
            ledger = RequestLedger(persistence_path=path)
            try:
                row = _row("req-content", time.time())
                row["prompt"] = "TOP-SECRET-PROMPT"
                row["response"] = "SECRET-RESPONSE"
                self.assertTrue(ledger._persistence.submit(row))  # type: ignore[union-attr]
                self.assertTrue(ledger.flush())
            finally:
                ledger.close()
            connection = sqlite3.connect(store_path_for(path))
            try:
                bodies = [row[0] for row in
                          connection.execute("SELECT body FROM ledger").fetchall()]
            finally:
                connection.close()
            self.assertEqual(len(bodies), 1)
            stored = json.loads(bodies[0])
            self.assertNotIn("prompt", stored)
            self.assertNotIn("response", stored)
            self.assertFalse(set(stored) - FIELDS)
            self.assertNotIn("SECRET", bodies[0])
            journal = path.read_text()
            self.assertNotIn("SECRET", journal)


class BoundedBootTests(unittest.TestCase):
    """AC3: boot over a full history is a bounded load, not a history read."""

    def test_boot_over_full_history_is_bounded_fast_and_truthful(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "request-ledger.jsonl"
            store = LedgerStore(store_path_for(path))
            store.open()
            try:
                _fill(store, VOLUME_ROWS)
            finally:
                store.close()

            # Boot 1: no sidecar yet -> full store replay (the migration /
            # recovery path). Correctness only; the speed claim below is
            # about the path every subsequent boot takes.
            first = RequestLedger(persistence_path=path)
            try:
                first_payload = json.loads(
                    path.with_name("request-ledger-rollup.json").read_text())
                self.assertEqual(first_payload["coverage"]["rebuild"]["mode"], "full")
                self.assertEqual(first_payload["coverage"]["requests"],
                                 first.totals()["requests"])
            finally:
                first.close()

            # Boot 2: sidecar present -> load + bounded replay, measured the
            # way production runs it (no profiler): this is the every-boot
            # path and the one the R56 sub-2s state bar depends on.
            started = time.perf_counter()
            second = RequestLedger(persistence_path=path)
            boot_seconds = time.perf_counter() - started
            try:
                self.assertLess(boot_seconds, 2.0,
                                f"incremental boot took {boot_seconds:.3f}s")

                payload = json.loads(
                    path.with_name("request-ledger-rollup.json").read_text())
                rebuild = payload["coverage"]["rebuild"]
                self.assertEqual(rebuild["mode"], "incremental")
                self.assertEqual(rebuild["trigger"], "boot")
                self.assertEqual(rebuild["lines_read"], 0)

                totals = second.totals()
                self.assertGreater(totals["requests"], VOLUME_ROWS * 0.99)
                self.assertEqual(payload["coverage"]["requests"], totals["requests"])
                # The ring stays a window; the totals do not.
                self.assertEqual(len(second.recent(50)), 50)
                self.assertEqual(second.evicted, totals["requests"] - 10_000)

                # first_row_ts is truthful: the stored window's own oldest
                # row (the boundary row ages out only when it falls outside
                # the window), not the capped index's idea of it.
                from datetime import UTC, datetime
                oldest_epoch = datetime.fromisoformat(
                    payload["coverage"]["first_row_ts"].replace("Z", "+00:00")).timestamp()
                self.assertLess(abs(oldest_epoch - (time.time() - RETENTION_SECONDS)), 3600)

                # The state-read path (rollup digest + store figures +
                # totals) is the R56 sub-2s bar's actual work; it must stay
                # far below it with the full history on disk.
                started = time.perf_counter()
                second.rollup()
                second.durability()
                second.totals()
                state_seconds = time.perf_counter() - started
                self.assertLess(state_seconds, 1.0,
                                f"state components took {state_seconds:.3f}s")
            finally:
                second.close()

            # Boot 3: the same path under tracemalloc for the memory bound.
            # A whole-history materialisation would hold the ~60 MiB of
            # bodies plus parsed rows; the load path holds the ring window
            # and a capped index instead.
            tracemalloc.start()
            third = RequestLedger(persistence_path=path)
            peak_bytes = tracemalloc.get_traced_memory()[1]
            tracemalloc.stop()
            third.close()
            self.assertLess(peak_bytes, 64 * 1024 * 1024,
                            f"boot peak {peak_bytes / 1048576:.1f} MiB")

    def test_runtime_amendment_after_a_loaded_boot_supersedes_not_double_counts(self) -> None:
        """The index seed's whole reason to exist (measured 60k-row check, as a test)."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "request-ledger.jsonl"
            store = LedgerStore(store_path_for(path))
            store.open()
            try:
                _fill(store, 60_000)
            finally:
                store.close()
            first = RequestLedger(persistence_path=path)
            first.close()
            second = RequestLedger(persistence_path=path)
            try:
                before = second.totals()["requests"]
                # Newest ids sit in the ring; amend must succeed and must
                # not add a second request to the rollup.
                target = f"r{60_000 - 5}"
                self.assertTrue(second.amend(target, status=500))
                self.assertEqual(second.totals()["requests"], before)
                self.assertTrue(second.flush())
                payload = json.loads(
                    path.with_name("request-ledger-rollup.json").read_text())
                self.assertEqual(payload["coverage"]["requests"], before)
                self.assertEqual(payload["coverage"]["rows_superseded"], 1)
                self.assertEqual(payload["coverage"]["rows_appended"], 1)
                self.assertFalse(payload["coverage"]["incomplete"])
            finally:
                second.close()


class MigrationTests(unittest.TestCase):
    """AC6: the legacy journal + rotations import without loss, once."""

    def test_legacy_rotations_import_without_loss_and_never_twice(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "request-ledger.jsonl"
            now = time.time()
            # Three files, oldest first, 7000 lines; row 6500 (main file)
            # amends row 100 (oldest file) - import order must preserve the
            # later-wins rule across the rotation boundary.
            def lines(start: int, end: int) -> str:
                out = []
                for index in range(start, end):
                    request_id = "r100" if index == 6500 else f"r{index}"
                    out.append(json.dumps(_row(request_id, int(now - 86400 + index))))
                return "\n".join(out) + "\n"

            path.with_suffix(path.suffix + ".2").write_text(lines(0, 3000))
            path.with_suffix(path.suffix + ".1").write_text(lines(3000, 6000))
            path.write_text(lines(6000, 7000))

            ledger = RequestLedger(persistence_path=path)
            try:
                connection = sqlite3.connect(store_path_for(path))
                try:
                    rows = connection.execute("SELECT COUNT(*) FROM ledger").fetchone()[0]
                finally:
                    connection.close()
                # Every line lands; the amendment pair costs an extra line,
                # not an extra request.
                self.assertEqual(rows, 7000)
                payload = json.loads(
                    path.with_name("request-ledger-rollup.json").read_text())
                self.assertEqual(payload["coverage"]["requests"], 6999)
                self.assertEqual(payload["coverage"]["rows_superseded"], 1)
                self.assertEqual(payload["coverage"]["rebuild"]["mode"], "full")
                self.assertFalse(payload["coverage"]["incomplete"])
                self.assertEqual(len(ledger.totals()), 5)
                self.assertEqual(ledger.totals()["requests"], 6999)
            finally:
                ledger.close()

            # Second boot: the store is populated, so no re-import happens.
            again = RequestLedger(persistence_path=path)
            try:
                connection = sqlite3.connect(store_path_for(path))
                try:
                    rows = connection.execute("SELECT COUNT(*) FROM ledger").fetchone()[0]
                finally:
                    connection.close()
                self.assertEqual(rows, 7000)
                payload = json.loads(
                    path.with_name("request-ledger-rollup.json").read_text())
                self.assertEqual(payload["coverage"]["rebuild"]["mode"], "incremental")
                self.assertEqual(payload["coverage"]["requests"], 6999)
                # The journal keeps its lines across the rewrite too.
                self.assertEqual(len(path.read_text().splitlines()), 1000)
            finally:
                again.close()

    def test_wal_mode_and_concurrent_submitters_without_lock_errors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "request-ledger.jsonl"
            persistence = LedgerPersistence(path)
            try:
                # AC6: WAL, on the volume the caller chose (the state dir
                # in production - the path is the caller's to set).
                probe = LedgerStore(store_path_for(path))
                probe.open()
                try:
                    mode = probe._reader().execute("PRAGMA journal_mode").fetchone()[0]
                finally:
                    probe.close()
                self.assertEqual(str(mode).lower(), "wal")

                # Single-writer behaviour under contention: many submitters,
                # one writer thread, no lock errors, no lost rows.
                errors: list[BaseException] = []

                def submit_many(worker: int) -> None:
                    try:
                        for index in range(50):
                            persistence.submit(_row(f"w{worker}-{index}", time.time()))
                    except BaseException as exc:  # noqa: BLE001 - surfaced below
                        errors.append(exc)

                threads = [threading.Thread(target=submit_many, args=(worker,))
                           for worker in range(8)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()
                self.assertEqual(errors, [])
                self.assertTrue(persistence.flush())
                connection = sqlite3.connect(store_path_for(path))
                try:
                    rows = connection.execute("SELECT COUNT(*) FROM ledger").fetchone()[0]
                finally:
                    connection.close()
                self.assertEqual(rows, 400)
                self.assertIsNone(persistence.durability()["store"]["error"])
            finally:
                persistence.close()


class CrashReplayTests(unittest.TestCase):
    """The store can hold rows no sidecar knows about; boot must find them."""

    def test_a_store_row_newer_than_the_sidecar_watermark_is_replayed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "request-ledger.jsonl"
            first = LedgerPersistence(path)
            try:
                self.assertTrue(first.submit(_row("covered", time.time())))
                self.assertTrue(first.flush())  # sidecar now covers this row
                store = first._store
                self.assertIsNotNone(store)
                assert store is not None
                # Crash-shaped state: the store took a row (a flush landed
                # for the store, the sidecar save did not follow). The
                # watermark in the saved sidecar stays behind.
                epoch = int(time.time())
                store.append(request_id="uncovered", ts=epoch, stamp=_stamp(epoch),
                             error_class=None, fallback_used=0,
                             body=json.dumps(_row("uncovered", epoch)))
                self.assertEqual(store.stats()["rows"], 2)
            finally:
                first.close()
            # The sidecar on disk must predate the second row for this test
            # to mean anything: its watermark has to sit below the store max.
            payload = json.loads(
                path.with_name("request-ledger-rollup.json").read_text())
            self.assertEqual(payload["coverage"]["requests"], 1)

            second = LedgerPersistence(path)
            try:
                self.assertEqual(second.durability()["store"]["requests"], 2)
                coverage = second.rollup()["coverage"]
                self.assertEqual(coverage["requests"], 2)
                self.assertEqual(coverage["rebuild"]["mode"], "incremental")
                self.assertEqual(coverage["rebuild"]["lines_read"], 1)
                self.assertEqual(coverage["rows_appended"], 1)
                self.assertFalse(coverage["incomplete"])
            finally:
                second.close()


class RuntimeRetentionTests(unittest.TestCase):
    """AC1/AC7: the running process ages rows out and reports it."""

    def test_running_process_prunes_by_age_and_counts_the_expiry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "request-ledger.jsonl"
            persistence = LedgerPersistence(path)
            try:
                self.assertTrue(persistence.submit(_row("fresh", time.time())))
                self.assertTrue(persistence.flush())
                store = persistence._store
                self.assertIsNotNone(store)
                assert store is not None
                # A row already past the window appears after boot (a clock
                # jump, a copied volume - the runtime prune exists for it).
                aged_epoch = int(time.time() - RETENTION_SECONDS - 86400)
                store.append(
                    request_id="aged", ts=aged_epoch, stamp=_stamp(aged_epoch),
                    error_class=None, fallback_used=0,
                    body=json.dumps(_row("aged", aged_epoch)),
                )
                self.assertEqual(store.stats()["rows"], 2)

                # Due now, then one append to drive the writer's prune.
                persistence._prune_due = 0.0
                self.assertTrue(persistence.submit(_row("fresh2", time.time())))
                self.assertTrue(persistence.flush())

                self.assertEqual(store.stats()["rows"], 2)  # fresh + fresh2
                coverage = persistence.rollup()["coverage"]
                self.assertGreaterEqual(coverage["rows_expired"], 1)
                self.assertFalse(coverage["incomplete"])
            finally:
                persistence.close()


class DurabilityReportingTests(unittest.TestCase):
    """AC7: size / rows / oldest row where the owner reads them."""

    def test_durability_block_reports_the_real_store_figures(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "request-ledger.jsonl"
            persistence = LedgerPersistence(path)
            try:
                for index in range(25):
                    persistence.submit(_row(f"d{index}", time.time()))
                self.assertTrue(persistence.flush())
                block = persistence.durability()
                self.assertTrue(block["enabled"])
                self.assertEqual(block["retention_days"], 30)
                self.assertFalse(block["billing_system"])
                store = block["store"]
                self.assertIsNone(store["error"])
                self.assertEqual(store["rows"], 25)
                self.assertEqual(store["requests"], 25)
                self.assertGreater(store["bytes"], 0)
                self.assertGreaterEqual(store["journal_bytes"], 0)
                self.assertEqual(store["path"], str(store_path_for(path)))
                self.assertTrue(store["oldest_row_ts"].endswith("Z"))
            finally:
                persistence.close()

    def test_an_unopenable_store_degrades_to_the_journal_and_says_so(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "request-ledger.jsonl"
            # A directory squatting on the store path: sqlite cannot open
            # it, the gateway must still boot, and the status must carry the
            # error instead of pretending the history is intact.
            store_path_for(path).mkdir()
            ledger = RequestLedger(persistence_path=path)
            try:
                block = ledger.durability()
                self.assertTrue(block["enabled"])
                self.assertTrue(block["store"]["error"])
                self.assertFalse(ledger.rollup()["coverage"]["incomplete"])
            finally:
                ledger.close()


class ExportTests(unittest.TestCase):
    """AC5: the T24 export keeps working over the store-backed ring."""

    def test_export_reads_store_backed_rows_after_a_restart(self) -> None:
        from gateway.observability import RequestEvent
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "request-ledger.jsonl"
            first = RequestLedger(persistence_path=path)
            try:
                for index in range(10):
                    first.record(RequestEvent(f"e{index}", _stamp(time.time() - index),
                                              "coding", status=200))
                self.assertTrue(first.flush())
            finally:
                first.close()
            restarted = RequestLedger(persistence_path=path)
            try:
                records = export_records(restarted.recent(50))
                self.assertEqual(len(records), 10)
                self.assertEqual(
                    list(records[0]),
                    ["timestamp", "alias", "serving_provider", "serving_model",
                     "outcome", "note"],
                )
                self.assertTrue(render_csv(records).startswith(b"timestamp,alias"))
                self.assertEqual(json.loads(render_json(records)), records)
            finally:
                restarted.close()
