"""Provider-controlled token-counter bounds (SEC-R66-C) regression gate.

A provider response body is valid JSON even when it carries a 401-digit
``prompt_tokens`` integer. Before this gate, every counter check was
``isinstance(int) and >= 0``, so such a number flowed through
``response_token_counters`` / ``normalise_usage`` into the ledger, and:

1. ``calculate_cost`` raised ``OverflowError: int too large to convert to
   float`` on the ``int * float`` price multiplication, which the owner
   console's ``GET /owner/api/state`` calls unguarded - one cached ledger
   row 500ed the console poll for as long as the row stayed in the window;
2. the number serialised into response bodies, where the console's
   ``JSON.parse`` reads it as ``Infinity`` (a display lie, not a throw).

The contract under test, end to end:

* every seam refuses counters that are not bounded non-negative integers
  (``bool`` included) - :data:`gateway.bridge.streams.MAX_TOKEN_COUNTER`;
* a refusal is *recorded* (``invalid_usage`` marker / ``None``), never
  silently dropped next to a smaller counter that still gets priced, and
  never clamped into a plausible-looking number;
* ``calculate_cost`` can no longer raise on provider input, and an
  over-bound ledger row - including rows written before the bound existed -
  reads Unknown/invalid, never $0.00 and never a 5xx.
"""
from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime

from gateway.bridge.streams import MAX_TOKEN_COUNTER, normalise_usage
from gateway.capability_probe import _usage_tokens
from gateway.config import Alias, ModelEntry, RouteRef
from gateway.costs import (
    COST_INVALID_USAGE,
    INVALID_USAGE_MARKER,
    aggregate_rollup_rows,
    aggregate_usage,
    annotate_events,
    calculate_cost,
    response_token_counters,
    stream_token_counters,
)
from gateway.ledger_persistence import _row_delta
from gateway.observability import RequestEvent, RequestLedger
from tests.test_end_to_end import GatewayHarness

#: A valid-JSON integer four hundred and one digits long.
HUGE = 10 ** 400
MODEL = "gemini-4-argon"  # has published prices in data/go_catalog.json
PROVIDER = "opencode_go"
ALIAS = "bounded"

PRICES = {MODEL: {"input_per_m": 2.0, "output_per_m": 10.0, "cached_read_per_m": 0.1,
                  "source": "catalogue", "observed_at": "2026-10-31"}}


def _max_int(value) -> int:
    """The largest non-boolean integer anywhere in a JSON-ready structure."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, dict):
        return max((_max_int(item) for item in value.values()), default=0)
    if isinstance(value, (list, tuple)):
        return max((_max_int(item) for item in value), default=0)
    return 0


def _event(request_id: str, counters: dict | None) -> RequestEvent:
    return RequestEvent(
        request_id=request_id,
        timestamp=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        requested_alias=ALIAS,
        requested_model=MODEL,
        serving_provider=PROVIDER,
        serving_model=MODEL,
        status=200,
        latency_ms=100,
        token_counters=counters,
    )


class NormaliseUsageBoundTests(unittest.TestCase):
    def test_over_bound_counter_is_dropped_not_clamped(self) -> None:
        result = normalise_usage({"prompt_tokens": HUGE, "completion_tokens": 7})
        self.assertEqual(result, {"completion_tokens": 7})
        self.assertNotIn("prompt_tokens", result)

    def test_bool_and_negative_counters_are_dropped(self) -> None:
        self.assertEqual(normalise_usage({"prompt_tokens": True}), {})
        self.assertEqual(normalise_usage({"prompt_tokens": -1}), {})

    def test_derived_total_is_bounded_too(self) -> None:
        near = MAX_TOKEN_COUNTER - 5
        result = normalise_usage({"prompt_tokens": near, "completion_tokens": 10})
        self.assertNotIn("total_tokens", result)
        self.assertEqual(result["prompt_tokens"], near)


class ResponseCounterRefusalTests(unittest.TestCase):
    """The refusal must be recorded, not silently dropped (never a partial claim)."""

    def _body(self, usage: dict) -> dict:
        raw = json.dumps({"usage": usage}).encode("utf-8")
        result = response_token_counters(raw, "openai_chat")
        assert result is not None
        return result

    def test_over_bound_prompt_with_cached_detail_records_refusal(self) -> None:
        result = self._body({"prompt_tokens": HUGE,
                             "prompt_tokens_details": {"cached_tokens": 50}})
        self.assertTrue(result[INVALID_USAGE_MARKER])
        self.assertNotIn("prompt_tokens", result)
        # The usable cached count survives - but the marker guarantees it can
        # never be priced as if the refused input were zero.
        self.assertEqual(result["cached_read_tokens"], 50)

    def test_refusal_never_clamps_to_a_plausible_number(self) -> None:
        result = self._body({"prompt_tokens": HUGE, "completion_tokens": 3})
        self.assertTrue(result[INVALID_USAGE_MARKER])
        self.assertNotIn("prompt_tokens", result)
        self.assertNotIn(MAX_TOKEN_COUNTER, result.values())

    def test_over_bound_cached_detail_records_both_markers(self) -> None:
        # The cached count is refused (withheld marker) *and* the refusal is
        # flagged, so calculate_cost answers invalid rather than pricing the
        # 40 surviving prompt tokens as if the cached split were known.
        result = self._body({"prompt_tokens": 40,
                             "prompt_tokens_details": {"cached_tokens": HUGE}})
        self.assertEqual(result, {"prompt_tokens": 40, "cached_tokens_reported": True,
                                  INVALID_USAGE_MARKER: True})

    def test_clean_body_is_unchanged(self) -> None:
        result = self._body({"prompt_tokens": 1200, "completion_tokens": 80,
                             "prompt_tokens_details": {"cached_tokens": 200}})
        self.assertEqual(result, {"prompt_tokens": 1200, "completion_tokens": 80,
                                  "total_tokens": 1280, "cached_read_tokens": 200})

    def test_stream_frames_record_the_refusal_too(self) -> None:
        frame = (f'data: {{"usage": {{"prompt_tokens": {HUGE}, "completion_tokens": 3}}}}\n\n'
                 ).encode()
        result = stream_token_counters([frame], "openai_chat")
        assert result is not None
        self.assertTrue(result[INVALID_USAGE_MARKER])
        self.assertNotIn("prompt_tokens", result)
        self.assertEqual(result["completion_tokens"], 3)


class CalculateCostCannotRaiseTests(unittest.TestCase):
    def test_over_bound_counters_are_invalid_even_with_a_cached_detail(self) -> None:
        for counters in ({"prompt_tokens": HUGE, "completion_tokens": 5,
                          "cached_read_tokens": 10},
                         {"prompt_tokens": HUGE}):
            with self.subTest(counters=sorted(counters)):
                cost = calculate_cost(counters, MODEL, PROVIDER, _prices=PRICES)
                self.assertIsNone(cost["amount_usd"])
                self.assertEqual(cost["reason"], COST_INVALID_USAGE)

    def test_marker_forces_invalid_even_beside_small_counters(self) -> None:
        cost = calculate_cost({INVALID_USAGE_MARKER: True, "prompt_tokens": 10,
                               "completion_tokens": 5, "cached_read_tokens": 1},
                              MODEL, PROVIDER, _prices=PRICES)
        self.assertIsNone(cost["amount_usd"])
        self.assertEqual(cost["reason"], COST_INVALID_USAGE)

    def test_non_finite_rate_is_never_a_price(self) -> None:
        prices = {MODEL: {**PRICES[MODEL], "input_per_m": float("inf")}}
        cost = calculate_cost({"prompt_tokens": 100, "completion_tokens": 1,
                               "cached_read_tokens": 0}, MODEL, PROVIDER, _prices=prices)
        self.assertIsNone(cost["amount_usd"])
        self.assertIsNotNone(cost["reason"])

    def test_clean_counters_still_price(self) -> None:
        cost = calculate_cost({"prompt_tokens": 1000, "completion_tokens": 500,
                               "cached_read_tokens": 100}, MODEL, PROVIDER, _prices=PRICES)
        self.assertEqual(cost["reason"], None)
        self.assertAlmostEqual(cost["amount_usd"], (900 * 2.0 + 500 * 10.0 + 100 * 0.1) / 1e6)


class RefusalIsRecordedNotClaimedTests(unittest.TestCase):
    """The write seam refuses over-bound counters and accepts the marker."""

    def test_ledger_amend_refuses_over_bound_counters(self) -> None:
        ledger = RequestLedger()
        ledger.record(_event("req_a", None))
        with self.assertRaises(ValueError):
            ledger.amend("req_a", token_counters={"prompt_tokens": HUGE})

    def test_ledger_amend_accepts_the_refusal_marker(self) -> None:
        ledger = RequestLedger()
        ledger.record(_event("req_b", None))
        refused = {"cached_read_tokens": 50, INVALID_USAGE_MARKER: True}
        self.assertTrue(ledger.amend("req_b", token_counters=refused))
        recent = ledger.recent()
        assert len(recent) == 1
        self.assertEqual(recent[0]["token_counters"], refused)

    def test_rollup_delta_of_a_refused_row_claims_nothing(self) -> None:
        delta = _row_delta({"token_counters": {"prompt_tokens": HUGE,
                                               "cached_read_tokens": 50,
                                               INVALID_USAGE_MARKER: True}})
        self.assertEqual(delta["input_tokens"], 0)
        self.assertEqual(delta["cached_tokens"], 0)
        self.assertEqual(delta["requests_without_usage"], 1)
        self.assertEqual(delta["cached_tokens_unreported"], 0)

    def test_rollup_read_bounds_a_tampered_sidecar_row(self) -> None:
        groups = aggregate_rollup_rows(
            [{"model": MODEL, "provider": PROVIDER, "requests": 1,
              "input_tokens": HUGE, "output_tokens": 10, "cached_tokens": 0}],
            _prices=PRICES)
        row = groups["model"][MODEL]
        self.assertLessEqual(_max_int(row), MAX_TOKEN_COUNTER)
        self.assertEqual(row["input_tokens"], 0)


class ProbeRunBodyBoundTests(unittest.TestCase):
    def test_run_body_usage_never_exceeds_the_bound(self) -> None:
        usage = _usage_tokens({"prompt_tokens": HUGE, "completion_tokens": 3})
        self.assertIsNone(usage["input_tokens"])
        self.assertEqual(usage["output_tokens"], 3)
        self.assertLessEqual(_max_int(usage), MAX_TOKEN_COUNTER)

    def test_run_body_usage_accepts_real_counters(self) -> None:
        usage = _usage_tokens({"prompt_tokens": 1200, "completion_tokens": 80})
        self.assertEqual(usage, {"input_tokens": 1200, "output_tokens": 80})

    def test_a_refused_run_body_counter_is_recorded_not_erased(self) -> None:
        """A *reported* count the bound refuses leaves a refusal record (SEC-R66-C).

        On candidate `d12d980` a 401-digit counter read exactly like absent
        usage — both produced `{"input_tokens": null, "output_tokens": null}`
        — so the evidence could not tell "the provider gave a number this
        gateway will not carry" from "the provider reported nothing". The
        `invalid_usage: true` marker (the ledger seam's own vocabulary)
        records the refusal; it is a bool, so it can never serialise into a
        number the console reads as `Infinity`.
        """
        self.assertEqual(
            _usage_tokens({"prompt_tokens": HUGE, "completion_tokens": 3}),
            {"input_tokens": None, "output_tokens": 3, INVALID_USAGE_MARKER: True},
        )
        for usage in (
            {"prompt_tokens": HUGE, "completion_tokens": 3},
            {"prompt_tokens": float("inf"), "completion_tokens": 3},
            {"prompt_tokens": 12.5, "completion_tokens": 3},
            {"completion_tokens": 3, "total_tokens": HUGE},
            {"prompt_tokens": True, "completion_tokens": 3},
        ):
            with self.subTest(usage=usage):
                result = _usage_tokens(usage)
                self.assertIs(result[INVALID_USAGE_MARKER], True)
                self.assertLessEqual(_max_int(result), MAX_TOKEN_COUNTER)

    def test_absent_or_non_numeric_usage_reports_nulls_without_a_refusal(self) -> None:
        """`not reported` stays distinct from `reported and refused`.

        An absent, null or non-numeric counter was never a number this
        gateway could refuse, so it must not acquire the marker — otherwise
        every ordinary run would claim a refusal that never happened.
        """
        for usage in (
            None,
            {},
            {"cost": 0.0},
            {"prompt_tokens": None, "completion_tokens": None},
        ):
            with self.subTest(usage=usage):
                result = _usage_tokens(usage)
                self.assertEqual(result, {"input_tokens": None, "output_tokens": None})
                self.assertNotIn(INVALID_USAGE_MARKER, result)
        # A non-numeric counter is "not reported" under the shared
        # `reported_counters_unusable` definition: it claims no number, and
        # it does not claim a refusal that never happened — but the counter
        # that *is* a number still travels.
        result = _usage_tokens({"prompt_tokens": "120", "completion_tokens": 40})
        self.assertEqual(result, {"input_tokens": None, "output_tokens": 40})
        self.assertNotIn(INVALID_USAGE_MARKER, result)


class OwnerStateAcceptanceTests(unittest.TestCase):
    """GET /owner/api/state stays 200 with a pre-bound 10**400 ledger row.

    The rows are recorded directly (as a pre-fix row in the durable ledger
    would be), with and without a cached-token detail: the cached path is
    the one that used to raise ``OverflowError`` inside ``calculate_cost``.
    """

    @classmethod
    def setUpClass(cls) -> None:
        harness = GatewayHarness()
        with harness.config.mutate() as snapshot:
            snapshot.providers[PROVIDER].models[MODEL] = ModelEntry(
                model_id=MODEL, enabled=True, dialect="openai_chat", source="discovered")
            snapshot.aliases[ALIAS] = Alias(
                name=ALIAS, enabled=True, primary=RouteRef(PROVIDER, MODEL))
        ledger = harness.app.ledger
        assert ledger is not None
        ledger.record(_event("req_huge_cached", {"prompt_tokens": HUGE,
                                                 "completion_tokens": 5,
                                                 "cached_read_tokens": 50}))
        ledger.record(_event("req_huge_plain", {"prompt_tokens": HUGE}))
        cls.status, cls.payload, _ = harness.request("GET", "/owner/api/state")
        harness.close()

    def test_state_stays_200(self) -> None:
        self.assertEqual(self.status, 200)

    def test_no_json_number_can_become_infinity(self) -> None:
        self.assertLessEqual(_max_int(self.payload), MAX_TOKEN_COUNTER)
        # Belt and braces: the serialised body must parse without JS-only
        # literals and hold no digit run the console would read as Infinity.
        raw = json.dumps(self.payload)
        self.assertNotIn("Infinity", raw)
        self.assertNotIn("NaN", raw)

    def test_costs_read_invalid_not_zero(self) -> None:
        costs = {row["request_id"]: row["cost"] for row in self.payload["ledger"]
                 if row.get("request_id") in {"req_huge_cached", "req_huge_plain"}}
        self.assertEqual(set(costs), {"req_huge_cached", "req_huge_plain"})
        for request_id, cost in costs.items():
            with self.subTest(request_id=request_id):
                self.assertIsNone(cost["amount_usd"])
                self.assertEqual(cost["reason"], COST_INVALID_USAGE)

    def test_ledger_rows_show_the_refusal_not_the_number(self) -> None:
        rows = {row["request_id"]: row for row in self.payload["ledger"]
                if row.get("request_id") in {"req_huge_cached", "req_huge_plain"}}
        for request_id, row in rows.items():
            with self.subTest(request_id=request_id):
                counters = row["token_counters"]
                self.assertNotIn("prompt_tokens", counters)
                self.assertTrue(counters[INVALID_USAGE_MARKER])

    def test_aggregates_withhold_instead_of_claiming(self) -> None:
        model_row = self.payload["usage_costs"]["model"][MODEL]
        self.assertIsNone(model_row["amount_usd"])
        self.assertEqual(model_row["cost_reason"], COST_INVALID_USAGE)
        self.assertLessEqual(_max_int(model_row), MAX_TOKEN_COUNTER)
        stats = self.payload["request_statistics"]["hour"]["model"][MODEL]
        self.assertIsNone(stats["input_tokens"])
        self.assertIsNone(stats["output_tokens"])
        self.assertIsNone(stats["estimated_cost_usd"])


class AggregateAndAnnotateBoundTests(unittest.TestCase):
    def test_aggregate_usage_never_sums_an_over_bound_row(self) -> None:
        events = [{"timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                   "requested_alias": ALIAS, "serving_provider": PROVIDER,
                   "serving_model": MODEL, "status": 200,
                   "token_counters": {"prompt_tokens": HUGE, "completion_tokens": 5,
                                      "cached_read_tokens": 50}}]
        usage = aggregate_usage(events, _prices=PRICES)
        row = usage["model"][MODEL]
        self.assertEqual(row["input_tokens"], 0)
        self.assertIsNone(row["amount_usd"])
        self.assertEqual(row["cost_reason"], COST_INVALID_USAGE)
        self.assertLessEqual(_max_int(usage), MAX_TOKEN_COUNTER)

    def test_annotate_events_sanitises_the_rendered_row(self) -> None:
        events = [{"request_id": "req_x", "serving_model": MODEL,
                   "serving_provider": PROVIDER,
                   "token_counters": {"prompt_tokens": HUGE, "cached_read_tokens": 50}}]
        annotated = annotate_events(events, _prices=PRICES)
        counters = annotated[0]["token_counters"]
        self.assertNotIn("prompt_tokens", counters)
        self.assertTrue(counters[INVALID_USAGE_MARKER])
        self.assertEqual(counters["cached_read_tokens"], 50)
        self.assertEqual(annotated[0]["cost"]["reason"], COST_INVALID_USAGE)


if __name__ == "__main__":
    unittest.main()
