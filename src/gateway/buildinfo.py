"""Release identity.

``GET /version`` returns a fixed four-key schema. It must never return
configuration, credentials, prompts, completions, provider account details,
or arbitrary environment values.

Two sources feed the identity:

``source_revision`` / ``source_tree``
    Baked into the image at build time from the git commit and git tree that
    were checked out. ``unknown`` when the build had no git context.

``image_revision``
    The immutable sha256 digest of the image that is actually running. A
    digest is only knowable *after* the image exists, so it is written by the
    release step into ``release.json`` on the persistent volume. The gateway
    prefers that file and falls back to the build-time value, then to
    ``unknown``.

Nothing in this module reads an arbitrary environment variable into the
response: only the four fields below are ever exposed.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

SERVICE = "personal-llm-gateway"

UNKNOWN = "unknown"

#: Build-time values. Overridden by the Dockerfile via ARG.
_BUILD_SOURCE_REVISION = os.environ.get("GATEWAY_SOURCE_REVISION", UNKNOWN)
_BUILD_SOURCE_TREE = os.environ.get("GATEWAY_SOURCE_TREE", UNKNOWN)
_BUILD_IMAGE_REVISION = os.environ.get("GATEWAY_IMAGE_REVISION", UNKNOWN)

#: Where the release step writes the running image digest.
DEFAULT_RELEASE_PATH = Path(os.environ.get("GATEWAY_RELEASE_FILE", "/var/lib/gateway/release.json"))


def _valid_revision(value: str) -> str:
    """Accept only a commit/tree/digest-shaped string.

    A commit is 7-40 hex characters, a git tree is 40 hex characters, and an
    image digest is ``sha256:`` followed by 64 hex characters. Anything else
    is reported as ``unknown`` rather than echoed, so a hostile or mistaken
    environment value cannot smuggle arbitrary text into ``/version``.
    """
    if not isinstance(value, str):
        return UNKNOWN
    value = value.strip()
    if value.startswith("sha256:"):
        digest = value[len("sha256:") :]
        if len(digest) == 64 and all(c in "0123456789abcdef" for c in digest.lower()):
            return "sha256:" + digest.lower()
        return UNKNOWN
    if 7 <= len(value) <= 40 and all(c in "0123456789abcdef" for c in value.lower()):
        return value.lower()
    return UNKNOWN


def _read_release_file(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def release_identity(release_path: Path | None = None) -> dict[str, str]:
    """Return the fixed four-key release identity."""
    path = release_path if release_path is not None else DEFAULT_RELEASE_PATH
    release = _read_release_file(path)

    source_revision = _valid_revision(release.get("source_revision") or _BUILD_SOURCE_REVISION)
    source_tree = _valid_revision(release.get("source_tree") or _BUILD_SOURCE_TREE)
    image_revision = _valid_revision(release.get("image_revision") or _BUILD_IMAGE_REVISION)

    return {
        "service": SERVICE,
        "source_revision": source_revision,
        "source_tree": source_tree,
        "image_revision": image_revision,
    }
