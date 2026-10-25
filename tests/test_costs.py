import unittest

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
        cost = calculate_cost(counters, "claude-haiku-5-5")
        self.assertAlmostEqual(cost["amount_usd"], 0.000225)
        self.assertEqual(cost["source"], "https://opencode.ai/docs/go/")
        # The dated catalogue snapshot is refreshed, so the as-of date is read
        # from it rather than pinned to one snapshot's date.
        self.assertEqual(cost["observed_at"], published_prices()["claude-haiku-5-5"]["observed_at"])

    def test_stream_usage_is_normalised_on_both_wire_dialects(self):
        chat = [b'data: {"choices":[],"usage":{"prompt_tokens":12,"completion_tokens":3,"total_tokens":15}}\n\n']
        responses = [b'event: response.completed\ndata: {"type":"response.completed","response":{"usage":{"input_tokens":12,"output_tokens":3,"total_tokens":15}}}\n\n']
        expected = {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15}
        self.assertEqual(stream_token_counters(chat, DIALECT_OPENAI_CHAT), expected)
        self.assertEqual(stream_token_counters(responses, DIALECT_OPENAI_RESPONSES), expected)

    def test_unpriced_catalogue_model_is_unknown_not_zero(self):
        cost = calculate_cost({"prompt_tokens": 10, "completion_tokens": 2}, "beam-free")
        self.assertIsNone(cost["amount_usd"])
        self.assertIsNone(cost["source"])

    def test_aggregates_are_metadata_only_by_alias_provider_model(self):
        events = [{
            "timestamp": "2099-01-01T00:00:00Z", "requested_alias": "friendly", "serving_provider": "opencode_go",
            "serving_model": "claude-haiku-5-5", "token_counters": {"prompt_tokens": 100, "completion_tokens": 20},
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
        cost = calculate_cost({"prompt_tokens": 100, "completion_tokens": 20}, "claude-haiku-5-5", provider="chatgpt_codex")
        self.assertIsNone(cost["amount_usd"])
        self.assertIsNone(cost["source"])

    def test_aggregate_loads_catalogue_once_for_many_events(self):
        from unittest.mock import patch
        events = [{
            "timestamp": "2099-01-01T00:00:00Z", "requested_alias": "a", "serving_provider": "opencode_go",
            "serving_model": "claude-haiku-5-5", "token_counters": {"prompt_tokens": 100, "completion_tokens": 20},
        } for _ in range(10_000)]
        with patch("gateway.costs.published_prices", wraps=published_prices) as prices:
            result = aggregate_usage(events)
        self.assertEqual(prices.call_count, 1)
        self.assertEqual(result["model"]["claude-haiku-5-5"]["requests"], 10_000)
        self.assertAlmostEqual(result["model"]["claude-haiku-5-5"]["amount_usd"], 0.2)

    def test_evicted_events_mark_window_aggregate_incomplete(self):
        events = [{
            "timestamp": "2099-01-01T00:00:00Z", "requested_alias": "a", "serving_provider": "opencode_go",
            "serving_model": "claude-haiku-5-5", "token_counters": {"prompt_tokens": 100, "completion_tokens": 20},
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
        self.assertEqual(row["source"], "https://opencode.ai/docs/go/")
        self.assertTrue(result["coverage"]["incomplete"])
        self.assertEqual(result["coverage"]["source"], "bounded_recent_events")

    def test_price_exists_even_when_some_requests_reported_no_usage(self):
        # The live gateway recorded fallback rows with no usage block at all;
        # those must not blank out the whole group's price.
        events = [
            {"timestamp": "2099-01-01T00:00:00Z", "requested_alias": "a", "serving_provider": "opencode_go",
             "serving_model": "claude-haiku-5-5", "token_counters": {"prompt_tokens": 100, "completion_tokens": 20}},
            {"timestamp": "2099-01-01T00:00:01Z", "requested_alias": "a", "serving_provider": "opencode_go",
             "serving_model": "claude-haiku-5-5", "token_counters": None, "status": 502},
        ]
        result = aggregate_usage(events, window_hours=24)
        row = result["model"]["claude-haiku-5-5"]
        self.assertEqual(row["requests"], 2)
        self.assertAlmostEqual(row["amount_usd"], 0.00002)
        self.assertEqual(row["cost_reason"], None)
        self.assertEqual(result["coverage"]["requests_without_usage"], 1)

    def test_amount_is_null_exactly_when_no_published_price_exists(self):
        events = [
            {"timestamp": "2099-01-01T00:00:00Z", "requested_alias": "a", "serving_provider": "opencode_go",
             "serving_model": "claude-haiku-5-5", "token_counters": {"prompt_tokens": 100, "completion_tokens": 20}},
            {"timestamp": "2099-01-01T00:00:00Z", "requested_alias": "b", "serving_provider": "opencode_go",
             "serving_model": "beam-free", "token_counters": {"prompt_tokens": 100, "completion_tokens": 20}},
        ]
        result = aggregate_usage(events, window_hours=24)
        priced = result["model"]["claude-haiku-5-5"]
        unpriced = result["model"]["beam-free"]
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

    def test_unknown_price_dominates_aggregate_instead_of_underreporting(self):
        events = [{
            "timestamp": "2099-01-01T00:00:00Z", "requested_alias": "alias", "serving_provider": "p",
            "serving_model": "beam-free", "token_counters": {"prompt_tokens": 1, "completion_tokens": 1},
        }]
        self.assertIsNone(aggregate_usage(events)["model"]["beam-free"]["amount_usd"])


if __name__ == "__main__":
    unittest.main()
