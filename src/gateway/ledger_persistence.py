"""Asynchronous, bounded, metadata-only request ledger persistence.

Two durable artifacts share the state directory:

``request-ledger.jsonl``
    The append-only metadata ledger: one line per recorded outcome, with an
    amendment appended after the original row for the same request.
    Rotated at 5 MiB with two rotations and a 30-day retention window.

``request-ledger-rollup.json``
    The daily rollup sidecar maintained from the *same* append path: one row
    per UTC day x alias x provider x model carrying requests, input, output
    and cached tokens, fallbacks broken out by the closed fallback-reason
    vocabulary (folded at ingest - see :mod:`gateway.fallback_reasons`, so
    raw per-second sentences never become sidecar keys), errors by
    ``error_class`` and a status breakdown, plus a ``coverage`` block that
    states exactly what the rollup covers. The ledger stays the source of
    truth; :func:`rebuild_rollups` regenerates the sidecar from it, and the
    store is rebuilt from the ledger on every start so aggregated history
    never begins at deploy time.

The store's two growth bounds are deliberate and both are visible in
``coverage``: the request index is count-capped at
:data:`INDEX_MAX_ENTRIES` entries (oldest-by-last-touch eviction, evicted
ids remembered for a late amendment), and every persisted structure derives
from either that cap or the durable ledger's own 5 MiB x 3 rotation, so an
appliance that runs for months does not grow without limit.

Both artifacts are metadata only. Neither holds a prompt, a response, a
credential, a provider account identifier, or - in the rollup's case - even
a request identifier: an aggregated day cannot be traced back to an
individual call. Cost estimates are deliberately *not* persisted into the
rollup, because a published price can change; they are computed at read time
from the token counters.
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import sys
import threading
import time
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from gateway.fallback_reasons import fallback_reason_class

RETENTION_SECONDS = 30 * 24 * 60 * 60
MAX_BYTES = 5 * 1024 * 1024
MAX_ROTATIONS = 2
FIELDS = frozenset({
    "request_id", "timestamp", "requested_alias", "requested_model",
    "serving_provider", "serving_model", "status", "error_class",
    "latency_ms", "fallback_reason", "fallback_used", "streamed",
    "token_counters", "profile_id", "skipped", "attempts", "degradations",
})
AMENDABLE = frozenset({"status", "error_class", "token_counters", "latency_ms"})

#: The sidecar's format identifier. A reader that does not recognise it must
#: refuse to interpret the file rather than guess at missing fields.
ROLLUP_SCHEMA = "daily-rollup/v1"
#: How often the running process refreshes the sidecar from the append path:
#: bounded both by rows written and by wall-clock time, so a crash can lose at
#: most this much incremental state. The next start rebuilds from the ledger,
#: which is the authoritative copy, so the loss is never permanent.
ROLLUP_SNAPSHOT_ROWS = 50
ROLLUP_SNAPSHOT_SECONDS = 15.0
#: Used for a dimension a row did not carry (an alias-less legacy row, a
#: failure recorded before a route was chosen). It is reported, never folded
#: into a real name and never silently dropped.
UNKNOWN = "unknown"
DEFAULT_LEDGER_PATH = "/var/lib/gateway/request-ledger.jsonl"

#: Hard count bound on the in-memory request index (security gate
#: t_eb1846b6, S-1). The index maps a request id to its contribution so a
#: later amendment replaces it instead of counting it twice, and it used to
#: hold one entry per request for the whole 30-day retention: measured
#: 300k rows -> +187 MB RSS, i.e. 30k requests/day -> ~540 MB and
#: 100k/day -> ~1.8 GB on an appliance whose uptime is effectively
#: unbounded. Every other ledger structure is count-bounded (ring 10_000,
#: writer queue 20_000, ledger file 5 MiB x 3); this is the only runtime one
#: that was not.
#:
#: 50_000 was chosen so that:
#:
#: * a normal boot never evicts - the whole durable ledger (5 MiB x 3 =
#:   15 MiB, ~40k rows at a typical row size, the bound the security gate
#:   already accepted for rebuild) fits under the cap;
#: * it stays several times wider than the 10_000-record ledger ring that
#:   gates amendments: ``RequestLedger.amend`` only writes an amendment row
#:   while the event is still in the ring, so an id the product can still
#:   amend is always among the newest index entries and is evicted only
#:   after at least this many *other* requests - by which time the ring no
#:   longer holds it and the amendment cannot be issued at all;
#: * worst-case memory stays ~30 MB at the measured ~0.6 KB per entry.
#:
#: Eviction drops the *entry*, never the counted request: the contribution
#: stays in its bucket, and the evicted id is remembered in a second
#: bounded set of the same size so a late amendment is dropped and reported
#: in ``coverage`` (``index_evicted`` / ``index_amendments_dropped``) rather
#: than silently double-counted.
INDEX_MAX_ENTRIES = 50_000

DimensionKey = tuple[str, str, str, str]


def rollup_path_for(ledger_path: Path) -> Path:
    """The sidecar path for a ledger path: ``request-ledger.jsonl`` -> ``request-ledger-rollup.json``."""
    path = Path(ledger_path)
    return path.with_name(path.stem + "-rollup.json")


def _ledger_files(path: Path) -> list[Path]:
    """Every ledger file, oldest first, so a later line supersedes an earlier one."""
    return [path.with_suffix(path.suffix + f".{i}") for i in range(MAX_ROTATIONS, 0, -1)] + [path]


class RollupStore:
    """The daily rollup: aggregated, metadata-only, and rebuildable from the ledger.

    Rows are folded in per request identifier, so an amendment (a later line
    for the same request) *replaces* the earlier contribution instead of
    being counted twice - the same rule the ledger's own loader applies.
    """

    def __init__(self, path: Path, source: str, *, now: Callable[[], datetime] | None = None) -> None:
        self.path = Path(path)
        self.source = source
        self._now = now or (lambda: datetime.now(UTC))
        self._lock = threading.RLock()
        self._rows: dict[DimensionKey, dict[str, Any]] = {}
        self._index: dict[str, tuple[DimensionKey, dict[str, Any], float]] = {}
        #: Ids the count cap evicted, newest last, bounded by
        #: ``INDEX_MAX_ENTRIES`` too: they exist only so a late amendment for
        #: an evicted request is recognised and dropped instead of counted a
        #: second time.
        self._evicted_ids: dict[str, None] = {}
        self._index_evicted = 0
        self._index_amendments_dropped = 0
        self._rows_skipped = 0
        self._rows_expired = 0
        self._rows_superseded = 0
        self._rows_appended = 0
        self._rebuild: dict[str, Any] = {"status": "pending", "trigger": "", "at": None, "lines_read": 0}
        self._first: float | None = None
        self._last: float | None = None
        self._pending = 0
        self._last_save = time.monotonic()

    # --- building -------------------------------------------------------

    def ingest(self, row: Any, *, appended: bool = False) -> bool:
        """Fold one ledger row in, replacing any earlier row for the same request.

        A row that cannot be represented (no identifier, no usable timestamp,
        or an unknown field the loader would also refuse) is counted as
        skipped rather than dropped, so the coverage block can say so.

        A row whose request id the count cap has already evicted is a late
        amendment of a request that is still counted: folding it again would
        double-count one request, so it is dropped and reported through
        ``coverage["index_amendments_dropped"]`` instead. Returning False
        means "nothing was folded", never "this request does not exist".
        """
        with self._lock:
            if not isinstance(row, dict) or set(row) - FIELDS:
                self._rows_skipped += 1
                return False
            if not isinstance(row.get("request_id"), str):
                self._rows_skipped += 1
                return False
            stamp = _parse_timestamp(row.get("timestamp"))
            if stamp is None:
                self._rows_skipped += 1
                return False
            request_id = row["request_id"]
            previous = self._index.get(request_id)
            if previous is None and request_id in self._evicted_ids:
                self._index_amendments_dropped += 1
                return False
            if previous is None:
                key = (
                    stamp.strftime("%Y-%m-%d"),
                    _dimension(row, "requested_alias"),
                    _dimension(row, "serving_provider"),
                    _dimension(row, "serving_model"),
                )
                epoch = stamp.timestamp()
            else:
                # An amendment carries the original timestamp, so the request
                # keeps its day and its place in the coverage window.
                key, _old_delta, epoch = previous
                self._rows_superseded += 1
                self._apply(self._rows[key], previous[1], -1)
                # Amend = last touch, so an amended request is the newest
                # entry in eviction order rather than the oldest.
                del self._index[request_id]
            delta = _row_delta(row)
            bucket = self._rows.get(key)
            if bucket is None:
                bucket = _empty_bucket()
                self._rows[key] = bucket
            self._apply(bucket, delta, +1)
            self._index[request_id] = (key, delta, epoch)
            self._evict_overflow()
            if self._first is None or epoch < self._first:
                self._first = epoch
            if self._last is None or epoch > self._last:
                self._last = epoch
            if appended:
                self._rows_appended += 1
            self._pending += 1
            return True

    def _evict_overflow(self) -> None:
        """Enforce ``INDEX_MAX_ENTRIES``: oldest by last touch first.

        Only the *entry* is evicted - the request stays counted in its
        bucket, so totals never lose data to the cap - and the id moves to
        the bounded ``_evicted_ids`` set, which is what turns a later
        amendment of it from a silent double count into a reported drop.
        """
        while len(self._index) > INDEX_MAX_ENTRIES:
            oldest = next(iter(self._index))
            self._index.pop(oldest)
            self._index_evicted += 1
            self._evicted_ids[oldest] = None
            if len(self._evicted_ids) > INDEX_MAX_ENTRIES:
                self._evicted_ids.pop(next(iter(self._evicted_ids)))

    def rebuild(self, files: Iterable[Path], *, trigger: str = "manual") -> dict[str, Any]:
        """Regenerate the whole rollup from ledger files.

        Idempotent: the previous state is discarded first, so re-running it
        over the same ledger always produces the same rollup.
        """
        with self._lock:
            self._rows.clear()
            self._index.clear()
            # The ledger replay re-establishes every amendment relationship,
            # so the eviction bookkeeping starts over with it.
            self._evicted_ids.clear()
            self._index_evicted = 0
            self._index_amendments_dropped = 0
            self._rows_skipped = 0
            self._rows_expired = 0
            self._rows_superseded = 0
            self._rows_appended = 0
            self._first = None
            self._last = None
            lines_read = 0
            status = "complete"
            now = time.time()
            for file in files:
                try:
                    with file.open(encoding="utf-8") as source:
                        for line in source:
                            lines_read += 1
                            try:
                                row = json.loads(line)
                            except ValueError:
                                self._rows_skipped += 1
                                continue
                            stamp = _parse_timestamp(row.get("timestamp")) if isinstance(row, dict) else None
                            if stamp is not None and now - stamp.timestamp() > RETENTION_SECONDS:
                                # Past the declared retention window: expected,
                                # and not a coverage gap.
                                self._rows_expired += 1
                                continue
                            self.ingest(row)
                except FileNotFoundError:
                    continue
                except (OSError, ValueError):
                    # A ledger file we could not read to the end - missing
                    # mid-read, unreadable, or not valid UTF-8 - is a real
                    # gap in the rollup, and the coverage block has to say so
                    # rather than presenting the rows we did read as complete.
                    status = "partial"
            self._rebuild = {
                "status": status,
                "trigger": trigger,
                "at": self._iso_now(),
                "lines_read": lines_read,
            }
            return self._snapshot_locked()

    # --- reading --------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """The full rollup payload: schema, generation time, coverage, daily trend and rows."""
        with self._lock:
            return self._snapshot_locked()

    def daily_totals(self) -> list[dict[str, Any]]:
        """Per-day totals across every alias, provider and model; oldest day first."""
        with self._lock:
            return self._daily_locked()

    def _daily_locked(self) -> list[dict[str, Any]]:
        totals: dict[str, dict[str, Any]] = {}
        for (day, _alias, _provider, _model), bucket in self._rows.items():
            entry = totals.get(day)
            if entry is None:
                entry = {key: value for key, value in _empty_bucket().items() if not isinstance(value, dict)}
                entry["fallback_reasons"] = {}
                entry["errors"] = {}
                entry["statuses"] = {}
                totals[day] = entry
            for key in ("requests", "input_tokens", "output_tokens", "cached_tokens",
                        "cached_tokens_unreported", "requests_without_usage", "fallbacks"):
                entry[key] += bucket[key]
            for key in ("fallback_reasons", "errors", "statuses"):
                for name, count in bucket[key].items():
                    entry[key][name] = entry[key].get(name, 0) + count
        return [{"day": day, **totals[day]} for day in sorted(totals)]

    # --- persisting -----------------------------------------------------

    def save(self) -> dict[str, Any] | None:
        """Atomically rewrite the sidecar; returns the payload written, or None on an I/O error."""
        with self._lock:
            self._prune_expired()
            payload = self._snapshot_locked()
            text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"
            try:
                _atomic_write(self.path, text)
            except OSError:
                return None
            self._pending = 0
            self._last_save = time.monotonic()
            return payload

    def maybe_save(self) -> None:
        """Refresh the sidecar when enough rows have accumulated since the last write."""
        with self._lock:
            if not self._pending:
                return
            due = self._pending >= ROLLUP_SNAPSHOT_ROWS or (time.monotonic() - self._last_save) >= ROLLUP_SNAPSHOT_SECONDS
            if due:
                self.save()

    # --- internals ------------------------------------------------------

    def _snapshot_locked(self) -> dict[str, Any]:
        rows = []
        covered_requests = 0
        for key in sorted(self._rows):
            day, alias, provider, model = key
            bucket = self._rows[key]
            covered_requests += bucket["requests"]
            entry = {name: value for name, value in bucket.items() if not isinstance(value, dict)}
            entry.update({name: dict(value) for name, value in bucket.items() if isinstance(value, dict)})
            entry.update({"day": day, "alias": alias, "provider": provider, "model": model})
            rows.append(entry)
        return {
            "schema": ROLLUP_SCHEMA,
            "generated_at": self._iso_now(),
            "source": self.source,
            "coverage": {
                "first_row_ts": _iso(self._first) if self._first is not None else None,
                "last_row_ts": _iso(self._last) if self._last is not None else None,
                # Requests currently aggregated: the buckets, not the index.
                # The count cap evicts index entries while their contribution
                # stays counted, so ``len(self._index)`` would understate what
                # this rollup actually covers.
                "requests": covered_requests,
                # The S-1 growth bound, stated rather than implied: how many
                # entries the index holds, its cap, how many the cap has
                # evicted, and how many late amendments that evicted instead
                # of double-counting.
                "index_entries": len(self._index),
                "index_max_entries": INDEX_MAX_ENTRIES,
                "index_evicted": self._index_evicted,
                "index_amendments_dropped": self._index_amendments_dropped,
                "rows_skipped": self._rows_skipped,
                "rows_expired": self._rows_expired,
                "rows_superseded": self._rows_superseded,
                "rows_appended": self._rows_appended,
                "retention_days": RETENTION_SECONDS // (24 * 60 * 60),
                "rebuild": dict(self._rebuild),
                "incomplete": bool(self._rows_skipped) or self._rebuild["status"] != "complete",
            },
            "rows": rows,
            "daily": self._daily_locked(),
        }

    def _prune_expired(self) -> None:
        """Drop contributions the ledger itself would already have pruned.

        Entries still in the index expire by age, exactly as before. A
        bucket whose *day* is past the retention window can additionally
        still hold requests the count cap evicted from the index - those
        contributions have no entry left to age out - so the day itself is
        the bound that keeps finished days from accumulating forever on a
        long-running appliance.
        """
        now = time.time()
        expired = [request_id for request_id, (_key, _delta, epoch) in self._index.items() if now - epoch > RETENTION_SECONDS]
        cutoff_day = datetime.fromtimestamp(now - RETENTION_SECONDS, UTC).strftime("%Y-%m-%d")
        stale_days = [key for key in self._rows if key[0] < cutoff_day]
        if not expired and not stale_days:
            return
        for request_id in expired:
            key, delta, _epoch = self._index.pop(request_id)
            self._apply(self._rows[key], delta, -1)
        # Every row in a stale day is older than the retention window, so no
        # surviving index entry can still point at a bucket dropped here.
        self._rows = {key: bucket for key, bucket in self._rows.items()
                      if bucket["requests"] > 0 and key[0] >= cutoff_day}
        # ``first_row_ts``/``last_row_ts`` describe the window the index
        # still tracks: under count-cap eviction the evicted entries are no
        # longer enumerated here, but their days are bounded by the rule
        # above.
        epochs = [epoch for _key, _delta, epoch in self._index.values()]
        self._first = min(epochs) if epochs else None
        self._last = max(epochs) if epochs else None

    @staticmethod
    def _apply(bucket: dict[str, Any], delta: dict[str, Any], sign: int) -> None:
        for name in ("requests", "input_tokens", "output_tokens", "cached_tokens",
                      "cached_tokens_unreported", "requests_without_usage"):
            bucket[name] += sign * delta[name]
        if delta["fallback"]:
            bucket["fallbacks"] += sign
            _tally(bucket["fallback_reasons"], delta["fallback"], sign)
        if delta["error"]:
            _tally(bucket["errors"], delta["error"], sign)
        if delta["status"]:
            _tally(bucket["statuses"], delta["status"], sign)

    def _iso_now(self) -> str:
        return _iso_datetime(self._now())


def rebuild_rollups(
    ledger_path: Path | str,
    *,
    rollup_path: Path | str | None = None,
    trigger: str = "manual",
    write: bool = True,
    now: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """Regenerate the daily rollup sidecar from an existing request ledger.

    Idempotent and safe to re-run: every call starts from an empty rollup and
    folds the ledger in again, so the result depends only on the ledger's
    contents. With ``write=False`` nothing touches the filesystem.
    """
    path = Path(ledger_path)
    store = RollupStore(Path(rollup_path) if rollup_path else rollup_path_for(path), path.name, now=now)
    store.rebuild(_ledger_files(path), trigger=trigger)
    payload = store.save() if write else None
    return payload if payload is not None else store.snapshot()


class LedgerPersistence:
    """Queue writes off the caller; newline JSON is append-only and rotated."""

    def __init__(self, path: Path, capacity: int = 20000) -> None:
        self.path = Path(path)
        self.rollup_path = rollup_path_for(self.path)
        self._rollup = RollupStore(self.rollup_path, self.path.name)
        # Rebuild the rollup from the ledger before the writer thread starts:
        # history already on disk is aggregated immediately, so the sidecar
        # never starts empty on a gateway that has been running before.
        self._rollup.rebuild(_ledger_files(self.path), trigger="boot")
        self._rollup.save()
        self._queue: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=capacity)
        self._thread = threading.Thread(target=self._run, name="ledger-writer", daemon=True)
        self._thread.start()

    def submit(self, row: dict[str, Any]) -> bool:
        clean = {key: value for key, value in row.items() if key in FIELDS}
        try:
            self._queue.put_nowait(clean)
            return True
        except queue.Full:
            return False

    def rollup(self) -> dict[str, Any]:
        """The live daily rollup: schema, coverage and one row per day/dimension."""
        return self._rollup.snapshot()

    def load(self, limit: int) -> list[dict[str, Any]]:
        rows: dict[str, dict[str, Any]] = {}
        now = time.time()
        files = _ledger_files(self.path)
        for file in files:
            retained: list[str] = []
            try:
                with file.open(encoding="utf-8") as source:
                    for line in source:
                        try:
                            row = json.loads(line)
                            if not isinstance(row, dict) or set(row) - FIELDS:
                                continue
                            if now - _timestamp(row.get("timestamp")) > RETENTION_SECONDS:
                                continue
                            if not isinstance(row.get("request_id"), str):
                                continue
                            row = _sanitize_row(row)
                            rows[row["request_id"]] = row
                            retained.append(json.dumps(row, separators=(",", ":"), ensure_ascii=True) + "\n")
                        except (ValueError, TypeError):
                            continue
                # Rewrite every file, physically removing expired and unsafe legacy rows.
                _rewrite(file, "".join(retained))
            except FileNotFoundError:
                continue
        # Return every retained row so callers can restore cumulative totals
        # and detect when the bounded in-memory ring is only a partial view.
        # The durable input itself is bounded by MAX_BYTES * (rotations + 1).
        return list(rows.values())

    def flush(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while self._queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.005)
        drained = self._queue.unfinished_tasks == 0
        # Whatever reached the ledger has also reached the rollup, so the
        # sidecar can be refreshed exactly when the queue is known drained.
        self._rollup.save()
        return drained

    def close(self, timeout: float = 5.0) -> None:
        self.flush(timeout)
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            return
        self._thread.join(timeout)

    def _run(self) -> None:
        while True:
            row = self._queue.get()
            try:
                if row is None:
                    return
                self._append(row)
            finally:
                self._queue.task_done()

    def _prune_rotations(self) -> None:
        cutoff = time.time() - RETENTION_SECONDS
        files = [self.path.with_suffix(self.path.suffix + f".{i}") for i in range(1, MAX_ROTATIONS + 1)]
        files.append(self.path)
        for file in files:
            try:
                retained = []
                with file.open(encoding="utf-8") as source:
                    for line in source:
                        try:
                            row = json.loads(line)
                        except (ValueError, TypeError):
                            continue
                        if isinstance(row, dict) and _timestamp(row.get("timestamp")) >= cutoff:
                            retained.append(json.dumps(_sanitize_row(row), separators=(",", ":"), ensure_ascii=True) + "\n")
                _rewrite(file, "".join(retained))
            except FileNotFoundError:
                continue

    def _append(self, row: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._prune_rotations()
        clean = _sanitize_row(row)
        encoded = (json.dumps(clean, separators=(",", ":"), ensure_ascii=True) + "\n").encode()
        if self.path.exists() and self.path.stat().st_size + len(encoded) > MAX_BYTES:
            oldest = self.path.with_suffix(self.path.suffix + f".{MAX_ROTATIONS}")
            oldest.unlink(missing_ok=True)
            for index in range(MAX_ROTATIONS - 1, 0, -1):
                source = self.path.with_suffix(self.path.suffix + f".{index}")
                if source.exists():
                    source.replace(self.path.with_suffix(self.path.suffix + f".{index + 1}"))
            self.path.replace(self.path.with_suffix(self.path.suffix + ".1"))
        descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(descriptor, encoded)
        finally:
            os.close(descriptor)
        # The row is on disk now, so fold exactly what was written - no more,
        # no less - into the rollup and refresh the sidecar when due.
        self._rollup.ingest(clean, appended=True)
        self._rollup.maybe_save()


def main(argv: list[str] | None = None) -> int:
    """Rebuild the rollup sidecar from a ledger; exits 1 when coverage is incomplete."""
    parser = argparse.ArgumentParser(
        prog="python3 -m gateway.ledger_persistence",
        description="Rebuild the daily analytics rollup sidecar from the durable request ledger.",
    )
    parser.add_argument("ledger", nargs="?", default=DEFAULT_LEDGER_PATH, help="path to request-ledger.jsonl")
    parser.add_argument("--rollup", default=None, help="sidecar path (default: alongside the ledger)")
    parser.add_argument("--dry-run", action="store_true", help="report the rollup without writing it")
    args = parser.parse_args(argv)
    path = Path(args.ledger)
    if not path.exists():
        print(f"no request ledger at {path}", file=sys.stderr)
        return 2
    payload = rebuild_rollups(path, rollup_path=args.rollup, write=not args.dry_run)
    print(json.dumps({
        "schema": payload["schema"],
        "source": payload["source"],
        "rows": len(payload["rows"]),
        "dry_run": bool(args.dry_run),
        "coverage": payload["coverage"],
    }, indent=2, sort_keys=True))
    return 1 if payload["coverage"]["incomplete"] else 0


def _empty_bucket() -> dict[str, Any]:
    return {
        "requests": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cached_tokens": 0,
        # Completeness signals, kept apart from the sums so a sum can never
        # carry a value the ledger never reported. See ``_row_delta``.
        "cached_tokens_unreported": 0,
        "requests_without_usage": 0,
        "fallbacks": 0,
        "fallback_reasons": {},
        "errors": {},
        "statuses": {},
    }


def _row_delta(row: dict[str, Any]) -> dict[str, Any]:
    """The one-row contribution to a bucket; also what an amendment subtracts.

    ``cached_tokens`` is a sum over the keys the provider actually reported,
    so a row whose cached count is absent would otherwise read as a reported
    zero. ``cached_tokens_unreported`` counts the ledger rows that reported
    usage without a cached count (absent key - including the
    ``cached_tokens_reported`` marker, which withholds the number), and
    ``requests_without_usage`` counts the rows that reported no usage at all.
    The surface prices exactly where those counts are zero and says
    "coverage unknown" where they are not; it never turns either absence
    into a zero.
    """
    counters = row.get("token_counters")
    counters = counters if isinstance(counters, dict) else {}
    usage_reported = any(name in counters for name in
                         ("prompt_tokens", "input_tokens", "completion_tokens", "output_tokens"))
    cached_reported = any(name in counters for name in ("cached_read_tokens", "cache_read_input_tokens"))
    fallback = bool(row.get("fallback_used"))
    reason = row.get("fallback_reason")
    error = row.get("error_class")
    status = row.get("status")
    return {
        "requests": 1,
        "input_tokens": _token(counters, "prompt_tokens", "input_tokens"),
        "output_tokens": _token(counters, "completion_tokens", "output_tokens"),
        "cached_tokens": _token(counters, "cached_read_tokens", "cache_read_input_tokens"),
        "cached_tokens_unreported": 1 if usage_reported and not cached_reported else 0,
        "requests_without_usage": 0 if usage_reported else 1,
        # Folded at ingest onto the shared closed vocabulary: a raw reason
        # embeds a per-second cooldown counter, a climbing failure count and
        # a provider-supplied retry-after, so persisting it verbatim would
        # open one sidecar key (and one rewrite) per request. The fold only
        # changes which bucket a count lands under - never how many counts
        # there are - and it is idempotent, so the reader-side fold in
        # ``gateway.http.app`` is a no-op on rows this store produced.
        "fallback": fallback_reason_class(reason) if fallback else None,
        "error": error if isinstance(error, str) and error else None,
        "status": str(status) if isinstance(status, int) and not isinstance(status, bool) else UNKNOWN,
    }


def _token(counters: dict[str, Any], *names: str) -> int:
    for name in names:
        if name in counters:
            value = counters[name]
            return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0
    return 0


def _dimension(row: dict[str, Any], name: str) -> str:
    value = row.get(name)
    return value if isinstance(value, str) and value else UNKNOWN


def _tally(counts: dict[str, int], name: str, sign: int) -> None:
    value = counts.get(name, 0) + sign
    if value > 0:
        counts[name] = value
    else:
        counts.pop(name, None)


def _rewrite(path: Path, content: str) -> None:
    if not path.exists():
        return
    temporary = path.with_name(path.name + ".prune")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(descriptor, content.encode("utf-8"))
    finally:
        os.close(descriptor)
    os.replace(temporary, path)


def _atomic_write(path: Path, content: str) -> None:
    """Replace the sidecar in one step: a reader never sees a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(descriptor, content.encode("utf-8"))
    finally:
        os.close(descriptor)
    os.replace(temporary, path)


def _sanitize_row(row: dict[str, Any]) -> dict[str, Any]:
    """Strip arbitrary legacy/provider detail while preserving safe metadata."""
    clean = {key: value for key, value in row.items() if key in FIELDS}
    attempts = clean.get("attempts")
    if isinstance(attempts, list):
        clean_attempts = []
        for item in attempts:
            if not isinstance(item, dict):
                continue
            safe = {key: item[key] for key in ("provider", "model", "outcome", "error_class") if key in item and isinstance(item[key], (str, type(None)))}
            if "reason" in item:
                from gateway.adapters.base import CLASS_MESSAGES
                from gateway.config import ERROR_UNKNOWN
                error_class = item.get("error_class", ERROR_UNKNOWN)
                if not isinstance(error_class, str):
                    error_class = ERROR_UNKNOWN
                safe["reason"] = CLASS_MESSAGES.get(error_class, CLASS_MESSAGES[ERROR_UNKNOWN])
            clean_attempts.append(safe)
        clean["attempts"] = clean_attempts
    return clean


def _parse_timestamp(value: Any) -> datetime | None:
    """An aware UTC timestamp, or None when the value cannot be placed in time.

    A naive timestamp is read as UTC because that is what the ledger writes
    (``utc_now_iso``); every row the gateway produces carries the ``Z`` form.
    """
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _iso(epoch: float | None) -> str | None:
    if epoch is None:
        return None
    return _iso_datetime(datetime.fromtimestamp(epoch, UTC))


def _iso_datetime(value: datetime) -> str:
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _timestamp(value: Any) -> float:
    parsed = _parse_timestamp(value)
    return parsed.timestamp() if parsed else 0


if __name__ == "__main__":
    raise SystemExit(main())
