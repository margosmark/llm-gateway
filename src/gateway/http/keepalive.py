"""Emit gateway-owned SSE comments while an upstream stream is idle."""

from __future__ import annotations

import queue
import threading
from collections.abc import Iterator

KEEPALIVE_INTERVAL_SECONDS = 15.0
_SSE_PING = b": ping\n\n"
_END = object()


def with_sse_keepalives(
    chunks: Iterator[bytes], interval: float = KEEPALIVE_INTERVAL_SECONDS
) -> Iterator[bytes]:
    """Yield source chunks and SSE pings during intervals without source output.

    A daemon producer lets the response writer's timed queue wait progress even
    when the upstream iterator is blocked waiting for its next frame. Pings are
    generated here, outside the upstream frame/conversion pipeline.
    """
    if interval <= 0:
        raise ValueError("keepalive interval must be positive")

    pending: queue.Queue[bytes | object] = queue.Queue(maxsize=1)
    stopped = threading.Event()

    def publish(item: bytes | object) -> bool:
        while not stopped.is_set():
            try:
                pending.put(item, timeout=0.1)
                return True
            except queue.Full:
                continue
        return False

    def produce() -> None:
        try:
            for chunk in chunks:
                if not publish(chunk):
                    return
        finally:
            publish(_END)

    threading.Thread(target=produce, name="sse-keepalive-source", daemon=True).start()
    try:
        while True:
            try:
                item = pending.get(timeout=interval)
            except queue.Empty:
                yield _SSE_PING
                continue
            if item is _END:
                return
            yield item  # type: ignore[misc]
    finally:
        stopped.set()
