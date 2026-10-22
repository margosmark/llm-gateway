"""A route the upstream no longer serves must not stop the chain.

Regression cover: OpenCode Go returned **410 Gone** for a deprecated model,
and the classifier called it `unknown`. `unknown` never falls back - by
design - so the chain stopped with a second, perfectly usable fallback
sitting unused, and the owner got a 502.

The properties under test:

* a 410, and the provider's own deprecation code, classify as **route
  gone**;
* route gone satisfies the "upstream unavailable" trigger, so a chain
  continues when the owner has enabled that trigger;
* a route that is gone is **not** retried on the same route;
* it does **not** put the provider into a cooldown, because a deprecated
  model is a fact about the model, not about the provider.
"""

from __future__ import annotations

import unittest

from gateway.adapters.base import AdapterError, classify_status
from gateway.config import ERROR_ROUTE_GONE, ModelEntry, ProviderState
from tests.test_end_to_end import FAKE_PROVIDER, FakeAdapter, GatewayHarness


class SelectiveAdapter(FakeAdapter):
    """Answers differently per model, so a chain can be exercised."""

    def __init__(self, provider_id: str, dialect: str, fail_on: dict) -> None:
        super().__init__(provider_id=provider_id, dialect=dialect)
        self.fail_on = fail_on

    def invoke(self, request: ProviderRequest):
        failure = self.fail_on.get(request.model)
        if failure is not None:
            raise failure
        return super().invoke(request)


class ClassificationTests(unittest.TestCase):
    def test_a_gone_status_classifies_as_route_gone(self) -> None:
        self.assertEqual(classify_status(410, {}), ERROR_ROUTE_GONE)

    def test_the_providers_deprecation_code_classifies_as_route_gone(self) -> None:
        payload = {"type": "error", "error": {"type": "ModelDeprecated", "message": "Model x has been deprecated."}}
        self.assertEqual(classify_status(410, payload), ERROR_ROUTE_GONE)

    def test_the_route_gone_class_is_not_retryable(self) -> None:
        error = AdapterError(ERROR_ROUTE_GONE, provider_id="opencode_go")
        self.assertFalse(error.retryable, "a deprecated model will not come back")


class ChainContinuationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = GatewayHarness()
        self.addCleanup(self.harness.close)
        with self.harness.config.mutate() as config:
            config.providers["opencode_go"] = ProviderState(
                provider_id="opencode_go",
                kind="opencode_go",
                state="connected",
                models={
                    "minimax-m3.1-flash-free": ModelEntry(
                        model_id="minimax-m3.1-flash-free",
                        enabled=True,
                        dialect="openai_chat",
                        source="discovered",
                    ),
                    "mimo-v2.6-flash": ModelEntry(
                        model_id="mimo-v2.6-flash",
                        enabled=True,
                        dialect="openai_chat",
                        source="discovered",
                    ),
                },
            )
            config.aliases["pm"] = _alias()

    def _request(self):
        return self.harness.request(
            "POST",
            "/v1/chat/completions",
            body={"model": "pm", "messages": [{"role": "user", "content": "hi"}]},
        )

    def test_a_gone_first_fallback_continues_to_the_second(self) -> None:
        # The first fallback is deprecated at the provider.
        self.harness.registry.register(
            SelectiveAdapter(
                provider_id="opencode_go",
                dialect="openai_chat",
                fail_on={
                    "minimax-m3.1-flash-free": AdapterError(
                        ERROR_ROUTE_GONE, provider_id="opencode_go"
                    )
                },
            )
        )
        primary = FakeAdapter(fail_with=AdapterError("exhausted", provider_id=FAKE_PROVIDER))
        self.harness.registry.register(primary)

        status, payload, _ = self._request()
        self.assertEqual(status, 200, "the second fallback must still serve the request")

    def test_the_second_fallback_is_recorded(self) -> None:
        gone = SelectiveAdapter(
            provider_id="opencode_go",
            dialect="openai_chat",
            fail_on={
                "minimax-m3.1-flash-free": AdapterError(
                    ERROR_ROUTE_GONE, provider_id="opencode_go"
                )
            },
        )
        self.harness.registry.register(gone)
        self.harness.registry.register(
            FakeAdapter(fail_with=AdapterError("exhausted", provider_id=FAKE_PROVIDER))
        )
        status, payload, _ = self._request()
        self.assertEqual(status, 200)

        event = self.harness.router._ledger.recent()[0]
        self.assertEqual(event["serving_provider"], "opencode_go")
        self.assertTrue(event["attempts"], "the attempts must be recorded")
        self.assertTrue(
            any(a.get("error_class") == ERROR_ROUTE_GONE for a in event["attempts"]),
            "the gone route must be recorded",
        )

    def test_a_gone_route_does_not_put_the_provider_in_cooldown(self) -> None:
        """A deprecated model is a fact about the model, not the provider."""
        gone = SelectiveAdapter(
            provider_id="opencode_go",
            dialect="openai_chat",
            fail_on={
                "minimax-m3.1-flash-free": AdapterError(
                    ERROR_ROUTE_GONE, provider_id="opencode_go"
                )
            },
        )
        self.harness.registry.register(gone)
        self.harness.registry.register(
            FakeAdapter(fail_with=AdapterError("exhausted", provider_id=FAKE_PROVIDER))
        )
        self._request()
        # The circuit store is scoped per route, so the provider's other
        # models are unaffected.
        allowed, reason = self.harness.router.circuits.check("opencode_go", "mimo-v2.6-flash")
        self.assertTrue(
            allowed,
            "the other models at that provider must stay usable: " + reason,
        )


def _alias():
    from gateway.config import Alias, RouteRef

    return Alias(
        name="pm",
        enabled=True,
        primary=RouteRef(FAKE_PROVIDER, "gpt-6-luna"),
        fallbacks=[
            RouteRef("opencode_go", "minimax-m3.1-flash-free"),
            RouteRef("opencode_go", "mimo-v2.6-flash"),
        ],
        fallback_triggers=["exhausted", "rate_limited", "upstream_unavailable", "explicit_cooldown"],
        fallback_authorized=True,
        fallback_confirmed_providers=["opencode_go"],
    )


if __name__ == "__main__":
    unittest.main()
