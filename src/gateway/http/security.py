"""Request hardening.

Everything in this module exists because the gateway is reachable from a LAN
and must not be reachable *from a browser on another site*, must not be
spoofable through a proxy, and must not accept a credential on the casually
reachable surface.

Rules enforced here:

* **Forwarded headers are rejected outright.** ``X-Forwarded-*``,
  ``Forwarded``, ``X-Real-IP`` and friends are refused with 400 rather than
  ignored, so an upstream proxy can never rewrite the canonical origin or the
  client identity the gateway reasons about.
* **``Host`` must match the configured bind address.** A request whose
  ``Host`` names another origin is refused with 403.
* **``Origin`` must match a canonical origin** for browser surfaces that
  mutate state.
* **CSRF tokens are required** on mutating requests to cookie-authenticated
  browser surfaces. The API surface authenticates with bearer tokens, which
  are never sent ambiently by a browser, so it is not CSRF-relevant.
"""

from __future__ import annotations

import hmac
import ipaddress
import secrets
import time
from dataclasses import dataclass, field
from typing import Iterable

from gateway.http.core import Request, Response, json_response

#: Headers that could rewrite the canonical origin or the client identity.
FORWARDED_HEADER_NAMES = (
    "forwarded",
    "x-forwarded-for",
    "x-forwarded-host",
    "x-forwarded-proto",
    "x-forwarded-port",
    "x-real-ip",
    "x-original-url",
    "x-rewrite-url",
    "x-client-ip",
    "true-client-ip",
    "cf-connecting-ip",
    "x-client-ip-chain",
)

MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

SESSION_COOKIE = "gw_session"
CSRF_COOKIE = "gw_csrf"
CSRF_HEADER = "x-gateway-csrf"


class SecurityError(Exception):
    """Raised when a request violates a policy. Carries the response."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message

    def to_response(self) -> Response:
        return json_response({"error": {"message": self.message, "type": self.code}}, self.status)


@dataclass
class SecurityPolicy:
    """Canonical origin and CSRF policy for one listener."""

    #: ``host:port`` values accepted in the ``Host`` header.
    allowed_hosts: set[str] = field(default_factory=set)
    #: ``scheme://host:port`` values accepted in the ``Origin`` header.
    canonical_origins: set[str] = field(default_factory=set)
    #: Cookie-authenticated browser surfaces (CSRF applies).
    browser_surfaces: frozenset[str] = frozenset({"/owner/", "/setup/"})
    #: Surfaces that may be reached only from a loopback client address.
    loopback_surfaces: frozenset[str] = frozenset({"/setup/"})

    def is_browser_surface(self, path: str) -> bool:
        return any(path.startswith(prefix) for prefix in self.browser_surfaces)

    def is_loopback_surface(self, path: str) -> bool:
        return any(path.startswith(prefix) for prefix in self.loopback_surfaces)


def check_forwarded_headers(request: Request) -> None:
    """Reject requests carrying proxy/forwarded headers."""
    for name in FORWARDED_HEADER_NAMES:
        if name in request.headers:
            raise SecurityError(
                400,
                "forwarded_header_rejected",
                f"request rejected: the {name} header is not accepted by this gateway",
            )


def check_host(request: Request, policy: SecurityPolicy) -> None:
    """Reject a request whose ``Host`` header is not a configured origin."""
    host = request.host
    if not host:
        raise SecurityError(400, "host_required", "request rejected: missing Host header")
    normalised = host.strip().lower()
    if normalised not in policy.allowed_hosts:
        raise SecurityError(
            403,
            "host_mismatch",
            "request rejected: Host header does not match this gateway's configured address",
        )


def check_origin(request: Request, policy: SecurityPolicy) -> None:
    """Reject a cross-origin mutation on a browser surface."""
    if request.method not in MUTATING_METHODS:
        return
    if not policy.is_browser_surface(request.path):
        return
    origin = request.origin
    if origin is None:
        raise SecurityError(
            403,
            "origin_required",
            "request rejected: a browser request must carry an Origin header",
        )
    if origin.strip().lower() not in policy.canonical_origins:
        raise SecurityError(
            403,
            "origin_mismatch",
            "request rejected: Origin does not match this gateway's configured address",
        )


def check_csrf(request: Request, expected_token: str | None) -> None:
    """Require a matching CSRF token on a mutating browser request."""
    if request.method not in MUTATING_METHODS:
        return
    provided = request.headers.get(CSRF_HEADER) or request.headers.get("X-CSRF-Token")
    if not provided or not expected_token:
        raise SecurityError(
            403,
            "csrf_missing",
            "request rejected: missing CSRF token",
        )
    if not hmac.compare_digest(provided.strip(), expected_token):
        raise SecurityError(
            403,
            "csrf_mismatch",
            "request rejected: CSRF token did not match",
        )


def is_loopback_client(address: str) -> bool:
    """True when the peer address is a loopback address."""
    try:
        return ipaddress.ip_address(address).is_loopback
    except ValueError:
        return False


def require_loopback(request: Request, policy: SecurityPolicy) -> None:
    """Refuse a loopback-only surface when the peer is not on loopback."""
    if not policy.is_loopback_surface(request.path):
        return
    if not is_loopback_client(request.client_address[0]):
        raise SecurityError(
            403,
            "loopback_only",
            "request rejected: this surface is only reachable from the host itself",
        )


def canonical_origin(host: str, port: int, scheme: str = "http") -> str:
    return f"{scheme}://{host}:{port}"


def build_allowed_hosts(host: str, port: int, extra: Iterable[str] = ()) -> set[str]:
    """Build the accepted ``Host`` header values.

    The bind address is always accepted, with and without an explicit port
    (the default port is implied when the URL omits it). Any additional host
    names the operator configures are accepted verbatim.
    """
    hosts: set[str] = set()
    hosts.add(f"{host}:{port}".lower())
    hosts.add(host.lower())
    for item in extra:
        item = item.strip().lower()
        if item:
            hosts.add(item)
    return hosts


def build_canonical_origins(host: str, port: int, extra: Iterable[str] = ()) -> set[str]:
    origins = {canonical_origin(host, port)}
    for item in extra:
        item = item.strip()
        if not item:
            continue
        if "://" in item:
            origins.add(item.rstrip("/").lower())
        else:
            origins.add(canonical_origin(item, port))
    return origins


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def constant_time_equals(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def monotonic_now() -> float:
    return time.monotonic()
