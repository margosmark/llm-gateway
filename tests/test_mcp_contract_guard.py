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
from pathlib import Path

from gateway.buildinfo import MCP_CONTRACT_VERSION, release_identity
from tests import mcp_contract
from tests.test_end_to_end import GatewayHarness


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
        current = mcp_contract.capture(self.harness)
        snapshots = mcp_contract.ancestor_snapshots()
        if not snapshots:
            # No git history to judge against (pre-commit, or a source
            # archive): the working-tree snapshot is the only record left.
            snapshots = [("working-tree snapshot", mcp_contract.load_baseline())]

        governing = next(
            ((origin, snap) for origin, snap in snapshots
             if current != snap.get("families")),
            None,
        )
        if governing is None:
            # Every recorded snapshot already matches: nothing gated changed.
            return
        origin, snapshot = governing
        reason = mcp_contract.evaluate(
            current, snapshot, MCP_CONTRACT_VERSION, origin=origin
        )
        self.assertIsNone(reason, reason)

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


if __name__ == "__main__":
    unittest.main()
