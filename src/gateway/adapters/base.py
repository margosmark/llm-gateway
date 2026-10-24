"""The adapter contract.

An adapter is the only seam between the routing core and a provider. The
routing core contains no provider-specific branch; adding or removing a
provider is a new module plus a registry entry.

Every adapter must:

* declare a **capability** per model, because dialect is a property of the
  model, not of the provider;
* classify upstream failures into exactly one of the shared error classes,
  so the routing core can make one decision for every provider;
* never raise an exception that carries credential material or request
  content.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Iterable, Iterator, Protocol

from gateway.config import (
    ERROR_AUTH,
    ERROR_EXHAUSTED,
    ERROR_MALFORMED,
    ERROR_POLICY,
    ERROR_RATE_LIMITED,
    ERROR_ROUTE_GONE,
    ERROR_TRANSIENT,
    ERROR_UNKNOWN,
)
from gateway.core.capabilities import ModelInfo

#: Upstream error codes that mean a *confirmed* exhaustion of a provider's
#: allowance. A plain HTTP 429 is not one of these.
EXHAUSTION_CODES = frozenset(
    {
        "insufficient_quota",
        "quota_exceeded",
        "usage_limit_reached",
        "billing_hard_limit_reached",
        "exceeded_current_quota",
        "plan_limit_exceeded",
    }
)

#: Upstream error codes that mean *rate limiting*, which is not exhaustion.
RATE_LIMIT_CODES = frozenset(
    {
        "rate_limit_exceeded",
        "requests_rate_limit_exceeded",
        "too_many_requests",
        "slow_down",
    }
)

#: Upstream error codes that mean the model is no longer served on this
#: route. It is not transient and not a policy failure: the route is simply
#: unavailable, and another route may still serve the request.
ROUTE_GONE_CODES = frozenset(
    {
        "ModelDeprecated",
        "model_deprecated",
        "model_not_found",
        "model_removed",
        "model_unavailable",
        "not_found",
    }
)

#: Upstream error codes that mean the provider refused on policy grounds.
POLICY_CODES = frozenset(
    {
        "unsupported_country_region_territory",
        "region_not_supported",
        "account_deactivated",
        "terms_violation",
        "content_policy_violation",
        "model_not_available_on_plan",
        "access_denied",
    }
)

#: Only documented classification codes may influence routing. Never retain
#: arbitrary provider-controlled strings: they can contain credential material.
KNOWN_ERROR_CODES = EXHAUSTION_CODES | RATE_LIMIT_CODES | ROUTE_GONE_CODES | POLICY_CODES
SAFE_CODE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$") # retained for compatibility; not an acceptance rule.

#: Fixed, safe explanation per error class. These never contain upstream
#: body text, because the body could echo request content.
CLASS_MESSAGES = {
    ERROR_EXHAUSTED: "the provider reported that this account's allowance for this route is exhausted",
    ERROR_RATE_LIMITED: "the provider rate-limited this request",
    ERROR_TRANSIENT: "the provider was temporarily unavailable",
    ERROR_AUTH: "the provider rejected the stored credential",
    ERROR_POLICY: "the provider refused this request on policy grounds",
    ERROR_MALFORMED: "the provider rejected the request as malformed",
    ERROR_ROUTE_GONE: "the provider no longer serves this model on this route",
    ERROR_UNKNOWN: "the provider returned a failure that could not be classified",
}

#: A route that no longer exists will not come back, so it is never retried
#: on the same route even though it may be served by another one.
RETRYABLE_CLASSES = frozenset({ERROR_TRANSIENT, ERROR_RATE_LIMITED})

#: The one thing a rejected request's *reason* may carry out of the
#: provider's own body: the **name** of a field the provider refused.
#:
#: A rejected body is provider text and routinely echoes request content -
#: prompt fragments, session identifiers, quoted values. The gateway copies
#: **no** part of that text into an error, an attempt, or the durable
#: ledger. Only an identifier-shaped token is retained, and only after it
#: has been checked against this rule, so a hostile or oversized "field
#: name" can never become gateway state.
SAFE_FIELD_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.\-\[\]]{0,63}$")

#: Structural segments of a validation ``loc`` path that are not field
#: names.
_VALIDATION_LOC_PREFIXES = ("body", "query", "path", "header", "formData")

#: The documented shape of a refusal that names a field in prose:
#: ``Unsupported parameter: temperature``. Matched exactly, because a
#: pattern that "finds a word" anywhere in a message would be a channel for
#: provider text to reach the logs.
_UNSUPPORTED_PARAMETER_MSG = re.compile(r"^Unsupported parameter: ([A-Za-z_][A-Za-z0-9_.\-]{0,63})$")

#: How many field names one failure may report. A rejection is expected to
#: name one; the bound keeps a pathological body from growing the ledger.
MAX_REJECTED_FIELDS = 8


def sanitize_rejected_fields(values: Any) -> tuple[str, ...]:
    """Keep only identifier-shaped field names, deduplicated and bounded.

    This is the *only* gate between an upstream rejection body and gateway
    state. It is deliberately strict: anything that is not a plain field
    name (values, prose, numbers, over-long tokens) is discarded, so no
    prompt text, credential, or arbitrary provider string can ride along.
    """
    if not isinstance(values, (list, tuple, set, frozenset)):
        return ()
    kept: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        name = value.strip()
        if not SAFE_FIELD_NAME.fullmatch(name):
            continue
        if name not in kept:
            kept.append(name)
        if len(kept) >= MAX_REJECTED_FIELDS:
            break
    return tuple(kept)


def _names_from_validation_loc(loc: Any) -> list[str]:
    """The top-level field a pydantic-style ``loc`` path points at."""
    if not isinstance(loc, list):
        return []
    segments = [segment for segment in loc if isinstance(segment, str)]
    while segments and segments[0] in _VALIDATION_LOC_PREFIXES:
        segments = segments[1:]
    return segments[:1]


def rejected_fields_from_payload(payload: Any) -> tuple[str, ...]:
    """Field names a *shape* rejection named, without retaining its text.

    Two documented bodies name a field: a pydantic validation entry with a
    ``loc`` path, and the ``Unsupported parameter: <name>`` message. Anything
    else - a prose ``detail`` string, an unknown ``msg`` - yields nothing,
    because copying it would put provider text into the ledger.
    """
    if not isinstance(payload, dict):
        return ()
    detail = payload.get("detail")
    entries = detail if isinstance(detail, list) else [detail]
    names: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        names.extend(_names_from_validation_loc(entry.get("loc")))
        msg = entry.get("msg")
        if isinstance(msg, str):
            match = _UNSUPPORTED_PARAMETER_MSG.match(msg.strip())
            if match:
                names.append(match.group(1))
    return sanitize_rejected_fields(names)

#: The longest window a provider's ``Retry-After`` may ask for.
#:
#: The provider is the only party that knows how long its own allowance
#: takes to recover, so its answer is worth honouring - but it is still a
#: value from the network, and a hostile or mistaken one must not bench a
#: route for a week. Anything longer is clamped to this.
MAX_RETRY_AFTER_SECONDS = 3600


def parse_retry_after(value: Any, now: datetime | None = None) -> int | None:
    """Read a provider's ``Retry-After`` as a number of seconds.

    The header has two forms: a number of seconds, and an HTTP date. Both
    are accepted. Anything unusable - unparseable, negative, already in
    the past - yields ``None``, which means "the provider did not say, so
    use the local policy".

    A value longer than :data:`MAX_RETRY_AFTER_SECONDS` is clamped rather
    than discarded, because waiting too long is safer than waiting too
    little.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None

    # The seconds form.
    if text.isdigit():
        seconds = int(text)
        return min(seconds, MAX_RETRY_AFTER_SECONDS)

    # The HTTP-date form.
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    seconds = int((when - reference).total_seconds())
    if seconds <= 0:
        # The window has already passed. There is nothing to wait for.
        return None
    return min(seconds, MAX_RETRY_AFTER_SECONDS)


def retry_after_from_headers(headers: dict[str, str] | None) -> int | None:
    """Read ``Retry-After`` out of an upstream response's headers."""
    if not isinstance(headers, dict):
        return None
    for key, value in headers.items():
        if isinstance(key, str) and key.lower() == "retry-after":
            return parse_retry_after(value)
    return None


class AdapterError(Exception):
    """A provider failure, classified into exactly one error class."""

    def __init__(
        self,
        error_class: str,
        message: str | None = None,
        provider_id: str | None = None,
        status: int | None = None,
        upstream_code: str | None = None,
        retry_after: int | None = None,
        rejected_fields: Iterable[str] | None = None,
    ) -> None:
        self.error_class = error_class
        self.provider_id = provider_id
        self.status = status
        self.upstream_code = _safe_classification_code(upstream_code)
        #: How long the provider asked the gateway to wait, in seconds. It
        #: is a hint from the provider about its own recovery, not evidence
        #: that the provider has recovered.
        self.retry_after = retry_after
        #: The names of request fields the provider refused, when the
        #: rejection named them. These are the one detail carried out of a
        #: rejected body, and they arrive here already identifier-shaped, so
        #: a rejection cannot smuggle request content into the ledger.
        self.rejected_fields = sanitize_rejected_fields(rejected_fields)
        self.message = _safe_error_message(error_class, message)
        super().__init__(self.message)

    @property
    def retryable(self) -> bool:
        return self.error_class in RETRYABLE_CLASSES

    def to_dict(self) -> dict[str, Any]:
        return {
            "error_class": self.error_class,
            "message": self.message,
            "provider_id": self.provider_id,
            "status": self.status,
            "upstream_code": self.upstream_code,
            "retryable": self.retryable,
            "retry_after": self.retry_after,
            "rejected_fields": list(self.rejected_fields) or None,
        }


@dataclass
class ProviderRequest:
    """A request bound for a provider, already in the provider's dialect."""

    model: str
    payload: dict[str, Any]
    stream: bool = False
    alias: str = ""
    request_id: str = ""
    session_id: str | None = None
    dialect: str = ""
    client_ip: str | None = None
    #: Set once any tool call or other side effect may have executed. The
    #: router refuses to switch provider after this is set.
    side_effects_possible: bool = False

    def with_side_effects_possible(self) -> "ProviderRequest":
        self.side_effects_possible = True
        return self


@dataclass
class ProviderResponse:
    """A provider's answer."""

    status: int
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes | None = None
    stream: Iterator[bytes] | None = None
    content_type: str = "application/json"
    dialect: str = ""

    def json(self) -> Any:
        import json

        if not self.body:
            return None
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None


@dataclass
class TestResult:
    """The outcome of an explicit connection test."""

    ok: bool
    state: str
    message: str
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "state": self.state, "message": self.message, "detail": self.detail}


class Adapter(Protocol):
    """The provider seam."""

    provider_id: str
    kind: str

    def list_models(self) -> list[ModelInfo]:
        """Return the provider's models, as the provider describes them."""

    def test_connection(self) -> TestResult:
        """Run an explicit connection test. Never called implicitly."""

    def invoke(self, request: ProviderRequest) -> ProviderResponse:
        """Send a request upstream and return the provider's response."""


def _extract_upstream_code(payload: Any) -> str | None:
    """Return a documented classification code, never arbitrary provider text."""
    if not isinstance(payload, dict):
        return None
    candidates: list[Any] = []
    error = payload.get("error")
    if isinstance(error, dict):
        candidates.extend(error.get(key) for key in ("code", "type"))
        inner = error.get("error")
        if isinstance(inner, dict):
            candidates.extend(inner.get(key) for key in ("code", "type"))
    candidates.extend(payload.get(key) for key in ("code", "type", "error_code"))
    return next(
        (value for value in candidates if isinstance(value, str) and value in KNOWN_ERROR_CODES),
        None,
    )


def _safe_classification_code(value: str | None) -> str | None:
    """Keep caller-supplied AdapterError codes within the classification allowlist."""
    return value if isinstance(value, str) and value in KNOWN_ERROR_CODES else None


def _safe_error_message(error_class: str, message: str | None = None) -> str:
    """Preserve gateway-authored context, but redact token-shaped credentials."""
    safe_message = message or CLASS_MESSAGES.get(error_class, CLASS_MESSAGES[ERROR_UNKNOWN])
    return re.sub(r"(?i)\b(?:sk|tok|key)-[A-Za-z0-9_-]{16,}\b", "[redacted]", safe_message)



def classify_status(status: int, payload: Any = None, upstream_code: str | None = None) -> str:
    """Map an upstream status and error body onto exactly one error class.

    The rule that matters: **unknown is never exhausted, and never
    unlimited.** A plain HTTP 429 is *rate limiting*, not a confirmed
    exhaustion; only a structured exhaustion code proves the allowance is
    gone.
    """
    code = upstream_code or _extract_upstream_code(payload)

    if code is not None:
        if code in EXHAUSTION_CODES:
            return ERROR_EXHAUSTED
        if code in ROUTE_GONE_CODES:
            return ERROR_ROUTE_GONE
        if code in POLICY_CODES:
            return ERROR_POLICY
        if code in RATE_LIMIT_CODES:
            return ERROR_RATE_LIMITED

    # 410 Gone is unambiguous: the upstream no longer has the resource.
    if status == 410:
        return ERROR_ROUTE_GONE

    if status == 402:
        # Payment required: the provider states the allowance is not
        # available. Treated as a confirmed exhaustion.
        return ERROR_EXHAUSTED
    if status in (401, 403):
        return ERROR_AUTH
    if status == 429:
        return ERROR_RATE_LIMITED
    if status in (400, 404, 405, 409, 413, 415, 422):
        return ERROR_MALFORMED
    if status in (408, 425) or 500 <= status <= 599:
        return ERROR_TRANSIENT
    if 200 <= status < 300:
        return ERROR_UNKNOWN
    return ERROR_UNKNOWN


def error_from_response(
    status: int,
    body: bytes | None,
    provider_id: str,
    payload: Any = None,
    operation: str | None = None,
    headers: dict[str, str] | None = None,
) -> AdapterError:
    """Build a classified, safe error from an upstream response.

    *operation* names what was being attempted, so a failure says which
    upstream call it was rather than only how it was classified.

    *headers* carries the upstream response headers, so the provider's own
    ``Retry-After`` reaches the router. It is the only party that knows
    how long its own allowance takes to recover.
    """
    code = _extract_upstream_code(payload)
    error_class = classify_status(status, payload, code)
    # A shape rejection names the field it refused. Carry that name - and
    # nothing else from the body - so the failure is attributable, which is
    # what the 2026-10-24 incident lacked: 93 malformed 502s whose offending
    # field could not be identified from any log.
    rejected_fields = (
        rejected_fields_from_payload(payload) if error_class == ERROR_MALFORMED else ()
    )
    return AdapterError(
        error_class=error_class,
        # Provider text (including codes and operation labels) is untrusted.
        # Expose only the fixed message for the gateway-owned error class.
        message=CLASS_MESSAGES.get(error_class, CLASS_MESSAGES[ERROR_UNKNOWN]),
        provider_id=provider_id,
        status=status,
        upstream_code=code,
        retry_after=retry_after_from_headers(headers),
        rejected_fields=rejected_fields,
    )


def safe_detail(mapping: dict[str, Any], allowed: Iterable[str]) -> dict[str, Any]:
    """Copy only the named keys out of a mapping."""
    return {key: mapping[key] for key in allowed if key in mapping}
