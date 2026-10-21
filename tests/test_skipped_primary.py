"""A request served by a route other than the primary must say why.

A primary that is skipped - because it is in a local cooldown, disabled, or
unknown - produces a request that looks like an unexplained fallback. The
record has to name the skipped route and its reason, or the Status view
cannot be trusted.
"""

from __future__ import annotations

import json
import unittest
from typing import Any

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
        # An exhausted failure is account-scoped, so the provider-wide
        # circuit carries the correlation to the request that set it.
        cooldown = self.harness.router.circuits.details()[f"{FAKE_PROVIDER}/*"]
        self.assertEqual(cooldown["error_class"], "exhausted")
        self.assertEqual(cooldown["request_id"], event["request_id"])
        self.assertGreater(cooldown["set_at"], 0)

    def test_a_new_failure_replaces_the_cooldown_correlation_atomically(self) -> None:
        circuits = self.harness.router.circuits
        circuits.record_failure(FAKE_PROVIDER, "gpt-6-luna", "exhausted", 300, request_id="request-old")
        circuits.record_failure(FAKE_PROVIDER, "gpt-6-luna", "exhausted", 300, request_id="request-new")
        entry = circuits.details()[f"{FAKE_PROVIDER}/*"]
        self.assertEqual(entry["request_id"], "request-new")
        self.assertEqual(entry["failures"], 2)
        self.assertGreater(entry["remaining_seconds"], 0)
        removed = circuits.clear(FAKE_PROVIDER)
        self.assertTrue(removed)
        self.assertEqual(circuits.details(), {})


class SkipPathAuthorizationTests(unittest.TestCase):
    """A skipped primary hands the request elsewhere only with the owner's gate.

    The security finding this pins (R49): both skip paths - a primary
    excluded during enumeration, and a primary whose invocation-boundary
    claim was refused - used to set ``fallback_used`` and serve a later,
    cross-provider route without ever consulting ``_fallback_authorized``,
    ``fallback_confirmed_providers`` or ``fallback_triggers``. The gate,
    the trigger each skip contributes, and the refusal when the gate
    declines are all pinned here. No test may reach a provider on a
    refused switch.
    """

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
                        # Image-capable, so a capability skip of the
                        # text-only primary always has a route to switch to.
                        supports_image_input=True,
                    )
                },
            )

    # --- fixtures ---------------------------------------------------------

    def _set_alias(self, *, authorized: bool, confirmed: list[str], triggers: list[str]) -> None:
        from gateway.config import Alias, RouteRef

        with self.harness.config.mutate() as config:
            config.aliases["coding"] = Alias(
                name="coding",
                enabled=True,
                primary=RouteRef(FAKE_PROVIDER, "gpt-6-luna"),
                fallbacks=[RouteRef("opencode_go", "mimo-v2.6-flash")],
                fallback_authorized=authorized,
                fallback_confirmed_providers=confirmed,
                fallback_triggers=triggers,
            )

    def _send(self, *, image: bool = False, tools: bool = False):
        content: Any = "hi"
        if image:
            content = [
                {"type": "text", "text": "x"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}},
            ]
        body: dict[str, Any] = {
            "model": "coding",
            "messages": [{"role": "user", "content": content}],
        }
        if tools:
            body["tools"] = [
                {"type": "function", "function": {"name": "f", "parameters": {"type": "object"}}}
            ]
        return self.harness.request("POST", "/v1/chat/completions", body=body)

    def _fallback_adapter(self) -> FakeAdapter:
        adapter = self.harness.registry.get("opencode_go")
        assert isinstance(adapter, FakeAdapter)
        return adapter

    def _latest_event(self) -> dict[str, Any]:
        router = self.harness.router
        assert router is not None
        return router._ledger.recent()[0]

    def _assert_refused(self, status: int, payload: Any, *, contains: str | None = None) -> None:
        self.assertEqual(status, 503, payload)
        self.assertEqual(payload["error"]["type"], "fallback_not_permitted")
        self.assertIn("fallback not used", payload["error"]["message"])
        if contains is not None:
            self.assertIn(contains, payload["error"]["message"])

    def _assert_not_side_effects_refusal(self, payload: Any) -> None:
        # The side-effects wording belongs to attempted routes only: a
        # skip refusal must name the owner's gate instead, never the
        # 'carried tools' hazard that cannot exist on a skip path (R50).
        self.assertNotIn("carried tools", json.dumps(payload))
        # A public response names no upstream identifier - neither the
        # skipped primary's nor the fallback that was declined.
        serialized = json.dumps(payload)
        for identifier in (FAKE_PROVIDER, "gpt-6-luna", "opencode_go", "mimo-v2.6-flash"):
            self.assertNotIn(identifier, serialized)
        # Nothing was invoked, by anyone.
        self.assertEqual(self.harness.adapter.calls, [])
        self.assertEqual(self._fallback_adapter().calls, [])

    # --- cooldown skips (operator-placed vs automatic) --------------------

    def test_operator_cooldown_refused_when_fallback_is_not_authorised(self) -> None:
        self._set_alias(authorized=False, confirmed=[], triggers=[])
        self.harness.router.circuits.set(FAKE_PROVIDER, 300)
        status, payload, _ = self._send()
        self._assert_refused(status, payload, contains="not authorised")
        # A refused request is a recorded outcome: nobody served it, no
        # fallback was claimed, and the skips that led here are visible.
        event = self._latest_event()
        self.assertEqual(event["status"], 503)
        self.assertEqual(event["error_class"], "policy")
        self.assertIsNone(event["serving_provider"])
        self.assertFalse(event["fallback_used"])
        self.assertTrue(event["skipped"])
        self.assertFalse(event["fallback_used"])
        self.assertIn("fallback refused: cross-provider fallback is not authorised", event["fallback_reason"])

    def test_policy_refusal_reason_is_persisted_in_ledger_event(self) -> None:
        self._set_alias(authorized=True, confirmed=["opencode_go"], triggers=[])
        self.harness.router.circuits.record_failure(
            FAKE_PROVIDER, "gpt-6-luna", "exhausted", base_seconds=900
        )
        status, payload, _ = self._send()
        self.assertEqual(status, 503, payload)
        event = self._latest_event()
        self.assertEqual(event["error_class"], "policy")
        self.assertFalse(event["fallback_used"])
        self.assertTrue(event["fallback_reason"])
        self.assertIn("exhausted", event["fallback_reason"])
        self.assertIn("trigger is not enabled", event["fallback_reason"])
        self.assertEqual(event["attempts"], [])
        self.assertEqual(self.harness.adapter.calls, [])
        self.assertEqual(self._fallback_adapter().calls, [])

    def test_operator_cooldown_refused_when_the_provider_is_not_confirmed(self) -> None:
        self._set_alias(authorized=True, confirmed=[], triggers=["explicit_cooldown"])
        self.harness.router.circuits.set(FAKE_PROVIDER, 300)
        status, payload, _ = self._send()
        # The confirmation reason would name the provider; the public
        # wording reports the gate without it.
        self._assert_refused(status, payload, contains="has not been confirmed")

    def test_operator_cooldown_refused_when_the_trigger_is_disabled(self) -> None:
        self._set_alias(authorized=True, confirmed=["opencode_go"], triggers=["exhausted"])
        self.harness.router.circuits.set(FAKE_PROVIDER, 300)
        status, payload, _ = self._send()
        self._assert_refused(status, payload, contains="'explicit_cooldown' trigger is not enabled")

    def test_operator_cooldown_serves_when_the_owner_enables_the_trigger(self) -> None:
        self._set_alias(
            authorized=True, confirmed=["opencode_go"], triggers=["explicit_cooldown"]
        )
        self.harness.router.circuits.set(FAKE_PROVIDER, 300)
        status, payload, _ = self._send()
        self.assertEqual(status, 200, payload)
        event = self._latest_event()
        self.assertTrue(event["fallback_used"])
        self.assertEqual(event["serving_provider"], "opencode_go")
        self.assertIn("cooldown", event["fallback_reason"])

    def test_automatic_cooldown_keeps_its_recorded_failure_trigger(self) -> None:
        # An automatic cooldown carries the failure class that opened it,
        # and ``explicit_cooldown`` is deliberately *not* needed here:
        # ``_maybe_open_circuit`` only opens a window for a trigger the
        # owner enabled, so the skip must honour that same trigger.
        self._set_alias(
            authorized=True, confirmed=["opencode_go"], triggers=["exhausted"]
        )
        from gateway.adapters.base import AdapterError

        self.harness.registry.register(
            FakeAdapter(fail_with=AdapterError("exhausted", provider_id=FAKE_PROVIDER))
        )
        status, payload, _ = self._send()
        self.assertEqual(status, 200, payload)
        status, payload, _ = self._send()
        self.assertEqual(status, 200, payload)
        event = self._latest_event()
        self.assertTrue(event["fallback_used"])
        self.assertEqual(event["serving_provider"], "opencode_go")
        self.assertIn("cooldown", event["fallback_reason"])
        # The circuit that held the primary names the failure class, so
        # the skip attributed to it, not to the operator's trigger.
        self.assertIn("exhausted", event["skipped"][0]["reason"])

    def test_a_cooldown_opened_by_a_disabled_trigger_refuses(self) -> None:
        # The circuit store is global: a window this alias never opted
        # into (opened by another alias's failure, or by the operator)
        # still holds the route - and this alias, whose triggers do not
        # include ``rate_limited``, must refuse to switch away from it.
        self._set_alias(
            authorized=True, confirmed=["opencode_go"], triggers=["explicit_cooldown"]
        )
        self.harness.router.circuits.record_failure(
            FAKE_PROVIDER, "gpt-6-luna", "rate_limited", base_seconds=300
        )
        status, payload, _ = self._send()
        self._assert_refused(status, payload, contains="'rate_limited' trigger is not enabled")

    # --- capability / configuration skips ---------------------------------

    def test_capability_skip_refused_when_fallback_is_not_authorised(self) -> None:
        self._set_alias(authorized=False, confirmed=[], triggers=[])
        status, payload, headers = self._send(image=True)
        self._assert_refused(status, payload, contains="not authorised")
        # A capability gap is not a cooldown: no retry hint may appear.
        self.assertNotIn("retry_after_seconds", payload["error"])
        self.assertNotIn("Retry-After", dict(headers))

    def test_capability_skip_serves_with_the_route_ineligible_trigger(self) -> None:
        self._set_alias(
            authorized=True, confirmed=["opencode_go"], triggers=["route_ineligible"]
        )
        status, payload, _ = self._send(image=True)
        self.assertEqual(status, 200, payload)
        event = self._latest_event()
        self.assertEqual(event["serving_provider"], "opencode_go")
        self.assertTrue(event["fallback_used"])
        self.assertIn("does not support image input", event["fallback_reason"])
        # The primary never reached a provider.
        self.assertEqual(self.harness.adapter.calls, [])

    # --- half-open probe contention ----------------------------------------

    def _hold_the_probe(self) -> None:
        """Claim the primary's half-open probe, as a concurrent probe would."""
        from gateway.circuits import CircuitStore
        from tests.test_probe_claim import _Clock

        clock = _Clock()
        self.harness.router._circuits = CircuitStore(clock=clock)
        self.harness.router.circuits.record_failure(
            FAKE_PROVIDER, "gpt-6-luna", "rate_limited", base_seconds=300
        )
        clock.advance(400)
        claimed, reason, _owner = self.harness.router.circuits.claim(
            FAKE_PROVIDER, "gpt-6-luna"
        )
        self.assertTrue(claimed, reason)

    def test_probe_contention_refused_without_its_trigger(self) -> None:
        self._set_alias(
            authorized=True,
            confirmed=["opencode_go"],
            triggers=["exhausted", "rate_limited", "upstream_unavailable"],
        )
        self._hold_the_probe()
        status, payload, _ = self._send()
        self._assert_refused(status, payload, contains="'probe_contention' trigger is not enabled")

    def test_probe_contention_serves_with_its_trigger(self) -> None:
        self._set_alias(
            authorized=True,
            confirmed=["opencode_go"],
            triggers=["probe_contention"],
        )
        self._hold_the_probe()
        status, payload, _ = self._send()
        self.assertEqual(status, 200, payload)
        event = self._latest_event()
        self.assertEqual(event["serving_provider"], "opencode_go")
        self.assertTrue(event["fallback_used"])
        self.assertIn("recovery probe", event["fallback_reason"])

    def _refuse_primary_claims(self) -> None:
        """Rig the store so only the invocation-boundary claim refuses.

        Enumeration still previews the primary as usable - this is the
        race the claim branch exists for: the hold appears between
        preview and claim, which no test can otherwise reach
        deterministically.
        """
        circuits = self.harness.router.circuits
        real_claim = circuits.claim

        def rigged(provider: str, model: str):
            if provider == FAKE_PROVIDER:
                return (
                    False,
                    "a recovery probe is already in flight for this route, so this route is not used yet",
                    "foreign-owner",
                )
            return real_claim(provider, model)

        circuits.claim = rigged  # type: ignore[method-assign]

    def test_a_claim_refused_at_the_boundary_is_gated_too(self) -> None:
        self._set_alias(
            authorized=True,
            confirmed=["opencode_go"],
            triggers=["exhausted", "rate_limited", "upstream_unavailable"],
        )
        self._refuse_primary_claims()
        status, payload, _ = self._send()
        self._assert_refused(status, payload, contains="'probe_contention' trigger is not enabled")
        # The tail records the stopped loop: a 503 with the skip, and no
        # fallback claimed for a request that was never switched.
        event = self._latest_event()
        self.assertEqual(event["status"], 503)
        self.assertFalse(event["fallback_used"])
        self.assertTrue(event["skipped"])

    def test_a_claim_refused_at_the_boundary_serves_with_its_trigger(self) -> None:
        self._set_alias(
            authorized=True, confirmed=["opencode_go"], triggers=["probe_contention"]
        )
        self._refuse_primary_claims()
        status, payload, _ = self._send()
        self.assertEqual(status, 200, payload)
        event = self._latest_event()
        self.assertEqual(event["serving_provider"], "opencode_go")
        self.assertTrue(event["fallback_used"])
        self.assertIn("primary route was skipped", event["fallback_reason"])
        self.assertIn("recovery probe", event["fallback_reason"])

    # --- tool-bearing requests and a never-invoked primary (R50) ------------

    def test_tools_do_not_block_a_switch_away_from_a_never_invoked_primary(self) -> None:
        # The fail-closed tools control exists so a switch never repeats a
        # request an upstream may have executed. A skipped primary was
        # never invoked - no attempt is recorded - so the owner's own
        # authorization, confirmation and trigger decide, and a
        # tool-bearing agent request takes the configured fallback.
        self._set_alias(
            authorized=True, confirmed=["opencode_go"], triggers=["explicit_cooldown"]
        )
        self.harness.router.circuits.set(FAKE_PROVIDER, 300)
        status, payload, _ = self._send(tools=True)
        self.assertEqual(status, 200, payload)
        event = self._latest_event()
        self.assertTrue(event["fallback_used"])
        self.assertEqual(event["serving_provider"], "opencode_go")
        self.assertIn("primary route was skipped", event["fallback_reason"])
        # The primary was never invoked: nothing could have executed.
        self.assertEqual(self.harness.adapter.calls, [])

    def test_tools_do_not_block_a_claim_refused_switch(self) -> None:
        self._set_alias(
            authorized=True, confirmed=["opencode_go"], triggers=["probe_contention"]
        )
        self._refuse_primary_claims()
        status, payload, _ = self._send(tools=True)
        self.assertEqual(status, 200, payload)
        event = self._latest_event()
        self.assertTrue(event["fallback_used"])
        self.assertEqual(event["serving_provider"], "opencode_go")
        self.assertEqual(self.harness.adapter.calls, [])

    def test_tools_still_refuse_when_the_owner_gate_declines(self) -> None:
        self._set_alias(authorized=False, confirmed=[], triggers=[])
        self.harness.router.circuits.set(FAKE_PROVIDER, 300)
        status, payload, _ = self._send(tools=True)
        self._assert_refused(status, payload, contains="not authorised")
        event = self._latest_event()
        self.assertEqual(event["status"], 503)
        self.assertFalse(event["fallback_used"])

    # --- tool-bearing requests on a skip (R50) ------------------------------

    def test_a_tool_request_falls_through_an_automatic_cooldown_skip(self) -> None:
        # R50: the production regression. While the primary sat in an
        # automatic allowance-exhaustion cooldown, every tool-bearing
        # request was refused (503, error_class=policy, zero attempts,
        # zero provider calls) although the fallback was healthy and the
        # owner had authorised it with the trigger enabled - the
        # side-effects gate of a *failed* attempt had been applied to a
        # route that was never invoked.
        self._set_alias(
            authorized=True, confirmed=["opencode_go"], triggers=["exhausted"]
        )
        self.harness.router.circuits.record_failure(
            FAKE_PROVIDER, "gpt-6-luna", "exhausted", base_seconds=900
        )
        status, payload, _ = self._send(tools=True)
        self.assertEqual(status, 200, payload)
        event = self._latest_event()
        self.assertEqual(event["serving_provider"], "opencode_go")
        self.assertTrue(event["fallback_used"])
        self.assertEqual(event["attempts"], [], "a skipped primary is never attempted")
        self.assertIn("primary route was skipped", event["fallback_reason"])
        self.assertIn("cooldown", event["fallback_reason"])
        self.assertTrue(event["skipped"])
        self.assertEqual(self.harness.adapter.calls, [], "the skipped primary must not be invoked")
        self.assertEqual(len(self._fallback_adapter().calls), 1)

    def test_a_tool_request_is_still_refused_when_the_switch_is_not_authorised(self) -> None:
        self._set_alias(authorized=False, confirmed=[], triggers=[])
        self.harness.router.circuits.set(FAKE_PROVIDER, 300)
        status, payload, _ = self._send(tools=True)
        self._assert_refused(status, payload, contains="not authorised")
        # The refusal names the missing authorization - never the
        # side-effects wording, which belongs to attempted routes only.
        self._assert_not_side_effects_refusal(payload)
        event = self._latest_event()
        self.assertEqual(event["status"], 503)
        self.assertEqual(event["error_class"], "policy")
        self.assertFalse(event["fallback_used"])
        self.assertTrue(event["skipped"])

    def test_a_tool_request_is_still_refused_when_the_trigger_is_disabled(self) -> None:
        self._set_alias(authorized=True, confirmed=["opencode_go"], triggers=["exhausted"])
        self.harness.router.circuits.set(FAKE_PROVIDER, 300)
        status, payload, _ = self._send(tools=True)
        self._assert_refused(
            status, payload, contains="'explicit_cooldown' trigger is not enabled"
        )
        self._assert_not_side_effects_refusal(payload)

    def test_a_tool_request_serves_on_probe_contention_with_its_trigger(self) -> None:
        self._set_alias(
            authorized=True, confirmed=["opencode_go"], triggers=["probe_contention"]
        )
        self._hold_the_probe()
        status, payload, _ = self._send(tools=True)
        self.assertEqual(status, 200, payload)
        event = self._latest_event()
        self.assertEqual(event["serving_provider"], "opencode_go")
        self.assertTrue(event["fallback_used"])
        self.assertIn("recovery probe", event["fallback_reason"])

    def test_a_tool_request_is_refused_on_probe_contention_without_its_trigger(self) -> None:
        self._set_alias(
            authorized=True,
            confirmed=["opencode_go"],
            triggers=["exhausted", "rate_limited", "upstream_unavailable"],
        )
        self._hold_the_probe()
        status, payload, _ = self._send(tools=True)
        self._assert_refused(
            status, payload, contains="'probe_contention' trigger is not enabled"
        )
        self._assert_not_side_effects_refusal(payload)

    def test_a_claim_refused_at_the_boundary_serves_a_tool_request_with_its_trigger(self) -> None:
        self._set_alias(
            authorized=True, confirmed=["opencode_go"], triggers=["probe_contention"]
        )
        self._refuse_primary_claims()
        status, payload, _ = self._send(tools=True)
        self.assertEqual(status, 200, payload)
        event = self._latest_event()
        self.assertEqual(event["serving_provider"], "opencode_go")
        self.assertTrue(event["fallback_used"])
        self.assertEqual(event["attempts"], [], "a claim refusal is not an attempt")
        self.assertIn("recovery probe", event["fallback_reason"])

    def test_a_claim_refused_at_the_boundary_refuses_a_tool_request_with_the_reason(self) -> None:
        self._set_alias(
            authorized=True,
            confirmed=["opencode_go"],
            triggers=["exhausted", "rate_limited", "upstream_unavailable"],
        )
        self._refuse_primary_claims()
        status, payload, _ = self._send(tools=True)
        self._assert_refused(
            status, payload, contains="'probe_contention' trigger is not enabled"
        )
        self._assert_not_side_effects_refusal(payload)
        event = self._latest_event()
        self.assertEqual(event["status"], 503)
        self.assertFalse(event["fallback_used"])
        self.assertTrue(event["skipped"])

    # --- the vocabulary reaches the owner surface --------------------------

    def test_phase_routes_round_trip_owner_state_to_save(self) -> None:
        # GET state returns nested route refs; POST save accepts that exact
        # representation so editing unrelated alias settings does not fail.
        from gateway.config import PhaseRouting, PhaseRoute, RouteRef
        with self.harness.config.mutate() as config:
            config.aliases["coding"] = _alias()
            config.aliases["coding"].phase_routing = PhaseRouting(
                enabled=True,
                routes={"plan": PhaseRoute("plan", RouteRef(FAKE_PROVIDER, "gpt-6-luna"), effort="high")},
            )
        status, state, _ = self.harness.request("GET", "/owner/api/state")
        self.assertEqual(status, 200)
        state_obj: Any = state
        self.assertIsInstance(state_obj, dict)
        aliases = state_obj.get("aliases")
        self.assertIsInstance(aliases, list)
        phase = next(a for a in aliases if isinstance(a, dict) and a.get("name") == "coding").get("phase_routing")
        self.assertIsInstance(phase, dict)
        phase_routes = phase.get("routes")
        self.assertIsInstance(phase_routes, dict)
        plan = phase_routes.get("plan")
        self.assertIsInstance(plan, dict)
        nested_route = plan.get("route")
        self.assertIsInstance(nested_route, dict)
        self.assertEqual(nested_route.get("provider"), FAKE_PROVIDER)
        payload = {"action": "save", "name": "coding", "primary": {"provider": FAKE_PROVIDER, "model": "gpt-6-luna"},
                   "fallbacks": [{"provider": "opencode_go", "model": "mimo-v2.6-flash"}],
                   "fallback_triggers": ["exhausted"], "fallback_authorized": True,
                   "fallback_confirmed_providers": ["opencode_go"], "phase_routing": phase}
        status, saved, _ = self.harness.request("POST", "/owner/api/alias", body=payload)
        self.assertEqual(status, 200, saved)
        self.assertEqual(self.harness.config.snapshot().aliases["coding"].phase_routing.routes["plan"].route.model, "gpt-6-luna")

    def test_owner_state_warns_about_missing_fallback_trigger_coverage(self) -> None:
        self._set_alias(authorized=True, confirmed=["opencode_go"], triggers=["exhausted"])
        status, state, _ = self.harness.request("GET", "/owner/api/state")
        self.assertEqual(status, 200)
        state_obj: Any = state
        self.assertIsInstance(state_obj, dict)
        warnings = state_obj.get("fallback_policy_warnings")
        self.assertIsInstance(warnings, list)
        combined = " ".join(str(item) for item in warnings)
        self.assertIn("explicit_cooldown", combined)
        self.assertIn("probe_contention", combined)
        self.assertIn("route_ineligible", combined)

    def test_the_owner_api_surfaces_and_saves_the_new_triggers(self) -> None:
        status, state, _ = self.harness.request("GET", "/owner/api/state")
        self.assertEqual(status, 200)
        for trigger in ("route_ineligible", "probe_contention"):
            self.assertIn(trigger, state["fallback_triggers"])
        status, payload, _ = self.harness.request(
            "POST",
            "/owner/api/alias",
            body={
                "action": "save",
                "name": "coding",
                "primary": {"provider": FAKE_PROVIDER, "model": "gpt-6-luna"},
                "fallbacks": [{"provider": "opencode_go", "model": "mimo-v2.6-flash"}],
                "fallback_triggers": ["route_ineligible", "probe_contention"],
                "fallback_authorized": True,
                "fallback_confirmed_providers": ["opencode_go"],
                "cooldown_seconds": 300,
            },
        )
        self.assertEqual(status, 200, payload)
        saved = self.harness.config.snapshot().aliases["coding"]
        self.assertEqual(
            sorted(saved.fallback_triggers), ["probe_contention", "route_ineligible"]
        )


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
