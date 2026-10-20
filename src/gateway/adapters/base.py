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
from typing import Any, Iterable, Iterator, Protocol

from gateway.config import (
    ERROR_AUTH,
    ERROR_EXHAUSTED,
    ERROR_MALFORMED,
    ERROR_POLICY,
    ERROR_RATE_LIMITED,
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
    ERROR_UNKNOWN: "the provider returned a failure that could not be classified",
}

RETRYABLE_CLASSES = frozenset({ERROR_TRANSIENT, ERROR_RATE_LIMITED})


class AdapterError(Exception):
    """A provider failure, classified into exactly one error class."""

    def __init__(
        self,
        error_class: str,
        message: str | None = None,
        provider_id: str | None = None,
        status: int | None = None,
        upstream_code: str | None = None,
    ) -> None:
        self.error_class = error_class
        self.provider_id = provider_id
        self.status = status
        self.upstream_code = upstream_code
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
        if code in POLICY_CODES:
            return ERROR_POLICY
        if code in RATE_LIMIT_CODES:
            return ERROR_RATE_LIMITED

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
) -> AdapterError:
    """Build a classified, safe error from an upstream response."""
    code = _extract_upstream_code(payload)
    error_class = classify_status(status, payload, code)
    message = CLASS_MESSAGES.get(error_class, CLASS_MESSAGES[ERROR_UNKNOWN])
    if code:
        message = f"{message} (upstream code: {code})"
    return AdapterError(
        error_class=error_class,
        message=message,
        provider_id=provider_id,
        status=status,
        upstream_code=code,
    )


def safe_detail(mapping: dict[str, Any], allowed: Iterable[str]) -> dict[str, Any]:
    """Copy only the named keys out of a mapping."""
    return {key: mapping[key] for key in allowed if key in mapping}
