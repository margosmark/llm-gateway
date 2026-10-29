import unittest
from datetime import UTC

from gateway.core.capabilities import DIALECT_OPENAI_CHAT, DIALECT_OPENAI_RESPONSES
from gateway.costs import (
    aggregate_usage,
    calculate_cost,
    published_prices,
    response_token_counters,
    stream_token_counters,
)


class UsageCostTests(unittest.TestCase):
    def test_buffered_usage_is_normalised_and_priced_from_catalogue(self):
        counters = response_token_counters(
            b'{"usage":{"prompt_tokens":1000,"completion_tokens":250,"total_tokens":1250}}',
            DIALECT_OPENAI_CHAT,
        )
        self.assertEqual(counters, {"prompt_tokens": 1000, "completion_tokens": 250, "total_tokens": 1250})
        # The response carried no cached accounting at all: absent is not a
        # reported zero, so the amount is Unknown rather than a price computed
        # as zero cached reads.
        without_cached = calculate_cost(counters, "claude-haiku-5-5")
        self.assertIsNone(without_cached["amount_usd"])
        self.assertEqual(without_cached["reason"], "missing_cached_tokens")
        # The same usage with an explicit cached count prices normally.
        reported = response_token_counters(
            b'{"usage":{"prompt_tokens":1000,"completion_tokens":250,"total_tokens":1250,'
            b'"prompt_tokens_details":{"cached_tokens":0}}}',
            DIALECT_OPENAI_CHAT,
        )
        cost = calculate_cost(reported, "claude-haiku-5-5")
        self.assertAlmostEqual(cost["amount_usd"], 0.000225)
        # Prices are transcribed from the model's own data page, so the
        # estimate names that page and the date it was read.
        self.assertEqual(cost["source"], "https://opencode.ai/data/anthropic/claude-haiku-5-5")
        # The dated catalogue snapshot is refreshed, so the as-of date is read
        # from it rather than pinned to one snapshot's date.
        self.assertEqual(cost["observed_at"], published_prices()["claude-haiku-5-5"]["observed_at"])

    def test_stream_usage_is_normalised_on_both_wire_dialects(self):
        chat = [b'data: {"choices":[],"usage":{"prompt_tokens":12,"completion_tokens":3,"total_tokens":15}}\n\n']
        responses = [b'event: response.completed\ndata: {"type":"response.completed","response":{"usage":{"input_tokens":12,"output_tokens":3,"total_tokens":15}}}\n\n']
        expected = {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15}
        self.assertEqual(stream_token_counters(chat, DIALECT_OPENAI_CHAT), expected)
        self.assertEqual(stream_token_counters(responses, DIALECT_OPENAI_RESPONSES), expected)

    def test_unpriced_catalogue_model_is_unknown_not_zero(self) -> None:
        # ``omen-alpha``'s data page carries no payload at all, so it stays
        # unpriced. ``beam-free`` used to be the example here; the 2026-10-25
        # transcription gave it a list price, which is a price, not a bill.
        cost = calculate_cost({"prompt_tokens": 10, "completion_tokens": 2}, "omen-alpha")
        self.assertIsNone(cost["amount_usd"])
        self.assertIsNone(cost["source"])

    def test_aggregates_are_metadata_only_by_alias_provider_model(self):
        events = [{
            "timestamp": "2099-01-01T00:00:00Z", "requested_alias": "friendly", "serving_provider": "opencode_go",
            "serving_model": "claude-haiku-5-5", "token_counters": {"prompt_tokens": 100, "completion_tokens": 20, "cached_read_tokens": 0},
        }]
        result = aggregate_usage(events, window_hours=24)
        self.assertEqual(set(result), {"window_hours", "coverage", "alias", "provider", "model"})
        self.assertEqual(result["alias"]["friendly"]["requests"], 1)
        self.assertEqual(result["provider"]["opencode_go"]["input_tokens"], 100)
        self.assertAlmostEqual(result["model"]["claude-haiku-5-5"]["amount_usd"], 0.00002)
        self.assertFalse(any(key in str(result).lower() for key in ("prompt", "message", "completion")))

    def test_missing_cached_price_does_not_assume_cached_tokens_are_free(self):
        from unittest.mock import patch
        with patch("gateway.costs.published_prices", return_value={"model": {
            "input_per_m": 1.0, "output_per_m": 2.0, "cached_read_per_m": None,
            "source": "published", "observed_at": "2026-01-01",
        }}):
            cost = calculate_cost({"prompt_tokens": 100, "completion_tokens": 2, "cached_read_tokens": 10}, "model")
        self.assertIsNone(cost["amount_usd"])

    def test_same_model_name_at_another_provider_is_not_assigned_opencode_price(self):
        cost = calculate_cost({"prompt_tokens": 100, "completion_tokens": 20, "cached_read_tokens": 0}, "claude-haiku-5-5", provider="chatgpt_codex")
        self.assertIsNone(cost["amount_usd"])
        self.assertIsNone(cost["source"])

    def test_aggregate_loads_catalogue_once_for_many_events(self):
        from unittest.mock import patch
        events = [{
            "timestamp": "2099-01-01T00:00:00Z", "requested_alias": "a", "serving_provider": "opencode_go",
            "serving_model": "claude-haiku-5-5", "token_counters": {"prompt_tokens": 100, "completion_tokens": 20, "cached_read_tokens": 0},
        } for _ in range(10_000)]
        with patch("gateway.costs.published_prices", wraps=published_prices) as prices:
            result = aggregate_usage(events)
        self.assertEqual(prices.call_count, 1)
        self.assertEqual(result["model"]["claude-haiku-5-5"]["requests"], 10_000)
        self.assertAlmostEqual(result["model"]["claude-haiku-5-5"]["amount_usd"], 0.2)

    def test_evicted_events_mark_window_aggregate_incomplete(self):
        events = [{
            "timestamp": "2099-01-01T00:00:00Z", "requested_alias": "a", "serving_provider": "opencode_go",
            "serving_model": "claude-haiku-5-5", "token_counters": {"prompt_tokens": 100, "completion_tokens": 20, "cached_read_tokens": 0},
        } for _ in range(10_000)]
        result = aggregate_usage(events, window_hours=24, evicted_events=2_000)
        row = result["model"]["claude-haiku-5-5"]
        self.assertEqual(row["requests"], 10_000)
        # Eviction makes the window partial, not unpriceable: the amount
        # covers exactly the tokens this window counts, the same basis the
        # token columns use, and coverage.incomplete stays the honest flag
        # for the rows the ring no longer holds.
        self.assertAlmostEqual(row["amount_usd"], 0.2)
        self.assertEqual(row["cost_reason"], None)
        self.assertEqual(row["source"], "https://opencode.ai/data/anthropic/claude-haiku-5-5")
        self.assertTrue(result["coverage"]["incomplete"])
        self.assertEqual(result["coverage"]["source"], "bounded_recent_events")

    def test_price_exists_even_when_some_requests_reported_no_usage(self):
        # The live gateway recorded fallback rows with no usage block at all;
        # those must not blank out the whole group's price.
        events = [
            {"timestamp": "2099-01-01T00:00:00Z", "requested_alias": "a", "serving_provider": "opencode_go",
             "serving_model": "claude-haiku-5-5", "token_counters": {"prompt_tokens": 100, "completion_tokens": 20, "cached_read_tokens": 0}},
            {"timestamp": "2099-01-01T00:00:01Z", "requested_alias": "a", "serving_provider": "opencode_go",
             "serving_model": "claude-haiku-5-5", "token_counters": None, "status": 502},
        ]
        result = aggregate_usage(events, window_hours=24)
        row = result["model"]["claude-haiku-5-5"]
        self.assertEqual(row["requests"], 2)
        # The priced request keeps its subtotal; the one that reported no
        # usage is Unknown (never $0.00) and is visible as coverage.
        self.assertAlmostEqual(row["amount_usd"], 0.00002)
        self.assertEqual(row["cost_reason"], "no_usage_reported")
        self.assertTrue(row["cost_incomplete"])
        self.assertEqual(row["unknown_cost_requests"], 1)
        self.assertEqual(result["coverage"]["requests_without_usage"], 1)

    def test_amount_is_null_exactly_when_no_published_price_exists(self):
        # ``omen-alpha`` is the unpriced exemplar after the 2026-10-25
        # transcription priced ``beam-free``; a published price is a price.
        events = [
            {"timestamp": "2099-01-01T00:00:00Z", "requested_alias": "a", "serving_provider": "opencode_go",
             "serving_model": "claude-haiku-5-5", "token_counters": {"prompt_tokens": 100, "completion_tokens": 20, "cached_read_tokens": 0}},
            {"timestamp": "2099-01-01T00:00:00Z", "requested_alias": "b", "serving_provider": "opencode_go",
             "serving_model": "omen-alpha", "token_counters": {"prompt_tokens": 100, "completion_tokens": 20, "cached_read_tokens": 0}},
        ]
        result = aggregate_usage(events, window_hours=24)
        priced = result["model"]["claude-haiku-5-5"]
        unpriced = result["model"]["omen-alpha"]
        self.assertIsNotNone(priced["amount_usd"])
        self.assertEqual(priced["cost_reason"], None)
        self.assertIsNone(unpriced["amount_usd"])
        self.assertEqual(unpriced["cost_reason"], "no_published_price")
        self.assertIsNone(unpriced["source"])

    def test_a_price_that_cannot_be_applied_is_reported_not_guessed(self):
        from unittest.mock import patch
        with patch("gateway.costs.published_prices", return_value={"model": {
            "input_per_m": 1.0, "output_per_m": 2.0, "cached_read_per_m": None,
            "source": "published", "observed_at": "2026-01-01",
        }}):
            events = [{"timestamp": "2099-01-01T00:00:00Z", "requested_alias": "a", "serving_provider": "opencode_go",
                       "serving_model": "model", "token_counters": {"prompt_tokens": 100, "completion_tokens": 2,
                                                                    "cached_read_tokens": 10}}]
            result = aggregate_usage(events, window_hours=24)
        row = result["model"]["model"]
        self.assertIsNone(row["amount_usd"])
        self.assertEqual(row["cost_reason"], "missing_cached_price")
        self.assertEqual(row["source"], "published")

    def test_unknown_price_dominates_aggregate_instead_of_underreporting(self) -> None:
        events = [{
            "timestamp": "2099-01-01T00:00:00Z", "requested_alias": "alias", "serving_provider": "opencode_go",
            "serving_model": "omen-alpha", "token_counters": {"prompt_tokens": 1, "completion_tokens": 1},
        }]
        self.assertIsNone(aggregate_usage(events)["model"]["omen-alpha"]["amount_usd"])

    def test_mixed_priced_and_unpriced_fallback_group_keeps_known_subtotal(self):
        events = [
            {"timestamp": "2099-01-01T00:00:00Z", "requested_alias": "coding", "serving_provider": "opencode_go",
             "serving_model": "claude-haiku-5-5", "token_counters": {"prompt_tokens": 1000, "completion_tokens": 250, "cached_read_tokens": 0}},
            {"timestamp": "2099-01-01T00:00:01Z", "requested_alias": "coding", "serving_provider": "chatgpt_codex",
             "serving_model": "gpt-6-luna", "token_counters": {"prompt_tokens": 500, "completion_tokens": 100, "cached_read_tokens": 0},
             "fallback_used": True},
        ]
        result = aggregate_usage(events, window_hours=24)
        alias = result["alias"]["coding"]
        self.assertAlmostEqual(alias["amount_usd"], 0.000225)
        self.assertEqual(alias["unknown_cost_requests"], 1)
        self.assertEqual(alias["unknown_cost_input_tokens"], 500)
        self.assertEqual(alias["unknown_cost_output_tokens"], 100)
        self.assertTrue(alias["cost_incomplete"])
        self.assertEqual(alias["source"], "https://opencode.ai/data/anthropic/claude-haiku-5-5")
        self.assertEqual(result["provider"]["opencode_go"]["unknown_cost_requests"], 0)
        self.assertEqual(result["provider"]["chatgpt_codex"]["unknown_cost_requests"], 1)

    def test_missing_cached_token_count_preserves_known_subtotal_and_marks_unknown(self):
        events = [
            {"timestamp": "2099-01-01T00:00:00Z", "requested_alias": "coding", "serving_provider": "opencode_go",
             "serving_model": "claude-haiku-5-5", "token_counters": {"prompt_tokens": 1000, "completion_tokens": 250, "cached_read_tokens": 0}},
            {"timestamp": "2099-01-01T00:00:01Z", "requested_alias": "coding", "serving_provider": "opencode_go",
             "serving_model": "claude-haiku-5-5", "token_counters": {"prompt_tokens": 500, "completion_tokens": 100,
             "cached_tokens_reported": True}},
        ]
        result = aggregate_usage(events, window_hours=24)
        row = result["model"]["claude-haiku-5-5"]
        self.assertAlmostEqual(row["amount_usd"], 0.000225)
        self.assertEqual(row["unknown_cost_requests"], 1)
        self.assertEqual(row["unknown_cost_input_tokens"], 500)
        self.assertEqual(row["unknown_cost_output_tokens"], 100)
        self.assertEqual(row["cost_reason"], "missing_cached_tokens")
        self.assertTrue(row["cost_incomplete"])
        self.assertEqual(row["source"], "https://opencode.ai/data/anthropic/claude-haiku-5-5")

    def test_caller_provider_model_name_does_not_borrow_published_price(self):
        events = [{
            "timestamp": "2099-01-01T00:00:00Z", "requested_alias": "coding", "serving_provider": "chatgpt_codex",
            "serving_model": "claude-haiku-5-5", "token_counters": {"prompt_tokens": 1000, "completion_tokens": 250, "cached_read_tokens": 0},
        }]
        row = aggregate_usage(events, window_hours=24)["model"]["claude-haiku-5-5"]
        self.assertIsNone(row["amount_usd"])
        self.assertEqual(row["unknown_cost_requests"], 1)
        self.assertTrue(row["cost_incomplete"])
        self.assertIsNone(row["source"])
        self.assertEqual(row["cost_reason"], "no_published_price")

    def test_cached_token_metadata_without_count_is_unknown_not_free(self):
        cost = calculate_cost({"prompt_tokens": 100, "completion_tokens": 10,
                               "cached_tokens_reported": True}, "claude-haiku-5-5")
        self.assertIsNone(cost["amount_usd"])
        self.assertEqual(cost["reason"], "missing_cached_tokens")
        self.assertEqual(cost["source"], "https://opencode.ai/data/anthropic/claude-haiku-5-5")

    def test_unpriced_only_group_has_zero_known_subtotal_but_unknown_coverage(self):
        events = [{
            "timestamp": "2099-01-01T00:00:00Z", "requested_alias": "alias", "serving_provider": "p",
            "serving_model": "omen-alpha", "token_counters": {"prompt_tokens": 1, "completion_tokens": 1, "cached_read_tokens": 0},
        }]
        row = aggregate_usage(events, window_hours=24)["model"]["omen-alpha"]
        self.assertIsNone(row["amount_usd"])
        self.assertTrue(row["cost_incomplete"])
        self.assertEqual(row["unknown_cost_requests"], 1)
        self.assertIsNone(row["source"])
        self.assertEqual(row["cost_reason"], "no_published_price")

    def test_unreported_usage_is_unknown_never_zero_dollars(self):
        # R26: no reported usage is Unknown, never $0.00. A silent zero in
        # the cost column would be an invented figure, so the group carries
        # no known subtotal at all here - only coverage that says why.
        events = [{
            "timestamp": "2099-01-01T00:00:00Z", "requested_alias": "alias", "serving_provider": "opencode_go",
            "serving_model": "claude-haiku-5-5", "token_counters": None,
        }]
        row = aggregate_usage(events, window_hours=24)["model"]["claude-haiku-5-5"]
        self.assertIsNone(row["amount_usd"])
        self.assertTrue(row["cost_incomplete"])
        self.assertEqual(row["known_cost_requests"], 0)
        self.assertEqual(row["unknown_cost_requests"], 1)
        self.assertEqual(row["cost_reason"], "no_usage_reported")
        # Per request: the price is known, the amount is not.
        cost = calculate_cost(None, "claude-haiku-5-5")
        self.assertIsNone(cost["amount_usd"])
        self.assertEqual(cost["reason"], "no_usage_reported")
        self.assertIsNotNone(cost["source"])

    def test_aggregate_unknown_cached_token_contribution_preserves_known_subtotal(self):
        from unittest.mock import patch
        with patch("gateway.costs.published_prices", return_value={"model": {
            "input_per_m": 1.0, "output_per_m": 2.0, "cached_read_per_m": 0.5,
            "source": "published", "observed_at": "2026-01-01",
        }}):
            events = [
                {"timestamp": "2099-01-01T00:00:00Z", "requested_alias": "alias", "serving_provider": "opencode_go",
                 "serving_model": "model", "token_counters": {"prompt_tokens": 100, "completion_tokens": 10, "cached_read_tokens": 0}},
                {"timestamp": "2099-01-01T00:00:01Z", "requested_alias": "alias", "serving_provider": "opencode_go",
                 "serving_model": "model", "token_counters": {"prompt_tokens": 50, "completion_tokens": 5,
                 "cached_tokens_reported": True}},
            ]
            row = aggregate_usage(events, window_hours=24)["model"]["model"]
        self.assertAlmostEqual(row["amount_usd"], 0.00012)
        self.assertEqual(row["cost_reason"], "missing_cached_tokens")
        self.assertEqual(row["unknown_cost_requests"], 1)
        self.assertEqual(row["unknown_cost_input_tokens"], 50)
        self.assertEqual(row["unknown_cost_output_tokens"], 5)
        self.assertTrue(row["cost_incomplete"])

    def test_catalogue_costs_dont_count_as_enforceable_limits(self):
        # Price metadata is not an operational spend cap.
        result = aggregate_usage([], window_hours=24)
        self.assertNotIn("limits", result)
        self.assertNotIn("billing_system", result)

    def test_mixed_group_cost_coverage_is_visible_in_state_table_contract(self):
        # The aggregate carries both the known subtotal and the unknown tokens;
        # presentation has enough metadata to keep them distinct.
        events = [
            {"timestamp": "2099-01-01T00:00:00Z", "requested_alias": "coding", "serving_provider": "opencode_go",
             "serving_model": "claude-haiku-5-5", "token_counters": {"prompt_tokens": 1000, "completion_tokens": 250, "cached_read_tokens": 0}},
            {"timestamp": "2099-01-01T00:00:01Z", "requested_alias": "coding", "serving_provider": "chatgpt_codex",
             "serving_model": "gpt-6-luna", "token_counters": {"prompt_tokens": 500, "completion_tokens": 100, "cached_read_tokens": 0}},
        ]
        alias = aggregate_usage(events, window_hours=24)["alias"]["coding"]
        self.assertEqual(alias["unknown_cost_requests"], 1)
        self.assertEqual(alias["unknown_cost_input_tokens"], 500)
        self.assertTrue(alias["cost_incomplete"])
        self.assertGreater(alias["amount_usd"], 0)


if __name__ == "__main__":
    unittest.main()


class AliasBudgetTests(unittest.TestCase):
    def test_unknown_spend_is_not_free_or_capped(self):
        from datetime import datetime

        from gateway.budget import budget_status, over_budget
        now = datetime(2026, 10, 21, tzinfo=UTC)
        events = [{"requested_alias": "a", "timestamp": "2026-10-02T00:00:00Z", "status": 200,
                   "serving_provider": "opencode_go", "serving_model": "unknown-model",
                   "token_counters": {"prompt_tokens": 10, "completion_tokens": 2}}]
        status = budget_status(events, "a", 1.0, now)
        self.assertIsNone(status["spend_usd"])
        self.assertFalse(status["capped"])
        self.assertIsNone(over_budget(events, "a", 1.0, now))

    def test_known_excess_refuses_at_utc_month_boundary(self):
        from datetime import datetime

        from gateway.budget import budget_status, over_budget
        now = datetime(2026, 10, 21, tzinfo=UTC)
        # An explicit reported zero cached counter keeps the spend known under
        # the catalogue's cached-token completeness rule; an absent one is
        # Unknown and must never refuse (covered above).
        events = [{"requested_alias": "a", "timestamp": "2026-10-02T00:00:00Z", "status": 200,
                   "serving_provider": "opencode_go", "serving_model": "gpt-6-luna",
                   "token_counters": {"prompt_tokens": 1_000_000, "completion_tokens": 0, "cached_read_tokens": 0}}]
        self.assertEqual(budget_status(events, "a", .01, now)["reset_at"], "2026-11-01T00:00:00Z")
        refusal = over_budget(events, "a", .01, now)
        self.assertIsNotNone(refusal)
        self.assertAlmostEqual(refusal["spend_usd"], 0.1)


if __name__ == "__main__":
    unittest.main()
