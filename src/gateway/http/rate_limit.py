"""Process-local per-peer rolling request limits.

This is capacity protection, not authentication or access control.
"""

from __future__ import annotations

import math
import threading
import time
from collections import defaultdict, deque

class RateLimiter:
    """In-memory rolling request window keyed by the socket peer IP."""

    def __init__(self, limit: int, window_seconds: float) -> None:
        if limit < 1 or not math.isfinite(window_seconds) or window_seconds <= 0:
            raise ValueError("rate limit and window must be positive")
        self.limit = limit
        self.window_seconds = window_seconds
        self._requests: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, client_ip: str) -> bool:
        now = time.monotonic()
        cutoff = now - self.window_seconds
        with self._lock:
            requests = self._requests[client_ip]
            while requests and requests[0] <= cutoff:
                requests.popleft()
            if not requests:
                self._requests.pop(client_ip, None)
                requests = self._requests[client_ip]
            # Expire idle peer keys too; otherwise a stream of source IPs could
            # grow this process-local limiter without bound.
            for peer in tuple(self._requests):
                if peer != client_ip and self._requests[peer] and self._requests[peer][-1] <= cutoff:
                    del self._requests[peer]
            if len(requests) >= self.limit:
                return False
            requests.append(now)
            return True
