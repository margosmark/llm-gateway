import unittest
from datetime import UTC, datetime, timedelta

from gateway.statistics import aggregate_statistics


class RequestStatisticsTests(unittest.TestCase):
    def test_statistics_group_outcomes_usage_cost_and_latency_per_dimension(self):
        now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        events = [
            {"timestamp": now, "requested_alias": "chat", "serving_provider": "opencode_go", "serving_model": "claude-haiku-5-5",
             "status": 200, "latency_ms": 100, "fallback_used": True,
             "token_counters": {"prompt_tokens": 1000, "completion_tokens": 250}},
            {"timestamp": now, "requested_alias": "chat", "serving_provider": "opencode_go", "serving_model": "claude-haiku-5-5",
             "status": 502, "error_class": "upstream_error", "latency_ms": 300,
             "token_counters": {"prompt_tokens": 1000, "completion_tokens": 250}},
            {"timestamp": now, "requested_alias": "chat", "error_class": "exhausted", "status": 503},
            {"timestamp": now, "requested_alias": "other", "serving_provider": "unknown_provider", "serving_model": "unpriced", "latency_ms": 50},
            {"timestamp": (datetime.now(UTC) - timedelta(days=40)).isoformat(), "requested_alias": "chat", "error_class": "stale"},
        ]
        result = aggregate_statistics(events, "month")
        alias = result["alias"]["chat"]
        self.assertEqual(alias["requests"], 3)
        self.assertEqual(alias["errors"], 2)
        self.assertEqual(alias["error_classes"], {"upstream_error": 1, "exhausted": 1})
        self.assertEqual(alias["fallbacks"], 1)
        self.assertAlmostEqual(alias["fallback_rate"], 1 / 3)
        self.assertEqual(alias["latency_ms"], {"p50": 100, "p95": 300, "p99": 300})
        self.assertIsNone(alias["input_tokens"])
        self.assertIsNone(alias["output_tokens"])
        self.assertEqual(alias["token_counted_requests"], 2)
        self.assertIsNone(alias["estimated_cost_usd"])
        provider = result["provider"]["opencode_go"]
        self.assertEqual(provider["requests"], 2)
        self.assertEqual(result["model"]["claude-haiku-5-5"]["requests"], 2)
        self.assertIsNone(result["provider"]["unknown_provider"]["estimated_cost_usd"])

    def test_window_rejects_unknown_and_ignores_malformed_or_futureless_metadata(self):
        with self.assertRaises(ValueError):
            aggregate_statistics([], "year")
        result = aggregate_statistics([
            {"timestamp": "not-a-date", "requested_alias": "bad"},
            {"timestamp": (datetime.now(UTC) - timedelta(hours=2)).isoformat(), "requested_alias": "old"},
            {"timestamp": datetime.now(UTC).isoformat(), "requested_alias": "now", "latency_ms": -1},
        ], "hour")
        self.assertEqual(set(result["alias"]), {"now"})
        self.assertIsNone(result["alias"]["now"]["latency_ms"]["p50"])
        self.assertIsNone(result["alias"]["now"]["input_tokens"])
        self.assertIsNone(result["alias"]["now"]["output_tokens"])
        self.assertIsNone(result["alias"]["now"]["estimated_cost_usd"])
        self.assertFalse(any(token in str(result).lower() for token in ("prompt", "completion", "body", "credential")))


if __name__ == "__main__":
    unittest.main()
