"""The owner analytics surface: fallback rate, daily series, priced usage.

Two layers are covered here:

* ``build_analytics`` against a hand-written rollup fixture, where every
  expected number is computed by hand first, so the API cannot drift into
  plausible-looking arithmetic;
* the served ``/owner/api/state`` path, fed by a real fallback request,
  including proof that a poll never opens the ledger file.

Everything is fixture-only: temporary directories, no provider, no network,
and never the production gateway.
"""
from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from gateway.http.app import analytics_days, build_analytics, fallback_reason_class
from gateway.ledger_persistence import ROLLUP_SCHEMA, rollup_path_for
from gateway.observability import RequestLedger

TODAY = datetime(2026, 10, 24, 12, 0, 0, tzinfo=UTC)

COOLDOWN_REASON = (
    "the primary route was skipped: the provider p-one is in a local "
    "cooldown for 771s (exhausted)"
)
INELIGIBLE_REASON = "the primary route was skipped: route not eligible for alias"
OTHER_REASON = "the primary route was skipped: something went sideways"

#: Published price used where a price exists. 3000 input + 300 output at
#: 0.3 / 1.2 per million = 0.00126; the 1000 + 100 day = 0.00042.
PRICES = {
    "mimo-v2.6-flash": {
        "input_per_m": 0.3,
        "output_per_m": 1.2,
        "cached_read_per_m": None,
        "source": "https://opencode.ai/docs/go/",
        "observed_at": "2026-10-22",
    }
}

CLOSED_REASON_VOCABULARY = {"cooldown", "route_ineligible", "upstream_failure", "other", "unknown"}


def fixture_rollup() -> dict[str, Any]:
    """Three rows over two days: 2026-10-23 deliberately has no rows."""
    return {
        "schema": ROLLUP_SCHEMA,
        "generated_at": "2026-10-24T12:00:00Z",
        "source": "request-ledger.jsonl",
        "coverage": {
            "requests": 8,
            "first_row_ts": "2026-10-22T08:00:00Z",
            "last_row_ts": "2026-10-24T09:00:00Z",
            "rows_superseded": 0,
            "rows_skipped": 0,
            "rows_expired": 0,
            "retention_days": 30,
            "incomplete": False,
        },
        "rows": [
            {"day": "2026-10-22", "alias": "coding", "provider": "opencode_go",
             "model": "mimo-v2.6-flash", "requests": 3, "input_tokens": 3000,
             "output_tokens": 300, "cached_tokens": 0, "fallbacks": 1,
             "fallback_reasons": {COOLDOWN_REASON: 1}, "errors": {"exhausted": 1},
             "statuses": {"200": 2, "502": 1}},
            {"day": "2026-10-22", "alias": "pm", "provider": "p-two",
             "model": "m-two", "requests": 1, "input_tokens": 100,
             "output_tokens": 10, "cached_tokens": 0, "fallbacks": 0,
             "fallback_reasons": {}, "errors": {}, "statuses": {"200": 1}},
            {"day": "2026-10-24", "alias": "coding", "provider": "opencode_go",
             "model": "mimo-v2.6-flash", "requests": 4, "input_tokens": 1000,
             "output_tokens": 100, "cached_tokens": 0, "fallbacks": 3,
             "fallback_reasons": {COOLDOWN_REASON: 1, INELIGIBLE_REASON: 1,
                                  OTHER_REASON: 1},
             "errors": {"other": 1}, "statuses": {"200": 3, "500": 1}},
        ],
        "daily": [
            {"day": "2026-10-22", "requests": 4, "input_tokens": 3100,
             "output_tokens": 310, "cached_tokens": 0, "fallbacks": 1,
             "fallback_reasons": {COOLDOWN_REASON: 1}, "errors": {"exhausted": 1},
             "statuses": {"200": 3, "502": 1}},
            {"day": "2026-10-24", "requests": 4, "input_tokens": 1000,
             "output_tokens": 100, "cached_tokens": 0, "fallbacks": 3,
             "fallback_reasons": {COOLDOWN_REASON: 1, INELIGIBLE_REASON: 1,
                                  OTHER_REASON: 1},
             "errors": {"other": 1}, "statuses": {"200": 3, "500": 1}},
        ],
    }


def analytics(**kwargs: Any) -> dict[str, Any]:
    kwargs.setdefault("rollup", fixture_rollup())
    kwargs.setdefault("days", 7)
    kwargs.setdefault("today", TODAY)
    kwargs.setdefault("prices", PRICES)
    return build_analytics(**kwargs)


class FallbackRateTests(unittest.TestCase):
    """Hand-computed rates: 8 requests, 4 fallbacks over the window."""

    def test_overall_and_per_group_rates_match_hand_computed_values(self) -> None:
        payload = analytics()
        overall = payload["fallback"]["overall"]
        self.assertEqual(overall["requests"], 8)
        self.assertEqual(overall["fallbacks"], 4)
        self.assertEqual(overall["fallback_rate"], 0.5)

        # coding: rows 1 and 3 -> 7 requests, 4 fallbacks.
        coding = payload["fallback"]["alias"]["coding"]
        self.assertEqual((coding["requests"], coding["fallbacks"]), (7, 4))
        self.assertEqual(coding["fallback_rate"], round(4 / 7, 6))
        # pm never fell back: a measured zero, not an unknown.
        pm = payload["fallback"]["alias"]["pm"]
        self.assertEqual((pm["requests"], pm["fallbacks"]), (1, 0))
        self.assertEqual(pm["fallback_rate"], 0.0)

        # The provider and model splits follow the same rows.
        self.assertEqual(payload["fallback"]["provider"]["opencode_go"]["fallback_rate"], round(4 / 7, 6))
        self.assertEqual(payload["fallback"]["provider"]["p-two"]["fallback_rate"], 0.0)
        self.assertEqual(payload["fallback"]["model"]["mimo-v2.6-flash"]["requests"], 7)
        self.assertEqual(payload["fallback"]["model"]["m-two"]["fallback_rate"], 0.0)

    def test_reasons_are_folded_onto_the_closed_vocabulary_and_sorted(self) -> None:
        payload = analytics()
        reasons = payload["fallback"]["overall"]["fallback_reasons"]
        self.assertEqual(
            reasons,
            [
                {"reason": "cooldown", "count": 2, "share": 0.5},
                {"reason": "other", "count": 1, "share": 0.25},
                {"reason": "route_ineligible", "count": 1, "share": 0.25},
            ],
        )
        self.assertTrue({entry["reason"] for entry in reasons} <= CLOSED_REASON_VOCABULARY)
        # The raw sentences never surface as buckets.
        self.assertNotIn(COOLDOWN_REASON, json.dumps(payload["fallback"]))

    def test_a_group_with_no_requests_reports_no_rate_rather_than_zero(self) -> None:
        rollup = fixture_rollup()
        for row in rollup["rows"]:
            row["requests"] = 0
        for entry in rollup["daily"]:
            entry["requests"] = 0
        payload = build_analytics(rollup, days=7, today=TODAY, prices=PRICES)
        self.assertIsNone(payload["fallback"]["overall"]["fallback_rate"])
        self.assertIsNone(payload["fallback"]["alias"]["coding"]["fallback_rate"])

    def test_reason_classification_is_first_match_and_never_raises(self) -> None:
        self.assertEqual(fallback_reason_class(COOLDOWN_REASON), "cooldown")
        self.assertEqual(fallback_reason_class(INELIGIBLE_REASON), "route_ineligible")
        self.assertEqual(fallback_reason_class(OTHER_REASON), "other")
        self.assertEqual(fallback_reason_class("unknown"), "unknown")
        self.assertEqual(fallback_reason_class(None), "unknown")
        self.assertEqual(fallback_reason_class("   "), "unknown")
        self.assertEqual(fallback_reason_class("nonsense"), "other")


class DailySeriesTests(unittest.TestCase):
    def test_series_matches_the_fixture_day_by_day(self) -> None:
        payload = analytics()
        series = payload["series"]
        self.assertEqual([entry["day"] for entry in series],
                         ["2026-10-18", "2026-10-19", "2026-10-20", "2026-10-21",
                          "2026-10-22", "2026-10-23", "2026-10-24"])
        by_day = {entry["day"]: entry for entry in series}

        recorded = by_day["2026-10-22"]
        self.assertEqual(recorded["status"], "ok")
        self.assertEqual(recorded["requests"], 4)
        self.assertEqual(recorded["input_tokens"], 3100)
        self.assertEqual(recorded["output_tokens"], 310)
        self.assertEqual(recorded["fallbacks"], 1)
        self.assertEqual(recorded["fallback_rate"], 0.25)
        self.assertEqual(recorded["errors"], 1)

        open_day = by_day["2026-10-24"]
        self.assertEqual(open_day["status"], "open")
        self.assertEqual(open_day["requests"], 4)
        self.assertEqual(open_day["fallbacks"], 3)
        self.assertEqual(open_day["fallback_rate"], 0.75)

    def test_a_missing_day_is_a_gap_with_nulls_not_zeros(self) -> None:
        payload = analytics()
        gap = [entry for entry in payload["series"] if entry["status"] == "gap"]
        self.assertEqual([entry["day"] for entry in gap], ["2026-10-23"])
        self.assertEqual(payload["gaps"], ["2026-10-23"])
        for key in ("requests", "input_tokens", "output_tokens", "fallbacks",
                    "errors", "fallback_rate", "fallback_reasons"):
            self.assertIsNone(gap[0][key], key)
        self.assertIsNone(payload["usage"]["2026-10-23"])

    def test_days_before_recorded_history_are_not_gaps(self) -> None:
        payload = analytics()
        by_day = {entry["day"]: entry for entry in payload["series"]}
        for day in ("2026-10-18", "2026-10-19", "2026-10-21"):
            self.assertEqual(by_day[day]["status"], "outside_coverage", day)
        self.assertNotIn("2026-10-21", payload["gaps"])

    def test_the_window_shows_exactly_the_days_it_was_asked_for(self) -> None:
        payload = analytics(days=40)
        self.assertEqual(payload["days"], 40)
        self.assertEqual(len(payload["series"]), 40)
        # The payload reports what it was asked for alongside what it shows.
        self.assertNotIn("requested_days", payload)
        echoed = analytics(days=40, requested_days=40)
        self.assertEqual(echoed["requested_days"], 40)

    def test_incomplete_rollup_coverage_is_carried_through(self) -> None:
        rollup = fixture_rollup()
        rollup["coverage"]["incomplete"] = True
        rollup["coverage"]["rows_skipped"] = 4
        payload = build_analytics(rollup, days=7, today=TODAY, prices=PRICES)
        self.assertTrue(payload["coverage"]["incomplete"])
        self.assertEqual(payload["coverage"]["rows_skipped"], 4)


class PricedUsageTests(unittest.TestCase):
    def test_amount_is_non_null_exactly_when_a_published_price_exists(self) -> None:
        usage = analytics()["usage"]["2026-10-22"]
        # Priced: opencode_go model with a published price, 3000 + 300 tokens.
        mimo = usage["model"]["mimo-v2.6-flash"]
        self.assertAlmostEqual(mimo["amount_usd"], 0.00126)
        self.assertIsNone(mimo["cost_reason"])
        self.assertEqual(mimo["source"], "https://opencode.ai/docs/go/")
        self.assertEqual(mimo["observed_at"], "2026-10-22")
        self.assertEqual(usage["alias"]["coding"]["amount_usd"], mimo["amount_usd"])

        # Not priced: another provider's model id never borrows that price.
        other = usage["model"]["m-two"]
        self.assertIsNone(other["amount_usd"])
        self.assertEqual(other["cost_reason"], "no_published_price")
        self.assertIsNone(other["source"])
        self.assertIsNone(usage["alias"]["pm"]["amount_usd"])

    def test_pricing_uses_the_whole_window_of_rows(self) -> None:
        usage = analytics()["usage"]["2026-10-24"]
        # 1000 input + 100 output at 0.3 / 1.2 per million = 0.00042.
        self.assertAlmostEqual(usage["model"]["mimo-v2.6-flash"]["amount_usd"], 0.00042)

    def test_without_a_price_snapshot_everything_stays_unknown(self) -> None:
        payload = build_analytics(fixture_rollup(), days=7, today=TODAY, prices={})
        for group in payload["usage"]["2026-10-22"].values():
            for row in group.values():
                self.assertIsNone(row["amount_usd"])
                self.assertEqual(row["cost_reason"], "no_published_price")

    def test_mixed_priced_and_unpriced_fallback_group_keeps_its_known_subtotal(self) -> None:
        """A ChatGPT fallback inside an OpenCode group must not poison the total."""
        rollup = fixture_rollup()
        rollup["rows"].append({
            "day": "2026-10-22", "alias": "coding", "provider": "chatgpt_codex",
            "model": "gpt-6-luna", "requests": 2, "input_tokens": 500,
            "output_tokens": 100, "cached_tokens": 0, "fallbacks": 0,
            "fallback_reasons": {}, "errors": {}, "statuses": {"200": 2},
        })
        usage = build_analytics(rollup, days=7, today=TODAY, prices=PRICES)["usage"]["2026-10-22"]
        coding = usage["alias"]["coding"]
        # 3000 input + 300 output at 0.3 / 1.2 per million = 0.00126.
        self.assertAlmostEqual(coding["amount_usd"], 0.00126)
        self.assertEqual(coding["requests"], 5)
        self.assertEqual(coding["known_cost_requests"], 3)
        self.assertEqual(coding["unknown_cost_requests"], 2)
        self.assertEqual(coding["unknown_cost_input_tokens"], 500)
        self.assertEqual(coding["unknown_cost_output_tokens"], 100)
        self.assertTrue(coding["cost_incomplete"])
        self.assertEqual(coding["cost_reason"], "no_published_price")
        self.assertEqual(coding["source"], "https://opencode.ai/docs/go/")
        # The unpriced provider row stays fully unknown, labelled and unpriced.
        codex = usage["provider"]["chatgpt_codex"]
        self.assertIsNone(codex["amount_usd"])
        self.assertEqual(codex["known_cost_requests"], 0)
        self.assertEqual(codex["unknown_cost_requests"], 2)
        self.assertEqual(codex["cost_reason"], "no_published_price")
        # The priced model row is untouched by the fallback.
        self.assertAlmostEqual(usage["model"]["mimo-v2.6-flash"]["amount_usd"], 0.00126)

    def test_cached_token_incomplete_rows_stay_explicit_not_guessed(self) -> None:
        from gateway.costs import aggregate_usage
        events = [
            {"timestamp": "2099-01-01T00:00:00Z", "requested_alias": "coding",
             "serving_provider": "opencode_go", "serving_model": "mimo-v2.6-flash",
             "token_counters": {"prompt_tokens": 3000, "completion_tokens": 300}},
            {"timestamp": "2099-01-01T00:00:01Z", "requested_alias": "coding",
             "serving_provider": "opencode_go", "serving_model": "mimo-v2.6-flash",
             "token_counters": {"prompt_tokens": 1000, "completion_tokens": 100,
                                "cached_tokens_reported": True}},
        ]
        row = aggregate_usage(events, window_hours=24, _prices=PRICES)["model"]["mimo-v2.6-flash"]
        # 3000 input + 300 output at 0.3 / 1.2 per million = 0.00126.
        self.assertAlmostEqual(row["amount_usd"], 0.00126)
        self.assertEqual(row["unknown_cost_requests"], 1)
        self.assertEqual(row["unknown_cost_input_tokens"], 1000)
        self.assertEqual(row["unknown_cost_output_tokens"], 100)
        self.assertEqual(row["cost_reason"], "missing_cached_tokens")
        self.assertTrue(row["cost_incomplete"])
        self.assertEqual(row["source"], "https://opencode.ai/docs/go/")


class UnavailableRollupTests(unittest.TestCase):
    def test_a_disabled_rollup_is_reported_not_invented(self) -> None:
        payload = build_analytics(None, days=7, today=TODAY, prices=PRICES)
        self.assertFalse(payload["available"])
        self.assertEqual(payload["unavailable_reason"], "durable_rollup_disabled")
        self.assertIsNone(payload["coverage"])
        self.assertEqual(payload["gaps"], [])
        self.assertEqual(payload["fallback"]["overall"], {
            "requests": 0, "fallbacks": 0, "fallback_rate": None, "fallback_reasons": [],
        })
        self.assertEqual(payload["fallback"]["alias"], {})
        for entry in payload["series"]:
            self.assertEqual(entry["status"], "unknown")
            self.assertIsNone(entry["requests"])
        self.assertTrue(all(value is None for value in payload["usage"].values()))

    def test_an_unrecognised_schema_is_refused_rather_than_interpreted(self) -> None:
        rollup = fixture_rollup()
        rollup["schema"] = "daily-rollup/v999"
        payload = build_analytics(rollup, days=7, today=TODAY, prices=PRICES)
        self.assertFalse(payload["available"])
        self.assertEqual(payload["unavailable_reason"], "unrecognised_rollup_schema")
        # Nothing from the refused payload leaks into the numbers.
        self.assertEqual(payload["fallback"]["overall"]["requests"], 0)
        self.assertTrue(all(value is None for value in payload["usage"].values()))

    def test_a_malformed_row_does_not_break_the_window(self) -> None:
        rollup = fixture_rollup()
        rollup["rows"].append("not-a-row")
        rollup["rows"].append({"alias": "coding"})  # no day, no counts
        payload = build_analytics(rollup, days=7, today=TODAY, prices=PRICES)
        self.assertTrue(payload["available"])
        self.assertEqual(payload["fallback"]["overall"]["requests"], 8)


class AnalyticsWindowTests(unittest.TestCase):
    class Query:
        def __init__(self, days: Any) -> None:
            self._days = days

        def query_one(self, key: str) -> Any:
            return self._days if key == "days" else None

    def test_default_window_is_seven_days(self) -> None:
        self.assertEqual(analytics_days(self.Query(None)), (7, None))

    def test_unparseable_window_falls_back_to_the_default(self) -> None:
        self.assertEqual(analytics_days(self.Query("soon")), (7, None))
        self.assertEqual(analytics_days(self.Query("")), (7, None))

    def test_window_is_clamped_to_thirty_and_the_request_is_echoed(self) -> None:
        self.assertEqual(analytics_days(self.Query("999")), (30, 999))
        self.assertEqual(analytics_days(self.Query("0")), (1, 0))
        self.assertEqual(analytics_days(self.Query("-5")), (1, -5))
        self.assertEqual(analytics_days(self.Query("30")), (30, None))
        self.assertEqual(analytics_days(self.Query("7")), (7, None))


class StateEndpointAnalyticsTests(unittest.TestCase):
    """The served path: a real fallback request, then the owner state."""

    @classmethod
    def setUpClass(cls) -> None:
        from gateway.adapters.base import AdapterError
        from gateway.config import Alias, ModelEntry, ProviderState, RouteRef
        from tests.test_end_to_end import FAKE_PROVIDER, FakeAdapter, GatewayHarness

        harness = GatewayHarness(FakeAdapter(fail_with=AdapterError("exhausted", provider_id=FAKE_PROVIDER)))
        cls.harness = harness
        harness.registry.register(FakeAdapter(provider_id="opencode_go", dialect="openai_chat"))
        with harness.config.mutate() as config:
            config.providers["opencode_go"] = ProviderState(
                provider_id="opencode_go", kind="opencode_go", state="connected",
                models={"mimo-v2.6-flash": ModelEntry(
                    model_id="mimo-v2.6-flash", enabled=True, dialect="openai_chat",
                    source="discovered")},
            )
            config.aliases["coding"] = Alias(
                name="coding", enabled=True, primary=RouteRef(FAKE_PROVIDER, "gpt-6-luna"),
                fallbacks=[RouteRef("opencode_go", "mimo-v2.6-flash")],
                fallback_authorized=True, fallback_confirmed_providers=["opencode_go"],
                fallback_triggers=["exhausted"],
            )
        cls.durable = Path(harness.data_dir) / "request-ledger.jsonl"
        ledger = RequestLedger(persistence_path=cls.durable)
        harness.router._ledger = ledger
        harness.app.ledger = ledger
        cls.ledger = ledger
        status, _payload, _headers = harness.request(
            "POST", "/v1/chat/completions",
            body={"model": "coding", "messages": [{"role": "user", "content": "zqx-analytics-marker"}]},
        )
        assert status == 200, f"the fallback request must succeed, got {status}"
        assert ledger.flush(), "the fixture ledger must flush"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.ledger.close()
        cls.harness.close()

    def test_state_carries_the_fallback_analytics_block(self) -> None:
        status, state, _ = self.harness.request("GET", "/owner/api/state")
        self.assertEqual(status, 200)
        analytics_block = state["analytics"]
        self.assertTrue(analytics_block["available"])
        self.assertIsNone(analytics_block["unavailable_reason"])
        self.assertEqual(analytics_block["schema"], ROLLUP_SCHEMA)
        self.assertEqual(analytics_block["source"], "request-ledger.jsonl")

        coding = analytics_block["fallback"]["alias"]["coding"]
        self.assertEqual(coding["requests"], 1)
        self.assertEqual(coding["fallbacks"], 1)
        self.assertEqual(coding["fallback_rate"], 1.0)
        self.assertEqual(analytics_block["fallback"]["overall"]["fallback_rate"], 1.0)
        # Reasons arrive folded onto the closed vocabulary, never as sentences.
        reasons = coding["fallback_reasons"]
        self.assertTrue(reasons)
        self.assertTrue({entry["reason"] for entry in reasons} <= CLOSED_REASON_VOCABULARY)

        today = datetime.now(UTC).strftime("%Y-%m-%d")
        series = {entry["day"]: entry for entry in analytics_block["series"]}
        self.assertEqual(series[today]["status"], "open")
        self.assertEqual(series[today]["requests"], 1)
        self.assertEqual(series[today]["fallbacks"], 1)
        self.assertEqual(series[today]["fallback_rate"], 1.0)
        # A day with no rows inside the recorded window is a gap, not a zero.
        for entry in analytics_block["series"]:
            if entry["status"] in {"gap", "outside_coverage", "unknown", "no_rows"}:
                self.assertIsNone(entry["requests"], entry["day"])

        # Cost invariant across every group: the dollar figure is only
        # absent when no request in the group had a published price, and the
        # unknown coverage is always stated alongside a known subtotal.
        for day_usage in analytics_block["usage"].values():
            if day_usage is None:
                continue
            for group in day_usage.values():
                for row in group.values():
                    self.assertEqual(
                        row["amount_usd"] is None, row["known_cost_requests"] == 0, row)
                    self.assertEqual(
                        row["unknown_cost_requests"] >= 0
                        and row["unknown_cost_input_tokens"] >= 0
                        and row["unknown_cost_output_tokens"] >= 0, True, row)
                    if row["unknown_cost_requests"]:
                        self.assertTrue(row["cost_incomplete"], row)
                        self.assertIsNotNone(row["cost_reason"], row)

    def test_state_window_query_is_honoured_and_clamped(self) -> None:
        status, state, _ = self.harness.request("GET", "/owner/api/state?days=30")
        self.assertEqual(status, 200)
        self.assertEqual(state["analytics"]["days"], 30)
        self.assertEqual(len(state["analytics"]["series"]), 30)
        self.assertNotIn("requested_days", state["analytics"])

        status, state, _ = self.harness.request("GET", "/owner/api/state?days=999")
        self.assertEqual(status, 200)
        self.assertEqual(state["analytics"]["days"], 30)
        self.assertEqual(state["analytics"]["requested_days"], 999)

        status, state, _ = self.harness.request("GET", "/owner/api/state?days=later")
        self.assertEqual(status, 200)
        self.assertEqual(state["analytics"]["days"], 7)

    def test_a_state_poll_never_opens_the_ledger_file(self) -> None:
        """The read path is bounded: the rollup is served from memory."""
        sidecar = rollup_path_for(self.durable)
        self.assertTrue(sidecar.exists(), "the fixture rollup must be on disk first")
        ledger_bytes = self.durable.read_bytes()
        sidecar_bytes = sidecar.read_bytes()
        self.durable.unlink()
        sidecar.unlink()
        try:
            status, state, _ = self.harness.request("GET", "/owner/api/state")
            self.assertEqual(status, 200)
            self.assertTrue(state["analytics"]["available"])
            self.assertEqual(state["analytics"]["fallback"]["overall"]["requests"], 1)
        finally:
            self.durable.write_bytes(ledger_bytes)
            sidecar.write_bytes(sidecar_bytes)

    def test_the_ledger_content_never_leaks_into_the_state(self) -> None:
        status, state, _ = self.harness.request("GET", "/owner/api/state")
        self.assertEqual(status, 200)
        # The analytics block aggregates rows; it must not carry the prompt,
        # the request identifier, or any other per-request content.
        analytics_blob = json.dumps(state["analytics"])
        self.assertNotIn("zqx-analytics-marker", analytics_blob)
        self.assertNotIn("request_id", analytics_blob)
        self.assertNotIn("token_counters", analytics_blob)
        self.assertNotIn("zqx-analytics-marker", json.dumps(state["usage_costs"]))


if __name__ == "__main__":
    unittest.main()
