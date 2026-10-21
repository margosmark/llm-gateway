from __future__ import annotations

import unittest

from gateway.adapters.base import AdapterError
from gateway.config import Alias, RouteRef, WeightedRoute
from tests.test_end_to_end import FakeAdapter, GatewayHarness
from gateway.core.weighted_pool import select_weighted, weighted_index


class WeightedPoolTests(unittest.TestCase):
    def test_selection_is_sticky_and_respects_weight_slot_ranges(self) -> None:
        routes = [RouteRef("p", "a"), RouteRef("p", "b")]
        weights = [1, 3]
        for conversation in ("conversation-1", "conversation-2", "conversation-3"):
            selected, fallbacks = select_weighted(routes, weights, conversation, "alias")
            self.assertIn(selected, routes)
            self.assertEqual(fallbacks, [route for route in routes if route != selected])
            self.assertEqual(selected, select_weighted(routes, weights, conversation, "alias")[0])
        self.assertEqual(weighted_index([1], "x", "y"), 0)

    def test_refuses_invalid_weight_vectors(self) -> None:
        for weights in ([], [0, 1], [-1, 2], [True, 1]):
            with self.subTest(weights=weights), self.assertRaises(ValueError):
                weighted_index(weights, "conversation", "alias")
        with self.assertRaises(ValueError):
            select_weighted(["a"], [1, 2], "conversation", "alias")

    def test_router_sticky_pool_selection_and_opt_in_fallback(self) -> None:
        harness = GatewayHarness()
        self.addCleanup(harness.close)
        from gateway.config import ModelEntry, ProviderState

        second = FakeAdapter(provider_id="opencode_go", dialect="openai_responses")
        harness.registry.register(second)
        with harness.config.mutate() as config:
            config.providers["chatgpt_codex"].models["gpt-6-luna"].enabled = True
            config.providers["chatgpt_codex"].models["gpt-6-luna"].dialect = "openai_responses"
            config.providers["opencode_go"] = ProviderState(
                provider_id="opencode_go", kind="opencode_go", state="connected",
                models={"m2": ModelEntry("m2", enabled=True, dialect="openai_responses")},
            )
            config.aliases["coding"] = Alias(
                name="coding", enabled=True,
                weighted_routes=[WeightedRoute(RouteRef("chatgpt_codex", "gpt-6-luna"), 1),
                                 WeightedRoute(RouteRef("opencode_go", "m2"), 1)],
            )
        first = harness.adapter
        for _ in range(2):
            status, _, _ = harness.request("POST", "/v1/chat/completions", body={
                "model": "coding", "messages": [{"role": "user", "content": "hi"}],
            })
            self.assertEqual(status, 200)
        self.assertEqual(len(first.calls) + len(second.calls), 2)
        self.assertTrue(len(first.calls) == 2 or len(second.calls) == 2)

        selected_adapter = first if first.calls else second
        backup = second if selected_adapter is first else first
        selected_adapter.fail_with = AdapterError("transient", provider_id=selected_adapter.provider_id)
        status, payload, _ = harness.request("POST", "/v1/chat/completions", body={
            "model": "coding", "messages": [{"role": "user", "content": "hi"}],
        })
        self.assertEqual(status, 502)
        self.assertIn("fallback not used", payload["error"]["message"])
        self.assertEqual(len(backup.calls), 0)

        with harness.config.mutate() as config:
            alias = config.aliases["coding"]
            alias.fallback_authorized = True
            alias.fallback_confirmed_providers = ["chatgpt_codex", "opencode_go"]
            alias.fallback_triggers = ["upstream_unavailable"]
        status, _, _ = harness.request("POST", "/v1/chat/completions", body={
            "model": "coding", "messages": [{"role": "user", "content": "hi"}],
        })
        self.assertEqual(status, 200)
        self.assertEqual(len(backup.calls), 1)

    def test_weighted_route_config_round_trip_and_legacy_shape(self) -> None:
        alias = Alias(name="a", weighted_routes=[
            WeightedRoute(RouteRef("p", "m1"), 3),
            WeightedRoute(RouteRef("p", "m2"), 1),
        ])
        restored = Alias.from_dict(alias.to_dict())
        self.assertEqual(restored.weighted_routes, alias.weighted_routes)
        self.assertEqual([r.model for r in restored.all_routes()], ["m1", "m2"])
        legacy = Alias.from_dict({"name": "legacy", "primary": {"provider": "p", "model": "m"}})
        self.assertEqual(legacy.weighted_routes, [])
        invalid = Alias.from_dict({"name": "bad", "weighted_routes": [
            {"route": {"provider": "p", "model": "m"}, "weight": 0}
        ]})
        self.assertEqual(invalid.weighted_routes, [])


if __name__ == "__main__":
    unittest.main()
