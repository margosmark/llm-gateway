"""Capture and evaluate the four gated MCP payload families.

The gateway publishes ``mcp_contract_version`` (``gateway.buildinfo``) and
``gateway-owner-mcp`` pins on it. The label may only move when one of the
**four gated payload families** changes:

1. ``version``       - ``GET /version``
2. ``state``         - ``GET /owner/api/state``
3. ``leaderboard``   - ``GET /owner/api/leaderboard``
4. ``capabilities``  - ``GET /owner/api/capabilities``

This module is the shared machinery for that rule:

* ``capture(harness)`` reads the four payloads and flattens each to a
  ``key-path -> type`` map, so a nested addition (the class of change a
  required-key presence check cannot see) is visible;
* ``evaluate(current, snapshot, constant)`` decides pass/fail;
* ``ancestor_snapshots()`` walks the committed baseline back through git,
  so regenerating the snapshot file cannot hide a change from the guard
  (the snapshot at the branch point is what the change is judged against).

Nothing here writes ``MCP_CONTRACT_VERSION``. There is no auto-bump path in
this repository: an automatic bump would be an automatic widening of the
write gate.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
BASELINE_PATH = REPO_ROOT / "tests" / "fixtures" / "mcp_contract_baseline.json"

#: The four gated payload families: name -> request path.
GATED_FAMILIES: dict[str, str] = {
    "version": "/version",
    "state": "/owner/api/state",
    "leaderboard": "/owner/api/leaderboard?days=7",
    "capabilities": "/owner/api/capabilities",
}

#: How far back the ancestor walk looks for a snapshot that differs from
#: the current schema. Normal runs stop at HEAD or HEAD~1; the bound only
#: matters for a long chain of schema-identical commits.
MAX_ANCESTOR_COMMITS = 50


def flat(obj: Any, prefix: str = "") -> dict[str, str]:
    """Flatten a payload to ``dotted.key[path] -> type name``."""
    out: dict[str, str] = {}
    if isinstance(obj, dict):
        for key, value in obj.items():
            out.update(flat(value, f"{prefix}.{key}" if prefix else str(key)))
    elif isinstance(obj, list):
        for value in obj:
            out.update(flat(value, f"{prefix}[*]"))
    else:
        out[prefix] = type(obj).__name__
    return out


def capture(harness: Any) -> dict[str, dict[str, str]]:
    """Read the four gated families off a running harness."""
    families: dict[str, dict[str, str]] = {}
    for name, path in GATED_FAMILIES.items():
        status, payload, _ = harness.request("GET", path, auth=True)
        if status != 200 or not isinstance(payload, dict):
            raise AssertionError(f"{name}: GET {path} -> HTTP {status}")
        families[name] = flat(payload)
    return families


def load_baseline(path: Path = BASELINE_PATH) -> dict[str, Any]:
    """The committed snapshot; a missing file is a hard failure."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AssertionError(
            f"baseline snapshot missing: {path} - run scripts/capture_mcp_contract.py"
        ) from exc
    if not isinstance(data, dict) or "families" not in data:
        raise AssertionError(f"baseline snapshot is malformed: {path}")
    return data


def evaluate(
    current: dict[str, dict[str, str]],
    snapshot: dict[str, Any],
    constant: int,
    *,
    origin: str,
) -> str | None:
    """Return ``None`` when the guard passes, else the failure reason.

    The rule, applied to whichever snapshot is newest that still differs
    from the current schema:

    * schema identical to the snapshot  -> pass (nothing gated changed);
    * ``constant == snapshot version``  -> **fail**: a gated change shipped
      with no bump;
    * ``constant < snapshot version``   -> **fail**: the label went backwards;
    * ``constant == snapshot + 1``      -> pass: this change is what the bump
      covers, and the snapshot refresh is now due;
    * ``constant > snapshot + 1``       -> **fail**: the snapshot is more than
      one generation stale, so the guard would stop biting - refresh it.
    """
    if current == snapshot.get("families"):
        return None
    recorded = snapshot.get("contract_version")
    if not isinstance(recorded, int) or isinstance(recorded, bool):
        return f"{origin}: snapshot has no usable contract_version: {recorded!r}"
    if constant < recorded:
        return (
            f"{origin}: MCP_CONTRACT_VERSION went backwards "
            f"({recorded} -> {constant}) after a gated payload family changed"
        )
    if constant == recorded:
        return (
            f"{origin}: a gated payload family changed but MCP_CONTRACT_VERSION "
            f"did not (still {recorded}); increment it by hand in the same change, "
            f"then refresh the snapshot with scripts/capture_mcp_contract.py"
        )
    if constant > recorded + 1:
        return (
            f"{origin}: the snapshot is {constant - recorded} versions stale "
            f"(recorded {recorded}, current {constant}); re-capture it with "
            f"scripts/capture_mcp_contract.py or the guard stops biting"
        )
    return None


def _git(*args: str) -> str | None:
    """Run git in the repo, or None when git/history is unavailable."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(REPO_ROOT), *args],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def ancestor_snapshots(limit: int = MAX_ANCESTOR_COMMITS) -> list[tuple[str, dict[str, Any]]]:
    """``[(origin, snapshot), ...]`` newest first, from committed history.

    The walk starts at ``HEAD``'s committed snapshot (not the working-tree
    file), so an uncommitted regeneration cannot stand in for the record the
    change is judged against. ``[]`` means git or history is unavailable -
    callers fall back to the working-tree snapshot.
    """
    log = _git("rev-list", "--max-count", str(limit), "HEAD")
    if not log:
        return []
    out: list[tuple[str, dict[str, Any]]] = []
    rel = str(BASELINE_PATH.relative_to(REPO_ROOT))
    for sha in log.split():
        text = _git("show", f"{sha}:{rel}")
        if text is None:
            continue
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and "families" in data:
            out.append((f"snapshot at {sha[:12]}", data))
    return out
