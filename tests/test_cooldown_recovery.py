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

import threading
import unittest

from gateway.circuits import EXHAUSTED_MAX_SECONDS, CircuitStore
from tests.test_circuits import _Clock
from tests.test_end_to_end import FAKE_PROVIDER, FakeAdapter, GatewayHarness
from tests.test_probe_claim import BlockingFakeAdapter


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

        # The probe claim moved to the invocation boundary (R42/R43): only a
        # request that actually reaches the provider holds it. Drive a real
        # in-flight probe with a blocking adapter so the concurrent window is
        # deterministic without sleeping.
        entered, release = threading.Event(), threading.Event()
        blocking = BlockingFakeAdapter(FAKE_PROVIDER, entered=entered, release=release)
        self.harness.registry.register(blocking)

        first: dict = {}

        def probe_request() -> None:
            first["result"] = self._request()

        worker = threading.Thread(target=probe_request, daemon=True)
        worker.start()
        self.assertTrue(entered.wait(timeout=10), "the probe never reached the provider")

        # While the recovery probe is genuinely running, a concurrent request
        # must not reach the provider and must be told why.
        alias = self.harness.router.resolve_alias("coding")
        concurrent = self.harness.router.candidates(alias)
        excluded = [c for c in concurrent if not c.eligible]
        self.assertTrue(excluded, "the held route must be excluded while the probe is live")
        self.assertTrue(
            any("probe" in (c.excluded_reason or "") for c in excluded),
            f"the skip reason must name the live probe: {[c.excluded_reason for c in excluded]}",
        )

        release.set()
        worker.join(timeout=10)
        self.assertFalse(worker.is_alive(), "the probing request never finished")
        self.assertEqual(first["result"][0], 200)
        # Exactly one invocation reached the primary across both requests:
        # the probe itself.
        self.assertEqual(len(blocking.calls), 1)


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
        self.assertGreater(policy["rate_limited_max_seconds"], 0)
        self.assertEqual(
            policy["transient_max_seconds"], 3600,
            "the default transient cap must be reported so the owner can see it",
        )
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


class CooldownClearScopeTests(unittest.TestCase):
    """The three clear scopes, and the refusal of an unnamed mass clear."""

    def setUp(self) -> None:
        self.harness = GatewayHarness()
        self.addCleanup(self.harness.close)
        self.circuits = self.harness.router.circuits

    def _seed(self) -> None:
        # Four circuits: a provider-wide and a route hold on the fake
        # provider, and the same pair on a second provider, so every scope
        # has something to remove and something to leave alone.
        self.circuits.record_failure(FAKE_PROVIDER, "gpt-6-luna", "exhausted", base_seconds=300)
        self.circuits.record_failure(
            "opencode_go", "mimo-v2.6-flash", "rate_limited", base_seconds=300, retry_after=600
        )
        self.circuits.record_failure("opencode_go", "mimo-v2.6-flash", "exhausted", base_seconds=300)
        self.assertEqual(len(self.circuits.details()), 4, "the fixture must hold four circuits")

    def _clear(self, **fields):
        body = {"action": "clear_cooldowns"}
        body.update(fields)
        return self.harness.request("POST", "/owner/api/provider", body=body)

    def _keys(self) -> set[str]:
        return set(self.circuits.details())

    def test_the_route_scope_clears_one_route_and_leaves_the_others(self) -> None:
        self._seed()
        status, payload, _headers = self._clear(
            provider_id=FAKE_PROVIDER, model="gpt-6-luna", scope="route"
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["scope"], "route")
        self.assertEqual(payload["keys"], [f"{FAKE_PROVIDER}/gpt-6-luna"])
        self.assertEqual(payload["cleared_count"], 1)
        self.assertEqual(
            self._keys(),
            {f"{FAKE_PROVIDER}/*", "opencode_go/*", "opencode_go/mimo-v2.6-flash"},
            "a route clear must leave every other circuit held",
        )

    def test_the_provider_scope_clears_its_whole_provider_only(self) -> None:
        self._seed()
        for fields in (
            {"provider_id": FAKE_PROVIDER},
            {"provider_id": FAKE_PROVIDER, "scope": "provider"},
        ):
            with self.subTest(fields=fields):
                status, payload, _headers = self._clear(**fields)
                self.assertEqual(status, 200, payload)
                self.assertEqual(payload["scope"], "provider")
                self.assertEqual(payload["provider"], FAKE_PROVIDER)
                self.assertEqual(payload["keys"], [f"{FAKE_PROVIDER}/*", f"{FAKE_PROVIDER}/gpt-6-luna"])
                self.assertEqual(
                    self._keys(),
                    {"opencode_go/*", "opencode_go/mimo-v2.6-flash"},
                    "another provider's circuits must be left alone",
                )
                self._seed()  # restore the four circuits for the next pass

    def test_the_all_scope_clears_every_circuit_and_says_so(self) -> None:
        self._seed()
        status, payload, _headers = self._clear(scope="all")
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["scope"], "all")
        self.assertIsNone(payload["provider"], "an all clear is addressed to no single provider")
        self.assertEqual(payload["cleared_count"], 4)
        self.assertEqual(
            set(payload["keys"]),
            {f"{FAKE_PROVIDER}/*", f"{FAKE_PROVIDER}/gpt-6-luna", "opencode_go/*", "opencode_go/mimo-v2.6-flash"},
        )
        self.assertEqual(self._keys(), set(), "the all scope must empty the store")

    def test_no_defaulted_field_can_reach_an_all_clear(self) -> None:
        self._seed()
        # The default scope is the provider one: a payload that never says
        # "all" must leave the other provider untouched.
        status, payload, _headers = self._clear(provider_id=FAKE_PROVIDER)
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["scope"], "provider")
        self.assertIn("opencode_go/*", self._keys())
        self._seed()  # back to four circuits, so every refusal below can prove it cleared nothing

        # No provider at all: the unknown-provider refusal, nothing cleared.
        status, payload, _headers = self._clear()
        self.assertEqual(status, 404, payload)
        self.assertEqual(payload["error"]["type"], "unknown_provider")
        self.assertEqual(len(self._keys()), 4)

        # A scope that is not one of the three is refused, not defaulted.
        status, payload, _headers = self._clear(provider_id=FAKE_PROVIDER, scope="everything")
        self.assertEqual(status, 400, payload)
        self.assertEqual(payload["error"]["type"], "invalid_scope")
        self.assertEqual(len(self._keys()), 4)

        # "all" with a route named is contradictory and refused.
        status, payload, _headers = self._clear(scope="all", model="gpt-6-luna")
        self.assertEqual(status, 400, payload)
        self.assertEqual(payload["error"]["type"], "invalid_scope")
        self.assertEqual(len(self._keys()), 4)

        # An unknown provider is refused even when the payload says "all".
        status, payload, _headers = self._clear(scope="all", provider_id="no_such_provider")
        self.assertEqual(status, 404, payload)
        self.assertEqual(payload["error"]["type"], "unknown_provider")
        self.assertEqual(len(self._keys()), 4)

    def test_a_route_scope_without_a_model_is_refused(self) -> None:
        self._seed()
        status, payload, _headers = self._clear(provider_id=FAKE_PROVIDER, scope="route")
        self.assertEqual(status, 400, payload)
        self.assertEqual(payload["error"]["type"], "invalid_scope")
        self.assertEqual(len(self._keys()), 4, "a refused clear must remove nothing")

    def test_every_scope_reports_metadata_and_never_a_secret(self) -> None:
        import json as _json

        for fields in (
            {"provider_id": FAKE_PROVIDER},
            {"provider_id": FAKE_PROVIDER, "model": "gpt-6-luna", "scope": "route"},
            {"scope": "all"},
        ):
            with self.subTest(fields=fields):
                self._seed()
                status, payload, _headers = self._clear(**fields)
                self.assertEqual(status, 200, payload)
                self.assertEqual(payload["cleared_count"], len(payload["cleared"]))
                self.assertEqual(payload["keys"], [entry["key"] for entry in payload["cleared"]])
                raw = _json.dumps(payload)
                for field in ("token", "api_key", "credential", "secret", "probe_owner", "prompt"):
                    self.assertNotIn(field, raw)
                for entry in payload["cleared"]:
                    for field in ("key", "provider", "error_class", "failures", "reason", "until"):
                        self.assertIn(field, entry)

    def test_the_state_table_refreshes_after_a_clear(self) -> None:
        # cooldown_details is the table's data source: the console must see
        # the post-clear truth on its next state fetch.
        self._seed()
        status, _payload, _headers = self._clear(
            provider_id=FAKE_PROVIDER, model="gpt-6-luna", scope="route"
        )
        self.assertEqual(status, 200)
        status, state, _headers = self.harness.request("GET", "/owner/api/state")
        self.assertEqual(status, 200)
        details = state["cooldown_details"]
        self.assertNotIn(f"{FAKE_PROVIDER}/gpt-6-luna", details)
        self.assertEqual(
            set(details), {f"{FAKE_PROVIDER}/*", "opencode_go/*", "opencode_go/mimo-v2.6-flash"}
        )

        status, _payload, _headers = self._clear(scope="all")
        self.assertEqual(status, 200)
        status, state, _headers = self.harness.request("GET", "/owner/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(state["cooldown_details"], {})

    def test_an_unknown_provider_is_still_refused_on_the_default_scope(self) -> None:
        self._seed()
        status, payload, _headers = self._clear(provider_id="no_such_provider")
        self.assertEqual(status, 404, payload)
        self.assertEqual(payload["error"]["type"], "unknown_provider")
        self.assertEqual(len(self._keys()), 4)


if __name__ == "__main__":
    unittest.main()
