#!/usr/bin/env python3
"""Re-capture ``tests/fixtures/mcp_contract_baseline.json``.

The snapshot records the four gated payload families (version, state,
leaderboard, capabilities) as ``key-path -> type`` maps **and** the
``MCP_CONTRACT_VERSION`` they were captured under. It is the record the bump
guard (``tests/test_mcp_contract_guard.py``) judges a change against.

Refresh only - this script never edits ``gateway.buildinfo``. Bumping
``MCP_CONTRACT_VERSION`` is a hand edit in the same change as the gated
family edit; nothing in this repository increments it automatically,
because an automatic bump would be an automatic widening of the write gate.

Procedure for a gated family change:

1. edit the family;
2. run the guard -> it must go RED;
3. increment ``MCP_CONTRACT_VERSION`` by hand (still RED? no - GREEN);
4. run this script so the snapshot records the new schema under the new
   version;
5. re-run the guard -> GREEN.

Usage (from the repository root)::

    PYTHONPATH=src python3 scripts/capture_mcp_contract.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT))

from gateway.buildinfo import MCP_CONTRACT_VERSION  # noqa: E402
from tests import mcp_contract  # noqa: E402
from tests.test_end_to_end import GatewayHarness  # noqa: E402


def main() -> int:
    harness = GatewayHarness()
    try:
        families = mcp_contract.capture(harness)
    finally:
        harness.close()

    baseline = {
        "contract_version": MCP_CONTRACT_VERSION,
        "gated_families": mcp_contract.GATED_FAMILIES,
        "families": families,
    }
    mcp_contract.BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
    mcp_contract.BASELINE_PATH.write_text(
        json.dumps(baseline, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "wrote": str(mcp_contract.BASELINE_PATH),
                "contract_version": MCP_CONTRACT_VERSION,
                "families": {k: len(v) for k, v in sorted(families.items())},
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
