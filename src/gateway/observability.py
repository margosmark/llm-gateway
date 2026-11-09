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

import json
import logging
import os
import re
import secrets
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .bridge.streams import usable_token_count
from .costs import COUNTER_MARKERS
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
    client_request_id: str | None = None
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
            "client_request_id": self.client_request_id,
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
        #: Bumped by every mutation below (record, amend, clear), so a
        #: consumer can detect "the ring changed" without scanning it.
        self._version = 0
        self._persistence = (LedgerPersistence(persistence_path, ring_capacity=capacity)
                             if persistence_path else None)
        self._totals = {"requests": 0, "errors": 0, "fallbacks": 0, "rate_limited": 0, "exhausted": 0}
        if self._persistence:
            # Restore only a bounded window into the ring. The whole-store
            # totals arrive as one SQL aggregate from restore(), so a boot
            # over a full 30-day history never materialises it into memory
            # (the R56 sub-2s state bar depends on this) while the totals
            # still cover every retained request - not just the ring.
            loaded, stored_totals = self._persistence.restore(capacity)
            events: list[RequestEvent] = []
            for row in loaded:
                try:
                    events.append(RequestEvent(**row))
                except TypeError:
                    continue
            self._totals = stored_totals
            self._evicted = max(0, stored_totals["requests"] - capacity)
            self._events.extend(events[-capacity:])

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
            self._version += 1
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
            # Only keys the provider reported belong here: the rollup reads a
            # missing cached key as "not reported", never as zero. The only
            # non-integer values allowed are the boolean completeness
            # markers: ``cached_tokens_reported`` ("cached accounting named,
            # count withheld") and ``invalid_usage`` ("counters reported,
            # this gateway refused them"). Every other counter stays a
            # bounded non-negative integer (``bool`` is an ``int`` subclass,
            # so it has to be excluded explicitly rather than by accident),
            # and the bound is enforced here too: a provider-controlled
            # 401-digit counter must be refused at the write seam, not
            # persisted to 500 the owner console on its next read.
            if not isinstance(counters, dict) or any(
                not isinstance(k, str)
                or (k in COUNTER_MARKERS and not isinstance(v, bool))
                or (k not in COUNTER_MARKERS and usable_token_count(v) is None)
                for k, v in counters.items()
            ):
                raise ValueError(
                    "token_counters must contain bounded non-negative integer metadata "
                    "or the cached_tokens_reported/invalid_usage flags"
                )
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
            self._version += 1
            row = event.to_dict()
        if self._persistence:
            self._persistence.submit(row)
        return True

    def durability(self) -> dict[str, Any]:
        """What the status view shows: retention, store figures and any store error.

        With persistence on this delegates to the ledger, so the store's own
        size, row count and oldest row are reported where the owner reads -
        a store that silently stops accepting rows must be visible, not
        inferred from a number that stays put.
        """
        if self._persistence:
            return self._persistence.durability()
        return {"enabled": False, "retention_days": 30, "billing_system": False}

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

    @property
    def version(self) -> int:
        """A monotonic change counter for the ring, totals and eviction state."""
        with self._lock:
            return self._version

    def clear(self) -> None:
        with self._lock:
            self._events.clear()
            self._evicted = 0
            for key in self._totals:
                self._totals[key] = 0
            self._version += 1

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


# --- account-quota publish path -----------------------------------------

#: How long one account-quota observation is served before state assembly
#: runs the read again. The fetch lives inside ``/owner/api/state``
#: assembly behind this TTL - never on an inference request, never on a
#: caller-controlled trigger - so a console poll costs one upstream read
#: per minute at most, and a failing endpoint is retried at that same
#: cadence instead of once per poll.
ACCOUNT_QUOTA_TTL_SECONDS = 60.0


class AccountQuotaStore:
    """The TTL memo in front of the account-quota read.

    ``snapshot`` is called from state assembly only:

    * ``build`` runs at most once per TTL - **failures memoised too**, so
      a 401 does not become one upstream call per console poll;
    * ``invalidate`` is compared with the previous value and forces a
      fresh read when it changes. The caller passes the credential
      store's ``describe()`` result, which is presence and field names
      only, so enrolling or disconnecting the account shows on the very
      next read rather than behind a stale minute - and it keeps a
      credential change from being masked by the TTL;
    * the lock is held across ``build`` so two concurrent renders cannot
      both spend the upstream budget; the second waits for the first
      result instead of issuing a second call.

    The returned block is the publish payload: a fresh copy, safe to
    embed in a response, and to be treated as read-only.
    """

    def __init__(
        self,
        *,
        ttl_seconds: float = ACCOUNT_QUOTA_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = float(ttl_seconds)
        self._clock = clock
        self._lock = threading.Lock()
        self._block: dict[str, Any] | None = None
        self._key: Any = None
        self._read_at: float | None = None

    def snapshot(
        self,
        build: Callable[[], dict[str, Any] | None],
        *,
        invalidate: Any = None,
    ) -> dict[str, Any] | None:
        """The current block, running ``build`` at most once per TTL.

        ``build`` returning ``None`` means this process has no observation
        path at all (no credential-backed adapter to read with); that is
        memoised like any other result and published as an absent block,
        never as a confident one.
        """
        with self._lock:
            now = self._clock()
            if (
                self._read_at is not None
                and invalidate == self._key
                and now - self._read_at < self._ttl
            ):
                return _copy_quota_block(self._block)
            block = build()
            self._block = block
            self._key = invalidate
            self._read_at = self._clock()
            return _copy_quota_block(block)

    def prime(self, block: dict[str, Any] | None, *, key: Any = None) -> None:
        """Install an observation this process just made itself.

        The redemption path re-reads usage immediately after a successful
        reset - its guard read predates the reset, so only the fresh read
        shows the restored windows. Priming that block here means the very
        next state render serves it from the memo instead of issuing a
        third read within the same second, and the console's post-redeem
        refresh shows restored windows. ``key`` follows ``invalidate``'s
        contract: the caller passes the credential describe() result, so a
        later enrolling or disconnecting still wins over this prime.
        """
        with self._lock:
            self._block = _copy_quota_block(block)
            self._key = key
            self._read_at = self._clock()


def _copy_quota_block(block: dict[str, Any] | None) -> dict[str, Any] | None:
    """A shallow copy of a published block: cached state is never handed
    out for a consumer to mutate."""
    if block is None:
        return None
    copied = dict(block)
    windows = copied.get("windows")
    if isinstance(windows, list):
        copied["windows"] = list(windows)
    return copied


# --- redemption audit --------------------------------------------------
# One JSON line per owner-triggered reset redemption attempt, next to
# ``config.json`` in the state directory, so backups and releases carry
# the trail with everything else the owner already keeps.

REDEMPTION_AUDIT_FILE = "redemption_audit.jsonl"
DEFAULT_REDEMPTION_AUDIT_CAPACITY = 200

#: The only fields a record may ever carry - an allowlist, so a future
#: extra argument cannot widen an audit line toward identity material.
REDEMPTION_AUDIT_FIELDS = ("ts", "actor", "provider", "outcome")

#: The outcomes the redemption path records. Everything that reached the
#: handler gets exactly one of these, refusals included: the four server
#: answers, ``not_exhausted`` (the guard refused before any POST), and
#: ``unavailable`` (the usage could not be confirmed, or the consume
#: failed in a way the gateway will not retry).
REDEMPTION_OUTCOMES = (
    "reset",
    "nothing_to_reset",
    "no_credit",
    "already_redeemed",
    "not_exhausted",
    "unavailable",
)


def _audit_field(value: Any) -> str:
    """One audit field: redacted, capped, never empty.

    The three inputs are the gateway's own vocabulary (a validated owner
    username or a fixed machine-key/session literal, a provider id, a
    fixed outcome word), so this is belt and braces: even a wrong future
    caller cannot push token-shaped material or an unbounded string into
    the trail.
    """
    text = redact(str(value)) if value is not None else ""
    text = text.strip()[:64]
    return text or "unknown"


class RedemptionAudit:
    """Durable who/when/outcome trail for owner-triggered redemptions.

    Every attempt that reached the redemption handler is recorded -
    success, refusal and failure alike - because "the owner clicked and
    nothing happened" is exactly the event an audit exists to answer.

    A record carries four fields and no more: ``ts`` (UTC), ``actor``
    (``owner:<username>``, ``machine_key:<id>``, or ``owner_session`` for
    gate-off mode), ``provider`` and ``outcome`` (one of
    :data:`REDEMPTION_OUTCOMES`). No token, header, response fragment,
    account identifier, prompt or reset request id fits that shape, and
    each field passes through :func:`redact` on the way in.

    Persistence is append-only JSONL, created ``0600`` in the state
    directory. A write failure never breaks the owner's action: the
    in-memory ring still holds the record for this process and the
    access-log line names the action and its status either way - the
    failure is logged instead of raised. :meth:`recent` replays the last
    :attr:`capacity` lines, so the trail survives a restart.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        capacity: int = DEFAULT_REDEMPTION_AUDIT_CAPACITY,
    ) -> None:
        self._path = Path(path)
        self._capacity = max(1, int(capacity))
        self._lock = threading.Lock()
        self._records: deque[dict[str, str]] = deque(maxlen=self._capacity)
        self._load()

    @property
    def path(self) -> Path:
        return self._path

    def record(self, *, actor: str, provider: str, outcome: str) -> dict[str, str]:
        """Append one attempt. Never raises."""
        values = {
            "ts": utc_now_iso(),
            "actor": _audit_field(actor),
            "provider": _audit_field(provider),
            "outcome": _audit_field(outcome),
        }
        with self._lock:
            self._records.append(values)
            try:
                self._append_line(json.dumps(values, sort_keys=True))
            except OSError as exc:
                # The action itself already stands; report the fs problem
                # instead of turning a spent credit into a 500.
                logging.getLogger("gateway.operations").warning(
                    "redemption audit write failed: path=%s error=%s",
                    self._path,
                    type(exc).__name__,
                )
        return dict(values)

    def recent(self, limit: int = 50) -> list[dict[str, str]]:
        """The newest records first, as plain dicts (a snapshot copy)."""
        count = max(0, int(limit))
        with self._lock:
            records = list(self._records)
        return [dict(record) for record in records[-count:][::-1]] if count else []

    def _load(self) -> None:
        """Replay the sidecar (best-effort; a partial line is skipped)."""
        try:
            lines = self._path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            return
        for line in lines[-self._capacity :]:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                record = json.loads(stripped)
            except ValueError:
                continue
            if isinstance(record, dict) and all(
                key in record for key in REDEMPTION_AUDIT_FIELDS
            ):
                self._records.append(
                    {key: _audit_field(record[key]) for key in REDEMPTION_AUDIT_FIELDS}
                )

    def _append_line(self, line: str) -> None:
        encoded = (line + "\n").encode("utf-8")
        fd = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, encoded)
        finally:
            os.close(fd)


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
