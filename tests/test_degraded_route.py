"""A reasoning effort is a preference, not a capability.

The capability check used to **exclude** a route whose model did not
publish the requested reasoning effort. Most OpenCode Go models publish no
reasoning levels at all, so every one of them was excluded from any request
that asked for one - and the fallback silently became unusable.

A model that cannot produce the requested depth of reasoning can still
answer the question. So the route stays eligible, the effort is dropped
**for that route**, and the substitution is recorded. It is never silent.
"""

from __future__ import annotations

import unittest

from gateway.adapters.base import AdapterError
from gateway.config import ModelEntry, ProviderState
from tests.test_end_to_end import FAKE_PROVIDER, FakeAdapter, GatewayHarness


class DegradedRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = GatewayHarness()
        self.addCleanup(self.harness.close)
        with self.harness.config.mutate() as config:
            # A fallback model that publishes no reasoning levels, like most
            # OpenCode Go models.
            config.providers["opencode_go"] = ProviderState(
                provider_id="opencode_go",
                kind="opencode_go",
                state="connected",
                models={
                    "mimo-v2.6-flash": ModelEntry(
                        model_id="mimo-v2.6-flash",
                        enabled=True,
                        dialect="openai_chat",
                        source="discovered",
                        reasoning_efforts=[],
                    )
                },
            )
            config.aliases["coding"] = _alias()

    def _event(self):
        return self.harness.router._ledger.recent()[0]

    def test_a_route_without_the_effort_is_not_excluded(self) -> None:
        failing = FakeAdapter(fail_with=AdapterError("exhausted", provider_id=FAKE_PROVIDER))
        self.harness.registry.register(failing)
        self.failing = failing
        status, _, _ = self.harness.request(
            "POST",
            "/v1/chat/completions",
            body={
                "model": "coding",
                "messages": [{"role": "user", "content": "hi"}],
                "reasoning_effort": "high",
            },
        )
        self.assertEqual(status, 200, "the fallback must still serve the request")

    def test_the_substitution_is_recorded(self) -> None:
        failing = FakeAdapter(fail_with=AdapterError("exhausted", provider_id=FAKE_PROVIDER))
        self.harness.registry.register(failing)
        self.failing = failing
        self.harness.request(
            "POST",
            "/v1/chat/completions",
            body={
                "model": "coding",
                "messages": [{"role": "user", "content": "hi"}],
                "reasoning_effort": "high",
            },
        )
        event = self._event()
        self.assertTrue(event["degradations"], "the substitution must be recorded")
        fallback = [
            entry for entry in event["degradations"] if entry["provider"] == "opencode_go"
        ]
        self.assertTrue(fallback, "the fallback route's substitution must be recorded")
        self.assertIn("reasoning effort", fallback[0]["reason"])

    def test_the_effort_is_dropped_from_the_upstream_payload(self) -> None:
        failing = FakeAdapter(fail_with=AdapterError("exhausted", provider_id=FAKE_PROVIDER))
        self.harness.registry.register(failing)
        self.failing = failing
        self.harness.request(
            "POST",
            "/v1/chat/completions",
            body={
                "model": "coding",
                "messages": [{"role": "user", "content": "hi"}],
                "reasoning_effort": "high",
            },
        )
        fallback_adapter = self.harness.registry.get("opencode_go")
        fallback_call = fallback_adapter.calls[-1]
        self.assertNotIn(
            "reasoning_effort",
            fallback_call.payload,
            "the unsupported effort must be dropped for the route that cannot honour it",
        )

    def test_a_route_that_supports_the_effort_keeps_it(self) -> None:
        failing = FakeAdapter(fail_with=AdapterError("exhausted", provider_id=FAKE_PROVIDER))
        self.harness.registry.register(failing)
        self.failing = failing
        with self.harness.config.mutate() as config:
            entry = config.providers["opencode_go"].models["mimo-v2.6-flash"]
            entry.reasoning_efforts = ["low", "medium", "high"]
        self.harness.request(
            "POST",
            "/v1/chat/completions",
            body={
                "model": "coding",
                "messages": [{"role": "user", "content": "hi"}],
                "reasoning_effort": "high",
            },
        )
        # The fallback adapter is the one for opencode_go, not the failing
        # primary.
        fallback_adapter = self.harness.registry.get("opencode_go")
        self.assertEqual(fallback_adapter.calls[-1].payload.get("reasoning_effort"), "high")
        self.assertEqual(self._event()["degradations"], [])

    def test_a_route_without_tools_is_still_excluded(self) -> None:
        """Tools are a hard requirement, not a preference."""
        failing = FakeAdapter(fail_with=AdapterError("exhausted", provider_id=FAKE_PROVIDER))
        self.harness.registry.register(failing)
        self.failing = failing
        with self.harness.config.mutate() as config:
            config.providers["opencode_go"].models["mimo-v2.6-flash"].supports_function_tools = False
        status, _, _ = self.harness.request(
            "POST",
            "/v1/chat/completions",
            body={
                "model": "coding",
                "messages": [{"role": "user", "content": "hi"}],
                "tools": [{"type": "function", "function": {"name": "f", "parameters": {"type": "object"}}}],
            },
        )
        self.assertEqual(status, 502)
        skipped = self._event()["skipped"]
        self.assertTrue(any("function tools" in str(entry["reason"]) for entry in skipped))


def _alias():
    from gateway.config import Alias, RouteRef

    return Alias(
        name="coding",
        enabled=True,
        primary=RouteRef(FAKE_PROVIDER, "gpt-6-luna"),
        fallbacks=[RouteRef("opencode_go", "mimo-v2.6-flash")],
        fallback_triggers=["exhausted", "rate_limited", "upstream_unavailable", "explicit_cooldown"],
        fallback_authorized=True,
        fallback_confirmed_providers=["opencode_go"],
    )


if __name__ == "__main__":
    unittest.main()
