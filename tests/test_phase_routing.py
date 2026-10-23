"""Opt-in gateway phase routing: caller hint, Jev inference, and the refusals.

The rules under test:

* A phase route is an owner-configured route, not a fallback. Selecting it
  never sets ``fallback_used``.
* The caller's phase hint is read and **stripped** before the request is
  converted or forwarded: no upstream provider ever sees it.
* Jev inference is opt-in per alias, runs only when the request is safe to
  classify (no tools, has text), and applies only on a confident, known
  choice. Anything else uses the ordinary route and says why.
* The ledger records the phase and its source as metadata only — never the
  request content, the Jev question, or the answer distribution.
"""
from __future__ import annotations

import json
import unittest
from typing import Any

from gateway.adapters.base import TestResult
from gateway.config import (
    Alias,
    JevInference,
    ModelEntry,
    PhaseRouting,
    PhaseRoute,
    ProviderState,
    RouteRef,
)
from gateway.identifiers import ValidationError
from gateway.routes import validate_phase_routing
from tests.test_end_to_end import FAKE_PROVIDER, FakeAdapter, GatewayHarness


class ScriptedJev:
    """A Jev (System One) adapter with a scripted choice and confidence."""

    provider_id = "opencode_console"
    kind = "opencode_console"

    def __init__(
        self,
        choice: str = "plan",
        confidence: float = 0.9,
        fail: bool = False,
        answer_type: str = "choice",
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self.choice = choice
        self.confidence = confidence
        self.fail = fail
        self.answer_type = answer_type
        self.model_list_calls = 0

    def list_models(self) -> list:
        self.model_list_calls += 1
        return []

    def test_connection(self) -> TestResult:
        return TestResult(ok=True, state="connected", message="scripted Jev reachable")

    def invoke_systemone(self, payload: dict[str, Any]) -> bytes:
        self.calls.append(payload)
        if self.fail:
            raise RuntimeError("synthetic Jev failure")
        return json.dumps(
            {
                "model": payload["model"],
                "answers": {
                    "phase": {
                        "type": self.answer_type,
                        "choice": self.choice,
                        "confidence": self.confidence,
                        # A distribution that must never reach the ledger.
                        "probabilities": {self.choice: self.confidence},
                    }
                },
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }
        ).encode()


def _phase_harness(
    jev: ScriptedJev | None = None,
    *,
    inference_enabled: bool = True,
    minimum_confidence: float = 0.8,
    phase_routes: dict[str, RouteRef] | None = None,
) -> tuple[GatewayHarness, ScriptedJev, FakeAdapter]:
    """A harness whose ``coding`` alias has an enabled phase policy.

    Primary: FAKE_PROVIDER/gpt-6-luna (openai_responses).
    ``plan`` phase route: opencode_go/mimo-v2.6-flash (openai_chat).
    """
    harness = GatewayHarness()
    scripted = jev or ScriptedJev()
    harness.registry.register(scripted)
    go = FakeAdapter(provider_id="opencode_go", dialect="openai_chat")
    harness.registry.register(go)
    with harness.config.mutate() as config:
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
        config.aliases["coding"] = Alias(
            name="coding",
            enabled=True,
            primary=RouteRef(FAKE_PROVIDER, "gpt-6-luna"),
            phase_routing=PhaseRouting(
                enabled=True,
                routes={
                    phase: PhaseRoute(phase=phase, route=ref)
                    for phase, ref in (phase_routes or {"plan": RouteRef("opencode_go", "mimo-v2.6-flash")}).items()
                },
                inference=JevInference(
                    enabled=inference_enabled,
                    provider="opencode_console",
                    model="jev-1.13",
                    minimum_confidence=minimum_confidence,
                ),
            ),
        )
    return harness, scripted, go


def _chat(metadata: dict[str, Any] | None = None, **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": "coding",
        "messages": [{"role": "user", "content": "synthetic request content"}],
    }
    if metadata is not None:
        body["metadata"] = metadata
    body.update(extra)
    return body


class CallerPhaseTests(unittest.TestCase):
    """The caller may name a phase; it is honoured and never forwarded."""

    def test_caller_phase_selects_the_phase_route_and_is_not_a_fallback(self) -> None:
        harness, scripted, go = _phase_harness(inference_enabled=False)
        self.addCleanup(harness.close)
        status, _, _ = harness.request(
            "POST", "/v1/chat/completions",
            body=_chat(metadata={"personal_gateway": {"phase": "plan"}}),
        )
        self.assertEqual(status, 200)
        # The phase route served, not the primary.
        self.assertEqual(len(go.calls), 1)
        self.assertEqual(harness.adapter.calls, [])
        event = harness.router._ledger.recent()[0]
        self.assertEqual(event["phase"], "plan")
        self.assertEqual(event["phase_source"], "caller")
        # A phase-selected route is not a fallback.
        self.assertFalse(event["fallback_used"])
        # Inference never ran: the caller was explicit.
        self.assertEqual(scripted.calls, [])

    def test_caller_phase_is_stripped_before_the_request_reaches_a_provider(self) -> None:
        harness, _, go = _phase_harness(inference_enabled=False)
        self.addCleanup(harness.close)
        status, _, _ = harness.request(
            "POST", "/v1/chat/completions",
            body=_chat(metadata={"personal_gateway": {"phase": "plan", "other": 1}}),
        )
        self.assertEqual(status, 200)
        payload = go.calls[0].payload
        self.assertNotIn("personal_gateway", json.dumps(payload))
        metadata = payload.get("metadata")
        self.assertTrue(metadata is None or "personal_gateway" not in metadata)

    def test_invalid_caller_phase_is_ignored_and_uses_the_primary(self) -> None:
        harness, scripted, go = _phase_harness(inference_enabled=False)
        self.addCleanup(harness.close)
        status, _, _ = harness.request(
            "POST", "/v1/chat/completions",
            body=_chat(metadata={"personal_gateway": {"phase": "banana"}}),
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(harness.adapter.calls), 1)
        self.assertEqual(go.calls, [])
        self.assertEqual(scripted.calls, [])

    def test_caller_phase_with_no_configured_route_uses_the_primary_and_records_it(self) -> None:
        # Only "plan" has a route; a caller asking for "validate" gets the
        # ordinary route, and the phase is recorded without re-classifying.
        harness, scripted, go = _phase_harness(inference_enabled=True)
        self.addCleanup(harness.close)
        status, _, _ = harness.request(
            "POST", "/v1/chat/completions",
            body=_chat(metadata={"personal_gateway": {"phase": "validate"}}),
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(harness.adapter.calls), 1)
        self.assertEqual(go.calls, [])
        self.assertEqual(scripted.calls, [])
        event = harness.router._ledger.recent()[0]
        self.assertEqual(event["phase"], "validate")
        self.assertEqual(event["phase_source"], "caller")


class JevInferenceTests(unittest.TestCase):
    """Inference is opt-in, confidence-gated, and fail-open to the route."""

    def test_confident_choice_selects_the_phase_route(self) -> None:
        harness, scripted, go = _phase_harness(ScriptedJev(choice="plan", confidence=0.93))
        self.addCleanup(harness.close)
        status, _, _ = harness.request("POST", "/v1/chat/completions", body=_chat())
        self.assertEqual(status, 200)
        self.assertEqual(len(scripted.calls), 1)
        self.assertEqual(len(go.calls), 1)
        self.assertEqual(harness.adapter.calls, [])
        event = harness.router._ledger.recent()[0]
        self.assertEqual(event["phase"], "plan")
        self.assertEqual(event["phase_source"], "jev")

    def test_low_confidence_uses_the_ordinary_route(self) -> None:
        harness, scripted, go = _phase_harness(ScriptedJev(choice="plan", confidence=0.3))
        self.addCleanup(harness.close)
        status, _, _ = harness.request("POST", "/v1/chat/completions", body=_chat())
        self.assertEqual(status, 200)
        self.assertEqual(len(scripted.calls), 1)
        self.assertEqual(go.calls, [])
        self.assertEqual(len(harness.adapter.calls), 1)
        event = harness.router._ledger.recent()[0]
        self.assertIsNone(event["phase"])
        self.assertEqual(event["phase_source"], "default")

    def test_unknown_choice_uses_the_ordinary_route(self) -> None:
        harness, scripted, go = _phase_harness(ScriptedJev(choice="banana", confidence=0.99))
        self.addCleanup(harness.close)
        status, _, _ = harness.request("POST", "/v1/chat/completions", body=_chat())
        self.assertEqual(status, 200)
        self.assertEqual(go.calls, [])
        self.assertEqual(len(harness.adapter.calls), 1)
        self.assertEqual(harness.router._ledger.recent()[0]["phase_source"], "default")

    def test_non_choice_answer_uses_the_ordinary_route(self) -> None:
        harness, scripted, go = _phase_harness(ScriptedJev(answer_type="noul"))
        self.addCleanup(harness.close)
        status, _, _ = harness.request("POST", "/v1/chat/completions", body=_chat())
        self.assertEqual(status, 200)
        self.assertEqual(go.calls, [])
        self.assertEqual(len(harness.adapter.calls), 1)

    def test_jev_failure_never_fails_the_completion(self) -> None:
        harness, scripted, go = _phase_harness(ScriptedJev(fail=True))
        self.addCleanup(harness.close)
        status, _, _ = harness.request("POST", "/v1/chat/completions", body=_chat())
        self.assertEqual(status, 200)
        self.assertEqual(len(scripted.calls), 1)
        self.assertEqual(go.calls, [])
        self.assertEqual(len(harness.adapter.calls), 1)

    def test_tools_skip_inference_and_use_the_ordinary_route(self) -> None:
        harness, scripted, go = _phase_harness(ScriptedJev(choice="plan", confidence=0.95))
        self.addCleanup(harness.close)
        status, _, _ = harness.request(
            "POST", "/v1/chat/completions",
            body=_chat(tools=[{"type": "function", "function": {"name": "f", "parameters": {"type": "object"}}}]),
        )
        # Tools on a responses-dialect primary: the request is served or
        # refused by the ordinary rules; the key point is that Jev was never
        # consulted to re-route a tool-bearing request.
        self.assertEqual(scripted.calls, [])
        self.assertEqual(go.calls, [])

    def test_inference_disabled_never_calls_jev(self) -> None:
        harness, scripted, go = _phase_harness(ScriptedJev(choice="plan", confidence=0.95), inference_enabled=False)
        self.addCleanup(harness.close)
        status, _, _ = harness.request("POST", "/v1/chat/completions", body=_chat())
        self.assertEqual(status, 200)
        self.assertEqual(scripted.calls, [])
        self.assertEqual(go.calls, [])
        self.assertEqual(len(harness.adapter.calls), 1)


class PhaseLedgerPrivacyTests(unittest.TestCase):
    def test_ledger_records_phase_metadata_only(self) -> None:
        harness, scripted, _ = _phase_harness(ScriptedJev(choice="plan", confidence=0.93))
        self.addCleanup(harness.close)
        status, _, _ = harness.request("POST", "/v1/chat/completions", body=_chat())
        self.assertEqual(status, 200)
        event = harness.router._ledger.recent()[0]
        self.assertEqual(event["phase"], "plan")
        self.assertEqual(event["phase_source"], "jev")
        text = json.dumps(event)
        # No request content, no Jev question, no answer distribution.
        self.assertNotIn("synthetic request content", text)
        self.assertNotIn("probabilities", text)
        self.assertNotIn("criteria", text)
        # The question the gateway sent Jev is fixed and gateway-owned; the
        # state (request content) it carried must not be recorded.
        self.assertNotIn("state", text)


class PhaseRoutingValidationTests(unittest.TestCase):
    """Every phase route is validated like an alias primary or fallback."""

    def _harness(self) -> GatewayHarness:
        harness = GatewayHarness()
        with harness.config.mutate() as config:
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
        return harness

    def test_valid_phase_routing_is_accepted(self) -> None:
        harness = self._harness()
        self.addCleanup(harness.close)
        policy = validate_phase_routing(
            harness.config, harness.registry,
            {
                "enabled": True,
                "routes": {"plan": {"provider": "opencode_go", "model": "mimo-v2.6-flash"}},
                "inference": {"enabled": True, "provider": "opencode_console", "model": "jev-1.13"},
            },
        )
        self.assertIsNotNone(policy)
        self.assertTrue(policy.enabled)
        self.assertIn("plan", policy.routes)

    def test_unknown_phase_key_is_refused(self) -> None:
        harness = self._harness()
        self.addCleanup(harness.close)
        with self.assertRaises(ValidationError):
            validate_phase_routing(
                harness.config, harness.registry,
                {"routes": {"banana": {"provider": "opencode_go", "model": "mimo-v2.6-flash"}}},
            )

    def test_phase_route_naming_an_unregistered_provider_is_refused(self) -> None:
        harness = self._harness()
        self.addCleanup(harness.close)
        with self.assertRaises(ValidationError):
            validate_phase_routing(
                harness.config, harness.registry,
                {"routes": {"plan": {"provider": "ghost", "model": "mimo-v2.6-flash"}}},
            )

    def test_phase_route_naming_an_unknown_model_is_refused(self) -> None:
        harness = self._harness()
        self.addCleanup(harness.close)
        with self.assertRaises(ValidationError):
            validate_phase_routing(
                harness.config, harness.registry,
                {"routes": {"plan": {"provider": "opencode_go", "model": "nope"}}},
            )

    def test_inference_naming_an_unregistered_provider_is_refused(self) -> None:
        harness = self._harness()
        self.addCleanup(harness.close)
        with self.assertRaises(ValidationError):
            validate_phase_routing(
                harness.config, harness.registry,
                {"inference": {"enabled": True, "provider": "ghost", "model": "jev-1.13"}},
            )

    def test_inference_naming_an_unknown_model_is_refused(self) -> None:
        harness = self._harness()
        self.addCleanup(harness.close)
        with self.assertRaises(ValidationError):
            validate_phase_routing(
                harness.config, harness.registry,
                {"inference": {"enabled": True, "provider": "opencode_console", "model": "ghost-1"}},
            )

    def test_inference_confidence_out_of_range_is_refused(self) -> None:
        harness = self._harness()
        self.addCleanup(harness.close)
        for bad in (-0.1, 1.5, "high", True):
            with self.subTest(confidence=bad):
                with self.assertRaises(ValidationError):
                    validate_phase_routing(
                        harness.config, harness.registry,
                        {"inference": {"enabled": True, "provider": "opencode_console",
                                       "model": "jev-1.13", "minimum_confidence": bad}},
                    )

    def test_disabled_inference_needs_no_model(self) -> None:
        harness = self._harness()
        self.addCleanup(harness.close)
        policy = validate_phase_routing(
            harness.config, harness.registry,
            {"inference": {"enabled": False}},
        )
        self.assertFalse(policy.inference.enabled)

    def test_non_object_phase_routing_is_refused(self) -> None:
        harness = self._harness()
        self.addCleanup(harness.close)
        with self.assertRaises(ValidationError):
            validate_phase_routing(harness.config, harness.registry, "banana")


if __name__ == "__main__":
    unittest.main()
