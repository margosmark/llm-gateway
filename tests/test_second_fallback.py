"""The second fallback must actually serve when the first two routes are down.

The fleet-wide 503 incident (2026-10-23): the primary's circuits opened
provider-wide, the alias's single fallback was rate-limited in the same burst,
the eligible set emptied, and every worker received 503 ``no eligible route``
at once. This file proves the fix the owner approved, behaviourally - a
critical alias carries a second fallback and that route serves the request
when both the primary and the first fallback are benched:

* arm the circuits **as production does** - through ``CircuitStore`` at both
  the route and the provider scope - then send a real chat-completions
  request over HTTP;
* the response is 200, served by the second fallback, recorded as a
  fallback, with **both** skipped routes named and reasoned in the ledger;
* a provider-wide cooldown on the fallback's own provider still empties a
  chain whose routes all share that provider - which is exactly the condition
  ``gateway.routes.single_provider_warning`` reports, so the guard's
  rationale is proven here rather than asserted.

No test contacts a real provider: the adapters are the fakes from
``tests.test_end_to_end``.
"""

from __future__ import annotations

import unittest

from gateway.config import Alias, ModelEntry, ProviderState, RouteRef
from gateway.routes import single_provider_warning
from tests.test_end_to_end import FAKE_PROVIDER, GatewayHarness

PRIMARY = (FAKE_PROVIDER, "gpt-6-luna")
FIRST_FALLBACK = ("opencode_go", "mimo-v2.6-flash")
SECOND_FALLBACK = ("opencode_go", "minimax-m3.1-flash")

#: The owner-approved inventory (Mark, 2026-10-25): alias -> (primary, fb1, fb2).
OWNER_INVENTORY = {
    "coding": (
        ("chatgpt_codex", "gpt-6-luna"),
        ("opencode_go", "mimo-v2.6-flash"),
        ("opencode_go", "minimax-m3.1-flash"),
    ),
    "flash": (
        ("opencode_go", "minimax-m3.1-flash"),
        ("opencode_go", "mimo-v2.6-flash"),
        ("chatgpt_codex", "gpt-5.6-terra"),
    ),
    "hermando": (
        ("chatgpt_codex", "gpt-5.6-terra"),
        ("opencode_go", "mimo-v2.6-flash"),
        ("opencode_go", "minimax-m3.1-flash"),
    ),
    "pm": (
        ("chatgpt_codex", "gpt-5.6-terra"),
        ("opencode_go", "mimo-v2.6-flash"),
        ("opencode_go", "minimax-m3.1-flash"),
    ),
}


def inventory_alias(name: str) -> Alias:
    """Build one critical alias exactly as the owner approved it."""
    primary, first, second = OWNER_INVENTORY[name]
    return Alias(
        name=name,
        enabled=True,
        primary=RouteRef(*primary),
        fallbacks=[RouteRef(*first), RouteRef(*second)],
        fallback_triggers=[
            "exhausted",
            "rate_limited",
            "upstream_unavailable",
            "explicit_cooldown",
        ],
        fallback_authorized=True,
        fallback_confirmed_providers=["chatgpt_codex", "opencode_go"],
        cooldown_seconds=300,
    )


class SecondFallbackTests(unittest.TestCase):
    """Arm the primary and the first fallback; the second fallback serves."""

    def setUp(self) -> None:
        self.harness = GatewayHarness()
        self.addCleanup(self.harness.close)
        with self.harness.config.mutate() as config:
            config.providers["opencode_go"] = ProviderState(
                provider_id="opencode_go",
                kind="opencode_go",
                state="connected",
                models={
                    model_id: ModelEntry(
                        model_id=model_id,
                        enabled=True,
                        dialect="openai_chat",
                        source="discovered",
                        supports_image_input=True,
                    )
                    for model_id in (FIRST_FALLBACK[1], SECOND_FALLBACK[1])
                },
            )
            # The alias advertises image input only if a route accepts images;
            # every route here does, so an image request must survive the outage.
            config.providers[FAKE_PROVIDER].models["gpt-6-luna"].supports_image_input = True
            config.aliases["coding"] = inventory_alias("coding")

    def _events(self):
        return self.harness.router._ledger.recent()

    def _arm_both_benched(self, *, primary_provider_wide: bool = False) -> None:
        """Open the primary's circuit and the first fallback's circuit."""
        circuits = self.harness.router.circuits
        if primary_provider_wide:
            # The incident's real shape: an exhausted account opens the whole
            # provider (`chatgpt_codex/*`), not just one route.
            circuits.set(
                FAKE_PROVIDER,
                7200,
                reason="the provider reported that this account's allowance is exhausted",
                error_class="exhausted",
            )
        else:
            circuits.set(
                FAKE_PROVIDER,
                900,
                reason="the provider reported that this account's allowance is exhausted",
                model=PRIMARY[1],
                error_class="exhausted",
            )
        circuits.set(
            FIRST_FALLBACK[0],
            300,
            reason="the provider rate-limited this route",
            model=FIRST_FALLBACK[1],
            error_class="rate_limited",
        )

    def _chat(self, body_extra: dict | None = None):
        body = {"model": "coding", "messages": [{"role": "user", "content": "hi"}]}
        body.update(body_extra or {})
        return self.harness.request("POST", "/v1/chat/completions", body=body)

    def test_the_second_fallback_serves_when_primary_and_first_fallback_are_benched(self) -> None:
        self._arm_both_benched()

        status, payload, _ = self._chat()
        self.assertEqual(status, 200, payload)

        event = self._events()[0]
        self.assertEqual(event["serving_provider"], SECOND_FALLBACK[0])
        self.assertEqual(event["serving_model"], SECOND_FALLBACK[1])
        self.assertTrue(
            event["fallback_used"],
            "a request served by the second fallback must be recorded as a fallback",
        )
        self.assertIn("primary route was skipped", event["fallback_reason"])

        skipped = {
            (row["provider"], row["model"]): row["reason"] for row in event["skipped"]
        }
        self.assertEqual(len(event["skipped"]), 2, event["skipped"])
        self.assertIn(PRIMARY, skipped)
        self.assertIn(FIRST_FALLBACK, skipped)
        for route, reason in skipped.items():
            self.assertTrue(reason, f"skipped route {route} must carry a reason")
            self.assertIn("cooldown", reason)

        # The alias never reaches the provider: the upstream call carries the
        # second fallback's own model id, and the benched providers were never dialled.
        upstream = self.harness.registry.get(SECOND_FALLBACK[0])
        self.assertEqual(len(upstream.calls), 1)
        self.assertEqual(upstream.calls[0].model, SECOND_FALLBACK[1])
        self.assertEqual(self.harness.adapter.calls, [])

    def test_a_provider_wide_primary_cooldown_still_leaves_the_second_fallback_serving(self) -> None:
        # The fleet incident: `chatgpt_codex/*` armed provider-wide while the
        # one fallback was rate-limited. The second fallback is on another
        # provider, so it must still serve.
        self._arm_both_benched(primary_provider_wide=True)

        status, payload, _ = self._chat()
        self.assertEqual(status, 200, payload)

        event = self._events()[0]
        self.assertTrue(event["fallback_used"])
        self.assertEqual(event["serving_model"], SECOND_FALLBACK[1])
        skipped = {row["provider"] for row in event["skipped"]}
        self.assertEqual(skipped, {PRIMARY[0], FIRST_FALLBACK[0]})
        primary_reason = next(
            row["reason"] for row in event["skipped"] if row["provider"] == PRIMARY[0]
        )
        self.assertIn(f"provider {PRIMARY[0]}", primary_reason)

    def test_an_image_request_is_served_by_the_second_fallback_during_the_outage(self) -> None:
        # Every route of the alias is image-capable, so the outage must not
        # turn a supported image request into a capability refusal.
        self._arm_both_benched()

        status, payload, _ = self._chat(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "look"},
                            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}},
                        ],
                    }
                ]
            }
        )
        self.assertEqual(status, 200, payload)
        event = self._events()[0]
        self.assertEqual(event["serving_model"], SECOND_FALLBACK[1])

    def test_a_provider_wide_cooldown_still_empties_a_chain_that_shares_one_provider(self) -> None:
        # The honest boundary: two routes on the SAME provider are not depth.
        # A provider-wide cooldown on opencode_go takes out both opencode_go
        # routes and the alias returns 503 - the exact condition the config
        # warning reports, so the guard is proven rather than asserted.
        circuits = self.harness.router.circuits
        circuits.set(
            FAKE_PROVIDER,
            900,
            reason="the provider reported that this account's allowance is exhausted",
            model=PRIMARY[1],
            error_class="exhausted",
        )
        circuits.set(
            SECOND_FALLBACK[0],
            7200,
            reason="the provider rate-limited this route",
            error_class="rate_limited",
        )

        status, payload, _ = self._chat()
        self.assertEqual(status, 503, payload)
        self.assertEqual(payload["error"]["type"], "no_eligible_route")
        self.assertIn("no eligible route", payload["error"]["message"])

    def test_the_second_fallback_provider_is_authorised_for_every_critical_alias(self) -> None:
        # Authorization surface: a fallback may only run to a provider the
        # owner confirmed. The second fallback's provider must be in
        # fallback_confirmed_providers for every critical alias, and no
        # critical alias may be left spanning a single provider.
        for name in OWNER_INVENTORY:
            with self.subTest(alias=name):
                alias = inventory_alias(name)
                primary, first, second = OWNER_INVENTORY[name]
                self.assertEqual((alias.primary.provider, alias.primary.model), primary)
                self.assertEqual(
                    [(route.provider, route.model) for route in alias.fallbacks],
                    [first, second],
                )
                self.assertIn(second[0], alias.fallback_confirmed_providers)
                providers = {ref.provider for ref in alias.all_routes()}
                self.assertGreaterEqual(len(providers), 2, alias.all_routes())
                self.assertIsNone(single_provider_warning(alias))


class SameProviderDepthTests(unittest.TestCase):
    """A same-provider second fallback is what the warning exists for."""

    def setUp(self) -> None:
        self.harness = GatewayHarness()
        self.addCleanup(self.harness.close)
        with self.harness.config.mutate() as config:
            config.providers["opencode_go"] = ProviderState(
                provider_id="opencode_go",
                kind="opencode_go",
                state="connected",
                models={
                    model_id: ModelEntry(
                        model_id=model_id,
                        enabled=True,
                        dialect="openai_chat",
                        source="discovered",
                    )
                    for model_id in (FIRST_FALLBACK[1], SECOND_FALLBACK[1])
                },
            )
            config.aliases["coding"] = inventory_alias("coding")

    def test_a_one_provider_chain_is_reported_by_the_state_surface(self) -> None:
        with self.harness.config.mutate() as config:
            alias = config.aliases["coding"]
            # Depth without a second provider: a fallback chain whose every
            # route - primary included - sits on opencode_go. It is servable,
            # so it must be kept, but one provider-wide cooldown empties it.
            alias.primary = RouteRef("opencode_go", SECOND_FALLBACK[1])
            alias.fallbacks = [RouteRef("opencode_go", FIRST_FALLBACK[1])]

        status, state, _ = self.harness.request("GET", "/owner/api/state")
        self.assertEqual(status, 200)
        reported = {a["name"]: a.get("route_warning") for a in state["aliases"]}
        warning = reported["coding"]
        self.assertTrue(warning, "a single-provider chain must be reported loudly")
        self.assertIn("opencode_go", warning)
        self.assertIn("503", warning)
        self.assertIn("second provider", warning)


if __name__ == "__main__":
    unittest.main()
