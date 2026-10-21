import unittest

from gateway.provider_health import provider_health


class ProviderHealthTests(unittest.TestCase):
    def test_composes_observed_state_cooldowns_models_and_success_failure_rates(self):
        providers = [
            {"provider_id": "z", "state": "failed", "last_test_at": "2026-10-21T12:00:00Z",
             "last_error": "offline", "catalog_observed_at": None, "models": [{"model_id": "a"}]},
            {"provider_id": "a", "state": "connected", "last_test_at": "2026-10-21T12:00:00Z",
             "last_error": None, "catalog_observed_at": "2026-10-21T12:01:00Z", "models": [{}, {}]},
        ]
        events = [
            {"serving_provider": "a", "error_class": None},
            {"serving_provider": "a", "error_class": "upstream_error"},
            {"serving_provider": "a", "error_class": None},
            {"serving_provider": "z", "error_class": "failed"},
            {"serving_provider": None, "error_class": "no_route"},
        ]
        cooldowns = {
            "a/model": {"provider": "a", "remaining_seconds": 42},
            "z/model": {"provider": "z", "remaining_seconds": 5},
            "a/expired": {"provider": "a", "remaining_seconds": 0},
        }
        health = provider_health(providers, cooldowns, events)
        self.assertEqual([row["provider_id"] for row in health], ["a", "z"])
        self.assertEqual(health[0], {
            "provider_id": "a", "connection_state": "connected",
            "last_test_at": "2026-10-21T12:00:00Z", "last_test_outcome": "success",
            "last_error": None, "catalog_observed_at": "2026-10-21T12:01:00Z",
            "model_count": 2, "cooldown_remaining_seconds": 42,
            "recent_requests": 3, "recent_success_rate": 2 / 3, "recent_failure_rate": 1 / 3,
        })
        self.assertEqual(health[1]["last_test_outcome"], "failure")
        self.assertEqual(health[1]["recent_failure_rate"], 1.0)

    def test_missing_observations_stay_unknown_and_empty_ledger_has_no_invented_rate(self):
        row = provider_health([{"provider_id": "p", "state": "configured_untested", "models": []}], {}, [])[0]
        self.assertEqual(row["last_test_outcome"], "unknown")
        self.assertIsNone(row["last_test_at"])
        self.assertIsNone(row["catalog_observed_at"])
        self.assertIsNone(row["recent_success_rate"])
        self.assertIsNone(row["recent_failure_rate"])
        self.assertEqual(row["model_count"], 0)
        self.assertEqual(row["cooldown_remaining_seconds"], 0)


if __name__ == "__main__":
    unittest.main()
