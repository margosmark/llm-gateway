"""A sampling parameter is not a malformed request.

The ChatGPT-authenticated Codex backend answers a long list of ordinary
Chat Completions request fields - ``temperature``, ``top_p``, ``n``, ``stop``,
``seed`` and fourteen more - with a 400 "Unsupported parameter". That 400 is
classified ``malformed``, which is **not** a fallbackable class: the router
serves no fallback, so one innocent field turned the whole request into a
502 for the caller. It hit the image path hardest because Hermes' vision and
summariser calls are exactly the requests that set ``temperature``.

The fields are all legitimate in OpenAI's own Chat dialect, so a client that
sets one is doing nothing wrong and the route can still serve the request.
The provider therefore declares what its upstream refuses, and the router
drops those fields **for that route only**, recording the substitution in the
ledger. The strip is provider-owned: the routing core holds no provider
branch, and an adapter that declares nothing is never stripped.
"""

from __future__ import annotations

import unittest

from gateway.adapters.base import AdapterError, ProviderRequest, ProviderResponse
from gateway.config import Alias, ModelEntry, ProviderState, RouteRef
from tests.test_end_to_end import FAKE_PROVIDER, FakeAdapter, GatewayHarness


def _alias():
    from gateway.config import Alias, RouteRef

    return Alias(
        name="coding",
        enabled=True,
        primary=RouteRef(FAKE_PROVIDER, "gpt-6-luna"),
        fallback_triggers=["exhausted", "rate_limited", "upstream_unavailable"],
    )


class _CodexLikeAdapter(FakeAdapter):
    """A fake that declares the real provider's unsupported-field set."""

    UNSUPPORTED_PAYLOAD_FIELDS = frozenset({"temperature", "top_p", "seed", "n"})


class UnsupportedParameterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = GatewayHarness()
        self.addCleanup(self.harness.close)
        with self.harness.config.mutate() as config:
            config.providers[FAKE_PROVIDER] = ProviderState(
                provider_id=FAKE_PROVIDER,
                kind=FAKE_PROVIDER,
                state="connected",
                models={
                    "gpt-6-luna": ModelEntry(
                        model_id="gpt-6-luna",
                        enabled=True,
                        dialect="openai_responses",
                        source="discovered",
                    )
                },
            )
            config.aliases["coding"] = _alias()

    def _send(self, extra: dict) -> tuple:
        return self.harness.request(
            "POST",
            "/v1/chat/completions",
            body={
                "model": "coding",
                "messages": [{"role": "user", "content": "hi"}],
                **extra,
            },
        )

    def _event(self):
        return self.harness.router._ledger.recent()[0]

    def test_a_declared_unsupported_field_is_dropped_not_forwarded(self) -> None:
        """The field never reaches the upstream, and the request succeeds."""
        adapter = _CodexLikeAdapter()
        self.harness.registry.register(adapter)

        status, _, _ = self._send({"temperature": 0.1})

        self.assertEqual(status, 200)
        self.assertEqual(len(adapter.calls), 1)
        self.assertNotIn("temperature", adapter.calls[0].payload)

    def test_the_substitution_is_recorded_and_never_silent(self) -> None:
        """The ledger names the dropped field, so nothing is hidden."""
        self.harness.registry.register(_CodexLikeAdapter())

        self._send({"temperature": 0.1})

        event = self._event()
        self.assertTrue(event["degradations"], "a dropped field must be recorded")
        degradation = event["degradations"][0]
        self.assertEqual(degradation["provider"], FAKE_PROVIDER)
        self.assertIn("temperature", degradation["reason"])
        self.assertEqual(degradation.get("dropped_fields"), ["temperature"])

    def test_several_dropped_fields_are_all_named_in_one_record(self) -> None:
        """Both sampling controls the bridge forwards are named together."""
        self.harness.registry.register(_CodexLikeAdapter())

        self._send({"temperature": 0.2, "top_p": 0.9})

        event = self._event()
        self.assertEqual(
            event["degradations"][0].get("dropped_fields"),
            ["temperature", "top_p"],
        )

    def test_a_field_the_bridge_never_forwards_is_not_reported(self) -> None:
        """``seed`` is rejected upstream but the bridge drops it at normalisation.

        The strip covers the whole rejected set so a payload built by any
        other path is still safe, but a field the bridge already discarded is
        not reported as a substitution - the ledger must describe what was
        actually taken off the wire.
        """
        self.harness.registry.register(_CodexLikeAdapter())

        status, _, _ = self._send({"seed": 7})

        self.assertEqual(status, 200)
        self.assertEqual(self._event()["degradations"], [])

    def test_a_field_not_declared_by_the_adapter_is_left_alone(self) -> None:
        """The strip is provider-owned: an adapter that declares nothing keeps its fields."""
        adapter = FakeAdapter()
        self.harness.registry.register(adapter)

        status, _, _ = self._send({"temperature": 0.1})

        self.assertEqual(status, 200)
        self.assertEqual(adapter.calls[0].payload.get("temperature"), 0.1)
        self.assertEqual(self._event()["degradations"], [])

    def test_a_request_with_no_unsupported_fields_records_no_degradation(self) -> None:
        self.harness.registry.register(_CodexLikeAdapter())

        status, _, _ = self._send({"max_tokens": 100})

        self.assertEqual(status, 200)
        self.assertEqual(self._event()["degradations"], [])

    def test_a_malformed_field_that_is_not_declared_still_fails_closed(self) -> None:
        """The strip does not weaken the malformed class: a real 400 still fails."""
        adapter = FakeAdapter(fail_with=AdapterError("malformed", provider_id=FAKE_PROVIDER))
        self.harness.registry.register(adapter)

        status, _, _ = self._send({})

        self.assertNotEqual(status, 200)


class ImagePlusParameterTests(unittest.TestCase):
    """The failure hit the image path: an image with a temperature still works."""

    def setUp(self) -> None:
        self.harness = GatewayHarness()
        self.addCleanup(self.harness.close)
        with self.harness.config.mutate() as config:
            config.providers[FAKE_PROVIDER] = ProviderState(
                provider_id=FAKE_PROVIDER,
                kind=FAKE_PROVIDER,
                state="connected",
                models={
                    "gpt-5.6-terra": ModelEntry(
                        model_id="gpt-5.6-terra",
                        enabled=True,
                        dialect="openai_responses",
                        source="discovered",
                        supports_image_input=True,
                    )
                },
            )
            config.aliases["pm"] = Alias(
                name="pm",
                enabled=True,
                primary=RouteRef(FAKE_PROVIDER, "gpt-5.6-terra"),
            )

    def test_an_image_request_with_a_temperature_is_served_and_recorded(self) -> None:
        adapter = _CodexLikeAdapter()
        self.harness.registry.register(adapter)

        status, _, _ = self.harness.request(
            "POST",
            "/v1/chat/completions",
            body={
                "model": "pm",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "describe"},
                            {
                                "type": "image_url",
                                "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="},
                            },
                        ],
                    }
                ],
                "temperature": 0.1,
            },
        )

        self.assertEqual(status, 200)
        payload = adapter.calls[0].payload
        self.assertNotIn("temperature", payload)
        # The image itself is preserved, not dropped alongside the parameter.
        self.assertIn("input", payload)

    def test_the_adapter_declares_the_real_provider_set(self) -> None:
        """The shipped constant must cover the fields that actually 400."""
        from gateway.adapters.chatgpt_codex import ChatGPTCodexAdapter

        declared = ChatGPTCodexAdapter.UNSUPPORTED_PAYLOAD_FIELDS
        # Every field confirmed rejected by the live backend probe.
        for field in (
            "temperature", "top_p", "n", "stop", "presence_penalty",
            "frequency_penalty", "logit_bias", "logprobs", "top_logprobs",
            "seed", "user", "metadata", "response_format", "service_tier",
            "max_tokens", "max_completion_tokens", "suffix", "stop_sequences",
        ):
            self.assertIn(field, declared, f"{field} is rejected upstream and must be declared")


if __name__ == "__main__":
    unittest.main()
