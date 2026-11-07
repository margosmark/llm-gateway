"""The MCP write-gate bump guard.

``gateway-owner-mcp`` refuses writes unless the gateway reports exactly the
contract version it was validated against, so the label is the whole gate.
This file is the control that keeps the label honest:

* ``test_gated_family_change_requires_a_version_bump`` - **the guard.**
  It captures the four gated payload families (version, state, leaderboard,
  capabilities), compares them against the newest committed snapshot that
  still differs, and fails unless ``MCP_CONTRACT_VERSION`` moved with them.
  Proven by mutation: change one field of a family -> RED; increment the
  constant -> GREEN; restore the tree -> clean.
* ``test_the_snapshot_is_date_independent_across_a_rollover`` - **the
  calendar proof.** The harness is moved to D+1 and to D+8 (across the
  7-day usage-window edge) with its own clock; the raw windows really do
  change, and the captured schema must still be byte-identical, and the
  guard must still pass. A snapshot that depends on today's date is a
  snapshot that goes red at every UTC midnight.
* ``test_a_real_field_change_in_any_gated_family_still_goes_red`` - the
  counterpart: adding, removing and re-typing a genuine field in **each**
  of the four families still fails the guard, so date normalisation has not
  turned the guard into a rubber stamp.
* ``test_the_constant_is_the_only_source`` - no second literal of the
  constant exists anywhere in ``src/``, and no runtime-writable file
  (``release.json``, the environment) can move it.

There is no auto-bump path: nothing in this repository writes
``MCP_CONTRACT_VERSION``.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from unittest import mock

import gateway.http.app as http_app
import gateway.leaderboard as leaderboard_module
from gateway.buildinfo import MCP_CONTRACT_VERSION, release_identity
from tests import mcp_contract
from tests.test_end_to_end import GatewayHarness

#: One pattern that catches every calendar-bearing key segment, whatever its
#: granularity (day, month or timestamp).
CALENDAR_SEGMENT = re.compile(r"\d{4}-\d{2}")


class EvaluateRuleTests(unittest.TestCase):
    """The rule itself, without a harness: four outcomes, four assertions."""

    SNAPSHOT = {
        "contract_version": 1,
        "families": {"version": {"mcp_contract_version": "int"}},
    }
    CHANGED = {"version": {"mcp_contract_version": "int", "new_field": "str"}}

    def test_identical_schema_passes(self) -> None:
        reason = mcp_contract.evaluate(
            self.SNAPSHOT["families"], self.SNAPSHOT, 1, origin="snap"
        )
        self.assertIsNone(reason)

    def test_changed_schema_without_a_bump_fails(self) -> None:
        reason = mcp_contract.evaluate(self.CHANGED, self.SNAPSHOT, 1, origin="snap")
        self.assertIsNotNone(reason)
        self.assertIn("did not", reason or "")

    def test_changed_schema_with_the_bump_passes(self) -> None:
        self.assertIsNone(
            mcp_contract.evaluate(self.CHANGED, self.SNAPSHOT, 2, origin="snap")
        )

    def test_a_version_that_went_backwards_fails(self) -> None:
        reason = mcp_contract.evaluate(self.CHANGED, self.SNAPSHOT, 0, origin="snap")
        self.assertIsNotNone(reason)
        self.assertIn("backwards", reason or "")

    def test_a_snapshot_more_than_one_generation_stale_fails(self) -> None:
        reason = mcp_contract.evaluate(self.CHANGED, self.SNAPSHOT, 3, origin="snap")
        self.assertIsNotNone(reason)
        self.assertIn("stale", reason or "")


def guard_reason(current: dict[str, dict[str, str]]) -> str | None:
    """The guard's verdict on ``current``: ``None`` when green, else why.

    This runs the *whole* selection path the guard runs - committed
    snapshots from git history (normalised), the working-tree fallback when
    there is no history at all, the newest-snapshot-that-differs pick, then
    the ``evaluate()`` rule - so every test that calls it exercises the real
    control rather than a stand-in for it.
    """
    snapshots = mcp_contract.ancestor_snapshots(current)
    if not snapshots:
        # No git history to judge against (pre-commit, or a source
        # archive): the working-tree snapshot is the only record left.
        snapshots = [("working-tree snapshot", mcp_contract.load_baseline())]
    governing = next(
        (
            (origin, snap)
            for origin, snap in snapshots
            if current != snap.get("families")
        ),
        None,
    )
    if governing is None:
        # Every recorded snapshot already matches: nothing gated changed.
        return None
    origin, snapshot = governing
    return mcp_contract.evaluate(
        current, snapshot, MCP_CONTRACT_VERSION, origin=origin
    )


def _raw_usage_days(harness: GatewayHarness) -> list[str]:
    """The **un-normalised** ``analytics.usage`` date keys, straight off
    the wire - the raw window the shape map deliberately does not record."""
    status, payload, _ = harness.request("GET", "/owner/api/state", auth=True)
    assert status == 200 and isinstance(payload, dict), f"state -> HTTP {status}"
    usage = payload.get("analytics", {}).get("usage", {})
    assert isinstance(usage, dict), "state.analytics.usage is not an object"
    return sorted(str(day) for day in usage)


@contextmanager
def _shifted_clock(harness: GatewayHarness, days: int) -> Iterator[None]:
    """Move the harness ``days`` into the future while it is captured.

    Both modules that build a **day window** for a gated payload read
    ``datetime.now(UTC)`` at request time (``build_analytics`` and
    ``build_leaderboard``; each has a ``today=`` seam that the request
    handlers do not pass), so shifting their ``datetime`` is the only way to
    make a live harness actually sit at another date. Values move with it -
    the recorded shape is what the tests then compare.

    The token-keyed ``/owner/api/state`` cache must be dropped as well:
    wall-clock drift alone never evicts an entry (only ``refresh_after``
    backgrounds a re-render), so without ``clear()`` the "shifted" capture
    would be served yesterday's body verbatim and the simulation would be
    a no-op. Cleared again on exit so no later test inherits a body
    rendered at a date that never happened.
    """
    shift = timedelta(days=days)

    class Shifted(datetime):
        @classmethod
        def now(cls, tz: Any = None, _shift: timedelta = shift) -> datetime:
            base = datetime.now(tz) if tz is not None else datetime.now()
            return base + _shift

    with mock.patch.object(http_app, "datetime", Shifted), mock.patch.object(
        leaderboard_module, "datetime", Shifted
    ):
        harness.app._state_cache.clear()
        try:
            yield
        finally:
            harness.app._state_cache.clear()


class McpContractGuardTests(unittest.TestCase):
    harness: GatewayHarness

    @classmethod
    def setUpClass(cls) -> None:
        cls.harness = GatewayHarness()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.harness.close()

    # --- the guard -------------------------------------------------------

    def test_gated_family_change_requires_a_version_bump(self) -> None:
        """A change to any of the four gated families without a bump fails."""
        reason = guard_reason(mcp_contract.capture(self.harness))
        self.assertIsNone(reason, reason)

    # --- the calendar (rollover) proof ----------------------------------

    def test_the_snapshot_is_date_independent_across_a_rollover(self) -> None:
        """Capture at D, D+1 and D+8 (across the 7-day edge) -> identical.

        The windows are checked raw first: if the simulation did not really
        move the date, surviving it would prove nothing. The shape map must
        be byte-identical at every date, and the guard must still pass there.
        """
        day0_days = _raw_usage_days(self.harness)
        day0 = mcp_contract.capture(self.harness)
        self.assertIsNone(guard_reason(day0), "the guard must start green")

        captures: dict[str, dict[str, dict[str, str]]] = {"D": day0}
        for label, days in (("D+1", 1), ("D+8", 8)):
            with self.subTest(window=label), _shifted_clock(self.harness, days):
                moved = _raw_usage_days(self.harness)
                self.assertNotEqual(
                    day0_days,
                    moved,
                    f"the {label} simulation did not move the usage window",
                )
                if days > 7:
                    # the 7-day edge is crossed: nothing from D's window
                    # survives in D+8's window
                    self.assertTrue(
                        set(day0_days).isdisjoint(moved),
                        f"{label} did not cross the 7-day window edge",
                    )
                captures[label] = mcp_contract.capture(self.harness)

        for label, captured in captures.items():
            with self.subTest(window=label):
                self.assertEqual(
                    json.dumps(day0, sort_keys=True),
                    json.dumps(captured, sort_keys=True),
                    f"the snapshot moved between D and {label}",
                )
                self.assertIsNone(
                    guard_reason(captured),
                    f"the guard went red at {label} (calendar drift)",
                )

    def test_a_real_field_change_in_any_gated_family_still_goes_red(self) -> None:
        """Add / remove / re-type a genuine field -> RED, in all four families.

        This is the anti-rubber-stamp half of the rollover proof: only the
        *calendar* may collapse; every other difference must still fail.
        """
        current = mcp_contract.capture(self.harness)
        self.assertIsNone(guard_reason(current), "the guard must start green")

        for family, paths in sorted(current.items()):
            victim = min(paths)
            for operation in ("added", "removed", "retyped"):
                with self.subTest(family=family, operation=operation):
                    mutated = {name: dict(mapping) for name, mapping in current.items()}
                    if operation == "added":
                        mutated[family][f"{family}_guard_probe_field"] = "int"
                    elif operation == "removed":
                        del mutated[family][victim]
                    else:
                        mutated[family][victim] = (
                            "str" if paths[victim] != "str" else "int"
                        )
                    reason = guard_reason(mutated)
                    self.assertIsNotNone(
                        reason,
                        f"{operation} a field of {family} was not detected",
                    )
                    self.assertIn("did not", reason or "")

    def test_no_captured_key_carries_a_calendar_date(self) -> None:
        """Inspection rule, kept live: all four families, no raw dates.

        The rollover test would still pass if a *new* date-keyed family
        appeared and happened not to move during the run; this asserts the
        property directly against whatever the payloads return today.
        """
        families = mcp_contract.capture(self.harness)
        self.assertEqual(sorted(families), sorted(mcp_contract.GATED_FAMILIES))
        for family, mapping in sorted(families.items()):
            with self.subTest(family=family):
                offenders = sorted(
                    key for key in mapping if CALENDAR_SEGMENT.search(key)
                )
                self.assertEqual(offenders, [])

    def test_every_committed_snapshot_is_date_independent_once_loaded(self) -> None:
        """History, normalised: no snapshot the guard can read carries a date."""
        snapshots = mcp_contract.ancestor_snapshots()
        self.assertTrue(snapshots, "no committed snapshot to judge against")
        for origin, snapshot in snapshots:
            for family, mapping in sorted(snapshot["families"].items()):
                with self.subTest(snapshot=origin, family=family):
                    offenders = sorted(
                        key for key in mapping if CALENDAR_SEGMENT.search(key)
                    )
                    self.assertEqual(offenders, [])

    def test_the_four_gated_families_are_enumerated(self) -> None:
        """The family list is a contract: name it or the guard is blind."""
        self.assertEqual(
            sorted(mcp_contract.GATED_FAMILIES),
            ["capabilities", "leaderboard", "state", "version"],
        )

    # --- publishing ------------------------------------------------------

    def test_the_version_family_publishes_the_constant(self) -> None:
        status, payload, _ = self.harness.request("GET", "/version")
        self.assertEqual(status, 200)
        self.assertIsInstance(payload, dict)
        assert isinstance(payload, dict)
        reported = payload.get("mcp_contract_version")
        self.assertIsInstance(reported, int)
        self.assertNotIsInstance(reported, bool)
        assert isinstance(reported, int) and not isinstance(reported, bool)
        self.assertGreaterEqual(reported, 1)
        self.assertEqual(reported, MCP_CONTRACT_VERSION)

    def test_the_owner_surface_publishes_the_constant(self) -> None:
        status, payload, _ = self.harness.request("GET", "/owner/api/state")
        self.assertEqual(status, 200)
        self.assertIsInstance(payload, dict)
        assert isinstance(payload, dict)
        release = payload.get("release")
        self.assertIsInstance(release, dict)
        assert isinstance(release, dict)
        self.assertEqual(release.get("mcp_contract_version"), MCP_CONTRACT_VERSION)

    # --- the constant cannot move at runtime (AC5) -----------------------

    def test_release_json_cannot_move_the_constant(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            record = Path(tmp) / "release.json"
            record.write_text(
                json.dumps(
                    {
                        "source_revision": "b" * 40,
                        "source_tree": "c" * 40,
                        "image_revision": "sha256:" + "d" * 64,
                        "mcp_contract_version": 9999,
                    }
                ),
                encoding="utf-8",
            )
            identity = release_identity(release_path=record)
        self.assertEqual(identity["mcp_contract_version"], MCP_CONTRACT_VERSION)
        self.assertNotEqual(identity["mcp_contract_version"], 9999)

    def test_the_environment_cannot_move_the_constant(self) -> None:
        key = "GATEWAY_MCP_CONTRACT_VERSION"
        previous = os.environ.get(key)
        os.environ[key] = "9999"
        try:
            identity = release_identity(release_path=Path("/nonexistent/release.json"))
        finally:
            if previous is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = previous
        self.assertEqual(identity["mcp_contract_version"], MCP_CONTRACT_VERSION)

    def test_the_constant_is_defined_exactly_once(self) -> None:
        """One module, one literal: a second mint is a second gate."""
        definition = re.compile(r"^MCP_CONTRACT_VERSION\s*=\s*(\d+)\s*$", re.M)
        found: list[tuple[str, int]] = []
        for path in sorted((mcp_contract.REPO_ROOT / "src").rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            for match in definition.finditer(text):
                found.append((str(path.relative_to(mcp_contract.REPO_ROOT)), int(match.group(1))))
        self.assertEqual(
            [(rel, value) for rel, value in found],
            [("src/gateway/buildinfo.py", MCP_CONTRACT_VERSION)],
        )
        # No hard-coded integer may be bound to the published key anywhere
        # in src/ - the key always carries the single constant.
        literal = re.compile(r"""["']mcp_contract_version["']\s*:\s*\d+""")
        offenders = [
            str(path.relative_to(mcp_contract.REPO_ROOT))
            for path in sorted((mcp_contract.REPO_ROOT / "src").rglob("*.py"))
            if literal.search(path.read_text(encoding="utf-8"))
        ]
        self.assertEqual(offenders, [])


class NormalizationTests(unittest.TestCase):
    """The calendar rule itself, without a harness."""

    SHAPE = {
        # the known offender, and its siblings in the same notation
        "analytics.usage.2026-11-07": "analytics.usage.<date>",
        "analytics.usage.2026-11": "analytics.usage.<month>",
        "series.2026-11-07T01:02:03Z.count": "series.<timestamp>.count",
        "daily_2026-11-07_rollup.requests": "daily_<date>_rollup.requests",
        # everything else must come back untouched
        "analytics.window.from": "analytics.window.from",
        "analytics.window.to": "analytics.window.to",
        "analytics.coverage.first_row_ts": "analytics.coverage.first_row_ts",
        "analytics.series[*].day": "analytics.series[*].day",
        "matrix.cells[*].day": "matrix.cells[*].day",
        "providers[*].models[*].model_id": "providers[*].models[*].model_id",
        "providers[*].max_in_flight_effective": "providers[*].max_in_flight_effective",
        "cooldown_policy.transient_failure_threshold": (
            "cooldown_policy.transient_failure_threshold"
        ),
        "release.source_revision": "release.source_revision",
        "window_days_7": "window_days_7",
        "v2_3_rollback": "v2_3_rollback",
    }

    def test_calendar_segments_become_shape_placeholders(self) -> None:
        for raw, shaped in self.SHAPE.items():
            with self.subTest(raw=raw):
                self.assertEqual(mcp_contract.normalize_key(raw), shaped)

    def test_normalisation_is_idempotent(self) -> None:
        for raw, shaped in self.SHAPE.items():
            with self.subTest(raw=raw):
                self.assertEqual(
                    mcp_contract.normalize_key(shaped), shaped, "placeholder re-matched"
                )

    def test_normalisation_is_not_a_rubber_stamp(self) -> None:
        """Different granularities stay different, so a granularity change
        (a day-keyed family becoming month-keyed) is still a real change."""
        by_day = mcp_contract.normalize_key("analytics.usage.2026-11-07")
        by_month = mcp_contract.normalize_key("analytics.usage.2026-11")
        by_stamp = mcp_contract.normalize_key("analytics.usage.2026-11-07T01:02:03Z")
        self.assertNotEqual(by_day, by_month)
        self.assertNotEqual(by_day, by_stamp)
        self.assertNotEqual(by_month, by_stamp)

    def test_dated_keys_collapse_to_one_shape_entry(self) -> None:
        """Seven concrete days become one ``<date>`` entry - the window may
        move and change size; the recorded structure may not."""
        window = {
            f"analytics.usage.2026-11-{day:02d}": "NoneType" for day in range(1, 8)
        }
        shapes = mcp_contract.normalize_families({"state": window})
        self.assertEqual(shapes, {"state": {"analytics.usage.<date>": "NoneType"}})

    def test_non_calendar_keys_are_untouched_by_family_map_normalisation(self) -> None:
        families = {
            "state": {"analytics.window.from": "str", "revision": "int"},
            "version": {"mcp_contract_version": "int"},
        }
        self.assertEqual(mcp_contract.normalize_families(families), families)


if __name__ == "__main__":
    unittest.main()
