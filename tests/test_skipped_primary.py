"""A request served by a route other than the primary must say why.

A primary that is skipped - because it is in a local cooldown, disabled, or
unknown - produces a request that looks like an unexplained fallback. The
record has to name the skipped route and its reason, or the Status view
cannot be trusted.
"""

from __future__ import annotations

import unittest

from gateway.adapters.base import AdapterError
from tests.test_end_to_end import FAKE_PROVIDER, FakeAdapter, GatewayHarness


class SkippedPrimaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = GatewayHarness()
        self.addCleanup(self.harness.close)
        with self.harness.config.mutate() as config:
            from gateway.config import ModelEntry, ProviderState

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
                    )
                },
            )
            config.aliases["coding"] = _alias()

    def _events(self):
        return self.harness.router._ledger.recent()

    def test_a_cooldown_on_the_primary_is_recorded_as_a_fallback(self) -> None:
        # Put the primary provider into a local cooldown.
        self.harness.router.circuits.set(FAKE_PROVIDER, 300)

        status, payload, _ = self.harness.request(
            "POST",
            "/v1/chat/completions",
            body={"model": "coding", "messages": [{"role": "user", "content": "hi"}]},
        )
        self.assertEqual(status, 200)

        event = self._events()[0]
        self.assertTrue(
            event["fallback_used"],
            "a request served by another route must be recorded as a fallback",
        )
        self.assertIn("primary route was skipped", event["fallback_reason"])
        self.assertIn("cooldown", event["fallback_reason"])
        self.assertTrue(event["skipped"], "the skipped route must be recorded")
        self.assertEqual(event["skipped"][0]["provider"], FAKE_PROVIDER)
        self.assertIn("cooldown", event["skipped"][0]["reason"])

    def test_the_fallback_counter_reflects_a_skipped_primary(self) -> None:
        self.harness.router.circuits.set(FAKE_PROVIDER, 300)
        self.harness.request(
            "POST",
            "/v1/chat/completions",
            body={"model": "coding", "messages": [{"role": "user", "content": "hi"}]},
        )
        self.assertEqual(self.harness.router._ledger.totals()["fallbacks"], 1)

    def test_a_normal_request_is_not_recorded_as_a_fallback(self) -> None:
        self.harness.request(
            "POST",
            "/v1/chat/completions",
            body={"model": "coding", "messages": [{"role": "user", "content": "hi"}]},
        )
        event = self._events()[0]
        self.assertFalse(event["fallback_used"])
        self.assertEqual(event["skipped"], [])
        self.assertEqual(self.harness.router._ledger.totals()["fallbacks"], 0)

    def test_a_failed_primary_is_recorded_with_its_reason(self) -> None:
        failing = FakeAdapter(fail_with=AdapterError("exhausted", provider_id=FAKE_PROVIDER))
        self.harness.registry.register(failing)
        status, _, _ = self.harness.request(
            "POST",
            "/v1/chat/completions",
            body={"model": "coding", "messages": [{"role": "user", "content": "hi"}]},
        )
        # The fallback is authorised, so the request is served.
        self.assertEqual(status, 200)
        event = self._events()[0]
        self.assertTrue(event["fallback_used"])
        self.assertTrue(event["attempts"])
        self.assertEqual(event["attempts"][0]["error_class"], "exhausted")
        self.assertEqual(event["serving_provider"], "opencode_go")


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
