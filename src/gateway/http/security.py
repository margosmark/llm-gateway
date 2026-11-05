"""Request hardening.

Everything here preserves the private-LAN request boundary: forwarded/proxy
headers are rejected, `Host` must match the configured bind address, and
browser mutations must carry the exact canonical `Origin`. The Origin check is
a cross-site safeguard, not authentication: reachable LAN peers are
administrators and callers by product decision.

There are two request shapes on the browser surface and they are routed
explicitly, never blended:

* a **browser** request authenticates with the owner session cookie and
  therefore must carry the canonical `Origin` on every mutation;
* a **machine** request (MCP, automation) authenticates with
  ``Authorization: Bearer <machine key>`` and carries no browser Origin,
  so it is exempted from the Origin requirement only - never from the
  forwarded-header or `Host` checks, which run first for every request.
"""

from __future__ import annotations

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
    #: Browser surfaces whose mutations require a canonical same-origin Origin.
    browser_surfaces: frozenset[str] = frozenset({"/owner/"})

    def is_browser_surface(self, path: str) -> bool:
        return any(path.startswith(prefix) for prefix in self.browser_surfaces)


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
    """Reject a cross-site mutation on a browser surface."""
    if request.method not in MUTATING_METHODS:
        return
    if not policy.is_browser_surface(request.path):
        return
    # A machine client (MCP, automation) presents ``Authorization: Bearer``
    # and has no browser Origin, so it is exempted here - and only here.
    # This does not open a cookie CSRF path: a cross-site page cannot set
    # the Authorization header (it is not a CORS-safelisted header and
    # this gateway serves no CORS), so any request that reaches this
    # exemption is one the caller authenticated with a machine key, and
    # the forwarded-header and Host checks above have already run for it.
    # Every cookie-authenticated browser request keeps the exact Origin
    # requirement below, unchanged.
    if bearer_authorization(request) is not None:
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


def bearer_authorization(request: Request) -> str | None:
    """The bearer credential a non-browser (machine) client presents.

    This defines the accepted machine-client request shape, in full:

    * ``Authorization: Bearer TOKEN`` - exactly two whitespace-separated
      fields, scheme compared case-insensitively, nothing else on the
      header (a third field, a scheme other than bearer, or an empty
      token all return ``None``);
    * no ``Cookie`` header - refused by the owner authoriser, because a
      machine principal must never ride a browser session;
    * no ``Origin`` requirement - see :func:`check_origin`;
    * the canonical ``Host`` and forwarded-header rejection that every
     request gets, applied before anything else.

    Returns ``None`` when there is no usable bearer credential, so a
    request without the header, or with any other auth scheme, keeps
    following the ordinary cookie/session path unchanged.
    """
    raw = request.headers.get("authorization")
    if not raw:
        return None
    fields = raw.split()
    if len(fields) != 2 or fields[0].lower() != "bearer":
        return None
    return fields[1] or None


def format_authority_host(host: str) -> str:
    """Format an IP literal for an HTTP authority (Host/URL) boundary."""
    value = host.strip()
    if ":" in value and not (value.startswith("[") and value.endswith("]")):
        return f"[{value}]"
    return value


def canonical_origin(host: str, port: int, scheme: str = "http") -> str:
    return f"{scheme}://{format_authority_host(host)}:{port}"


def build_allowed_hosts(host: str, port: int, extra: Iterable[str] = ()) -> set[str]:
    """Build the accepted ``Host`` header values.

    The bind address is always accepted, with and without an explicit port
    (the default port is implied when the URL omits it). Any additional host
    names the operator configures are accepted verbatim.
    """
    hosts: set[str] = set()
    authority_host = format_authority_host(host)
    hosts.add(f"{authority_host}:{port}".lower())
    hosts.add(authority_host.lower())
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


def monotonic_now() -> float:
    return time.monotonic()
