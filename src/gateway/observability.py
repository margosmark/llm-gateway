"""Metadata-only observability.

Two stores:

* :class:`RequestLedger` - a bounded ring of request *outcomes*. It records
  which alias was requested, which provider and model actually served it,
  how long it took, and how it ended. It never records a prompt, a response,
  a token, or a credential.
* :class:`LimitStore` - limit snapshots. Every snapshot carries a ``source``
  and a ``confidence`` so the console can distinguish "the provider told us",
  "we estimated it", and "we don't know". An unknown value is stored as
  unknown; it is never stored as zero and never rendered as unlimited.

The redaction helpers exist so that a single rule - "no secret, no prompt,
no response text" - is enforced in one place rather than at each call site.
"""

from __future__ import annotations

import re
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

# Sources for a limit snapshot. Ordered by decreasing authority.
SOURCE_PROVIDER_REPORTED = "provider_reported"
SOURCE_PUBLISHED_ESTIMATE = "published_estimate"
SOURCE_LOCALLY_ESTIMATED = "locally_estimated"
SOURCE_OPERATOR_CONFIGURED = "operator_configured"
SOURCE_UNKNOWN = "unknown"

LIMIT_SOURCES = (
    SOURCE_PROVIDER_REPORTED,
    SOURCE_PUBLISHED_ESTIMATE,
    SOURCE_LOCALLY_ESTIMATED,
    SOURCE_OPERATOR_CONFIGURED,
    SOURCE_UNKNOWN,
)

CONFIDENCE_HIGH = "high"
CONFIDENCE_MEDIUM = "medium"
CONFIDENCE_LOW = "low"
CONFIDENCE_NONE = "none"

DEFAULT_LEDGER_CAPACITY = 10_000


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass
class RequestEvent:
    """One request outcome. Metadata only."""

    request_id: str
    timestamp: str
    requested_alias: str
    requested_model: str | None = None
    serving_provider: str | None = None
    serving_model: str | None = None
    status: int | None = None
    error_class: str | None = None
    latency_ms: int | None = None
    fallback_reason: str | None = None
    fallback_used: bool = False
    streamed: bool = False
    token_counters: dict[str, int] | None = None
    profile_id: str | None = None
    #: Routes that were not eligible, with the reason.
    skipped: list[dict[str, Any]] = field(default_factory=list)
    #: What was attempted, and how each attempt ended. Metadata only.
    attempts: list[dict[str, Any]] = field(default_factory=list)
    #: What was dropped to serve the request on the route that served it.
    degradations: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "timestamp": self.timestamp,
            "requested_alias": self.requested_alias,
            "requested_model": self.requested_model,
            "serving_provider": self.serving_provider,
            "serving_model": self.serving_model,
            "status": self.status,
            "error_class": self.error_class,
            "latency_ms": self.latency_ms,
            "fallback_reason": self.fallback_reason,
            "fallback_used": self.fallback_used,
            "streamed": self.streamed,
            "token_counters": self.token_counters,
            "profile_id": self.profile_id,
            "skipped": list(self.skipped),
            "attempts": list(self.attempts),
            "degradations": list(self.degradations),
        }


class RequestLedger:
    """Bounded in-memory ring of request outcomes."""

    def __init__(self, capacity: int = DEFAULT_LEDGER_CAPACITY) -> None:
        self._events: deque[RequestEvent] = deque(maxlen=capacity)
        self._lock = threading.Lock()
        self._evicted = 0
        self._totals = {"requests": 0, "errors": 0, "fallbacks": 0, "rate_limited": 0, "exhausted": 0}

    def record(self, event: RequestEvent) -> None:
        with self._lock:
            if len(self._events) == self._events.maxlen:
                self._evicted += 1
            self._events.append(event)
            self._totals["requests"] += 1
            if event.error_class:
                self._totals["errors"] += 1
            if event.error_class == "rate_limited":
                self._totals["rate_limited"] += 1
            if event.error_class == "exhausted":
                self._totals["exhausted"] += 1
            if event.fallback_used:
                self._totals["fallbacks"] += 1

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            items = list(self._events)[-limit:]
        return [event.to_dict() for event in reversed(items)]

    def totals(self) -> dict[str, int]:
        with self._lock:
            return dict(self._totals)

    @property
    def evicted(self) -> int:
        with self._lock:
            return self._evicted

    def clear(self) -> None:
        with self._lock:
            self._events.clear()
            self._evicted = 0
            for key in self._totals:
                self._totals[key] = 0


@dataclass
class LimitSnapshot:
    """One observation about a limit, with its provenance."""

    provider: str
    window: str
    value: float | None = None
    limit: float | None = None
    reset_at: str | None = None
    observed_at: str = field(default_factory=utc_now_iso)
    source: str = SOURCE_UNKNOWN
    confidence: str = CONFIDENCE_NONE
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "window": self.window,
            "value": self.value,
            "limit": self.limit,
            "reset_at": self.reset_at,
            "observed_at": self.observed_at,
            "source": self.source,
            "confidence": self.confidence,
            "note": self.note,
        }

    @classmethod
    def unknown(cls, provider: str, window: str, note: str | None = None) -> "LimitSnapshot":
        """An explicitly unknown observation.

        Used when a provider offers no machine-readable limits. The value
        stays ``None`` - never zero - and the source says ``unknown``.
        """
        return cls(
            provider=provider,
            window=window,
            value=None,
            limit=None,
            reset_at=None,
            source=SOURCE_UNKNOWN,
            confidence=CONFIDENCE_NONE,
            note=note,
        )


class LimitStore:
    """The latest limit snapshot per (provider, window)."""

    def __init__(self) -> None:
        self._snapshots: dict[tuple[str, str], LimitSnapshot] = {}
        self._lock = threading.Lock()

    def put(self, snapshot: LimitSnapshot) -> None:
        with self._lock:
            self._snapshots[(snapshot.provider, snapshot.window)] = snapshot

    def put_many(self, snapshots: Iterable[LimitSnapshot]) -> None:
        for snapshot in snapshots:
            self.put(snapshot)

    def get(self, provider: str, window: str) -> LimitSnapshot | None:
        with self._lock:
            return self._snapshots.get((provider, window))

    def all(self) -> list[dict[str, Any]]:
        with self._lock:
            items = list(self._snapshots.values())
        return [snapshot.to_dict() for snapshot in sorted(items, key=lambda s: (s.provider, s.window))]

    def clear(self) -> None:
        with self._lock:
            self._snapshots.clear()


# --- redaction ---------------------------------------------------------

#: Patterns that must never appear in a log line, an export, or an
#: analytics record. The release gate runs a secret scanner over every one
#: of those surfaces using this table.
REDACTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}\b")),
    ("opencode_key", re.compile(r"\bgw_[A-Za-z0-9_\-]{16,}\b")),
    ("bearer_token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{12,}")),
    ("device_code", re.compile(r"\b[A-Z0-9]{4}-[A-Z0-9]{4}\b")),
    ("authorization_header", re.compile(r"(?i)\bauthorization\b\s*[:=]\s*\S+")),
    ("chatgpt_account_id", re.compile(r"\bchatgpt-account-id\s*[:=]\s*\S+")),
    ("x_gateway_key", re.compile(r"(?i)\bx-gateway-key\b\s*[:=]\s*\S+")),
)

REDACTED = "[redacted]"


def redact(text: str) -> str:
    """Replace anything that looks like credential material with a marker."""
    if not isinstance(text, str) or not text:
        return text
    result = text
    for _name, pattern in REDACTION_PATTERNS:
        result = pattern.sub(REDACTED, result)
    return result


def scan_for_secrets(text: str) -> list[str]:
    """Return the names of every secret pattern found in *text*."""
    if not isinstance(text, str) or not text:
        return []
    found: list[str] = []
    for name, pattern in REDACTION_PATTERNS:
        if pattern.search(text):
            found.append(name)
    return found
