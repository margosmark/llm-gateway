"""The single-provider config warning: loud, non-fatal, correctly bounded.

An enabled alias whose routes all name one provider *looks* redundant and is
not: circuits open provider-wide as well as route-wide, so one cooldown can
empty the alias's eligible set and hand every caller a 503 at the same moment.
The gateway reports that condition instead of refusing it - an owner with one
live provider has no legal two-provider chain to choose - and the report must
be visible where the owner acts (the save and publish responses) and where the
console reads (owner state), while staying off the public ``/v1`` surfaces,
which must never name providers.
"""

from __future__ import annotations

import unittest

from gateway.config import Alias, ModelEntry, RouteRef
from gateway.routes import single_provider_warning
from tests.test_end_to_end import FAKE_PROVIDER, GatewayHarness


def _alias(**overrides) -> Alias:
    values = {
        "name": "coding",
        "enabled": True,
        "primary": RouteRef(FAKE_PROVIDER, "gpt-6-luna"),
        "fallbacks": [RouteRef(FAKE_PROVIDER, "gpt-5.6-terra")],
        "fallback_authorized": True,
        "fallback_confirmed_providers": [FAKE_PROVIDER],
        "fallback_triggers": ["exhausted"],
    }
    values.update(overrides)
    return Alias(**values)


class SingleProviderWarningUnitTests(unittest.TestCase):
    def test_an_enabled_single_provider_chain_warns(self) -> None:
        warning = single_provider_warning(_alias())
        self.assertIsNotNone(warning)
        self.assertIn(FAKE_PROVIDER, warning)
        # The warning says what breaks and what would fix it.
        self.assertIn("503", warning)
        self.assertIn("second provider", warning)
        self.assertIn("provider-wide", warning)

    def test_a_chain_spanning_two_providers_is_silent(self) -> None:
        alias = _alias(fallbacks=[RouteRef("opencode_go", "mimo-v2.6-flash")])
        self.assertIsNone(single_provider_warning(alias))

    def test_a_disabled_alias_is_silent(self) -> None:
        self.assertIsNone(single_provider_warning(_alias(enabled=False)))

    def test_an_alias_without_a_fallback_chain_is_not_this_warning(self) -> None:
        # The rule is about chains of two or more routes that add depth
        # without adding a provider; a lone primary is a different, already
        # visible console state ("Fallbacks: none"), not this report.
        self.assertIsNone(single_provider_warning(_alias(fallbacks=[])))

    def test_phase_routes_count_toward_the_provider_span(self) -> None:
        from gateway.config import JevInference, PhaseRouting

        alias = _alias()
        alias.phase_routing = PhaseRouting(
            enabled=True,
            routes={},
            default_route=RouteRef("opencode_go", "mimo-v2.6-flash"),
            inference=JevInference(),
        )
        # A phase policy that can select a second provider is real depth.
        self.assertIsNone(single_provider_warning(alias))

    def test_a_missing_alias_is_silent(self) -> None:
        self.assertIsNone(single_provider_warning(None))


class SingleProviderWarningApiTests(unittest.TestCase):
    """The warning on the responses the owner actually sees."""

    def setUp(self) -> None:
        self.harness = GatewayHarness()
        self.addCleanup(self.harness.close)
        with self.harness.config.mutate() as config:
            config.providers[FAKE_PROVIDER].models["gpt-5.6-terra"] = ModelEntry(
                model_id="gpt-5.6-terra",
                enabled=True,
                dialect="openai_responses",
                source="discovered",
            )

    def _save(self, name: str, fallbacks: list[dict], confirmed: list[str]):
        return self.harness.request(
            "POST",
            "/owner/api/alias",
            body={
                "action": "save",
                "name": name,
                "enabled": True,
                "primary": {"provider": FAKE_PROVIDER, "model": "gpt-6-luna"},
                "fallbacks": fallbacks,
                "fallback_triggers": ["exhausted", "rate_limited", "upstream_unavailable"],
                "fallback_authorized": True,
                "fallback_confirmed_providers": confirmed,
                "cooldown_seconds": 300,
            },
        )

    def test_saving_a_single_provider_alias_returns_the_warning(self) -> None:
        status, payload, _ = self._save(
            "single-prov",
            [{"provider": FAKE_PROVIDER, "model": "gpt-5.6-terra"}],
            [FAKE_PROVIDER],
        )
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["ok"], "the save must succeed - the warning is not a refusal")
        self.assertIn("warning", payload)
        self.assertIn(FAKE_PROVIDER, payload["warning"])

    def test_saving_a_two_provider_alias_returns_no_warning(self) -> None:
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
        status, payload, _ = self._save(
            "two-prov",
            [{"provider": "opencode_go", "model": "mimo-v2.6-flash"}],
            ["opencode_go"],
        )
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["ok"])
        self.assertNotIn("warning", payload)

    def test_owner_state_carries_the_warning_beside_the_alias(self) -> None:
        self._save(
            "single-prov",
            [{"provider": FAKE_PROVIDER, "model": "gpt-5.6-terra"}],
            [FAKE_PROVIDER],
        )
        status, state, _ = self.harness.request("GET", "/owner/api/state")
        self.assertEqual(status, 200)
        reported = {alias["name"]: alias.get("route_warning") for alias in state["aliases"]}
        self.assertTrue(reported.get("single-prov"))

    def test_publishing_a_single_provider_alias_reports_it_too(self) -> None:
        status, payload, _ = self._save(
            "single-prov",
            [{"provider": FAKE_PROVIDER, "model": "gpt-5.6-terra"}],
            [FAKE_PROVIDER],
        )
        self.assertEqual(status, 200, payload)
        status, published, _ = self.harness.request(
            "POST",
            "/owner/api/alias",
            body={"action": "publish", "name": "single-prov", "enabled": True},
        )
        self.assertEqual(status, 200, published)
        self.assertTrue(published["ok"])
        self.assertIn("warning", published)

    def test_the_public_surfaces_never_carry_the_warning(self) -> None:
        # The warning names providers. The public catalogue and the models
        # list must stay free of it, exactly like the rest of owner state.
        self._save(
            "single-prov",
            [{"provider": FAKE_PROVIDER, "model": "gpt-5.6-terra"}],
            [FAKE_PROVIDER],
        )
        for path in ("/v1/alias-catalog", "/v1/models"):
            with self.subTest(path=path):
                status, raw, _ = self.harness.request("GET", path, raw=True)
                self.assertEqual(status, 200)
                body = raw.decode("utf-8", "replace")
                self.assertNotIn("route_warning", body)
                self.assertNotIn("every route of alias", body)


if __name__ == "__main__":
    unittest.main()
