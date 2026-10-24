"""Reset-aware recovery for a provider-wide exhaustion circuit.

The incident this covers: a ChatGPT subscription window reopened, but the
gateway held the provider in a multi-hour local circuit, so every
ChatGPT-primary alias kept falling back and the reopened capacity was
unused. The properties under test:

* a confirmed exhaustion is held only within the bounded window;
* when that window ends, exactly one recovery probe reaches the provider
  and a concurrent request gets a truthful skip instead of a duplicate
  probe;
* a successful probe clears the provider and route circuits and the next
  request is served by the configured primary with no fallback;
* a failed probe re-arms within the cap and keeps the failure reason.
"""

from __future__ import annotations

import unittest

from gateway.circuits import EXHAUSTED_MAX_SECONDS, CircuitStore
from tests.test_circuits import _Clock
from tests.test_end_to_end import FAKE_PROVIDER, FakeAdapter, GatewayHarness


class RecoveryProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = GatewayHarness()
        self.addCleanup(self.harness.close)
        self.clock = _Clock()
        self.harness.router._circuits = CircuitStore(clock=self.clock)
        from gateway.config import Alias, ModelEntry, ProviderState, RouteRef

        with self.harness.config.mutate() as config:
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
                fallbacks=[RouteRef("opencode_go", "mimo-v2.6-flash")],
                fallback_triggers=["exhausted", "rate_limited", "upstream_unavailable"],
                fallback_authorized=True,
                fallback_confirmed_providers=["opencode_go"],
            )

    def _request(self):
        return self.harness.request(
            "POST",
            "/v1/chat/completions",
            body={"model": "coding", "messages": [{"role": "user", "content": "hi"}]},
        )

    def _exhaust_the_primary(self) -> None:
        from gateway.adapters.base import AdapterError

        self.harness.registry.register(
            FakeAdapter(fail_with=AdapterError("exhausted", provider_id=FAKE_PROVIDER))
        )
        status, _payload, _headers = self._request()
        self.assertEqual(status, 200, "the fallback must serve while the primary is exhausted")

    def test_a_successful_recovery_probe_restores_the_primary(self) -> None:
        self._exhaust_the_primary()
        event = self.harness.router._ledger.recent()[0]
        self.assertTrue(event["fallback_used"], "the fallback must have served")

        # The bounded window passes and the provider answers again.
        self.clock.advance(EXHAUSTED_MAX_SECONDS + 10)
        self.harness.registry.register(FakeAdapter())
        self._request()

        event = self.harness.router._ledger.recent()[0]
        self.assertEqual(event["serving_provider"], FAKE_PROVIDER)
        self.assertFalse(
            event["fallback_used"],
            "after a successful recovery probe the configured primary serves without fallback",
        )
        circuits = self.harness.router.circuits
        self.assertEqual(circuits.remaining(FAKE_PROVIDER), 0, "the provider circuit must be cleared")
        self.assertEqual(
            circuits.remaining(FAKE_PROVIDER, "gpt-6-luna"),
            0,
            "the route circuit must be cleared",
        )

    def test_a_failed_recovery_probe_rearms_within_the_cap(self) -> None:
        self._exhaust_the_primary()
        self.clock.advance(EXHAUSTED_MAX_SECONDS + 10)
        self._exhaust_the_primary()  # the probe fails; the fallback serves again

        circuits = self.harness.router.circuits
        self.assertGreater(circuits.remaining(FAKE_PROVIDER), 0, "the provider must be re-armed")
        self.assertLessEqual(circuits.remaining(FAKE_PROVIDER), EXHAUSTED_MAX_SECONDS)
        details = circuits.details()[f"{FAKE_PROVIDER}/*"]
        self.assertEqual(details["error_class"], "exhausted")
        self.assertEqual(details["failures"], 2)
        self.assertIn("2 consecutive failures", details["reason"])

    def test_a_concurrent_request_gets_a_truthful_skip_not_a_duplicate_probe(self) -> None:
        self._exhaust_the_primary()
        self.clock.advance(EXHAUSTED_MAX_SECONDS + 10)
        self.harness.registry.register(FakeAdapter())

        alias = self.harness.router.resolve_alias("coding")
        # The first candidate build claims the single recovery probe...
        self.harness.router.candidates(alias)
        # ...so a concurrent request must not reach the provider and must
        # be told why.
        concurrent = self.harness.router.candidates(alias)
        excluded = [c for c in concurrent if not c.eligible]
        self.assertTrue(excluded, "the held route must be excluded while the probe is live")
        self.assertTrue(
            any("probe" in (c.excluded_reason or "") for c in excluded),
            f"the skip reason must name the live probe: {[c.excluded_reason for c in excluded]}",
        )


class OwnerRecoveryActionTests(unittest.TestCase):
    """The deliberate owner action that clears one provider's circuits."""

    def setUp(self) -> None:
        self.harness = GatewayHarness()
        self.addCleanup(self.harness.close)
        from gateway.config import Alias, ModelEntry, ProviderState, RouteRef

        with self.harness.config.mutate() as config:
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
                fallbacks=[RouteRef("opencode_go", "mimo-v2.6-flash")],
                fallback_triggers=["exhausted", "rate_limited", "upstream_unavailable"],
                fallback_authorized=True,
                fallback_confirmed_providers=["opencode_go"],
            )

    def _seed_circuits(self) -> None:
        circuits = self.harness.router.circuits
        circuits.record_failure(FAKE_PROVIDER, "gpt-6-luna", "exhausted", base_seconds=300)
        circuits.record_failure(
            "opencode_go", "mimo-v2.6-flash", "rate_limited", base_seconds=300, retry_after=600
        )

    def test_the_clear_action_is_refused_without_the_canonical_origin(self) -> None:
        self._seed_circuits()
        status, payload, _headers = self.harness.request(
            "POST",
            "/owner/api/provider",
            body={"action": "clear_cooldowns", "provider_id": FAKE_PROVIDER},
            headers={"Origin": "http://evil.example.com"},
        )
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"]["type"], "origin_mismatch")
        self.assertGreater(
            self.harness.router.circuits.remaining(FAKE_PROVIDER, "gpt-6-luna"),
            0,
            "a refused action must clear nothing",
        )

    def test_the_clear_action_clears_only_the_named_provider(self) -> None:
        import json as _json

        self._seed_circuits()
        status, payload, _headers = self.harness.request(
            "POST",
            "/owner/api/provider",
            body={"action": "clear_cooldowns", "provider_id": FAKE_PROVIDER},
        )
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["provider"], FAKE_PROVIDER)
        self.assertEqual(
            {entry["provider"] for entry in payload["cleared"]},
            {FAKE_PROVIDER},
            "only the named provider's entries may be reported as cleared",
        )
        self.assertEqual(payload["cleared_count"], len(payload["cleared"]))
        circuits = self.harness.router.circuits
        self.assertEqual(circuits.remaining(FAKE_PROVIDER), 0)
        self.assertEqual(circuits.remaining(FAKE_PROVIDER, "gpt-6-luna"), 0)
        self.assertGreater(
            circuits.remaining("opencode_go", "mimo-v2.6-flash"),
            0,
            "another provider's circuits must be left alone",
        )
        raw = _json.dumps(payload)
        for field in ("token", "api_key", "credential", "secret"):
            self.assertNotIn(field, raw)

    def test_the_clear_action_can_run_an_explicit_recovery_probe(self) -> None:
        self._seed_circuits()
        status, payload, _headers = self.harness.request(
            "POST",
            "/owner/api/provider",
            body={
                "action": "clear_cooldowns",
                "provider_id": FAKE_PROVIDER,
                "probe_alias": "coding",
            },
        )
        self.assertEqual(status, 200, payload)
        probe = payload["probe"]
        self.assertTrue(probe["ok"])
        self.assertEqual(probe["serving_provider"], FAKE_PROVIDER)
        self.assertEqual(probe["serving_model"], "gpt-6-luna")
        self.assertFalse(probe["fallback_used"])

    def test_the_clear_action_runs_no_provider_call_without_a_probe_alias(self) -> None:
        from gateway.adapters.base import AdapterError

        self._seed_circuits()
        # A provider that would fail proves no provider call happened
        # unless the owner asked for one explicitly.
        self.harness.registry.register(
            FakeAdapter(fail_with=AdapterError("exhausted", provider_id=FAKE_PROVIDER))
        )
        status, payload, _headers = self.harness.request(
            "POST",
            "/owner/api/provider",
            body={"action": "clear_cooldowns", "provider_id": FAKE_PROVIDER},
        )
        self.assertEqual(status, 200, payload)
        self.assertIsNone(payload.get("probe"))

    def test_the_state_reports_the_recovery_telemetry_and_policy(self) -> None:
        self._seed_circuits()
        status, state, _headers = self.harness.request("GET", "/owner/api/state")
        self.assertEqual(status, 200)
        policy = state["cooldown_policy"]
        self.assertGreater(policy["exhausted_max_seconds"], 0)
        self.assertGreaterEqual(
            policy["exhausted_max_seconds"],
            policy["exhausted_base_seconds"],
            "the cap must not sit below the floor",
        )
        detail = state["cooldown_details"][f"{FAKE_PROVIDER}/*"]
        for field in (
            "opened_at",
            "until",
            "next_probe_at",
            "probe_in_flight",
            "scope",
            "failures",
            "error_class",
            "remaining_seconds",
            "reason",
        ):
            self.assertIn(field, detail)
        self.assertEqual(detail["scope"], "provider")
        self.assertEqual(detail["error_class"], "exhausted")


if __name__ == "__main__":
    unittest.main()
