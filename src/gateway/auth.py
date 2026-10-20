"""Authentication and authorisation.

Three separate identities, deliberately kept apart:

**Owner session**
    The person operating the console. Established by an owner passphrase,
    held in an ``HttpOnly`` cookie, and paired with a CSRF token for every
    mutating request. The passphrase is stored as a PBKDF2 hash in the
    owner-only credential store; it is never stored, logged, or displayed.

**Client profile**
    A LAN client. In authenticated mode it presents a bearer token; in LAN
    no-auth mode every request uses the single **Anonymous LAN profile**.
    A profile scopes which *aliases* a caller may use. It never scopes
    upstream identity - all callers share the owner's single upstream
    account, and the console says so.

**Setup surface**
    Reachable only from loopback, and only with an owner session. This is
    where credential enrollment happens.
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from gateway.config import (
    ACCESS_AUTHENTICATED,
    ACCESS_LAN_NOAUTH,
    ConfigStore,
    Profile,
)
from gateway.credentials import (
    OWNER_STORE,
    CredentialStore,
    hash_caller_token,
    hash_passphrase,
    new_caller_token,
    verify_caller_token,
    verify_passphrase,
)
from gateway.http.security import CSRF_COOKIE, SESSION_COOKIE

#: The identifier of the single anonymous profile used in LAN no-auth mode.
ANONYMOUS_PROFILE_ID = "anonymous-lan"

#: Owner sessions expire after this long.
SESSION_TTL_SECONDS = 12 * 60 * 60

#: PBKDF2 iterations for the owner passphrase. Lower in tests.
PBKDF2_ITERATIONS = 240_000
PBKDF2_ITERATIONS_TEST = 1_000


@dataclass
class OwnerSession:
    """One authenticated owner session."""

    token: str
    csrf: str
    created_at: float = field(default_factory=time.time)

    def expired(self, now: float | None = None) -> bool:
        return (now if now is not None else time.time()) - self.created_at > SESSION_TTL_SECONDS


class OwnerAuth:
    """Owner passphrase, sessions, and CSRF tokens."""

    def __init__(
        self,
        credentials: CredentialStore,
        iterations: int = PBKDF2_ITERATIONS,
        session_ttl: int = SESSION_TTL_SECONDS,
    ) -> None:
        self._credentials = credentials
        self._iterations = iterations
        self._session_ttl = session_ttl
        self._sessions: dict[str, OwnerSession] = {}
        self._lock = threading.Lock()

    # --- passphrase ------------------------------------------------------

    def passphrase_is_set(self) -> bool:
        payload = self._credentials.handle(OWNER_STORE).read()
        if not payload:
            return False
        return isinstance(payload.get("passphrase_hash"), str) and bool(payload.get("passphrase_hash"))

    def set_passphrase(self, passphrase: str) -> None:
        """Set or replace the owner passphrase."""
        digest, salt = hash_passphrase(passphrase, iterations=self._iterations)
        self._credentials.handle(OWNER_STORE).write(
            {"passphrase_hash": digest, "salt": salt, "iterations": self._iterations}
        )

    def check_passphrase(self, passphrase: str) -> bool:
        payload = self._credentials.handle(OWNER_STORE).read()
        if not payload:
            return False
        digest = payload.get("passphrase_hash")
        salt = payload.get("salt")
        if not isinstance(digest, str) or not isinstance(salt, str):
            return False
        iterations = payload.get("iterations")
        if not isinstance(iterations, int):
            iterations = self._iterations
        return verify_passphrase(passphrase, digest, salt, iterations=iterations)

    # --- sessions --------------------------------------------------------

    def create_session(self) -> OwnerSession:
        session = OwnerSession(token=secrets.token_urlsafe(32), csrf=secrets.token_urlsafe(32))
        with self._lock:
            self._prune()
            self._sessions[session.token] = session
        return session

    def get_session(self, token: str | None) -> OwnerSession | None:
        if not token:
            return None
        with self._lock:
            self._prune()
            session = self._sessions.get(token)
        if session is None:
            return None
        if session.expired():
            self.destroy_session(token)
            return None
        return session

    def destroy_session(self, token: str | None) -> None:
        if not token:
            return
        with self._lock:
            self._sessions.pop(token, None)

    def _prune(self) -> None:
        now = time.time()
        for token in [t for t, s in self._sessions.items() if s.expired(now)]:
            self._sessions.pop(token, None)

    def session_from_cookies(self, cookies: dict[str, str]) -> OwnerSession | None:
        return self.get_session(cookies.get(SESSION_COOKIE))

    def session_cookies(self, session: OwnerSession) -> list[tuple[str, str]]:
        """The ``Set-Cookie`` headers that establish a session.

        The session cookie is ``HttpOnly`` so script cannot read it, and
        ``SameSite=Strict`` so it is never sent on a cross-site request. The
        CSRF cookie is deliberately readable by script: the console has to
        echo it back in a header, which is the double-submit pattern.
        """
        return [
            (
                "Set-Cookie",
                f"{SESSION_COOKIE}={session.token}; Path=/; HttpOnly; SameSite=Strict",
            ),
            (
                "Set-Cookie",
                f"{CSRF_COOKIE}={session.csrf}; Path=/; SameSite=Strict",
            ),
        ]


def parse_cookies(header: str | None) -> dict[str, str]:
    """Parse a ``Cookie`` header into a mapping."""
    if not header:
        return {}
    cookies: dict[str, str] = {}
    for part in header.split(";"):
        name, _, value = part.strip().partition("=")
        if name:
            cookies[name] = value
    return cookies


class ProfileAuth:
    """Resolves an API request to a client profile."""

    def __init__(self, config: ConfigStore, credentials: CredentialStore) -> None:
        self._config = config
        self._credentials = credentials

    def anonymous_profile(self) -> Profile:
        """The single profile used by every caller in LAN no-auth mode."""
        config = self._config.snapshot()
        existing = config.profiles.get(ANONYMOUS_PROFILE_ID)
        if existing is not None:
            return existing
        return Profile(
            profile_id=ANONYMOUS_PROFILE_ID,
            name="Anonymous LAN profile",
            enabled=True,
            allowed_aliases=sorted(alias.name for alias in config.published_aliases()),
            anonymous=True,
        )

    def resolve(self, bearer_token: str | None) -> tuple[Profile | None, str]:
        """Resolve a bearer token to a profile.

        Returns ``(profile, reason)``. In LAN no-auth mode the anonymous
        profile is returned regardless of the token.
        """
        config = self._config.snapshot()
        if config.access_mode == ACCESS_LAN_NOAUTH:
            return self.anonymous_profile(), "lan_noauth"

        if not bearer_token:
            return None, "missing bearer token"

        stored_hash = None
        matched: Profile | None = None
        for profile in config.profiles.values():
            if profile.anonymous or not profile.enabled:
                continue
            handle = self._credentials.handle(f"profile_{profile.profile_id}")
            payload = handle.read()
            if not payload:
                continue
            candidate = payload.get("token_hash")
            if isinstance(candidate, str) and verify_caller_token(bearer_token, candidate):
                matched = profile
                stored_hash = candidate
                break
        if matched is None:
            return None, "invalid bearer token"
        assert stored_hash is not None
        return matched, "bearer"

    def issue_token(self, profile: Profile) -> str:
        """Mint a new caller token for a profile and store its hash."""
        token = new_caller_token()
        self._credentials.handle(f"profile_{profile.profile_id}").write(
            {"token_hash": hash_caller_token(token), "issued_at": time.time()}
        )
        return token

    def revoke_token(self, profile_id: str) -> bool:
        return self._credentials.handle(f"profile_{profile_id}").clear()

    def profile_has_token(self, profile_id: str) -> bool:
        payload = self._credentials.handle(f"profile_{profile_id}").read()
        return bool(payload and payload.get("token_hash"))

    def set_profile_has_token(self, profile_id: str, has_token: bool) -> None:
        """Mirror the token-presence flag into the configuration.

        The token *hash* never enters the configuration; only the fact that a
        token exists does.
        """
        with self._config.mutate() as config:
            profile = config.profiles.get(profile_id)
            if profile is not None:
                profile.has_token = has_token


def profile_allows(profile: Profile, alias_name: str) -> bool:
    """Whether a profile may use an alias.

    A profile with no explicit allow-list may use every published alias.
    """
    if not profile.allowed_aliases:
        return True
    return alias_name in profile.allowed_aliases


def access_mode_description(mode: str) -> str:
    if mode == ACCESS_AUTHENTICATED:
        return "Authenticated mode. Each client presents a caller token and is scoped to its own allowed aliases."
    return (
        "LAN no-auth mode. Every request on this network is treated as one shared identity "
        "using the Anonymous LAN profile. There is no per-device and no per-person isolation."
    )
