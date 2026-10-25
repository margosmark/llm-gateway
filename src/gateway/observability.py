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
import secrets
import threading
import time
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .ledger_persistence import LedgerPersistence

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
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


#: A random per-process component of every request identifier. An identifier
#: minted after a restart must not collide with one that is already in the
#: durable ledger, and a random component is the cheapest way to guarantee it.
_REQUEST_ID_NONCE = secrets.token_hex(4)

#: The counter that makes an identifier unique inside this process.
_request_id_counter = 0
_request_id_lock = threading.Lock()


def new_request_id() -> str:
    """Mint an identifier that names exactly one request.

    The ledger amends a recorded outcome by its request identifier, so two
    requests that share an identifier would have one request's usage
    counters written onto another request's record. A millisecond
    timestamp alone is not unique: the gateway serves requests
    concurrently, and two requests routinely begin inside the same
    millisecond.

    The identifier therefore carries three parts - the millisecond, a
    counter that is unique inside this process, and a nonce that is unique
    to this process - so no two requests, in this process or across a
    restart, can share one.
    """
    global _request_id_counter
    with _request_id_lock:
        _request_id_counter += 1
        sequence = _request_id_counter
    return f"req_{int(time.time() * 1000):x}_{sequence:x}_{_REQUEST_ID_NONCE}"


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
    #: Phase-routing metadata, when an alias has an enabled phase policy.
    #: Metadata only: the phase, its source, and a confidence bucket. Never
    #: the request content, the Jev question text, or the answer values.
    phase: str | None = None
    phase_source: str | None = None

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
            "phase": self.phase,
            "phase_source": self.phase_source,
        }


class RequestLedger:
    """Bounded hot ring with asynchronous metadata-only durable storage."""

    def __init__(self, capacity: int = DEFAULT_LEDGER_CAPACITY, persistence_path: Path | None = None) -> None:
        self._events: deque[RequestEvent] = deque(maxlen=capacity)
        self._lock = threading.Lock()
        self._evicted = 0
        self._persistence = LedgerPersistence(persistence_path) if persistence_path else None
        self._totals = {"requests": 0, "errors": 0, "fallbacks": 0, "rate_limited": 0, "exhausted": 0}
        if self._persistence:
            loaded: list[RequestEvent] = []
            for row in self._persistence.load(capacity):
                try:
                    event = RequestEvent(**row)
                except TypeError:
                    continue
                loaded.append(event)
                self._count(event)
            self._evicted = max(0, len(loaded) - capacity)
            self._events.extend(loaded[-capacity:])

    def _count(self, event: RequestEvent) -> None:
        self._totals["requests"] += 1
        if event.error_class:
            self._totals["errors"] += 1
        if event.error_class == "rate_limited":
            self._totals["rate_limited"] += 1
        if event.error_class == "exhausted":
            self._totals["exhausted"] += 1
        if event.fallback_used:
            self._totals["fallbacks"] += 1

    def record(self, event: RequestEvent) -> None:
        with self._lock:
            if len(self._events) == self._events.maxlen:
                self._evicted += 1
            self._events.append(event)
            self._count(event)
        if self._persistence:
            self._persistence.submit(event.to_dict())

    def amend(self, request_id: str, **fields: Any) -> bool:
        """Amend safe outcome metadata in memory and durable history, if present."""
        from .ledger_persistence import AMENDABLE
        if not fields or not set(fields).issubset(AMENDABLE):
            raise ValueError("only status, error_class, token_counters, latency_ms may be amended")
        if "status" in fields and fields["status"] is not None and (not isinstance(fields["status"], int) or not 100 <= fields["status"] <= 599):
            raise ValueError("status must be an HTTP status code or None")
        if "latency_ms" in fields and fields["latency_ms"] is not None and (not isinstance(fields["latency_ms"], int) or fields["latency_ms"] < 0):
            raise ValueError("latency_ms must be a non-negative integer or None")
        if "token_counters" in fields and fields["token_counters"] is not None:
            counters = fields["token_counters"]
            if not isinstance(counters, dict) or any(not isinstance(k, str) or not isinstance(v, int) or v < 0 for k, v in counters.items()):
                raise ValueError("token_counters must contain non-negative integer metadata")
        with self._lock:
            matches = [item for item in self._events if item.request_id == request_id]
            if len(matches) != 1:
                # No match: the outcome was never recorded, or it has already
                # been evicted. More than one match: the identifier does not
                # name a single request, and guessing would write one
                # request's counters onto another request's record.
                return False
            event = matches[0]
            before = event.error_class
            event.status = fields.get("status", event.status)
            event.error_class = fields.get("error_class", event.error_class)
            event.token_counters = fields.get("token_counters", event.token_counters)
            event.latency_ms = fields.get("latency_ms", event.latency_ms)
            if before != event.error_class:
                self._totals["errors"] += int(bool(event.error_class)) - int(bool(before))
                self._totals["rate_limited"] += int(event.error_class == "rate_limited") - int(before == "rate_limited")
                self._totals["exhausted"] += int(event.error_class == "exhausted") - int(before == "exhausted")
            row = event.to_dict()
        if self._persistence:
            self._persistence.submit(row)
        return True

    def durability(self) -> dict[str, Any]:
        return {"enabled": self._persistence is not None, "retention_days": 30, "billing_system": False}

    def rollup(self) -> dict[str, Any] | None:
        """The durable daily rollup derived from this ledger, or None when persistence is off.

        Unlike :meth:`recent`, the rollup does not depend on the bounded ring:
        rows the ring has evicted, and days from before a restart, are already
        aggregated. Its ``coverage`` block says what it actually covers - the
        first row's timestamp, rows that could not be represented, and the
        rebuild status - so a consumer can tell "no data before this point"
        apart from "no usage", instead of reading a gap as a zero.
        """
        return self._persistence.rollup() if self._persistence else None

    def flush(self, timeout: float = 5.0) -> bool:
        return self._persistence.flush(timeout) if self._persistence else True

    def close(self, timeout: float = 5.0) -> None:
        if self._persistence:
            self._persistence.close(timeout)

    @property
    def persistence_enabled(self) -> bool:
        return self._persistence is not None

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

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            items = list(self._events)[-limit:]
        return [event.to_dict() for event in reversed(items)]

    def totals(self) -> dict[str, int]:
        with self._lock:
            return dict(self._totals)


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
    def unknown(cls, provider: str, window: str, note: str | None = None) -> LimitSnapshot:
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
