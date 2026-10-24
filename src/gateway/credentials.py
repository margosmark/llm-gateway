"""The owner-only credential store.

Rules this module enforces:

* **A saved credential is never re-displayed.** There is no read API that
  returns secret material to a caller that could serialise it into a
  response. The only read path is :meth:`ProviderCredentialHandle.read`,
  which hands the payload to the adapter that owns that provider, and the
  console never calls it.
* **Provider isolation is structural, not conventional.** Each provider's
  credential lives in its own file, and access is through a handle bound to
  one provider id. A handle for one provider cannot read another's file.
* **Files are owner-only and written atomically.** Mode ``0600`` inside a
  ``0700`` directory, written via a temp file plus ``os.replace``.
* **Local removal only.** Deleting a credential here removes it from this
  machine. Provider-side session revocation remains the account owner's
  action.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import threading
from pathlib import Path
from typing import Any, Iterable

from gateway.stores import atomic_write_json, ensure_private_dir, read_json, remove_file

CREDENTIAL_DIR_NAME = "credentials"

#: Providers that may hold a credential. Anything else is rejected so a
#: crafted provider id cannot escape the credential directory.
KNOWN_PROVIDERS = ("chatgpt_codex", "opencode_go", "opencode_console", "openai_api")


class CredentialError(Exception):
    """Raised for invalid credential operations."""


def _validate_store_id(store_id: str) -> str:
    if not isinstance(store_id, str) or not store_id:
        raise CredentialError("credential store id must be a non-empty string")
    if not store_id.replace("_", "").replace("-", "").isalnum():
        raise CredentialError("credential store id contains unsupported characters")
    if store_id in {".", ".."}:
        raise CredentialError("invalid credential store id")
    return store_id


class ProviderCredentialHandle:
    """A handle bound to exactly one credential store id.

    The handle is what an adapter receives. It cannot be pointed at another
    provider's file after construction, which is what makes provider
    isolation a structural property rather than a convention.
    """

    __slots__ = ("_store", "_store_id")

    def __init__(self, store: "CredentialStore", store_id: str) -> None:
        self._store = store
        self._store_id = _validate_store_id(store_id)

    @property
    def store_id(self) -> str:
        return self._store_id

    def read(self) -> dict[str, Any] | None:
        """Return the stored payload for this handle's provider only."""
        return self._store._read(self._store_id)

    def write(self, payload: dict[str, Any]) -> None:
        self._store._write(self._store_id, payload)

    def clear(self) -> bool:
        return self._store._clear(self._store_id)

    def exists(self) -> bool:
        return self._store._read(self._store_id) is not None


class CredentialStore:
    """Filesystem-backed credential store, one file per provider."""

    def __init__(self, root: Path) -> None:
        self._root = ensure_private_dir(root / CREDENTIAL_DIR_NAME)
        self._lock = threading.RLock()

    @property
    def root(self) -> Path:
        return self._root

    def handle(self, store_id: str) -> ProviderCredentialHandle:
        """Return a handle bound to *store_id*."""
        return ProviderCredentialHandle(self, store_id)

    def provider_handle(self, provider_id: str) -> ProviderCredentialHandle:
        if provider_id not in KNOWN_PROVIDERS:
            raise CredentialError(f"unknown provider id: {provider_id!r}")
        return ProviderCredentialHandle(self, provider_id)

    def _path_for(self, store_id: str) -> Path:
        return self._root / f"{_validate_store_id(store_id)}.json"

    def _read(self, store_id: str) -> dict[str, Any] | None:
        with self._lock:
            data = read_json(self._path_for(store_id), None)
        return data if isinstance(data, dict) else None

    def _write(self, store_id: str, payload: dict[str, Any]) -> None:
        if not isinstance(payload, dict):
            raise CredentialError("credential payload must be a mapping")
        with self._lock:
            atomic_write_json(self._path_for(store_id), payload, mode=0o600)

    def _clear(self, store_id: str) -> bool:
        with self._lock:
            return remove_file(self._path_for(store_id))

    def list_store_ids(self) -> list[str]:
        ids: list[str] = []
        try:
            entries = sorted(self._root.iterdir())
        except OSError:
            return ids
        for entry in entries:
            if entry.suffix == ".json" and entry.stem.replace("-", "").replace("_", "").isalnum():
                ids.append(entry.stem)
        return ids

    def describe(self, store_id: str) -> dict[str, Any]:
        """Return a **safe** description: presence and shape, never values.

        This is what the console renders. It reports which keys exist so the
        owner can tell "a key is stored" from "nothing is stored", without
        any secret crossing the boundary.
        """
        payload = self._read(store_id)
        if payload is None:
            return {"store_id": store_id, "present": False, "fields": []}
        fields = sorted(key for key in payload.keys() if isinstance(key, str))
        return {"store_id": store_id, "present": True, "fields": fields}

    def permissions_are_owner_only(self, store_id: str) -> bool:
        path = self._path_for(store_id)
        try:
            mode = path.stat().st_mode & 0o777
        except OSError:
            return False
        return mode == 0o600



def hash_caller_token(token: str) -> str:
    """Hash a high-entropy caller token for storage.

    Caller tokens are 256-bit random values, so a fast hash is appropriate
    here: there is no low-entropy secret to brute force.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def verify_caller_token(token: str, stored_hash: str | None) -> bool:
    if not token or not stored_hash:
        return False
    return hmac.compare_digest(hash_caller_token(token), stored_hash)


def new_caller_token() -> str:
    """Mint a new caller token. Shown once, to the caller, only."""
    return "gw_" + secrets.token_urlsafe(32)



def provider_ids() -> Iterable[str]:
    return KNOWN_PROVIDERS


def file_mode(path: Path) -> int:
    try:
        return path.stat().st_mode & 0o777
    except OSError:
        return -1


def ensure_directory_mode(path: Path) -> bool:
    try:
        return (path.stat().st_mode & 0o777) == 0o700
    except OSError:
        return False


__all__ = [
    "CredentialError",
    "CredentialStore",
    "ProviderCredentialHandle",
    "KNOWN_PROVIDERS",

    "hash_caller_token",
    "verify_caller_token",
    "new_caller_token",

    "file_mode",
    "ensure_directory_mode",
    "os",
]
