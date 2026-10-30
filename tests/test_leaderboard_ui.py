"""The leaderboard tab as served: assets, wiring, and route separation.

The DOM-level rendering rules (cost honesty, no-data vs measured zero) live
in ``tests/console_dom_test.js`` and run through ``test_console_js.py``.
This module proves the *served* console actually carries the tab, that its
assets ship the leaderboard styles, and — the wave-2B non-negotiable — that
``/owner/api/state`` still has no leaderboard payload bolted onto it.

Fixture-only: a loopback harness, no provider calls, no network, never the
production gateway.
"""
from __future__ import annotations

import unittest

from tests.test_end_to_end import GatewayHarness


class LeaderboardUiServingTests(unittest.TestCase):
    """The console assets served over the owner route."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.harness = GatewayHarness()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.harness.close()

    def test_console_script_carries_the_leaderboard_tab_and_its_own_route(self) -> None:
        status, script, _ = self.harness.request("GET", "/owner/app.js", raw=True)
        self.assertEqual(status, 200)
        source = script.decode("utf-8")
        self.assertIn('{ id: "leaderboard", label: "Leaderboard" }', source)
        self.assertIn("leaderboard: renderLeaderboard", source)
        # The tab fetches its payload from its own endpoint; the state
        # route must stay free of it (R56).
        self.assertIn('"/api/leaderboard?days="', source)
        self.assertNotIn("/api/session", source)

    def test_console_styles_carry_the_leaderboard_chart_rules(self) -> None:
        status, styles, _ = self.harness.request("GET", "/owner/style.css", raw=True)
        self.assertEqual(status, 200)
        css = styles.decode("utf-8")
        for rule in (".lb-chart {", ".lb-nodata {", ".lb-zero {", ".lb-absent td {"):
            self.assertIn(rule, css)

    def test_state_stays_free_of_the_leaderboard_payload(self) -> None:
        # The leaderboard is a separate read-only route on purpose: folding
        # it into /owner/api/state is exactly the R56 regression shape.
        status, payload, _ = self.harness.request("GET", "/owner/api/state")
        self.assertEqual(status, 200)
        self.assertNotIn("leaderboard", payload)

    def test_the_leaderboard_route_answers_on_the_same_mount(self) -> None:
        status, payload, _ = self.harness.request("GET", "/owner/api/leaderboard?days=7")
        self.assertEqual(status, 200)
        self.assertIsNotNone(payload)
        assert isinstance(payload, dict)
        self.assertIn("available", payload)
        self.assertIn("matrix", payload)
        self.assertIn("ranked", payload)
        self.assertEqual(payload["timezone"], "UTC")
        # An empty harness has no rollup rows: the payload reports absence —
        # no ranked model, and no day may carry a fabricated value or a
        # "recorded" status.
        self.assertEqual(payload["ranked"], [])
        cells = payload["matrix"]["cells"]
        self.assertEqual(len(cells), 7, "one cell per window day, absent days included")
        for cell in cells:
            self.assertNotIn(cell["status"], ("ok", "open"),
                             "an empty harness must not claim a recorded day")
            self.assertEqual(cell["values"], [],
                             "an absent day carries no per-model values")


if __name__ == "__main__":
    unittest.main()
