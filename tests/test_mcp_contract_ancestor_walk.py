"""F-1 regression: the ancestor walk behind the MCP bump guard.

``test_mcp_contract_guard.py::test_gated_family_change_requires_a_version_bump``
judges a gated family change against the newest committed snapshot that
still differs from the current capture. That walk used to stop at
``MAX_ANCESTOR_COMMITS = 50``: once the last pre-change snapshot sat
outside the window, every snapshot examined already matched, the guard
found nothing to judge against, and a gated payload family could ship with
no ``MCP_CONTRACT_VERSION`` bump. It failed open at its own bound.

Every test here builds a throwaway git history in a temp directory - the
real checkout is never touched - and pins a boundary rule:

* the F-1 scenario itself (mutation + regenerated snapshot + 50 more
  commits) still finds the governing snapshot and the guard FAILS on it;
* the 50-commit window that used to hide it genuinely contains no
  differing snapshot - the old pass, kept as the counterfactual;
* with ``current`` given, the walk stops at the first differing snapshot
  instead of materialising the rest of history;
* history that exists but records no snapshot anywhere raises - it must
  never fall back to the working-tree file the developer just regenerated;
* only absent git history (no repository, no commits yet) returns ``[]``,
  which is the one case where the working-tree fallback is legitimate.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests import mcp_contract

BASELINE_REL = "tests/fixtures/mcp_contract_baseline.json"

#: The gated ``state`` family as reviewed, under contract version 1.
REVIEWED: dict[str, dict[str, str]] = {
    "version": {"mcp_contract_version": "int"},
    "state": {"providers": "list", "providers[*].max_in_flight_effective": "int"},
}

#: The same family after a gated change shipped with **no** bump (a nested
#: key dropped), with the snapshot regenerated to match - so HEAD's own
#: snapshot can no longer reveal the change.
MUTATED: dict[str, dict[str, str]] = {
    "version": {"mcp_contract_version": "int"},
    "state": {"providers": "list"},
}


class AncestorWalkTestCase(unittest.TestCase):
    """A disposable git history per test; the real checkout stays clean."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name) / "repo"
        (self.repo / BASELINE_REL).parent.mkdir(parents=True)
        self._git("init", "-q")
        self._git("config", "user.email", "bump-guard@example.invalid")
        self._git("config", "user.name", "bump-guard")

    def _git(self, *args: str) -> str:
        proc = subprocess.run(
            ["git", "-C", str(self.repo), *args],
            capture_output=True,
            text=True,
            check=True,
        )
        return proc.stdout

    def _write_baseline(self, families: dict[str, dict[str, str]], version: int) -> None:
        (self.repo / BASELINE_REL).write_text(
            json.dumps(
                {"contract_version": version, "families": families},
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    def _commit(self, message: str, *, empty: bool = False) -> None:
        if not empty:
            self._git("add", "-A")
        self._git(
            "-c", "commit.gpgsign=false",
            "commit", "-q", "--allow-empty", "-m", message,
        )

    def _build_f1_history(self) -> list[str]:
        """The F-1 shape, newest-first SHAs returned.

        reviewed snapshot -> 10 commits that inherit it -> gated mutation
        with its regenerated snapshot (still version 1) -> 50 burials that
        push the reviewed snapshot past any 50-commit window.
        """
        self._write_baseline(REVIEWED, 1)
        self._commit("reviewed snapshot: the record the guard judges against")
        for i in range(10):
            self._commit(f"pre-history {i}", empty=True)
        self._write_baseline(MUTATED, 1)
        self._commit("gated family changed + snapshot regenerated, no bump")
        for i in range(50):
            self._commit(f"burial {i}", empty=True)
        return self._git("rev-list", "HEAD").split()

    # --- F-1 itself -----------------------------------------------------

    def test_a_snapshot_beyond_the_old_window_still_governs_and_fails(self) -> None:
        """The bug: mutation + regenerated snapshot + 50 burials -> RED."""
        shas = self._build_f1_history()
        current = MUTATED

        # the walk the guard runs: no bound, full history, then the
        # guard's own selection - newest snapshot that still differs
        snapshots = mcp_contract.ancestor_snapshots(repo=self.repo)
        governing = next(
            (
                (origin, snap)
                for origin, snap in snapshots
                if current != snap.get("families")
            ),
            None,
        )
        self.assertIsNotNone(governing, "the governing snapshot was not found")
        assert governing is not None
        origin, snapshot = governing
        self.assertEqual(snapshot["families"], REVIEWED)
        self.assertEqual(snapshot["contract_version"], 1)

        # ... and the guard bites on it with the exact rule
        reason = mcp_contract.evaluate(current, snapshot, 1, origin=origin)
        self.assertIsNotNone(reason, "the guard passed on an ungated change")
        self.assertIn("did not", reason or "")

        # the governing snapshot sits outside the 50-commit window the old
        # walk examined...
        gov_index = next(
            i for i, sha in enumerate(shas) if sha.startswith(origin.rsplit(" ", 1)[-1])
        )
        self.assertGreaterEqual(gov_index, 50)

        # ...whose snapshots all match the regenerated file, which is why
        # the bounded walk used to find nothing to judge and PASS here
        window_families = [
            json.loads(self._git("show", f"{sha}:{BASELINE_REL}"))["families"]
            for sha in shas[:50]
        ]
        self.assertTrue(
            all(families == current for families in window_families),
            "the old 50-commit window should have contained no differing "
            "snapshot - otherwise this test does not reproduce F-1",
        )

    def test_the_walk_stops_at_the_first_differing_snapshot(self) -> None:
        """With ``current`` given, older history is not materialised."""
        shas = self._build_f1_history()
        snapshots = mcp_contract.ancestor_snapshots(current=MUTATED, repo=self.repo)

        # every snapshot before the last still matches; the last differs
        # and the walk ended there
        self.assertTrue(
            all(snap.get("families") == MUTATED for _, snap in snapshots[:-1])
        )
        self.assertEqual(snapshots[-1][1].get("families"), REVIEWED)
        self.assertLess(len(snapshots), len(shas))

    # --- the two boundaries --------------------------------------------

    def test_history_without_any_committed_snapshot_fails_closed(self) -> None:
        """Git history exists but records no snapshot -> raise, never
        fall back to the working-tree file (the second F-1 face)."""
        (self.repo / "README.md").write_text("history, but no snapshot\n", encoding="utf-8")
        self._commit("history without a snapshot")
        self._commit("more history", empty=True)

        with self.assertRaises(AssertionError) as caught:
            mcp_contract.ancestor_snapshots(repo=self.repo)
        self.assertIn("must not fall back to the working-tree file", str(caught.exception))

        # supplying the current capture does not soften the boundary
        with self.assertRaises(AssertionError):
            mcp_contract.ancestor_snapshots(current=REVIEWED, repo=self.repo)

    def test_a_repository_with_no_commits_returns_empty(self) -> None:
        """Unborn branch: there is no history to judge against yet."""
        unborn = Path(self._tmp.name) / "unborn"
        unborn.mkdir()
        subprocess.run(["git", "-C", str(unborn), "init", "-q"], check=True)
        self.assertEqual(mcp_contract.ancestor_snapshots(repo=unborn), [])

    def test_a_directory_that_is_not_a_repository_returns_empty(self) -> None:
        """Source archive / no git at all: the working-tree fallback is
        the only record left, so ``[]`` is the right answer."""
        plain = Path(self._tmp.name) / "plain"
        plain.mkdir()
        self.assertEqual(mcp_contract.ancestor_snapshots(repo=plain), [])


if __name__ == "__main__":
    unittest.main()
