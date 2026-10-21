"""Optional, self-contained periodic config snapshots into the state volume."""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

from gateway.backup import snapshot_interval_seconds
from gateway.config import ConfigStore
from gateway.stores import atomic_write_json


class SnapshotWorker:
    """Daemon worker; interval zero disables writes and settings apply live."""

    def __init__(self, config: ConfigStore, interval_seconds: int = 0) -> None:
        self.config = config
        self.interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_snapshot = time.monotonic()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="config-snapshot", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def _run(self) -> None:
        while not self._stop.wait(1):
            interval = snapshot_interval_seconds(self.config.snapshot())
            if interval <= 0:
                self._last_snapshot = time.monotonic()
                continue
            now = time.monotonic()
            if now - self._last_snapshot < interval:
                continue
            snapshot_dir = self.config.path.parent / "snapshots"
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            atomic_write_json(snapshot_dir / f"config-{stamp}.json", self.config.snapshot().to_dict())
            self._last_snapshot = now
