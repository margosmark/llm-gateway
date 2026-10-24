"""Asynchronous, metadata-only request ledger persistence over two stores.

Three durable artifacts share the state directory:

``request-ledger.db``
    The **store of record**: SQLite (WAL), one row per durable ledger line,
    on the persistent volume beside the journal. This is what makes the
    published retention window real - rows leave only when they are older
    than :data:`RETENTION_SECONDS` (``LedgerStore.prune``), never because a
    file grew. At the measured write rate (~400 KiB/h, ~5,190 requests/day)
    thirty days is ~156k rows / ~150 MB; the previous JSONL design dropped
    the oldest 5 MiB rotation after ~1.3 days, so its 30-day claim was
    unreachable by more than an order of magnitude. Boot rebuilds, ring
    restore, totals and the status figures all read this store (streaming or
    as single SQL aggregates - a boot never materialises the history).

``request-ledger.jsonl``
    The **plain-text journal tail** with the original mechanics: append-only
    newline JSON, rotated at 5 MiB with two rotations (:data:`MAX_BYTES` x
    (``MAX_ROTATIONS`` + 1) = 15 MiB maximum). It is *not* the history bound
    anymore - every row reaches the store first, so rotation only drops the
    tail's copy. It remains the human-readable tail on the volume and the
    migration source: the first boot over a legacy journal imports it into
    the store without losing a row (6,673 requests / 12,795 lines measured
    at migration time).

``request-ledger-rollup.json``
    The daily rollup sidecar maintained from the *same* append path: one row
    per UTC day x alias x provider x model carrying requests, input, output
    and cached tokens, fallbacks broken out by the closed fallback-reason
    vocabulary (folded at ingest - see :mod:`gateway.fallback_reasons`, so
    raw per-second sentences never become sidecar keys), errors by
    ``error_class`` and a status breakdown, plus a ``coverage`` block that
    states exactly what the rollup covers. The store is the source of
    truth; :func:`rebuild_rollups` regenerates the sidecar from it (falling
    back to a legacy journal when the store is empty), and the store is
    replayed into the rollup on every start so aggregated history never
    begins at deploy time.

Growth bounds are deliberate and visible in ``coverage``: the request index
is count-capped at :data:`INDEX_MAX_ENTRIES` entries (oldest-by-last-touch
eviction, evicted ids remembered for a late amendment), and the durable
history is bounded by the 30-day age rule alone - no byte cap, no rotation.
Both are stated rather than implied.

Both stores are metadata only. Neither holds a prompt, a response, a
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

from gateway.bridge.streams import usable_token_count
from gateway.costs import INVALID_USAGE_MARKER
from gateway.fallback_reasons import fallback_reason_class
from gateway.ledger_store import LedgerStore, LedgerStoreError

RETENTION_SECONDS = 30 * 24 * 60 * 60
#: Journal bounds. These bound the *plain-text tail only* - they no longer
#: bound history, which lives in the SQLite store and ages out by time.
MAX_BYTES = 5 * 1024 * 1024
MAX_ROTATIONS = 2
#: How often the writer thread re-applies the 30-day age rule to the store
#: while running (an appliance's uptime is unbounded, so waiting for the
#: next boot would let history grow past its window). Boot always prunes.
PRUNE_INTERVAL_SECONDS = 10 * 60
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
#: most this much incremental state. The next start rebuilds from the store,
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
#: writer queue 20_000, journal 5 MiB x 3); this is the only runtime one
#: that was not.
#:
#: 50_000 was chosen so that:
#:
#: * a typical boot never evicts - the measured durable history at the
#:   measured rate (~5,190 requests/day) is ~156k rows over 30 days, so a
#:   long-running appliance DOES evict index entries at boot; that is the
#:   designed behaviour (the counted contribution stays in its bucket and
#:   ``coverage`` reports ``index_evicted``), not a loss;
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


def store_path_for(ledger_path: Path | str) -> Path:
    """The SQLite store path for a ledger path: ``request-ledger.jsonl`` -> ``request-ledger.db``."""
    return Path(ledger_path).with_suffix(".db")


def rollup_path_for(ledger_path: Path) -> Path:
    """The sidecar path for a ledger path: ``request-ledger.jsonl`` -> ``request-ledger-rollup.json``."""
    path = Path(ledger_path)
    return path.with_name(path.stem + "-rollup.json")


def _ledger_files(path: Path) -> list[Path]:
    """Every journal file, oldest first, so a later line supersedes an earlier one."""
    return [path.with_suffix(path.suffix + f".{i}") for i in range(MAX_ROTATIONS, 0, -1)] + [path]


class RollupStore:
    """The daily rollup: aggregated, metadata-only, and rebuildable from the store.

    Rows are folded in per request identifier, so an amendment (a later line
    for the same request) *replaces* the earlier contribution instead of
    being counted twice - the same rule the store's own reader applies.
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
        self._rebuild: dict[str, Any] = {
            "status": "pending", "trigger": "", "at": None, "lines_read": 0, "mode": "full",
        }
        #: Per-UTC-day [oldest, newest] epoch bounds, kept beside the buckets
        #: (at most one entry per retained day). The index is count-capped,
        #: so deriving ``first_row_ts``/``last_row_ts`` from it would shrink
        #: the stated window to whatever the cap still holds - these bounds
        #: survive index eviction and answer the honesty question from the
        #: days the rollup actually still covers.
        self._day_bounds: dict[str, list[float]] = {}
        #: Store rowid up to which every line is folded into this rollup.
        #: Persisted in the sidecar; the next boot replays only what came
        #: after, which is what makes boot a load instead of a full rebuild.
        self._watermark = 0
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
            bounds = self._day_bounds.setdefault(key[0], [epoch, epoch])
            if epoch < bounds[0]:
                bounds[0] = epoch
            if epoch > bounds[1]:
                bounds[1] = epoch
            self._index[request_id] = (key, delta, epoch)
            self._evict_overflow()
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
        """Regenerate the whole rollup from legacy JSONL journal files.

        Idempotent: the previous state is discarded first, so re-running it
        over the same ledger always produces the same rollup. Kept for the
        CLI and for legacy journals the store has not consumed yet.
        """
        with self._lock:
            self._reset_locked()
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
                            self._ingest_for_rebuild(row, now)
                except FileNotFoundError:
                    continue
                except (OSError, ValueError):
                    # A ledger file we could not read to the end - missing
                    # mid-read, unreadable, or not valid UTF-8 - is a real
                    # gap in the rollup, and the coverage block has to say so
                    # rather than presenting the rows we did read as complete.
                    status = "partial"
            self._finish_rebuild_locked(status, trigger, lines_read, mode="full")
            return self._snapshot_locked()

    def rebuild_from_store(
        self,
        bodies: Iterable[str],
        *,
        trigger: str = "manual",
        skipped: int = 0,
        expired: int = 0,
        status: str = "complete",
        mode: str = "full",
    ) -> dict[str, Any]:
        """Regenerate the rollup from the durable store's row bodies.

        Streams: one body at a time, so a 156k-row history is aggregated
        without ever being held in memory. ``lines_read`` counts store rows
        scanned. ``skipped``/``expired`` carry what the boot migration and
        age-prune already established (those rows never became - or no
        longer are - store rows), so coverage reports them truthfully for
        this rebuild instead of losing them between paths.
        """
        with self._lock:
            self._reset_locked()
            self._rows_skipped += int(skipped)
            self._rows_expired += int(expired)
            lines_read = 0
            now = time.time()
            for body in bodies:
                lines_read += 1
                try:
                    row = json.loads(body)
                except ValueError:
                    # Store bodies are sanitised JSON at write time, so this
                    # is corruption, not a legacy row - and it must be said.
                    self._rows_skipped += 1
                    continue
                self._ingest_for_rebuild(row, now)
            self._finish_rebuild_locked(status, trigger, lines_read, mode=mode)
            return self._snapshot_locked()

    def note_source(self, *, skipped: int = 0, expired: int = 0, partial: bool = False) -> None:
        """Fold source facts found after the rebuild into coverage.

        Used by the runtime age-prune (rows leaving the store while the
        process runs) so ``rows_expired`` keeps counting them instead of
        silently shrinking the window.
        """
        if not (skipped or expired or partial):
            return
        with self._lock:
            self._rows_skipped += int(skipped)
            self._rows_expired += int(expired)
            if partial:
                self._rebuild["status"] = "partial"

    def _reset_locked(self) -> None:
        """Drop all folded state so a rebuild starts from nothing."""
        self._rows.clear()
        self._index.clear()
        # The store replay re-establishes every amendment relationship,
        # so the eviction bookkeeping starts over with it.
        self._evicted_ids.clear()
        self._index_evicted = 0
        self._index_amendments_dropped = 0
        self._rows_skipped = 0
        self._rows_expired = 0
        self._rows_superseded = 0
        self._rows_appended = 0
        self._day_bounds.clear()
        # A rebuilt-from-journal sidecar has no store coverage: clearing the
        # watermark makes the next boot reject it for the load path and do a
        # full rebuild instead of trusting a claim it cannot back.
        self._watermark = 0

    def _ingest_for_rebuild(self, row: Any, now: float) -> None:
        """One source row during a rebuild: age-check first, then fold."""
        stamp = _parse_timestamp(row.get("timestamp")) if isinstance(row, dict) else None
        if stamp is not None and now - stamp.timestamp() > RETENTION_SECONDS:
            # Past the declared retention window: expected, and not a
            # coverage gap.
            self._rows_expired += 1
            return
        self.ingest(row)

    def _finish_rebuild_locked(self, status: str, trigger: str, lines_read: int,
                               *, mode: str = "full") -> None:
        """Close a (re)build pass. ``mode`` says how the rollup got here:
        ``full`` read every store/journal line; ``incremental`` loaded the
        sidecar and replayed only the rows past its watermark. Both are
        honest statements of what happened, so a reader can tell a load
        from a rebuild instead of assuming every boot re-read history.
        """
        self._rebuild = {
            "status": status,
            "trigger": trigger,
            "at": self._iso_now(),
            "lines_read": lines_read,
            "mode": mode,
        }

    # --- boot-time load (sidecar + watermark) ---------------------------

    @property
    def watermark(self) -> int:
        """Store rowid up to which this rollup has folded every line."""
        with self._lock:
            return self._watermark

    def note_watermark(self, rowid: int) -> None:
        """Record that every store line up to ``rowid`` is folded in.

        Called only *after* the fold, never before: a watermark that
        overstates what was aggregated would silently drop rows from the
        next boot's replay, while one that lags just replays a row the
        fold already counted - and the fold is driven by one writer in one
        order, so lagging is the only direction a race can push it.
        """
        if rowid <= 0:
            return
        with self._lock:
            if rowid > self._watermark:
                self._watermark = int(rowid)

    def load_sidecar(self) -> bool:
        """Restore buckets, window bounds and counters from the sidecar.

        Returns False, touching nothing, for anything untrustworthy: a
        missing file, an unreadable or foreign schema, a missing/invalid
        watermark, or malformed rows. The caller then falls back to a full
        rebuild - a slow boot is acceptable, a wrong rollup is not.
        """
        try:
            with self.path.open(encoding="utf-8") as source:
                payload = json.load(source)
            if not isinstance(payload, dict) or payload.get("schema") != ROLLUP_SCHEMA:
                return False
            raw_bounds = payload.get("day_bounds")
            rows = payload.get("rows")
            coverage = payload.get("coverage")
            rebuild = coverage.get("rebuild") if isinstance(coverage, dict) else None
            if not isinstance(raw_bounds, dict) or not isinstance(rows, list) or not isinstance(rebuild, dict):
                return False
            watermark = rebuild.get("store_watermark")
            if not isinstance(watermark, int) or isinstance(watermark, bool) or watermark < 1:
                # 0 means "no store line was ever folded" - the signature a
                # journal-only rebuild leaves behind. Such a sidecar may be
                # years of correct journal rows and still must not be
                # trusted against a store it never read.
                return False
            buckets: dict[DimensionKey, dict[str, Any]] = {}
            for entry in rows:
                if not isinstance(entry, dict):
                    return False
                try:
                    key = (entry["day"], entry["alias"], entry["provider"], entry["model"])
                except (KeyError, TypeError):
                    return False
                bucket = _empty_bucket()
                for name in bucket:
                    if name in entry:
                        bucket[name] = entry[name]
                buckets[key] = bucket
            day_bounds: dict[str, list[float]] = {}
            for day, bounds in raw_bounds.items():
                if (not isinstance(day, str) or not isinstance(bounds, list) or len(bounds) != 2
                        or not all(isinstance(value, (int, float)) and not isinstance(value, bool)
                                   for value in bounds)):
                    return False
                day_bounds[day] = [float(bounds[0]), float(bounds[1])]
        except (OSError, ValueError):
            return False
        with self._lock:
            self._rows = buckets
            self._day_bounds = day_bounds
            # The amendment index is rebuilt from the store's newest lines
            # (seed_index) rather than persisted: it is derived state, and
            # persisting it would double the sidecar's size to save one
            # bounded query.
            self._index.clear()
            self._evicted_ids.clear()
            self._index_evicted = int(coverage.get("index_evicted") or 0)
            self._index_amendments_dropped = int(coverage.get("index_amendments_dropped") or 0)
            self._rows_skipped = int(coverage.get("rows_skipped") or 0)
            self._rows_expired = int(coverage.get("rows_expired") or 0)
            self._rows_superseded = int(coverage.get("rows_superseded") or 0)
            self._rows_appended = int(coverage.get("rows_appended") or 0)
            self._rebuild = {name: rebuild.get(name)
                             for name in ("status", "trigger", "at", "lines_read", "mode")}
            self._watermark = watermark
            self._pending = 0
            self._last_save = time.monotonic()
        return True

    def seed_index(self, store: LedgerStore, *, watermark: int, target: int) -> int:
        """Rebuild the amendment index from the newest store lines at or
        below ``watermark``, until ``target`` distinct requests are known.

        Why this exists: boot loads the sidecar instead of re-reading
        history, so the index - which maps a request id to its folded
        contribution so a later line supersedes instead of double-counting
        - would start empty, and a runtime amendment of a pre-boot request
        would count as a brand-new request. ``target`` is twice the ring
        capacity: ``RequestLedger.amend`` only succeeds while the event is
        still in the ring, so the ring's newest ids are always among the
        newest ``2 x capacity`` distinct ids this seeds. Rows are paged
        newest-first and inserted oldest-first so eviction order stays
        correct, and the ``<= watermark`` bound keeps replay rows out of
        the seed - a replayed amendment must find its ORIGINAL here rather
        than index itself as its own predecessor.
        """
        if target < 1 or watermark <= 0:
            return 0
        upper = int(watermark)
        collected: list[tuple[str, str]] = []
        seen: set[str] = set()
        lines_read = 0
        # Bound the scan even if amend lines somehow outnumber records.
        scan_cap = target * 10 + INDEX_MAX_ENTRIES
        while upper > 0 and len(seen) < target and lines_read < scan_cap:
            page = store.newest_page(upper, page=5000)
            if not page:
                break
            for rowid, request_id, body in page:
                lines_read += 1
                if request_id in seen:
                    continue
                seen.add(request_id)
                collected.append((request_id, body))
                if len(seen) >= target:
                    break
            upper = page[-1][0] - 1
        seeded = 0
        for request_id, body in reversed(collected):
            try:
                row = json.loads(body)
            except ValueError:
                continue
            if not isinstance(row, dict) or set(row) - FIELDS:
                continue
            stamp = _parse_timestamp(row.get("timestamp"))
            if stamp is None:
                continue
            epoch = stamp.timestamp()
            key = (
                stamp.strftime("%Y-%m-%d"),
                _dimension(row, "requested_alias"),
                _dimension(row, "serving_provider"),
                _dimension(row, "serving_model"),
            )
            self._index[request_id] = (key, _row_delta(row), epoch)
            self._evict_overflow()
            seeded += 1
        return seeded

    def finish_boot_load(self, rows_replayed: int) -> None:
        """Close the load path: coverage says this was an incremental boot.

        ``rows_replayed`` is what this boot read past the sidecar watermark -
        zero on a clean boot, the crash-recovery count otherwise. Both
        ``lines_read`` and ``rows_appended`` report it: the rows really were
        read, and they really were added to the rollup beyond what the
        sidecar held. Neither is cumulative across processes; a clean boot
        reports zero for both, exactly as a full rebuild reports zero once
        its sidecar exists.
        """
        with self._lock:
            self._rows_appended = rows_replayed
            self._finish_rebuild_locked("complete", "boot", rows_replayed, mode="incremental")

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
        first = min((bounds[0] for bounds in self._day_bounds.values()), default=None)
        last = max((bounds[1] for bounds in self._day_bounds.values()), default=None)
        rebuild = dict(self._rebuild)
        rebuild["store_watermark"] = self._watermark
        return {
            "schema": ROLLUP_SCHEMA,
            "generated_at": self._iso_now(),
            "source": self.source,
            "coverage": {
                "first_row_ts": _iso(first),
                "last_row_ts": _iso(last),
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
                "rebuild": rebuild,
                "incomplete": bool(self._rows_skipped) or self._rebuild["status"] != "complete",
            },
            "rows": rows,
            "daily": self._daily_locked(),
            # Day -> [oldest, newest] epoch bounds. Top-level (not coverage)
            # because it is load state, not a claim about what was read: the
            # next boot restores the exact window from it instead of
            # re-deriving it from a count-capped index.
            "day_bounds": {day: list(bounds) for day, bounds in sorted(self._day_bounds.items())},
        }

    def _prune_expired(self) -> None:
        """Drop contributions the store itself would already have pruned.

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
        # The window bounds follow the surviving bucket days, not the
        # (count-capped) index: a day leaves only when it ages out (or its
        # last contribution expires), so ``first_row_ts`` stays the oldest
        # day the rollup still covers even after index eviction.
        surviving_days = {key[0] for key in self._rows}
        for day in [day for day in self._day_bounds if day not in surviving_days]:
            self._day_bounds.pop(day, None)

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

    Reads the SQLite store when one exists and holds rows (the store of
    record), and falls back to the JSONL journal when it does not, so a
    legacy volume - or a store that has not been migrated yet - still
    rebuilds. Idempotent and safe to re-run: every call starts from an empty
    rollup and folds the ledger in again, so the result depends only on the
    durable contents. With ``write=False`` nothing touches the filesystem.
    """
    path = Path(ledger_path)
    sidecar = Path(rollup_path) if rollup_path else rollup_path_for(path)
    store_path = store_path_for(path)
    store: LedgerStore | None = None
    if store_path.exists():
        candidate = LedgerStore(store_path)
        try:
            candidate.open()
            if not candidate.empty():
                store = candidate
            else:
                candidate.close()
        except LedgerStoreError:
            store = None
    if store is not None:
        try:
            holder = RollupStore(sidecar, store_path.name, now=now)
            holder.rebuild_from_store(store.iter_bodies(), trigger=trigger, mode="full")
            holder.note_watermark(store.max_rowid())
            payload = holder.save() if write else None
            return payload if payload is not None else holder.snapshot()
        finally:
            store.close()
    store_legacy = RollupStore(sidecar, path.name, now=now)
    store_legacy.rebuild(_ledger_files(path), trigger=trigger)
    payload = store_legacy.save() if write else None
    return payload if payload is not None else store_legacy.snapshot()


class LedgerPersistence:
    """Queue writes off the caller; SQLite store of record + JSONL tail."""

    def __init__(self, path: Path, capacity: int = 20000, *,
                 ring_capacity: int = 10000) -> None:
        self.path = Path(path)
        self.store_path = store_path_for(self.path)
        self.rollup_path = rollup_path_for(self.path)
        #: The ring's capacity, which bounds what a runtime amendment can
        #: name: the boot-time index seed covers ``2 x`` of it, so every
        #: request still amendable after a load-based boot is indexed.
        self._ring_capacity = int(ring_capacity)
        self._store: LedgerStore | None = None
        self._store_open_error: str | None = None
        #: Last failure raised *outside* the store's own catches while the
        #: writer loop was handling a row (journal write, rollup fold, ...).
        #: Kept beside the store's last_error so a writer that stops making
        #: progress is always visible through durability() - never a thread
        #: that died with store.error left None (F2 / AC7).
        self._writer_error: str | None = None
        # Open the store of record first; boot is open -> migrate-if-empty
        # -> age-prune -> rebuild the rollup from the store, all before the
        # writer thread starts, so the sidecar is aggregated immediately and
        # never starts empty on a gateway that has been running before.
        store = LedgerStore(self.store_path)
        try:
            store.open()
            self._store = store
        except (LedgerStoreError, OSError) as exc:
            # A store we cannot open must not stop the gateway: fall back to
            # the journal, and say so through durability() rather than
            # pretending the history is intact.
            self._store_open_error = str(exc)
            store.close()
        source = self.store_path.name if self._store is not None else self.path.name
        self._rollup = RollupStore(self.rollup_path, source)
        self._rollup_boot()

        self._prune_due = time.monotonic() + PRUNE_INTERVAL_SECONDS
        self._queue: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=capacity)
        self._thread = threading.Thread(target=self._run, name="ledger-writer", daemon=True)
        self._thread.start()

    # --- boot -----------------------------------------------------------

    def _rollup_boot(self) -> None:
        """Migrate, prune by age, then build the rollup: load the sidecar
        when it is trustworthy, full-rebuild from the store when it is not.

        The load path (sidecar restore + bounded index seed + watermark
        replay) keeps a boot over a full 30-day / 156k-row history a
        bounded operation - measured at ~0.3 s against ~6 s for a full
        store replay - while the sidecar stays a faithful image of the
        store. The full path runs on first boot, after a journal import,
        or when the sidecar is missing, unreadable, journal-derived, or
        claims more than the store holds. Every failure degrades to the
        path below it with the error recorded for durability(): a store
        that cannot read must never stop the gateway from booting with
        truthful coverage.
        """
        store = self._store
        if store is None:
            self._rollup.rebuild(_ledger_files(self.path), trigger="boot")
            self._rollup.save()
            return
        carry: dict[str, Any] = {"skipped": 0, "expired": 0, "status": "complete"}
        imported = False
        try:
            if store.empty():
                # First boot over a legacy journal (or a fresh directory):
                # consume every journal line into the store, oldest file
                # first so line order - and therefore later-wins amendment
                # order - is preserved. One transaction: a crash re-runs it.
                skipped, import_status = self._import_journal(store)
                carry["skipped"] = skipped
                carry["status"] = import_status
                imported = True
        except LedgerStoreError as exc:
            self._store_open_error = str(exc)
        try:
            if store.empty():
                # Nothing durable yet (fresh directory, or an import that
                # failed and rolled back): the journal read is the honest
                # equivalent, and the next boot retries the import.
                self._rollup.rebuild(_ledger_files(self.path), trigger="boot")
                self._rollup.save()
                return
            # The one retention bound: age. Rows past the window leave the
            # store here (boot) and every PRUNE_INTERVAL_SECONDS while
            # running - never because of a size cap.
            carry["expired"] = store.prune(time.time() - RETENTION_SECONDS)
        except LedgerStoreError as exc:
            # Rows past the window are still excluded (and counted) by the
            # rebuild's own age check below; the prune retries next boot.
            self._store_open_error = str(exc)
            carry["expired"] = 0
        # A fresh import means the store was regenerated underneath whatever
        # sidecar exists: its watermark describes a different store, so the
        # load path must not compare the two histories.
        if not imported and self._try_load_sidecar(store, carry):
            self._rollup.save()
            return
        try:
            max_rowid = store.max_rowid()
            self._rollup.rebuild_from_store(store.iter_bodies(), trigger="boot",
                                            mode="full", **carry)
            self._rollup.note_watermark(max_rowid)
        except LedgerStoreError as exc:
            self._store_open_error = str(exc)
            self._close_store()
            self._store = None
            self._rollup.rebuild(_ledger_files(self.path), trigger="boot")
        self._rollup.save()

    def _try_load_sidecar(self, store: LedgerStore, carry: dict[str, Any]) -> bool:
        """The load path: restore the sidecar, seed the index, replay the gap.

        Returns False whenever anything is off; the caller then runs the
        full rebuild, whose first step resets every part of this state, so
        failing over can never double-fold a row. Order matters: the index
        is seeded from lines at or below the watermark *before* the replay,
        so a replayed amendment finds its original instead of indexing
        itself as its own predecessor.
        """
        try:
            if not self._rollup.load_sidecar():
                return False
            watermark = self._rollup.watermark
            if watermark > store.max_rowid():
                # The sidecar claims lines the store does not have (the
                # store was regenerated underneath it): trust the store.
                return False
            # Bounded to what an amendment can still name: amend() only
            # succeeds while the event is in the ring, and the ring holds
            # `ring_capacity` requests, so the newest
            # `2 x ring_capacity` distinct ids cover every one of them.
            self._rollup.seed_index(
                store, watermark=watermark,
                target=min(2 * self._ring_capacity, INDEX_MAX_ENTRIES),
            )
            replayed = 0
            lower = watermark
            while True:
                page = store.rows_after(lower)
                if not page:
                    break
                now = time.time()
                for rowid, body in page:
                    replayed += 1
                    lower = rowid
                    try:
                        row = json.loads(body)
                    except ValueError:
                        # Store bodies are sanitised JSON at write time, so
                        # this is corruption - count it, never guess.
                        self._rollup.note_source(skipped=1)
                        continue
                    self._rollup._ingest_for_rebuild(row, now)
            if carry["skipped"] or carry["expired"] or carry["status"] != "complete":
                self._rollup.note_source(
                    skipped=carry["skipped"], expired=carry["expired"],
                    partial=carry["status"] != "complete",
                )
            self._rollup.finish_boot_load(replayed)
            return True
        except LedgerStoreError as exc:
            self._store_open_error = str(exc)
            return False

    def _import_journal(self, store: LedgerStore) -> tuple[int, str]:
        """Fold the JSONL journal + rotations into the store. Returns (skipped, status)."""
        skipped = 0
        status = "complete"
        items: list[tuple[int, str, str, str | None, int, str]] = []
        for file in _ledger_files(self.path):
            try:
                with file.open(encoding="utf-8") as source:
                    for line in source:
                        try:
                            row = json.loads(line)
                        except ValueError:
                            skipped += 1
                            continue
                        if not isinstance(row, dict) or set(row) - FIELDS:
                            skipped += 1
                            continue
                        if not isinstance(row.get("request_id"), str):
                            skipped += 1
                            continue
                        stamp = _parse_timestamp(row.get("timestamp"))
                        if stamp is None:
                            skipped += 1
                            continue
                        clean = _sanitize_row(row)
                        body = json.dumps(clean, separators=(",", ":"), ensure_ascii=True)
                        request_id = clean["request_id"]
                        stamp_text = str(clean["timestamp"])
                        error_class = _error_class_for(clean)
                        try:
                            # Every TEXT param must survive UTF-8 encoding
                            # before it enters the batch: sqlite raises
                            # UnicodeEncodeError (not sqlite3.Error) for a
                            # lone surrogate such as a hand-crafted
                            # ``"error_class": "\\ud800"`` line, which used
                            # to escape every boot-level catch and take the
                            # gateway down on first start (F1/AC4: a
                            # malformed line is counted, never fatal).
                            for value in (request_id, stamp_text, error_class, body):
                                if value is not None:
                                    value.encode("utf-8")
                        except UnicodeEncodeError:
                            skipped += 1
                            continue
                        items.append((
                            request_id,
                            int(stamp.timestamp()),
                            stamp_text,
                            error_class,
                            int(bool(clean.get("fallback_used"))),
                            body,
                        ))
            except FileNotFoundError:
                continue
            except (OSError, ValueError):
                # Unreadable or non-UTF-8 journal file: a real gap, reported
                # through coverage exactly as a rebuild would report it.
                status = "partial"
        if items:
            store.insert_many(items)
        return skipped, status

    # --- public surface -------------------------------------------------

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

    def restore(self, limit: int) -> tuple[list[dict[str, Any]], dict[str, int]]:
        """Ring rows plus whole-store totals for a boot.

        The store path returns at most ``limit`` rows (the newest window)
        and gets totals from one SQL aggregate, so a boot over a full
        30-day history never materialises it - the sub-2s state bar (R56)
        and bounded memory both depend on this. Without a store the journal
        read is the bounded legacy fallback (it is at most 15 MiB).
        """
        rows = self.load(limit)
        if self._store is not None:
            try:
                return rows, self._store.totals()
            except LedgerStoreError as exc:
                self._store.last_error = str(exc)
        return rows, _totals_from_rows(rows)

    def load(self, limit: int) -> list[dict[str, Any]]:
        """Rewrite the journal (age + sanitisation) and return ring rows.

        The journal rewrite physically removes expired and unsafe legacy
        rows from the tail files, exactly as before. Row *content* for the
        ring comes from the store when one is open (bounded by ``limit``);
        the journal read remains the fallback.
        """
        journal_rows: dict[str, dict[str, Any]] = {}
        now = time.time()
        files = _ledger_files(self.path)
        unreadable = False
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
                            journal_rows[row["request_id"]] = row
                            retained.append(json.dumps(row, separators=(",", ":"), ensure_ascii=True) + "\n")
                        except (ValueError, TypeError):
                            continue
            except FileNotFoundError:
                continue
            except (OSError, ValueError):
                # A file we could not read to the end - unreadable or not
                # valid UTF-8 - is a real gap, exactly as _import_journal
                # (:1007) and rebuild() (:305) treat it (F7): report it
                # through coverage below, and do NOT rewrite the file -
                # _rewrite would replace it with only the lines read
                # before the failure, silently truncating every line after
                # the bad byte.
                unreadable = True
                continue
            # Rewrite every file, physically removing expired and unsafe legacy rows.
            try:
                _rewrite(file, "".join(retained))
            except FileNotFoundError:
                continue
        if unreadable:
            # The sidecar load path closes coverage as "complete" before
            # restore() runs, so this reader has to state its own gap:
            # the rollup must not claim a complete read over a journal
            # file it never finished.
            self._rollup.note_source(partial=True)
        if self._store is not None:
            rows: list[dict[str, Any]] = []
            try:
                for body in self._store.recent(limit):
                    try:
                        row = json.loads(body)
                    except ValueError:
                        continue
                    if isinstance(row, dict):
                        rows.append(row)
                return rows
            except LedgerStoreError as exc:
                self._store.last_error = str(exc)
        # Fallback: every retained journal row so callers can restore
        # cumulative totals and detect when the bounded in-memory ring is
        # only a partial view. The durable input itself is bounded by
        # MAX_BYTES * (rotations + 1).
        return list(journal_rows.values())

    def durability(self) -> dict[str, Any]:
        """What the owner reads in the status view: retention, store figures, errors.

        ``store`` states the size, row count and oldest row of the store of
        record plus the journal tail's size, so a store that silently stops
        accepting rows (or a history that stops aging out) is visible where
        the owner looks - the numbers must never be a claim the data can't
        back.
        """
        info: dict[str, Any] = {
            "enabled": True,
            "retention_days": RETENTION_SECONDS // (24 * 60 * 60),
            "billing_system": False,
        }
        store_info: dict[str, Any] = {"path": str(self.store_path)}
        if self._store is not None:
            try:
                store_info.update(self._store.stats())
                store_info["path"] = str(self.store_path)
                # Store failures first (more specific), then the writer
                # loop's own last failure: either way a store that has
                # stopped accepting rows is visible here, never None while
                # the writer is failing (F2 / AC7).
                store_info["error"] = self._store.last_error or self._writer_error
            except LedgerStoreError as exc:
                store_info["error"] = str(exc)
        else:
            store_info["error"] = self._store_open_error or self._writer_error or "store unavailable"
        store_info["journal_path"] = str(self.path)
        store_info["journal_bytes"] = self._journal_bytes()
        info["store"] = store_info
        return info

    def _journal_bytes(self) -> int:
        total = 0
        for file in _ledger_files(self.path):
            try:
                total += file.stat().st_size
            except OSError:
                continue
        return total

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
        self._close_store()

    def _close_store(self) -> None:
        if self._store is not None:
            # Closing the last connection checkpoints the WAL into the db.
            self._store.close()

    # --- writer thread --------------------------------------------------

    def _run(self) -> None:
        while True:
            row = self._queue.get()
            try:
                if row is None:
                    return
                try:
                    self._append(row)
                except Exception as exc:  # noqa: BLE001 - a writer crash must be recorded, never silently fatal
                    # Store failures already carry last_error; anything else
                    # (journal write, rollup fold) is recorded here, and the
                    # loop keeps draining so one bad row cannot stop every
                    # later one.
                    self._note_writer_failure(exc)
                else:
                    # A fully successful row clears the record: recovery is
                    # as visible as the failure was.
                    self._writer_error = None
            finally:
                self._queue.task_done()

    def _note_writer_failure(self, exc: BaseException) -> None:
        """Record a writer-loop failure where the owner reads durability()."""
        self._writer_error = f"{type(exc).__name__}: {exc}"

    def _append(self, row: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        clean = _sanitize_row(row)
        body = json.dumps(clean, separators=(",", ":"), ensure_ascii=True)
        # 1) Store of record first. The invariant this ordering buys: any
        #    line in the journal is already in the store (or was refused by
        #    a store error that durability() reports), never the reverse -
        #    so the first-boot import can never miss a journal line.
        rowid = self._store_append(clean, body)
        # 2) The plain-text journal tail, with the original rotation rules.
        encoded = (body + "\n").encode()
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
        # 3) The row is durable now, so fold exactly what was written - no
        #    more, no less - into the rollup, advance the watermark only
        #    after that fold, and refresh the sidecar when due.
        self._rollup.ingest(clean, appended=True)
        if rowid:
            self._rollup.note_watermark(rowid)
        self._rollup.maybe_save()
        # 4) Keep the store inside its 30-day window while running.
        self._maybe_prune_store()

    def _store_append(self, clean: dict[str, Any], body: str) -> int:
        """One store line; its rowid (0 when it could not be written)."""
        if self._store is None:
            return 0
        request_id = clean.get("request_id")
        stamp = _parse_timestamp(clean.get("timestamp"))
        if not isinstance(request_id, str) or stamp is None:
            # Unrepresentable for the store (as for the rollup); the rollup
            # counts it as skipped instead of it vanishing silently.
            return 0
        try:
            return self._store.append(
                request_id=request_id,
                ts=int(stamp.timestamp()),
                stamp=str(clean.get("timestamp")),
                error_class=_error_class_for(clean),
                fallback_used=int(bool(clean.get("fallback_used"))),
                body=body,
            )
        except LedgerStoreError:
            # last_error is recorded on the store and surfaced by
            # durability(); the journal still receives the row below, so a
            # failing store degrades to a visible journal tail rather than
            # silent history loss. The watermark does not advance either,
            # so the next boot replays this row once the store recovers.
            return 0

    def _maybe_prune_store(self) -> None:
        if self._store is None or time.monotonic() < self._prune_due:
            return
        self._prune_due = time.monotonic() + PRUNE_INTERVAL_SECONDS
        try:
            expired = self._store.prune(time.time() - RETENTION_SECONDS)
        except LedgerStoreError:
            return  # last_error recorded; surfaced through durability()
        if expired:
            self._rollup.note_source(expired=expired)


def main(argv: list[str] | None = None) -> int:
    """Rebuild the rollup sidecar from a ledger; exits 1 when coverage is incomplete."""
    parser = argparse.ArgumentParser(
        prog="python3 -m gateway.ledger_persistence",
        description="Rebuild the daily analytics rollup sidecar from the durable request ledger.",
    )
    parser.add_argument("ledger", nargs="?", default=DEFAULT_LEDGER_PATH, help="path to request-ledger.jsonl (the store beside it is used when present)")
    parser.add_argument("--rollup", default=None, help="sidecar path (default: alongside the ledger)")
    parser.add_argument("--dry-run", action="store_true", help="report the rollup without writing it")
    args = parser.parse_args(argv)
    path = Path(args.ledger)
    if not path.exists() and not store_path_for(path).exists():
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
    # A refused counter set (``invalid_usage``) contributes no tokens and is
    # counted among the unpriced requests: its numbers were reported but this
    # gateway will not claim them, and summing the smaller counters that
    # survived would price the refusal as if the refused part were zero. The
    # sidecar vocabulary has no "invalid" bucket, so the row lands in
    # ``requests_without_usage`` - which withholds the amount instead of
    # asserting one.
    refused = counters.get(INVALID_USAGE_MARKER) is True
    usage_reported = not refused and any(name in counters for name in
                                         ("prompt_tokens", "input_tokens", "completion_tokens", "output_tokens"))
    cached_reported = any(name in counters for name in ("cached_read_tokens", "cache_read_input_tokens"))
    fallback = bool(row.get("fallback_used"))
    reason = row.get("fallback_reason")
    error = row.get("error_class")
    status = row.get("status")
    return {
        "requests": 1,
        "input_tokens": 0 if refused else _token(counters, "prompt_tokens", "input_tokens"),
        "output_tokens": 0 if refused else _token(counters, "completion_tokens", "output_tokens"),
        "cached_tokens": 0 if refused else _token(counters, "cached_read_tokens", "cache_read_input_tokens"),
        "cached_tokens_unreported": 0 if refused else (1 if usage_reported and not cached_reported else 0),
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
    """A row's counter under the first name it reported, or 0 when unusable.

    The bound matters here: a provider-controlled 401-digit counter must
    never be summed into a rollup bucket, where it would persist in the
    sidecar and serialise into the console as ``Infinity``.
    """
    for name in names:
        if name in counters:
            return usable_token_count(counters[name]) or 0
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


def _totals_from_rows(rows: Iterable[dict[str, Any]]) -> dict[str, int]:
    """Whole-ledger totals from already-deduplicated rows (journal fallback).

    Mirrors ``RequestLedger._count`` exactly: one request counted once, on
    its latest state (the caller's rows are already latest-per-request).
    """
    totals = {"requests": 0, "errors": 0, "fallbacks": 0, "rate_limited": 0, "exhausted": 0}
    for row in rows:
        totals["requests"] += 1
        error = row.get("error_class")
        if error:
            totals["errors"] += 1
        if error == "rate_limited":
            totals["rate_limited"] += 1
        if error == "exhausted":
            totals["exhausted"] += 1
        if row.get("fallback_used"):
            totals["fallbacks"] += 1
    return totals


def _error_class_for(row: dict[str, Any]) -> str | None:
    """The store's denormalised error_class column: truthy values as text."""
    value = row.get("error_class")
    return str(value) if value else None


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
        # Imports are local so this module stays import-light; they are
        # hoisted above the loop because both branches need them.
        from gateway.adapters.base import CLASS_MESSAGES, sanitize_rejected_fields
        from gateway.config import ERROR_UNKNOWN
        clean_attempts = []
        for item in attempts:
            if not isinstance(item, dict):
                continue
            safe = {key: item[key] for key in ("provider", "model", "outcome", "error_class") if key in item and isinstance(item[key], (str, type(None)))}
            if "reason" in item:
                error_class = item.get("error_class", ERROR_UNKNOWN)
                if not isinstance(error_class, str):
                    error_class = ERROR_UNKNOWN
                # The reason is *derived*, never copied: the fixed class
                # message plus the field names the provider refused. A row
                # written by hand or tampered with on disk cannot smuggle
                # arbitrary text into the ledger through this field.
                reason = CLASS_MESSAGES.get(error_class, CLASS_MESSAGES[ERROR_UNKNOWN])
                fields = sanitize_rejected_fields(item.get("rejected_fields"))
                if fields:
                    reason = f"{reason} (rejected fields: {', '.join(fields)})"
                safe["reason"] = reason
            # Field *names* are the one detail a shape rejection may carry
            # into durable state. They are re-checked here, so a hostile row
            # cannot inject content - only identifier-shaped tokens survive.
            fields = sanitize_rejected_fields(item.get("rejected_fields"))
            if fields:
                safe["rejected_fields"] = list(fields)
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
