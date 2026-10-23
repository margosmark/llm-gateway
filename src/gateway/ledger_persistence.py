"""Asynchronous, bounded, metadata-only request ledger persistence."""
from __future__ import annotations

import json
import os
import queue
import threading
import time
from pathlib import Path
from typing import Any

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


class LedgerPersistence:
    """Queue writes off the caller; newline JSON is append-only and rotated."""

    def __init__(self, path: Path, capacity: int = 20000) -> None:
        self.path = Path(path)
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

    def load(self, limit: int) -> list[dict[str, Any]]:
        rows: dict[str, dict[str, Any]] = {}
        for file in [self.path.with_suffix(self.path.suffix + f".{i}") for i in range(MAX_ROTATIONS, 0, -1)] + [self.path]:
            try:
                with file.open(encoding="utf-8") as source:
                    for line in source:
                        try:
                            row = json.loads(line)
                            if time.time() - _timestamp(row.get("timestamp")) > RETENTION_SECONDS:
                                continue
                            if set(row) <= FIELDS and isinstance(row.get("request_id"), str):
                                rows[row["request_id"]] = row
                        except (ValueError, TypeError):
                            continue
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
        return self._queue.unfinished_tasks == 0

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

    def _append(self, row: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        encoded = (json.dumps(row, separators=(",", ":"), ensure_ascii=True) + "\n").encode()
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


def _timestamp(value: Any) -> float:
    if not isinstance(value, str):
        return 0
    try:
        from datetime import datetime
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0
