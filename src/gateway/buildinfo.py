"""Release identity.

``GET /version`` returns a fixed five-key schema. It must never return
configuration, credentials, prompts, completions, provider account details,
or arbitrary environment values.

Two sources feed the identity:

``source_revision`` / ``source_tree``
    Baked into the image at build time from the git commit and git tree that
    were checked out. ``unknown`` when the build had no git context.

``image_revision``
    The immutable sha256 digest of the image that is actually running, as
    recorded by the release step. A digest is only knowable *after* the
    image exists, so ``scripts/release.py`` writes it into ``release.json``
    on the persistent volume **together with the id of the container it was
    written for**. The gateway reports the recorded digest only when that
    recorded container id equals the id of the container running right now
    (resolved read-only from ``/proc/self/mountinfo``); in every other case
    - a record left behind by an earlier cutover, a record with no
    container binding, no record at all, or a container whose own id cannot
    be resolved - it reports ``unknown``.

    The binding is fail-closed on purpose. The state volume outlives
    cutovers, so an unverified record can lag the running image (observed
    across three consecutive production cutovers, 2026-10-28/29); a digest
    that cannot be proven to belong to this container must never be
    presented as image provenance. A build-time digest is structurally
    impossible - the digest does not exist while the image is being built -
    so there is no build-time fallback for this field.

``mcp_contract_version``
    An integer handshake label for the ``gateway-owner-mcp`` write gate.
    It is a module-level literal in **this** module only: it is never read
    from ``release.json``, from the environment, or from any other file, so
    no runtime-writable surface can move it (see
    ``tests/test_mcp_contract_guard.py``).

    **It increments ONLY when one of the four gated payload families
    changes.** The four gated families are the read payloads the package
    schema-gates on:

    1. ``version``       - ``GET /version``
    2. ``state``         - ``GET /owner/api/state``
    3. ``leaderboard``   - ``GET /owner/api/leaderboard``
    4. ``capabilities``  - ``GET /owner/api/capabilities``

    "Changes" means any key added, removed or re-typed at **any** nesting
    depth of those four payloads (not merely a required-key set change: a
    nested addition such as ``state.providers[].max_in_flight`` counts).
    A change to any of them requires ``MCP_CONTRACT_VERSION`` to be
    incremented **by hand, in the same change**, together with a refresh of
    ``tests/fixtures/mcp_contract_baseline.json``.

    Nothing in this repository increments it automatically: there is no
    script, test or CI step that writes ``MCP_CONTRACT_VERSION``. An
    automatic bump would be an automatic widening of the write gate and is
    forbidden.

Nothing in this module reads an arbitrary environment variable into the
response: only the five fields below are ever exposed.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

SERVICE = "personal-llm-gateway"

UNKNOWN = "unknown"

#: The MCP write-gate contract version this gateway publishes. It is the
#: only thing ``gateway-owner-mcp`` pins on (``TESTED_CONTRACT_VERSION``);
#: see the module docstring for the four gated payload families and the
#: bump rule. Hand-edited only - never derived, never auto-incremented.
MCP_CONTRACT_VERSION = 1

#: Build-time values. Overridden by the Dockerfile via ARG.
_BUILD_SOURCE_REVISION = os.environ.get("GATEWAY_SOURCE_REVISION", UNKNOWN)
_BUILD_SOURCE_TREE = os.environ.get("GATEWAY_SOURCE_TREE", UNKNOWN)

#: Where the release step writes the running image digest.
DEFAULT_RELEASE_PATH = Path(os.environ.get("GATEWAY_RELEASE_FILE", "/var/lib/gateway/release.json"))

#: The kernel mount table of this process.
DEFAULT_MOUNTINFO_PATH = Path("/proc/self/mountinfo")

#: Docker bind-mounts ``/var/lib/docker/containers/<container id>/…``
#: (resolv.conf, hostname, hosts) into every container; the source path of
#: those mounts is the one in-container handle on the running container's
#: own id. ``/proc/self/cgroup`` cannot serve this purpose: Docker's
#: default cgroup namespace is private (``0::/``) - observed on production
#: 2026-10-29.
_CONTAINER_PATH = re.compile(r"/docker/containers/([0-9a-f]{64})/")


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


def _valid_container_id(value: Any) -> str | None:
    """Accept only a full 64-hex container id, else None."""
    if not isinstance(value, str):
        return None
    value = value.strip().lower()
    if len(value) == 64 and all(c in "0123456789abcdef" for c in value):
        return value
    return None


def own_container_id(mountinfo_path: Path | None = None) -> str | None:
    """The id of the container this process runs in, or None.

    A read-only probe of the process mount table; every failure - unreadable
    file, no container mounts, two mounts naming different containers -
    returns None rather than guessing.
    """
    path = mountinfo_path if mountinfo_path is not None else DEFAULT_MOUNTINFO_PATH
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    found: list[str] = []
    for line in text.splitlines():
        match = _CONTAINER_PATH.search(line)
        if match and match.group(1) not in found:
            found.append(match.group(1))
    # Every container mount carries the same id; anything else is not a
    # container environment this code understands.
    return found[0] if len(found) == 1 else None


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


def release_identity(
    release_path: Path | None = None,
    mountinfo_path: Path | None = None,
) -> dict[str, str | int]:
    """Return the fixed five-key release identity.

    ``mcp_contract_version`` comes from the module constant only - it is
    deliberately not read from ``release_path`` - so a runtime-writable
    release record can never move the write-gate label.
    """
    path = release_path if release_path is not None else DEFAULT_RELEASE_PATH
    release = _read_release_file(path)

    source_revision = _valid_revision(release.get("source_revision") or _BUILD_SOURCE_REVISION)
    source_tree = _valid_revision(release.get("source_tree") or _BUILD_SOURCE_TREE)

    # The recorded image digest is reported only when the record belongs to
    # the container running right now. A record written for an earlier
    # container (a cutover that never re-ran the release step), a record
    # with no binding, and a value that is not a digest-shaped string are
    # all reported as unknown: none of them can be proven to be the
    # running image's identity.
    recorded = _valid_revision(release.get("image_revision") or "")
    recorded_for = _valid_container_id(release.get("container_id"))
    current = _valid_container_id(own_container_id(mountinfo_path))
    if recorded.startswith("sha256:") and recorded_for is not None and recorded_for == current:
        image_revision = recorded
    else:
        image_revision = UNKNOWN

    return {
        "service": SERVICE,
        "source_revision": source_revision,
        "source_tree": source_tree,
        "image_revision": image_revision,
        "mcp_contract_version": MCP_CONTRACT_VERSION,
    }
