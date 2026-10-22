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

#: A safe upstream code looks like this. Anything else is not echoed.
#: Uppercase is allowed, because providers use camel-cased type names such
#: as ``MissingSessionID``; refusing them would hide the useful cause
#: behind a generic code.
SAFE_CODE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")

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
    ) -> None:
        self.error_class = error_class
        self.provider_id = provider_id
        self.status = status
        self.upstream_code = upstream_code
        #: How long the provider asked the gateway to wait, in seconds. It
        #: is a hint from the provider about its own recovery, not evidence
        #: that the provider has recovered.
        self.retry_after = retry_after
        self.message = message or CLASS_MESSAGES.get(error_class, CLASS_MESSAGES[ERROR_UNKNOWN])
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
    """Pull a short, safe error code out of an upstream error body."""
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    if isinstance(error, dict):
        for key in ("code", "type"):
            value = error.get(key)
            if isinstance(value, str) and SAFE_CODE.match(value):
                return value
        inner = error.get("error")
        if isinstance(inner, dict):
            for key in ("code", "type"):
                value = inner.get(key)
                if isinstance(value, str) and SAFE_CODE.match(value):
                    return value
    for key in ("code", "type", "error_code"):
        value = payload.get(key)
        if isinstance(value, str) and SAFE_CODE.match(value):
            return value
    return None


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
    message = CLASS_MESSAGES.get(error_class, CLASS_MESSAGES[ERROR_UNKNOWN])
    # The upstream status is metadata, and it is what tells an operator
    # whether the provider is refusing our request or is failing itself.
    details = [f"upstream status {status}"]
    if code:
        details.append(f"upstream code: {code}")
    message = f"{message} ({'; '.join(details)})"
    if operation:
        message = f"{operation}: {message}"
    return AdapterError(
        error_class=error_class,
        message=message,
        provider_id=provider_id,
        status=status,
        upstream_code=code,
        retry_after=retry_after_from_headers(headers),
    )


def safe_detail(mapping: dict[str, Any], allowed: Iterable[str]) -> dict[str, Any]:
    """Copy only the named keys out of a mapping."""
    return {key: mapping[key] for key in allowed if key in mapping}
