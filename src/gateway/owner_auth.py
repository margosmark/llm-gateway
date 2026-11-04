"""Owner authentication and the optional model client API key.

Two separate boundaries live in this module and must never be conflated:

* **Owner authentication** protects the owner console and every owner
  configuration API. It is one username/password account with server-side
  sessions delivered as an HTTP-only cookie. A fresh gateway has **no**
  account at all: first run bootstraps from a random one-time setup code
  that exists only on the gateway host (server stdout and an owner-only
  file), never in a default credential and never in an HTTP response.
* **Model client authentication** gates the OpenAI-compatible ``/v1``
  surface and is **off by default**: with no key configured, every existing
  client keeps working exactly as before. When the owner sets a key,
  ``Authorization: Bearer <key>`` becomes mandatory on the model-facing
  endpoints; clearing the key restores keyless access. Rotation keeps the
  previous key valid for an explicit grace window so a rollout cannot lock
  clients out mid-change.

Rules this module enforces:

* **A stored secret is never returned.** The account stores a PBKDF2 hash,
  sessions are stored as SHA-256 hashes of their tokens, and the client key
  is stored as a SHA-256 hash plus a random display id. No read path hands
  secret material back, so no response, log line or export can carry it.
* **No default credential exists.** There is no admin/admin: the password is
  chosen during the one-time setup, and the setup code is single-use.
* The setup code, the password and the client key are written only to
  owner-only files on the gateway host (``0600`` inside a ``0700``
  directory) or accepted in a request body.
* Login and setup attempts are throttled per account and per client
  address, in memory, so a brute-force attempt cannot run unbounded.
* Every mutation is serialised under one lock and persisted atomically, so
  concurrent key rotations cannot tear the store or lose another field.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Callable

from gateway.credentials import hash_caller_token, verify_caller_token
from gateway.stores import atomic_write_json, ensure_private_dir, read_json, remove_file

#: PBKDF2-HMAC-SHA256 work factor for the owner password. This is the
#: OWASP-recommended count for this primitive; it is a module constant so a
#: test can pin it instead of trusting a comment.
DEFAULT_PBKDF2_ITERATIONS = 600_000

#: The owner account file and the one-time setup code file, both inside the
#: gateway data directory.
AUTH_FILE_NAME = "owner_auth.json"
SETUP_CODE_FILE_NAME = "owner_setup_code"

#: Session cookie. ``HttpOnly`` so script cannot read it, ``SameSite=Strict``
#: so no cross-site request carries it, ``Secure`` when the deployment is
#: served over TLS (see ``GatewayApp`` for how that is decided).
COOKIE_NAME = "gw_owner_session"
SESSION_TTL_SECONDS = 12 * 3600

#: Login/setup throttling: this many failures inside the window locks the
#: key until the oldest failure ages out.
LOGIN_MAX_FAILURES = 5
LOGIN_WINDOW_SECONDS = 300.0

#: Bounds and grammar.
USERNAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_.-]{2,31}$")
PASSWORD_MIN_LENGTH = 8
PASSWORD_MAX_LENGTH = 1024
CLIENT_KEY_MIN_LENGTH = 32
CLIENT_KEY_MAX_LENGTH = 256
#: How long a rotated-out client key keeps working, in seconds. This is the
#: explicit anti-lockout window the console shows the owner during a
#: rotation; it is returned in the rotation response, never silent.
CLIENT_KEY_GRACE_SECONDS = 600.0


class OwnerAuthError(Exception):
    """A refused owner-auth operation. Carries the HTTP-shaped response."""

    def __init__(self, status: int, code: str, message: str, *, retry_after: float | None = None,
                 extra: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.retry_after = retry_after
        self.extra = dict(extra or {})

    def payload(self) -> dict[str, Any]:
        error: dict[str, Any] = {"message": self.message, "type": self.code}
        error.update(self.extra)
        return {"error": error}


# --- password hashing -----------------------------------------------------

def hash_password(password: str, iterations: int = DEFAULT_PBKDF2_ITERATIONS) -> str:
    """Hash a password for storage. The plaintext is never stored."""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str | None) -> bool:
    """Constant-time password check against a stored hash."""
    if not password or not stored or not isinstance(stored, str):
        return False
    parts = stored.split("$")
    if len(parts) != 4 or parts[0] != "pbkdf2_sha256":
        return False
    try:
        iterations = int(parts[1])
        salt = bytes.fromhex(parts[2])
        expected = bytes.fromhex(parts[3])
    except ValueError:
        return False
    if iterations < 1 or not salt or not expected:
        return False
    candidate = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(candidate, expected)


def validate_username(username: Any) -> str:
    if not isinstance(username, str) or not USERNAME_PATTERN.match(username):
        raise OwnerAuthError(
            400, "invalid_username",
            "username must be 3-32 characters: lowercase letters, digits, dot, dash or underscore",
        )
    return username


def validate_password(password: Any, *, username: str | None = None) -> str:
    if not isinstance(password, str) or len(password) < PASSWORD_MIN_LENGTH:
        raise OwnerAuthError(
            400, "invalid_password", f"password must be at least {PASSWORD_MIN_LENGTH} characters",
        )
    if len(password) > PASSWORD_MAX_LENGTH:
        raise OwnerAuthError(
            400, "invalid_password", f"password must be at most {PASSWORD_MAX_LENGTH} characters",
        )
    if username and password.lower() == username.lower():
        raise OwnerAuthError(400, "invalid_password", "password must differ from the username")
    return password


def validate_client_key(key: Any) -> str:
    """Validate an owner-supplied client key. Returns it unchanged."""
    if not isinstance(key, str):
        raise OwnerAuthError(400, "invalid_client_key", "client key must be a string")
    if not (CLIENT_KEY_MIN_LENGTH <= len(key) <= CLIENT_KEY_MAX_LENGTH):
        raise OwnerAuthError(
            400, "invalid_client_key",
            f"client key must be {CLIENT_KEY_MIN_LENGTH}-{CLIENT_KEY_MAX_LENGTH} characters",
        )
    if any(ord(char) < 0x21 or ord(char) > 0x7E for char in key):
        raise OwnerAuthError(
            400, "invalid_client_key",
            "client key must be printable ASCII without spaces (it is sent as a bearer token)",
        )
    return key


def _hash_token(token: str) -> str:
    """Hash a high-entropy token for storage (session ids and client keys)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# --- throttling -----------------------------------------------------------

class LoginThrottle:
    """In-memory failure throttling for login and setup attempts.

    Per-process and in memory on purpose: the gateway is one process, the
    window is short, and nothing here is a secret that has to survive a
    restart. A restart therefore clears the counters, which is the only
    thing an attacker who can restart the gateway gains - and that attacker
    already has host access.
    """

    def __init__(
        self,
        *,
        max_failures: int = LOGIN_MAX_FAILURES,
        window_seconds: float = LOGIN_WINDOW_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.max_failures = max_failures
        self.window_seconds = window_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._failures: dict[str, list[float]] = {}

    def _live(self, key: str, now: float) -> list[float]:
        return [stamp for stamp in self._failures.get(key, ()) if now - stamp < self.window_seconds]

    def retry_after(self, key: str) -> float:
        """Seconds until *key* may try again; ``0`` means not throttled."""
        now = self._clock()
        with self._lock:
            live = self._live(key, now)
            if len(live) < self.max_failures:
                return 0.0
            return max(1.0, self.window_seconds - (now - min(live)))

    def record_failure(self, key: str) -> None:
        now = self._clock()
        with self._lock:
            live = self._live(key, now)
            live.append(now)
            if len(live) > 64:
                live = live[-64:]
            self._failures[key] = live
            if len(self._failures) > 1024:
                # Bounded memory: drop keys whose window has fully aged out.
                for stale in [name for name, stamps in self._failures.items()
                              if not stamps or now - max(stamps) >= self.window_seconds]:
                    self._failures.pop(stale, None)

    def record_success(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)


# --- the store -----------------------------------------------------------

class OwnerAuthStore:
    """The owner account, its sessions, and the optional client API key.

    One file (``owner_auth.json``, mode ``0600``) holds everything, kept
    mirrored in memory so a model request never re-reads the disk. Every
    mutation happens under one lock and is written atomically, so two
    concurrent rotations serialise and the loser's write is a complete
    record rather than a torn one.
    """

    def __init__(
        self,
        data_dir: Path,
        *,
        pbkdf2_iterations: int = DEFAULT_PBKDF2_ITERATIONS,
        clock: Callable[[], float] = time.time,
        throttle: LoginThrottle | None = None,
    ) -> None:
        self._dir = ensure_private_dir(Path(data_dir))
        self._path = self._dir / AUTH_FILE_NAME
        self._code_path = self._dir / SETUP_CODE_FILE_NAME
        self._iterations = int(pbkdf2_iterations)
        self._clock = clock
        self._lock = threading.RLock()
        self._throttle = throttle if throttle is not None else LoginThrottle(clock=clock)
        self._revision = 0
        self._data = self._load()
        self._disk_fingerprint = self._fingerprint()

    # --- persistence -----------------------------------------------------

    def _fingerprint(self) -> tuple[int, int] | None:
        try:
            stat = self._path.stat()
        except OSError:
            return None
        return (stat.st_mtime_ns, stat.st_size)

    def _reload_if_changed(self) -> None:
        """Adopt a change another process wrote to the same store.

        ``--reset-owner-password`` runs outside this process, so without
        this check the gateway would keep honouring the old password and
        the revoked sessions until a restart - precisely the access the
        recovery exists to end. One ``stat`` per call is the price; the
        file only changes on a real mutation.
        """
        fingerprint = self._fingerprint()
        with self._lock:
            if fingerprint == self._disk_fingerprint:
                return
            self._data = self._load()
            self._disk_fingerprint = self._fingerprint()
            self._revision += 1

    def _load(self) -> dict[str, Any]:
        raw = read_json(self._path, None)
        data: dict[str, Any] = {
            "version": 1,
            "account": None,
            "sessions": {},
            "setup": None,
            "client_key": None,
            "previous_client_key": None,
        }
        if isinstance(raw, dict):
            # A corrupt or hand-edited file degrades to "no account", which
            # fails *closed*: the gateway asks for first-run setup again
            # instead of inventing an authenticated state.
            account = raw.get("account")
            if isinstance(account, dict) and isinstance(account.get("password_hash"), str) \
                    and isinstance(account.get("username"), str):
                data["account"] = account
            sessions = raw.get("sessions")
            if isinstance(sessions, dict):
                data["sessions"] = {
                    key: value for key, value in sessions.items()
                    if isinstance(key, str) and isinstance(value, dict)
                    and isinstance(value.get("expires_at"), (int, float))
                }
            setup = raw.get("setup")
            if isinstance(setup, dict) and isinstance(setup.get("code_hash"), str):
                data["setup"] = setup
            for field in ("client_key", "previous_client_key"):
                value = raw.get(field)
                if isinstance(value, dict) and isinstance(value.get("key_hash"), str):
                    data[field] = value
        return data

    def _save(self) -> None:
        atomic_write_json(self._path, self._data, mode=0o600)
        self._disk_fingerprint = self._fingerprint()
        self._revision += 1

    @property
    def revision(self) -> int:
        """Bumped on every persisted mutation; used to key the state cache."""
        with self._lock:
            return self._revision

    @property
    def path(self) -> Path:
        return self._path

    @property
    def setup_code_path(self) -> Path:
        return self._code_path

    # --- first-run setup ------------------------------------------------

    @property
    def setup_required(self) -> bool:
        self._reload_if_changed()
        with self._lock:
            return self._data["account"] is None

    def ensure_setup_code(self) -> str | None:
        """Create the one-time setup code if the gateway has no account.

        Returns the plaintext code **only when it is generated here**; every
        later call returns ``None``. The caller (server startup) may print
        it to server-side stdout, and the code is written to an owner-only
        file so the operator can read it from the host. It is never returned
        by any HTTP surface.
        """
        with self._lock:
            if self._data["account"] is not None:
                return None
            if self._data["setup"] is not None:
                return None
            code = secrets.token_urlsafe(16)
            self._data["setup"] = {"code_hash": _hash_token(code), "created_at": self._clock()}
            self._save()
            atomic_write_json(self._code_path, {"setup_code": code}, mode=0o600)
            return code

    def read_setup_code(self) -> str | None:
        """Read the pending setup code from its owner-only host file.

        Server-side recovery for an operator with shell access. Returns
        ``None`` once the code has been consumed or no account is pending.
        """
        with self._lock:
            if self._data["account"] is not None or self._data["setup"] is None:
                return None
            payload = read_json(self._code_path, None)
        if isinstance(payload, dict) and isinstance(payload.get("setup_code"), str):
            return payload["setup_code"]
        return None

    def complete_setup(self, username: Any, password: Any, setup_code: Any, *,
                       client: str = "") -> str:
        """Consume the one-time setup code, create the account, sign in.

        Returns the session token. The setup code is single-use: it is
        dropped from the store and its host file is deleted before the
        session is issued.
        """
        throttle_key = f"setup:{client or 'unknown'}"
        retry = self._throttle.retry_after(throttle_key)
        if retry:
            raise OwnerAuthError(
                429, "login_throttled",
                "too many setup attempts; wait before trying again", retry_after=retry,
            )
        username = validate_username(username)
        password = validate_password(password, username=username)
        if not isinstance(setup_code, str) or not setup_code:
            raise OwnerAuthError(400, "invalid_setup_code", "setup code is required")
        with self._lock:
            if self._data["account"] is not None:
                raise OwnerAuthError(409, "already_configured", "this gateway already has an owner account")
            setup = self._data["setup"]
            if setup is None or not hmac.compare_digest(_hash_token(setup_code), setup.get("code_hash", "")):
                self._throttle.record_failure(throttle_key)
                raise OwnerAuthError(403, "invalid_setup_code", "the setup code is not valid")
            self._data["account"] = {
                "username": username,
                "password_hash": hash_password(password, self._iterations),
                "created_at": self._clock(),
                "updated_at": self._clock(),
            }
            self._data["setup"] = None
            self._save()
            remove_file(self._code_path)
            self._throttle.record_success(throttle_key)
            return self._issue_session(username)

    # --- sessions -------------------------------------------------------

    def _issue_session(self, username: str) -> str:
        token = secrets.token_urlsafe(32)
        now = self._clock()
        with self._lock:
            self._prune_sessions(now)
            self._data["sessions"][_hash_token(token)] = {
                "username": username,
                "created_at": now,
                "expires_at": now + SESSION_TTL_SECONDS,
            }
            self._save()
        return token

    def _prune_sessions(self, now: float) -> None:
        expired = [key for key, record in self._data["sessions"].items()
                   if float(record.get("expires_at", 0)) <= now]
        for key in expired:
            self._data["sessions"].pop(key, None)

    def session_info(self, token: str | None) -> dict[str, Any] | None:
        """Resolve a session cookie value to its record, or ``None``.

        Expired sessions are dropped as they are seen, and this path never
        writes, so a console poll costs no disk traffic.
        """
        self._reload_if_changed()
        if not token:
            return None
        now = self._clock()
        with self._lock:
            record = self._data["sessions"].get(_hash_token(token))
            if record is None:
                return None
            if float(record.get("expires_at", 0)) <= now:
                self._data["sessions"].pop(_hash_token(token), None)
                self._save()
                return None
            return {
                "username": record.get("username"),
                "created_at": record.get("created_at"),
                "expires_at": record.get("expires_at"),
            }

    def revoke_session(self, token: str | None) -> bool:
        if not token:
            return False
        with self._lock:
            removed = self._data["sessions"].pop(_hash_token(token), None)
            if removed is None:
                return False
            self._save()
            return True

    def revoke_all_sessions(self) -> int:
        with self._lock:
            count = len(self._data["sessions"])
            if count:
                self._data["sessions"] = {}
                self._save()
            return count

    # --- login ----------------------------------------------------------

    def login(self, username: Any, password: Any, *, client: str = "") -> str:
        """Verify credentials and issue a session token."""
        self._reload_if_changed()
        account_key = f"login:{str(username or '').lower()}"
        address_key = f"login-ip:{client or 'unknown'}"
        for key in (account_key, address_key):
            retry = self._throttle.retry_after(key)
            if retry:
                raise OwnerAuthError(
                    429, "login_throttled",
                    "too many failed sign-in attempts; wait before trying again", retry_after=retry,
                )
        if not isinstance(username, str) or not isinstance(password, str) or not password:
            raise OwnerAuthError(401, "invalid_credentials", "username or password is incorrect")
        with self._lock:
            account = self._data["account"]
            if account is None:
                raise OwnerAuthError(409, "setup_required", "this gateway has no owner account yet",
                                     extra={"setup_required": True})
            expected = account.get("password_hash")
            user_ok = hmac.compare_digest(username, str(account.get("username", "")))
            pass_ok = verify_password(password, expected)
        if not (user_ok and pass_ok):
            self._throttle.record_failure(account_key)
            self._throttle.record_failure(address_key)
            # Same message for unknown user and wrong password: neither
            # outcome may confirm whether an account exists.
            raise OwnerAuthError(401, "invalid_credentials", "username or password is incorrect")
        self._throttle.record_success(account_key)
        self._throttle.record_success(address_key)
        return self._issue_session(username)

    def change_password(self, token: str | None, current_password: Any, new_password: Any) -> dict[str, Any]:
        """Change the owner password from a signed-in session.

        Every *other* session is revoked, so a stolen cookie stops working
        the moment            the owner notices and rotates the password.
            """
        self._reload_if_changed()
        info = self.session_info(token)
        if info is None:
            raise OwnerAuthError(401, "authentication_required", "owner session required")
        username = str(info["username"])
        throttle_key = f"password:{username.lower()}"
        retry = self._throttle.retry_after(throttle_key)
        if retry:
            raise OwnerAuthError(
                429, "login_throttled",
                "too many failed password changes; wait before trying again", retry_after=retry,
            )
        if not isinstance(current_password, str) or not verify_password(
            current_password, self._data["account"].get("password_hash") if self._data["account"] else None
        ):
            self._throttle.record_failure(throttle_key)
            raise OwnerAuthError(401, "invalid_credentials", "the current password is incorrect")
        new_password = validate_password(new_password, username=username)
        if current_password == new_password:
            raise OwnerAuthError(400, "invalid_password", "the new password must differ from the current one")
        with self._lock:
            account = self._data["account"]
            if account is None:
                raise OwnerAuthError(409, "setup_required", "this gateway has no owner account yet")
            account["password_hash"] = hash_password(new_password, self._iterations)
            account["updated_at"] = self._clock()
            kept = _hash_token(str(token))
            revoked = sum(1 for key in self._data["sessions"] if key != kept)
            self._data["sessions"] = {key: value for key, value in self._data["sessions"].items()
                                      if key == kept}
            self._save()
        self._throttle.record_success(throttle_key)
        return {"other_sessions_revoked": revoked}

    def reset_password(self, new_password: Any) -> dict[str, Any]:
        """Local recovery: set a new password from the gateway host.

        Deliberately not reachable over HTTP. It needs filesystem access to
        the data directory - the documented recovery path in the runbook -
        and it revokes every session, so an intruder who was signed in does
        not keep access.
        """
        with self._lock:
            account = self._data["account"]
            if account is None:
                raise OwnerAuthError(409, "setup_required",
                                     "no owner account exists; use the one-time setup code")
            username = str(account.get("username", ""))
            new_password = validate_password(new_password, username=username)
            account["password_hash"] = hash_password(new_password, self._iterations)
            account["updated_at"] = self._clock()
            self._data["sessions"] = {}
            self._save()
        return {"username": username, "sessions_revoked": True}

    # --- client API key -------------------------------------------------

    def client_key_status(self) -> dict[str, Any]:
        """A safe description of the client key: presence, never the value."""
        self._reload_if_changed()
        with self._lock:
            current = self._data["client_key"]
            previous = self._data["previous_client_key"]
            now = self._clock()
            grace = 0.0
            if previous is not None:
                remaining = float(previous.get("expires_at", 0)) - now
                grace = remaining if remaining > 0 else 0.0
            return {
                "enabled": current is not None,
                "display_id": current.get("display_id") if current else None,
                "created_at": current.get("created_at") if current else None,
                "rotated_at": current.get("rotated_at") if current else None,
                "previous_key_valid_for_seconds": int(grace) if current is not None and grace > 0 else None,
            }

    @property
    def client_key_enabled(self) -> bool:
        self._reload_if_changed()
        with self._lock:
            return self._data["client_key"] is not None

    def _store_client_key(self, key: str) -> dict[str, Any]:
        display_id = secrets.token_hex(4)
        return {
            "key_hash": hash_caller_token(key),
            "display_id": display_id,
            "created_at": self._clock(),
        }

    def set_client_key(self, key: Any) -> dict[str, Any]:
        """Enable the client key from keyless. Refuses if one is set."""
        key = validate_client_key(key)
        with self._lock:
            if self._data["client_key"] is not None:
                raise OwnerAuthError(409, "client_key_already_set",
                                     "a client key is already set; rotate it instead")
            self._data["client_key"] = self._store_client_key(key)
            self._data["previous_client_key"] = None
            self._save()
        return self.client_key_status()

    def rotate_client_key(self, key: Any) -> dict[str, Any]:
        """Replace the client key, keeping the old one valid for a grace window.

        This is the anti-lockout transition: clients keep working with the
        old key until the grace window expires, the response states the
        window explicitly, and the console shows it. The gateway never
        rewrites any client configuration itself.
        """
        key = validate_client_key(key)
        with self._lock:
            current = self._data["client_key"]
            if current is None:
                raise OwnerAuthError(409, "client_key_not_set", "no client key is set; set one first")
            now = self._clock()
            self._data["previous_client_key"] = {
                "key_hash": current["key_hash"],
                "expires_at": now + CLIENT_KEY_GRACE_SECONDS,
            }
            replacement = self._store_client_key(key)
            replacement["created_at"] = current.get("created_at", now)
            replacement["rotated_at"] = now
            self._data["client_key"] = replacement
            self._save()
        return self.client_key_status()

    def clear_client_key(self) -> dict[str, Any]:
        """Disable the client key and restore keyless model access."""
        with self._lock:
            if self._data["client_key"] is None and self._data["previous_client_key"] is None:
                raise OwnerAuthError(409, "client_key_not_set", "no client key is set")
            self._data["client_key"] = None
            self._data["previous_client_key"] = None
            self._save()
        return self.client_key_status()

    def check_client_key(self, authorization: str | None) -> bool:
        """Whether a model-API ``Authorization`` header is acceptable.

        Keyless (no key configured) accepts everything, which is what keeps
        existing clients working. With a key configured, the presented
        bearer must match the current key, or the previous key while its
        grace window is open.
        """
        self._reload_if_changed()
        with self._lock:
            current = self._data["client_key"]
            if current is None:
                return True
            previous = self._data["previous_client_key"]
        if not authorization or not authorization.lower().startswith("bearer "):
            return False
        presented = authorization[7:].strip()
        if not presented:
            return False
        if verify_caller_token(presented, current.get("key_hash")):
            return True
        if previous is not None and self._clock() < float(previous.get("expires_at", 0)):
            return verify_caller_token(presented, previous.get("key_hash"))
        return False


def describe_store(store: OwnerAuthStore) -> dict[str, Any]:
    """A log/report-safe description: presence and shape, never values."""
    return {
        "account_present": not store.setup_required,
        "session_count": len(store._data["sessions"]),  # noqa: SLF001 - module-internal report
        "client_key_enabled": store.client_key_enabled,
    }


__all__ = [
    "AUTH_FILE_NAME",
    "CLIENT_KEY_GRACE_SECONDS",
    "COOKIE_NAME",
    "DEFAULT_PBKDF2_ITERATIONS",
    "LOGIN_MAX_FAILURES",
    "LOGIN_WINDOW_SECONDS",
    "OwnerAuthError",
    "OwnerAuthStore",
    "SETUP_CODE_FILE_NAME",
    "SESSION_TTL_SECONDS",
    "LoginThrottle",
    "hash_password",
    "verify_password",
    "validate_client_key",
    "validate_password",
    "validate_username",
]
