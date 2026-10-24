from __future__ import annotations

import unittest

from gateway.adapters.base import AdapterError
from gateway.config import Alias, RouteRef, WeightedRoute
from gateway.core.weighted_pool import select_weighted, weighted_index
from tests.test_end_to_end import FakeAdapter, GatewayHarness


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

    def _pool_harness(self):
        """A two-member pool alias with every fallback opt-in enabled."""
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
                # Every opt-in the fallback policy demands is granted, so a
                # refusal below can only come from the stream hard stop.
                fallback_authorized=True,
                fallback_confirmed_providers=["chatgpt_codex", "opencode_go"],
                fallback_triggers=["exhausted", "rate_limited", "upstream_unavailable"],
            )
        return harness, harness.adapter, second

    def test_stream_failure_inside_pool_never_switches_route(self) -> None:
        """QA t_668683b3: a stream failure inside a pool member must not fall back.

        The alias grants every fallback opt-in, so the only rule that can
        refuse the switch is the pool's stream hard stop. The failure must
        surface on the same alias with the other member never invoked.
        """
        harness, first, second = self._pool_harness()

        # Find which member this conversation sticks to.
        status, _, _ = harness.request("POST", "/v1/chat/completions", body={
            "model": "coding", "messages": [{"role": "user", "content": "hi"}],
        })
        self.assertEqual(status, 200)
        selected = first if first.calls else second
        backup = second if selected is first else first

        selected.fail_with = AdapterError("transient", provider_id=selected.provider_id)
        status, payload, _ = harness.request("POST", "/v1/chat/completions", body={
            "model": "coding", "messages": [{"role": "user", "content": "hi"}], "stream": True,
        })
        self.assertEqual(status, 502)
        self.assertIn("weighted pool", payload["error"]["message"])
        # The refusal names the hard stop, not a missing opt-in.
        self.assertIn("stream", payload["error"]["message"])
        # No cross-route attempt: the other pool member was never invoked,
        # and the error detail lists only the one member that failed.
        self.assertEqual(len(backup.calls), 0)
        attempts = payload["error"].get("detail", {}).get("attempts", [])
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["provider"], selected.provider_id)

        # The same failure on a buffered request still falls back - the
        # hard stop is the stream rule, not the pool itself.
        status, _, _ = harness.request("POST", "/v1/chat/completions", body={
            "model": "coding", "messages": [{"role": "user", "content": "hi"}],
        })
        self.assertEqual(status, 200)
        self.assertEqual(len(backup.calls), 1)

    def test_stream_failure_inside_pool_surfaces_even_with_no_opt_ins(self) -> None:
        """Without opt-ins the stream failure also surfaces on the same alias."""
        harness, first, second = self._pool_harness()
        with harness.config.mutate() as config:
            alias = config.aliases["coding"]
            alias.fallback_authorized = False
            alias.fallback_confirmed_providers = []
            alias.fallback_triggers = []

        status, _, _ = harness.request("POST", "/v1/chat/completions", body={
            "model": "coding", "messages": [{"role": "user", "content": "hi"}],
        })
        self.assertEqual(status, 200)
        selected = first if first.calls else second
        backup = second if selected is first else first

        selected.fail_with = AdapterError("transient", provider_id=selected.provider_id)
        status, payload, _ = harness.request("POST", "/v1/chat/completions", body={
            "model": "coding", "messages": [{"role": "user", "content": "hi"}], "stream": True,
        })
        self.assertEqual(status, 502)
        self.assertIn("weighted pool", payload["error"]["message"])
        self.assertEqual(len(backup.calls), 0)

    def test_stream_inside_pool_serves_from_the_sticky_member(self) -> None:
        """A healthy stream request is served by the sticky member, not bounced."""
        harness, first, second = self._pool_harness()
        status, _, _ = harness.request("POST", "/v1/chat/completions", body={
            "model": "coding", "messages": [{"role": "user", "content": "hi"}],
        })
        self.assertEqual(status, 200)
        selected = first if first.calls else second
        calls_before = len(selected.calls)

        status, _, _ = harness.request("POST", "/v1/chat/completions", body={
            "model": "coding", "messages": [{"role": "user", "content": "hi"}], "stream": True,
        }, raw=True)
        self.assertEqual(status, 200)
        self.assertEqual(len(selected.calls), calls_before + 1)

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
