"""Owner-console state latency regression gate (wave-2B rollback, 2026-10-29).

Production incident: deployed tree ``3e2d6c86`` (PR #38) made
``GET /owner/api/state`` take 46.9s for a 92KB payload, so the owner console
never rendered below its banner and wave-2B was rolled back to ``f024ae6``.
Measured cause: the handler re-pulled ``ledger.recent(limit=10_000)`` once
per alias per consumer (~20 ring scans with 4 aliases) and, far worse, each
aggregation's per-event ``calculate_cost`` re-read the 42KB published-price
catalogue from disk — over 100,000 file reads per console poll.

This file is the mandatory release gate the earlier dual PASS missed: it
times the endpoint on a realistic fixture (>=10,000 ledger rows, 4 aliases)
and asserts the payload is still complete with the same numbers. A fast
endpoint that drops data is not a fix.

Fixture-only: loopback harness, synthetic events, no provider calls, and no
production lifecycle.
"""
from __future__ import annotations

import time
import unittest
from datetime import UTC, datetime, timedelta

from gateway.config import Alias, ModelEntry, RouteRef
from gateway.observability import RequestEvent
from tests.test_end_to_end import GatewayHarness

ROW_COUNT = 10_000
ALIAS_NAMES = ("a0", "a1", "a2", "a3")
BUDGET_CAP = 5.0
MODEL = "gemini-4-argon"
PROVIDER = "opencode_go"

#: Published gemini-4-argon prices (input 2.0, output 10.0, cached 0.1 per
#: million). Each seeded event reports 1000 prompt / 100 completion / 100
#: cached-read tokens: billable input is 900, so one event costs
#: (900*2.0 + 100*10.0 + 100*0.1) / 1e6 = 0.00281 USD. Hand-computed, so the
#: numbers in the payload cannot drift into plausible-looking arithmetic.
PER_EVENT_USD = (900 * 2.0 + 100 * 10.0 + 100 * 0.1) / 1_000_000


def _seed_ledger(ledger, row_count: int, now: datetime | None = None) -> None:
    """Fill the bounded ring with priced, successful, in-window events.

    Timestamps are spread over [start, now] where start is the later of
    "one month-ago-window start + 1s" and "1000s ago", so every event is in
    the past, inside the current calendar month (budget window), and inside
    the last hour (statistics hour window) regardless of when the suite runs.
    """
    now = now or datetime.now(UTC)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    start = max(month_start + timedelta(seconds=1), now - timedelta(seconds=1000))
    span_us = int((now - start).total_seconds() * 1_000_000)
    for i in range(row_count):
        offset_us = i * span_us // max(1, row_count - 1)
        stamp = (start + timedelta(microseconds=offset_us)).isoformat().replace("+00:00", "Z")
        ledger.record(RequestEvent(
            request_id=f"req_seed_{i}",
            timestamp=stamp,
            requested_alias=ALIAS_NAMES[i % len(ALIAS_NAMES)],
            requested_model=MODEL,
            serving_provider=PROVIDER,
            serving_model=MODEL,
            status=200,
            latency_ms=100 + (i % 500),
            token_counters={"prompt_tokens": 1000, "completion_tokens": 100,
                            "cached_read_tokens": 100, "total_tokens": 1100},
        ))


def _build_seeded_harness(row_count: int) -> GatewayHarness:
    harness = GatewayHarness()
    with harness.config.mutate() as snapshot:
        snapshot.providers[PROVIDER].models[MODEL] = ModelEntry(
            model_id=MODEL, enabled=True, dialect="openai_chat", source="discovered",
        )
        for name in ALIAS_NAMES:
            snapshot.aliases[name] = Alias(
                name=name, enabled=True,
                primary=RouteRef(PROVIDER, MODEL),
                budget=BUDGET_CAP,
            )
    ledger = harness.app.ledger
    assert ledger is not None
    _seed_ledger(ledger, row_count)
    return harness


def _timed_state_request(harness) -> tuple[float, int, dict]:
    """GET /owner/api/state, timing it; a client-timeout is a failed gate."""
    start = time.perf_counter()
    try:
        status, payload, _ = harness.request("GET", "/owner/api/state")
    except TimeoutError as exc:
        raise AssertionError(
            "GET /owner/api/state did not answer within the 10s client timeout; "
            "the state endpoint has regressed far past the 2s bar "
            "(wave-2B-class latency: per-consumer ledger re-pulls and per-row "
            "catalogue re-reads)"
        ) from exc
    assert isinstance(payload, dict)
    return time.perf_counter() - start, status, payload


class OwnerStateLatencyGateTests(unittest.TestCase):
    """The acceptance bar: >=10k rows, >=4 aliases, <2s, payload complete."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.harness = _build_seeded_harness(ROW_COUNT)
        cls.elapsed, cls.status, cls.payload = _timed_state_request(cls.harness)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.harness.close()

    def test_state_answers_under_two_seconds(self) -> None:
        self.assertEqual(self.status, 200)
        self.assertLess(
            self.elapsed, 2.0,
            f"GET /owner/api/state took {self.elapsed:.2f}s with {ROW_COUNT} ledger "
            f"rows and {len(ALIAS_NAMES)} aliases; the bar is 2s "
            f"(production measured 46.9s on tree 3e2d6c86)",
        )

    def test_every_consumer_is_populated_with_the_same_numbers(self) -> None:
        payload = self.payload
        per_alias = ROW_COUNT // len(ALIAS_NAMES)

        # Per-alias budget: present, computed, and equal to the hand total.
        aliases = {entry["name"]: entry for entry in payload["aliases"]}
        self.assertEqual(sorted(aliases), sorted(ALIAS_NAMES))
        for name in ALIAS_NAMES:
            budget = aliases[name]["budget_status"]
            self.assertEqual(budget["budget_usd"], BUDGET_CAP)
            self.assertIsNotNone(budget["spend_usd"])
            self.assertAlmostEqual(budget["spend_usd"], per_alias * PER_EVENT_USD, places=9)

        # Statistics: all three windows share the snapshot, all rows counted.
        statistics = payload["request_statistics"]
        self.assertEqual(sorted(statistics), ["day", "hour", "month"])
        for window in ("hour", "day", "month"):
            groups = statistics[window]
            for dimension in ("alias", "provider", "model"):
                self.assertEqual(
                    sum(row["requests"] for row in groups[dimension].values()),
                    ROW_COUNT,
                    f"window={window} dimension={dimension}",
                )
            self.assertAlmostEqual(
                groups["alias"]["a0"]["estimated_cost_usd"], per_alias * PER_EVENT_USD, places=9,
            )

        # Provider health: every configured provider reported, counts intact.
        health = {row["provider_id"]: row for row in payload["provider_health"]}
        self.assertEqual(health[PROVIDER]["recent_requests"], ROW_COUNT)
        self.assertEqual(health[PROVIDER]["recent_success_rate"], 1.0)
        self.assertEqual(health[PROVIDER]["recent_failure_rate"], 0.0)

        # Usage costs: priced, complete coverage over the whole ring.
        usage = payload["usage_costs"]
        self.assertEqual(usage["coverage"]["events_considered"], ROW_COUNT)
        self.assertFalse(usage["coverage"]["incomplete"])
        self.assertEqual(
            sum(row["requests"] for row in usage["alias"].values()), ROW_COUNT,
        )

        # Ledger excerpt and totals still render from the same snapshot.
        self.assertEqual(len(payload["ledger"]), 25)
        self.assertEqual(payload["totals"]["requests"], ROW_COUNT)


class OwnerStateHistoryIncompleteTests(unittest.TestCase):
    """An evicted ring row must still fail budgets closed, endpoint-wide."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.harness = _build_seeded_harness(ROW_COUNT)
        ledger = cls.harness.app.ledger
        assert ledger is not None
        # One row past the ring capacity: the oldest row is evicted, so the
        # ring no longer holds complete history.
        _seed_ledger(ledger, 1, now=None)
        cls.elapsed, cls.status, cls.payload = _timed_state_request(cls.harness)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.harness.close()

    def test_evicted_history_reports_unknown_spend_not_a_number(self) -> None:
        self.assertEqual(self.status, 200)
        ledger = self.harness.app.ledger
        assert ledger is not None
        self.assertEqual(ledger.evicted, 1)
        for entry in self.payload["aliases"]:
            self.assertIsNone(
                entry["budget_status"]["spend_usd"],
                f"alias {entry['name']}: evicted history must withhold the "
                f"spend figure, never report a partial sum",
            )
        self.assertTrue(self.payload["usage_costs"]["coverage"]["incomplete"])


if __name__ == "__main__":
    unittest.main()
