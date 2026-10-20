"""Upstream conversation identifiers.

OpenCode Go **requires** an ``x-opencode-session`` header and refuses a
request without one, so the gateway must always supply one. Most
OpenAI-compatible clients send no session header at all, so it cannot simply
be passed through.

The value is derived server-side:

* **stable** for the same client and alias, so the provider can route and
  cache the conversation;
* **different** for different clients, so two callers cannot collide;
* **validated**, so a caller-supplied hint cannot smuggle bytes upstream or
  forge another client's session;
* **scoped to this process**, because the derivation key is generated at
  start-up, so a restart ends the session rather than extending it forever.

The provider scope is added by the router, because the provider that serves
a request is not known until a route is chosen.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets

#: The derivation key. Generated once per process.
_SECRET = secrets.token_bytes(32)

#: A caller-supplied hint is reduced to this alphabet: no control
#: characters, no separators, nothing that could be read as a header or a
#: field delimiter.
_HINT_PATTERN = re.compile(r"[^A-Za-z0-9._-]")

MAX_HINT_LENGTH = 32


def sanitize_hint(hint: str | None) -> str:
    """Reduce a caller-supplied hint to a safe, bounded token."""
    if not hint:
        return ""
    return _HINT_PATTERN.sub("", hint.strip())[:MAX_HINT_LENGTH]


def derive_session(
    discriminator: str | None,
    alias_name: str,
    client_ip: str | None,
    hint: str | None = None,
) -> str:
    """Derive the upstream conversation identifier.

    *discriminator* identifies the caller - the client profile id, or
    ``anonymous``. *alias_name* and *client_ip* scope it to one client's
    conversation with one alias. *hint* is an optional client-supplied
    conversation name.
    """
    seed = f"{discriminator or 'anonymous'}|{alias_name}|{client_ip or ''}"
    digest = hmac.new(_SECRET, seed.encode("utf-8"), hashlib.sha256).hexdigest()[:24]
    safe_hint = sanitize_hint(hint)
    return f"{digest}.{safe_hint}" if safe_hint else digest
