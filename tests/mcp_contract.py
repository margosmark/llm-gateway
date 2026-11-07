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
* **calendar-dated path segments are normalised at capture time**: a key
  that carries a date (``analytics.usage.2026-11-07``), a month
  (``usage.2026-11``) or an ISO timestamp is recorded as its *shape*
  (``analytics.usage.<date>``), never as the day it happened to be
  captured on. The snapshot answers "does this family have the same
  structure?" and can never answer "is it the same day?" - a guard that
  goes red at every UTC rollover is a guard that gets waived, and a
  waived guard is a dead guard. Snapshots loaded from git history pass
  through the same normalisation, so a baseline committed before this
  rule existed still compares like-for-like;
* ``evaluate(current, snapshot, constant)`` decides pass/fail;
* ``ancestor_snapshots()`` walks the committed baseline back to the root
  of history, so regenerating the snapshot file - or burying the last
  matching snapshot under enough later commits - cannot hide a change
  from the guard.

Nothing here writes ``MCP_CONTRACT_VERSION``. There is no auto-bump path in
this repository: an automatic bump would be an automatic widening of the
write gate.
"""

from __future__ import annotations

import json
import re
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

# --- volatile (calendar-bearing) path segments ---------------------------
#
# Found by scanning the flattened keys of **all four** families, not by
# naming one offender: the only path segments that carry a calendar are
# ``analytics.usage.<YYYY-MM-DD>`` (the 7-day rollup window, which advances
# every UTC midnight and *replaces* its oldest key at every 7-day edge).
# The window edges themselves - ``analytics.window.from``/``to``,
# ``leaderboard.window.from``, ``first_row_ts``, ``series[*].day``,
# ``cells[*].day``, ``generated_at`` - are **values**, not path segments, so
# a key-path -> type map records each as one fixed type - ``str`` for the
# populated fields, ``NoneType`` for the empty ones in the committed
# baseline (captured on an empty ledger, where ``first_row_ts`` is absent
# with its ``coverage`` container) - and they cannot roll; the rules below
# are written against the segment, not against
# the ``analytics.usage`` name, so a sibling date-keyed family (a monthly
# bucket, a per-day rollup keyed by ISO timestamp) is covered without
# anyone having to know its name.
#
# Granularity is preserved: a day, a month and a timestamp key become three
# *different* placeholders, so a family that changes which granularity it is
# keyed by still registers as a change.

_TIMESTAMP_SEGMENT = re.compile(
    r"\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?"
    r"(?:Z|[+-]\d{2}:?\d{2})?"
)
_DATE_SEGMENT = re.compile(r"\d{4}-\d{2}-\d{2}")
_MONTH_SEGMENT = re.compile(r"\d{4}-\d{2}(?![\d-])")


def normalize_key(path: str) -> str:
    """Rewrite the calendar-bearing segments of a flattened key to shape.

    ``analytics.usage.2026-11-07`` -> ``analytics.usage.<date>``; a month
    bucket becomes ``<month>``, an ISO timestamp key ``<timestamp>``.
    Anything else - field names, ids, ``[*]`` list markers, digits inside
    non-calendar names - is returned untouched, and normalising an already
    normalised key is a no-op (the placeholders match no rule), so the
    same function safely runs over snapshots recorded before it existed.
    """
    path = _TIMESTAMP_SEGMENT.sub("<timestamp>", path)
    path = _DATE_SEGMENT.sub("<date>", path)
    return _MONTH_SEGMENT.sub("<month>", path)


def normalize_map(mapping: dict[str, str]) -> dict[str, str]:
    """Normalise every key of one flattened ``key-path -> type`` map.

    Two keys that normalise onto the same path (the calendar itself moving
    under a fixed shape) collapse to one entry, keeping the later window
    order - a date-dependent difference is exactly what this map is not
    supposed to record.
    """
    return {normalize_key(key): value for key, value in mapping.items()}


def normalize_families(families: dict[str, Any]) -> dict[str, Any]:
    """Normalise the ``families`` block of a snapshot: family -> flat map.

    This is the shape both snapshot loaders hold, so a baseline committed
    with concrete calendar keys comes back identical to a capture taken on
    any other day.
    """
    return {
        name: normalize_map(mapping) if isinstance(mapping, dict) else mapping
        for name, mapping in families.items()
    }


def flat(obj: Any, prefix: str = "") -> dict[str, str]:
    """Flatten a payload to ``dotted.key[path] -> type name``.

    Keys go through :func:`normalize_key` on the way out, so what lands in
    the map (and therefore in the snapshot) is the family's *shape*.
    """
    out: dict[str, str] = {}
    if isinstance(obj, dict):
        for key, value in obj.items():
            out.update(flat(value, f"{prefix}.{key}" if prefix else str(key)))
    elif isinstance(obj, list):
        for value in obj:
            out.update(flat(value, f"{prefix}[*]"))
    else:
        out[normalize_key(prefix)] = type(obj).__name__
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
    """The committed snapshot; a missing file is a hard failure.

    Its families are normalised on the way out: a snapshot written before
    the calendar rule existed (or by an older checkout) still compares
    like-for-like with a capture taken today.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AssertionError(
            f"baseline snapshot missing: {path} - run scripts/capture_mcp_contract.py"
        ) from exc
    if not isinstance(data, dict) or "families" not in data:
        raise AssertionError(f"baseline snapshot is malformed: {path}")
    if isinstance(data.get("families"), dict):
        data["families"] = normalize_families(data["families"])
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


def _git(*args: str, repo: Path | None = None) -> str | None:
    """Run git in ``repo`` (this checkout by default), or None on failure."""
    root = REPO_ROOT if repo is None else repo
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def ancestor_snapshots(
    current: dict[str, dict[str, str]] | None = None,
    *,
    repo: Path | None = None,
) -> list[tuple[str, dict[str, Any]]]:
    """``[(origin, snapshot), ...]`` newest first, from committed history.

    The walk starts at ``HEAD``'s committed snapshot (not the working-tree
    file), so an uncommitted regeneration cannot stand in for the record the
    change is judged against.

    There is **no bound**. ``git rev-list HEAD`` is walked to the root of
    history and only stops early when ``current`` is given, at the first
    snapshot whose ``families`` differ from it - the newest snapshot the
    change can be judged against. A fixed window was the F-1 fail-open: a
    differing snapshot older than the window made every examined snapshot
    match, ``governing`` came back empty, and a gated payload family could
    ship with no ``MCP_CONTRACT_VERSION`` bump (regression:
    ``tests/test_mcp_contract_ancestor_walk.py``).

    Returns ``[]`` **only** when git or history itself is unavailable -
    callers may then fall back to the working-tree snapshot, because there
    is no committed record at all. When history exists but no committed
    snapshot is found anywhere in it, this raises instead: falling back to
    the working-tree file there would judge the developer's own regenerated
    snapshot against itself, which passes for any change.
    """
    root = REPO_ROOT if repo is None else repo
    log = _git("rev-list", "HEAD", repo=root)
    if not log:
        return []
    shas = log.split()
    rel = str(BASELINE_PATH.relative_to(REPO_ROOT))
    out: list[tuple[str, dict[str, Any]]] = []
    for sha in shas:
        text = _git("show", f"{sha}:{rel}", repo=root)
        if text is None:
            continue
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            continue
        if not (isinstance(data, dict) and "families" in data):
            continue
        if isinstance(data["families"], dict):
            # Same normalisation as a capture, so a snapshot committed with
            # concrete calendar keys (every baseline written before this
            # rule) is judged on its shape instead of on the day it was
            # recorded - without this, the walk would stop at the first
            # date-only difference and fail the guard on the calendar.
            data["families"] = normalize_families(data["families"])
        out.append((f"snapshot at {sha[:12]}", data))
        if current is not None and data["families"] != current:
            break
    if not out:
        raise AssertionError(
            f"git history at {root} has {len(shas)} commits but none records "
            f"{rel}; the bump guard has no committed snapshot to judge "
            "against and must not fall back to the working-tree file (that "
            "compares the developer's own regenerated snapshot with itself) "
            "- commit the baseline with scripts/capture_mcp_contract.py first"
        )
    return out
