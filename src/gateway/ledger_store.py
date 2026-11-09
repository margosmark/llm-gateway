"""SQLite store for the durable request ledger: metadata only, age-bounded.

The store is the *source of record* for request history. One row per durable
ledger line (a recorded outcome, and an amendment as a later line for the
same request id - later line wins at read time, exactly like the JSONL
append log it replaces at boot).

Design commitments, each of which is a test:

* **30 days by age, never by size.** The only history bound is
  ``ts < now - retention`` (``prune(cutoff)``); there is no byte cap and no
  rotation. At the measured write rate (~400 KiB/h, ~5,190 requests/day)
  thirty days is ~156k rows / ~150 MB - far beyond the old 15 MiB JSONL
  cap, which is what made the published 30-day window unreachable.
* **Metadata only.** The store never sees a prompt, a response, a
  credential or any field outside the caller's whitelist: it receives
  pre-sanitised, pre-serialised rows as ``(request_id, ts, stamp,
  error_class, fallback_used, body)``. The schema has no content columns;
  ``body`` is the caller's JSONL line minus its newline. ``error_class`` and
  ``fallback_used` exist only so retention and totals can be answered by SQL
  without parsing 156k JSON bodies - they are the same metadata the
  rollup already counts.
* **WAL, single writer.** The writer thread owns the write connection (the
  only code that writes after boot); readers use a second connection under
  a lock. WAL means readers never block the request path, and the request
  path never touches SQLite at all: rows reach the store from the ledger
  writer queue.
* **0600, like every sibling state artifact.** sqlite creates the db and
  its ``-wal``/``-shm`` sidecars with the process umask (0644 under the
  standard 0022); the state-directory convention is "0600 inside a 0700
  directory" (``docs/RUNBOOK.md``), so every connect re-tightens all three
  files to owner-only.
* **Bounded reads.** ``iter_bodies`` streams (for the boot rebuild),
  ``recent(n)`` returns at most *n* rows (for the in-memory ring), and
  ``totals``/``stats`` are single SQL aggregates - no read path
  materialises the whole history.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

#: Schema version this build writes (``PRAGMA user_version``).
SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger (
    request_id    TEXT NOT NULL,
    ts            INTEGER NOT NULL,
    stamp         TEXT NOT NULL,
    error_class   TEXT,
    fallback_used INTEGER NOT NULL DEFAULT 0,
    body          TEXT NOT NULL
)
"""

_INDEXES = (
    # Age pruning (``DELETE ... WHERE ts < cutoff``) and oldest-row lookup.
    "CREATE INDEX IF NOT EXISTS ledger_by_ts ON ledger(ts)",
    # Latest-row-per-request and totals: groups and MAX(rowid) per id.
    "CREATE INDEX IF NOT EXISTS ledger_by_request ON ledger(request_id)",
)


class LedgerStoreError(RuntimeError):
    """Raised when the SQLite store cannot be opened or written."""


def _tighten_permissions(path: Path) -> None:
    """Force the store and its WAL sidecars to owner-only (0600).

    sqlite creates every file it owns with the process umask - 0644 under
    the standard 0022 - while each sibling state artifact (journal,
    rotations, rollup sidecar) is created explicitly 0600 and the documented
    convention is "0600 inside a 0700 directory" (``docs/RUNBOOK.md``).
    Called after every connect so a sidecar a checkpoint recreated keeps the
    same mode. A file sqlite removes between the call and the chmod is not
    an error; any other failure propagates, so the store fails closed rather
    than world-readable.
    """
    for candidate in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
        try:
            os.chmod(candidate, 0o600)
        except FileNotFoundError:  # pragma: no cover - sidecar raced away
            continue


def _connect(path: Path) -> sqlite3.Connection:
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            str(path),
            timeout=5.0,
            # Both connections are used from more than one thread over their
            # lifetime (boot on the main thread, then the writer or an HTTP
            # reader thread); every use is serialised by the locks below.
            check_same_thread=False,
            isolation_level=None,  # autocommit; transactions are explicit
        )
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA busy_timeout=5000")
        _tighten_permissions(path)
        return connection
    except (sqlite3.Error, OSError) as exc:
        if connection is not None:
            try:
                connection.close()
            except sqlite3.Error:  # pragma: no cover - best effort
                pass
        raise LedgerStoreError(f"cannot open ledger store {path}: {exc}") from exc


class LedgerStore:
    """A single SQLite file holding the durable request ledger."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._write_lock = threading.Lock()
        self._read_lock = threading.Lock()
        self._write_conn: sqlite3.Connection | None = None
        self._read_conn: sqlite3.Connection | None = None
        #: Last append failure, surfaced through durability() so a failing
        #: store is never a silent hole in the history.
        self.last_error: str | None = None

    # --- lifecycle ------------------------------------------------------

    def open(self) -> None:
        """Create the file, schema and indexes if needed. Idempotent."""
        if self._write_conn is not None:
            return
        connection: sqlite3.Connection | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = _connect(self.path)
            connection.execute(_SCHEMA)
            for statement in _INDEXES:
                connection.execute(statement)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            # The schema write is what creates the -wal/-shm sidecars, so
            # re-tighten after it (F4: sqlite leaves them at the umask's
            # 0644 until this).
            _tighten_permissions(self.path)
        except (LedgerStoreError, OSError, sqlite3.Error) as exc:
            if connection is not None:
                try:
                    connection.close()
                except sqlite3.Error:  # pragma: no cover - best effort
                    pass
            raise LedgerStoreError(f"cannot initialise ledger store {self.path}: {exc}") from exc
        self._write_conn = connection

    def close(self) -> None:
        with self._write_lock:
            if self._write_conn is not None:
                try:
                    self._write_conn.close()
                except sqlite3.Error:  # pragma: no cover - best effort
                    pass
                self._write_conn = None
        with self._read_lock:
            if self._read_conn is not None:
                try:
                    self._read_conn.close()
                except sqlite3.Error:  # pragma: no cover - best effort
                    pass
                self._read_conn = None

    def _writer(self) -> sqlite3.Connection:
        if self._write_conn is None:
            raise LedgerStoreError("ledger store is not open")
        return self._write_conn

    def _reader(self) -> sqlite3.Connection:
        if self._read_conn is None:
            self._read_conn = _connect(self.path)
        return self._read_conn

    # --- writing --------------------------------------------------------

    def empty(self) -> bool:
        """True when no row has ever been committed - the migration gate."""
        with self._read_lock:
            try:
                return self._reader().execute("SELECT 1 FROM ledger LIMIT 1").fetchone() is None
            except sqlite3.Error as exc:
                raise LedgerStoreError(f"ledger store unreadable: {exc}") from exc

    def append(self, *, request_id: str, ts: int, stamp: str,
               error_class: str | None, fallback_used: int, body: str) -> int:
        """Append one already-sanitised row; returns its rowid.

        The rowid is the rollup watermark cursor: the writer folds the row
        into the rollup and then records this id, so the sidecar can state
        "every line up to here is aggregated" and the next boot replays only
        what came after. Raises LedgerStoreError on failure.
        """
        with self._write_lock:
            try:
                cursor = self._writer().execute(
                    "INSERT INTO ledger (request_id, ts, stamp, error_class, fallback_used, body)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (request_id, ts, stamp, error_class, fallback_used, body),
                )
                rowid = int(cursor.lastrowid or 0)
            except (sqlite3.Error, ValueError) as exc:
                # ValueError covers UnicodeEncodeError (a lone surrogate in
                # a TEXT param): anything non-SQLite that would otherwise
                # escape the writer must still surface as a visible
                # LedgerStoreError with last_error set, never as an
                # exception that kills the writer thread (F2).
                self.last_error = f"{type(exc).__name__}: {exc}"
                raise LedgerStoreError(self.last_error) from exc
            self.last_error = None
            return rowid

    def max_rowid(self) -> int:
        """The newest rowid ever committed (0 when the store has no rows).

        Monotonic: deleting aged rows never lowers it, so it stays a usable
        watermark even after a prune empties the store.
        """
        with self._read_lock:
            try:
                value = self._reader().execute(
                    "SELECT COALESCE(MAX(rowid), 0) FROM ledger").fetchone()
            except sqlite3.Error as exc:
                raise LedgerStoreError(f"ledger store unreadable: {exc}") from exc
        return int(value[0]) if value else 0

    def newest_page(self, upper_rowid: int, page: int = 5000) -> list[tuple[int, str, str]]:
        """One page of ``(rowid, request_id, body)``, newest-first, ``rowid <= upper_rowid``.

        A plain list (not a cursor generator) so the read lock is never held
        across a caller's ``break``. Paged by rowid so the amendment-index
        seed never materialises the history: the caller stops as soon as it
        has seen enough distinct requests (the index only has to cover the
        ring it can amend), and the ``rowid <= max_rowid`` bound keeps seed
        rows strictly older than the replay window, so a replayed amendment
        can never be mistaken for its own original.
        """
        if upper_rowid <= 0:
            return []
        with self._read_lock:
            try:
                rows = self._reader().execute(
                    "SELECT rowid, request_id, body FROM ledger"
                    " WHERE rowid <= ? ORDER BY rowid DESC LIMIT ?",
                    (int(upper_rowid), int(page)),
                ).fetchall()
            except sqlite3.Error as exc:
                raise LedgerStoreError(f"ledger store unreadable: {exc}") from exc
        return [(int(rowid), str(request_id), str(body)) for rowid, request_id, body in rows]

    def insert_many(self, items: Iterable[tuple[str, int, str, str | None, int, str]]) -> int:
        """Insert migration rows in one transaction. Idempotent only by
        caller discipline (the caller imports exactly when the store is
        empty), so a crash mid-import rolls back to empty and re-runs."""
        rows = list(items)
        if not rows:
            return 0
        with self._write_lock:
            connection = self._writer()
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.executemany(
                    "INSERT INTO ledger (request_id, ts, stamp, error_class, fallback_used, body)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    rows,
                )
                connection.execute("COMMIT")
            except (sqlite3.Error, ValueError) as exc:
                # Same non-SQLite surface as append(): a row whose TEXT
                # cannot encode (lone surrogate) must roll the batch back
                # and raise LedgerStoreError - never escape as the raw
                # UnicodeEncodeError that used to abort first-boot import.
                try:
                    connection.execute("ROLLBACK")
                except (sqlite3.Error, ValueError):  # pragma: no cover - rollback of a failed txn
                    pass
                self.last_error = f"{type(exc).__name__}: {exc}"
                raise LedgerStoreError(self.last_error) from exc
            self.last_error = None
            return len(rows)

    def prune(self, cutoff_ts: int) -> int:
        """Delete rows older than ``cutoff_ts`` (epoch seconds); returns the count.

        This is the *only* history bound: age, applied against the caller's
        retention window. There is no size cap anywhere in this module.
        """
        with self._write_lock:
            try:
                cursor = self._writer().execute("DELETE FROM ledger WHERE ts < ?", (int(cutoff_ts),))
            except (sqlite3.Error, ValueError) as exc:  # ValueError: un-castable cutoff, same visibility rule as append()
                self.last_error = f"{type(exc).__name__}: {exc}"
                raise LedgerStoreError(self.last_error) from exc
            self.last_error = None
            return max(0, cursor.rowcount or 0)

    # --- reading --------------------------------------------------------

    def iter_bodies(self) -> Iterator[str]:
        """Stream every row body in write order (boot rebuild input).

        Lazy on purpose: a 156k-row history is never held in memory - the
        caller consumes one body at a time.
        """
        with self._read_lock:
            try:
                cursor = self._reader().execute("SELECT body FROM ledger ORDER BY rowid")
                for (body,) in cursor:
                    yield body
            except sqlite3.Error as exc:
                raise LedgerStoreError(f"ledger store unreadable: {exc}") from exc

    def recent(self, limit: int) -> list[str]:
        """The newest *limit* distinct requests, oldest first.

        Distinct by request id with the latest line winning (an amendment
        supersedes its original), ordered by first appearance - the same
        window and order the JSONL loader produced. Bounded by ``limit``,
        never by history size.
        """
        if limit < 1:
            return []
        with self._read_lock:
            try:
                connection = self._reader()
                # Newest `limit` distinct requests by first appearance.
                top = connection.execute(
                    "SELECT MIN(rowid) AS first_seen, MAX(rowid) AS latest"
                    " FROM ledger GROUP BY request_id ORDER BY first_seen DESC LIMIT ?",
                    (limit,),
                ).fetchall()
                if not top:
                    return []
                latest_ids = [latest for _first, latest in top]
                bodies: dict[int, str] = {}
                for start in range(0, len(latest_ids), 900):
                    chunk = latest_ids[start:start + 900]
                    placeholders = ",".join("?" * len(chunk))
                    for rowid, body in connection.execute(
                        f"SELECT rowid, body FROM ledger WHERE rowid IN ({placeholders})", chunk
                    ):
                        bodies[rowid] = body
                # Return oldest-first among the newest window (ring order).
                return [bodies[latest] for _first, latest in sorted(top)]
            except sqlite3.Error as exc:
                raise LedgerStoreError(f"ledger store unreadable: {exc}") from exc

    def bodies_between(self, lo_ts: int, hi_ts: int) -> Iterator[str]:
        """Stream row bodies with ``ts`` in ``[lo_ts, hi_ts)``, in write order.

        The bounded read behind the boot-time sidecar reconcile: only the
        retention window's boundary day is re-read (one day's rows at
        most, indexed by ``ledger_by_ts``), never the whole history, so a
        load stays a load.
        """
        with self._read_lock:
            try:
                cursor = self._reader().execute(
                    "SELECT body FROM ledger WHERE ts >= ? AND ts < ? ORDER BY rowid",
                    (int(lo_ts), int(hi_ts)),
                )
                for (body,) in cursor:
                    yield body
            except sqlite3.Error as exc:
                raise LedgerStoreError(f"ledger store unreadable: {exc}") from exc

    def rows_after(self, lower_rowid: int, page: int = 5000) -> list[tuple[int, str]]:
        """One page of ``(rowid, body)`` for rows newer than ``lower_rowid``, oldest-first.

        The boot replay reads the store in these pages: what the sidecar's
        watermark says was never folded (at most the rows written since the
        last sidecar save - normally zero to a few dozen). Paged rather
        than unbounded so even a sidecar that went stale for a day is
        replayed with bounded memory.
        """
        with self._read_lock:
            try:
                rows = self._reader().execute(
                    "SELECT rowid, body FROM ledger WHERE rowid > ?"
                    " ORDER BY rowid ASC LIMIT ?",
                    (int(lower_rowid), int(page)),
                ).fetchall()
            except sqlite3.Error as exc:
                raise LedgerStoreError(f"ledger store unreadable: {exc}") from exc
        return [(int(rowid), str(body)) for rowid, body in rows]

    def totals(self) -> dict[str, int]:
        """Whole-store request totals over the latest line per request.

        One SQL aggregate over the store: the boot-time replacement for
        counting every restored row in Python (bounded memory, ~ms at
        156k rows). Semantics mirror ``RequestLedger._count``: one request
        counted once, on its latest state.
        """
        with self._read_lock:
            try:
                row = self._reader().execute(
                    "SELECT COUNT(*),"
                    " COALESCE(SUM(CASE WHEN error_class IS NOT NULL AND error_class <> ''"
                    "  THEN 1 ELSE 0 END), 0),"
                    " COALESCE(SUM(CASE WHEN error_class = 'rate_limited' THEN 1 ELSE 0 END), 0),"
                    " COALESCE(SUM(CASE WHEN error_class = 'exhausted' THEN 1 ELSE 0 END), 0),"
                    " COALESCE(SUM(CASE WHEN fallback_used = 1 THEN 1 ELSE 0 END), 0)"
                    " FROM ledger WHERE rowid = (SELECT MAX(rowid) FROM ledger l2"
                    "  WHERE l2.request_id = ledger.request_id)"
                ).fetchone()
            except sqlite3.Error as exc:
                raise LedgerStoreError(f"ledger store unreadable: {exc}") from exc
        requests, errors, rate_limited, exhausted, fallbacks = (value or 0 for value in row)
        return {
            "requests": int(requests),
            "errors": int(errors),
            "rate_limited": int(rate_limited),
            "exhausted": int(exhausted),
            "fallbacks": int(fallbacks),
        }

    def stats(self) -> dict[str, Any]:
        """Store size, row count and oldest row - the observability block.

        ``rows`` counts durable lines (an amended request contributes two);
        ``requests`` counts distinct request ids (what the rollup counts).
        ``bytes`` is the on-disk footprint: db plus its WAL sidecars.
        """
        with self._read_lock:
            try:
                connection = self._reader()
                rows = int(connection.execute("SELECT COUNT(*) FROM ledger").fetchone()[0])
                requests = int(connection.execute(
                    "SELECT COUNT(DISTINCT request_id) FROM ledger").fetchone()[0])
                oldest = connection.execute(
                    "SELECT stamp FROM ledger ORDER BY ts, rowid LIMIT 1").fetchone()
            except sqlite3.Error as exc:
                raise LedgerStoreError(f"ledger store unreadable: {exc}") from exc
        size = 0
        for suffix in ("", "-wal", "-shm"):
            try:
                size += self.path.with_name(self.path.name + suffix).stat().st_size
            except OSError:
                continue
        return {
            "path": str(self.path),
            "bytes": size,
            "rows": rows,
            "requests": requests,
            "oldest_row_ts": oldest[0] if oldest else None,
        }
