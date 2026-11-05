"""Destination policy for owner-configured local model endpoints.

A local provider is the one place where the owner types a URL the gateway
will connect to. That makes it the only new outbound destination class this
gateway gains, so it gets its own module rather than a check buried in a
handler. Two checks, both mandatory:

* **Save time** (``normalize_base_url``): the URL is refused unless it is a
  plain ``http``/``https`` URL with no userinfo, query or fragment, whose
  host is a private/loopback IP literal - or a hostname that *currently*
  resolves to nothing but such addresses. Mixed public/private resolution is
  refused, not averaged.
* **Connect time** (``make_peer_check``): the socket's actual peer is read
  after the TCP handshake and **before any request byte is written** (and,
  for ``https``, before the TLS handshake). A hostname rebound between the
  save and the request is refused with nothing sent.

The save-time check goes stale by design - DNS answers change - which is why
the connect-time check exists and why neither may be dropped.

Ranges allowed: ``127.0.0.0/8``, ``10/8``, ``172.16/12``, ``192.168/16`` and
``fc00::/7``. Explicitly outside them: link-local ``169.254/16`` and
``fe80::/10`` (the cloud-metadata class), multicast, the unspecified
addresses and the broadcast address. IPv4-mapped IPv6 addresses are unwrapped
and judged as IPv4, so ``::ffff:169.254.169.254`` cannot slip through as an
IPv6 literal.
"""

from __future__ import annotations

import ipaddress
import re
import socket
import urllib.parse
from typing import Any, Callable, Sequence

from gateway.adapters.base import AdapterError
from gateway.config import ERROR_POLICY

#: The schemes a local endpoint may use. ``file:``, ``gopher:`` and friends
#: are refused outright: this URL is only ever an HTTP origin.
ALLOWED_SCHEMES = ("http", "https")

#: The destination ranges a local endpoint may address.
PRIVATE_NETWORKS: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("fc00::/7"),
)

#: A path prefix is a short sequence of plain segments. Anything else - a
#: query, a fragment, percent-encoding, a traversal segment - is refused
#: rather than normalised, because a local endpoint has no reason to need
#: them and each one is a way to hide what the URL really addresses.
_PATH_SEGMENT = r"[A-Za-z0-9._~-]+"
_PATH_RE = re.compile(rf"^(?:/{_PATH_SEGMENT})*$")

#: Fixed, owner-facing reasons. They never interpolate the value that was
#: refused: the URL can carry a password-shaped string, and a refusal
#: message is exactly the kind of text that lands in a log.
REASON_SCHEME = "the endpoint must be an http or https URL"
REASON_CREDENTIAL_IN_URL = "the endpoint must not carry a username, password, query or fragment"
REASON_HOST_MISSING = "the endpoint must include a host"
REASON_PORT = "the endpoint port must be between 1 and 65535"
REASON_PATH = "the endpoint path must be a short plain prefix such as / or /v1"
REASON_PRIVATE = "the endpoint host must be a private or loopback address"
REASON_RESOLVE = "the endpoint host did not resolve only to private or loopback addresses"
REASON_UNRESOLVED = "the endpoint host could not be resolved"


class LocalEndpointError(ValueError):
    """A refused local endpoint. ``reason`` is always one of the constants."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def unwrap(address: str) -> str:
    """Return the IPv4 address behind an IPv4-mapped IPv6 literal."""
    try:
        parsed = ipaddress.ip_address(address.strip())
    except ValueError:
        return address
    mapped = getattr(parsed, "ipv4_mapped", None)
    if mapped is not None:
        return str(mapped)
    return str(parsed)


def is_private_address(address: str) -> bool:
    """True when *address* is an allowed private or loopback destination."""
    if not isinstance(address, str) or not address.strip():
        return False
    try:
        parsed = ipaddress.ip_address(unwrap(address))
    except ValueError:
        return False
    if parsed.is_unspecified or parsed.is_multicast or parsed.is_link_local:
        # ``is_link_local`` is the cloud-metadata class (169.254/16,
        # fe80::/10). It is checked explicitly as well as by range so a
        # later edit to PRIVATE_NETWORKS can never re-admit it.
        return False
    if str(parsed) == "255.255.255.255":
        return False
    return any(parsed in network for network in PRIVATE_NETWORKS)


def resolve_addresses(host: str, port: int) -> list[str]:
    """Resolve *host* at save time. Returns the addresses, possibly empty.

    This is a module-level function so a test can substitute a scripted
    answer without touching DNS.
    """
    try:
        answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        return []
    found: list[str] = []
    for entry in answers:
        sockaddr = entry[4]
        if isinstance(sockaddr, tuple) and sockaddr and isinstance(sockaddr[0], str):
            if sockaddr[0] not in found:
                found.append(sockaddr[0])
    return found


def normalize_base_url(raw: str, resolver: Callable[[str, int], list[str]] | None = None) -> str:
    """Validate and canonicalise an owner-supplied local endpoint URL.

    Returns the normalised URL: lowercase scheme, no trailing slash, and a
    trailing ``/v1`` removed - the adapter always appends ``/v1/...`` itself,
    so ``http://127.0.0.1:11434/v1`` and ``http://127.0.0.1:11434`` name the
    same server and both normalise to the second.

    Raises :class:`LocalEndpointError` with a fixed ``reason``.
    """
    if not isinstance(raw, str):
        raise LocalEndpointError(REASON_SCHEME)
    text = raw.strip()
    if not text:
        raise LocalEndpointError(REASON_HOST_MISSING)

    parts = urllib.parse.urlsplit(text)
    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise LocalEndpointError(REASON_SCHEME)
    if parts.username is not None or parts.password is not None:
        raise LocalEndpointError(REASON_CREDENTIAL_IN_URL)
    if parts.query or parts.fragment:
        raise LocalEndpointError(REASON_CREDENTIAL_IN_URL)
    host = parts.hostname
    if not host:
        raise LocalEndpointError(REASON_HOST_MISSING)
    try:
        port = parts.port
    except ValueError:
        raise LocalEndpointError(REASON_PORT) from None
    if port is None:
        port = 443 if scheme == "https" else 80
    if not 1 <= port <= 65535:
        raise LocalEndpointError(REASON_PORT)

    path = (parts.path or "").rstrip("/")
    if path and not _PATH_RE.match(path):
        raise LocalEndpointError(REASON_PATH)
    segments = [segment for segment in path.split("/") if segment]
    if any(segment in (".", "..") for segment in segments):
        raise LocalEndpointError(REASON_PATH)
    # The adapter appends /v1/... to whatever is stored, so an owner who
    # pastes the documented OpenAI root (…/v1) gets the same endpoint as an
    # owner who pastes the server root.
    if segments and segments[-1] == "v1":
        segments = segments[:-1]
    path = "".join(f"/{segment}" for segment in segments)

    try:
        ipaddress.ip_address(host)
    except ValueError:
        addresses = (resolver or resolve_addresses)(host, port)
        if not addresses:
            raise LocalEndpointError(REASON_UNRESOLVED) from None
        if not all(is_private_address(address) for address in addresses):
            raise LocalEndpointError(REASON_RESOLVE) from None
    else:
        if not is_private_address(host):
            raise LocalEndpointError(REASON_PRIVATE) from None

    host_out = f"[{host}]" if ":" in host else host
    default_port = 443 if scheme == "https" else 80
    authority = host_out if port == default_port else f"{host_out}:{port}"
    return f"{scheme}://{authority}{path}"


def peer_address(sock: Any) -> str | None:
    """The peer address of a connected socket, or ``None`` if unreadable."""
    try:
        name: Sequence[Any] = sock.getpeername()
    except (OSError, AttributeError):
        return None
    if not isinstance(name, tuple) or not name:
        return None
    first = name[0]
    return first if isinstance(first, str) and first else None


def make_peer_check(provider_id: str) -> Callable[[Any], None]:
    """Build the connect-time destination check for one provider.

    The callable takes a connected socket, reads the address it actually
    reached and raises :class:`AdapterError` - carrying a fixed message and
    never the address - when that address is outside the private ranges. The
    caller closes the socket on failure: the check runs inside connection
    setup, before any byte of the request or of a TLS handshake is written.
    """
    def check(sock: Any) -> None:
        address = peer_address(sock)
        if address is None or not is_private_address(address):
            raise AdapterError(
                ERROR_POLICY,
                "the configured local endpoint did not resolve to a private address",
                provider_id=provider_id,
            )

    return check
