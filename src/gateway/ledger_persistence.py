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
        now = time.time()
        files = [self.path.with_suffix(self.path.suffix + f".{i}") for i in range(MAX_ROTATIONS, 0, -1)] + [self.path]
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
        encoded = (json.dumps(_sanitize_row(row), separators=(",", ":"), ensure_ascii=True) + "\n").encode()
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


def _timestamp(value: Any) -> float:
    if not isinstance(value, str):
        return 0
    try:
        from datetime import datetime
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0
