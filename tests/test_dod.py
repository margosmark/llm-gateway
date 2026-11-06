"""The definition-of-done walkthrough, automated.

The charter defines thirteen steps. Three of them need the owner's own
account - the device-code sign-in, the API key enrolment, and the live
provider tests - and are performed by the owner in their own browser.

This test walks the **gateway side** of all thirteen steps against a fake
provider, so that everything the gateway is responsible for is verified
before the owner is asked to do anything.
"""

from __future__ import annotations

import json
import unittest

from gateway.config import ModelEntry, ProviderState, RouteRef
from tests.test_end_to_end import FAKE_PROVIDER, FakeAdapter, GatewayHarness


class DefinitionOfDoneTests(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = GatewayHarness()
        self.addCleanup(self.harness.close)
        self.cookies: dict[str, str] = {}

    # --- helpers ---------------------------------------------------------

    def _login(self) -> None:
        self.cookies = {}

    def _owner(self, path: str, body: dict) -> tuple[int, object]:
        """Configuration changes happen directly on the trusted LAN console."""
        return self.harness.request(
            "POST",
            "/owner" + path,
            body=body,
            headers=self.cookies,
        )

    # --- the walkthrough --------------------------------------------------

    def test_step_1_the_browser_ui_opens(self) -> None:
        status, payload, _ = self.harness.request("GET", "/owner/", raw=True)
        self.assertEqual(status, 200)
        self.assertIn(b"Personal LLM Gateway", payload)

    def test_step_2_the_zero_state_is_understandable(self) -> None:
        status, payload, _ = self.harness.request("GET", "/owner/api/status")
        self.assertEqual(status, 200)
        self.assertFalse(payload["initialized"])
        checklist = payload["checklist"]
        self.assertEqual(len(checklist), 7)
        self.assertEqual(checklist[0]["title"], "Connect a provider")
        self.assertEqual(checklist[-1]["title"], "Use it from a LAN client")
        # No migration language on a fresh install.
        self.assertNotIn("migration", json.dumps(payload).lower())

    def test_steps_3_and_4_the_provider_surfaces_are_ready(self) -> None:
        """The owner performs these; the gateway must be ready for them."""
        self._login()
        status, payload, _ = self.harness.request("GET", "/owner/api/state", headers=self.cookies)
        self.assertEqual(status, 200)
        providers = {p["provider_id"]: p for p in payload["providers"]}
        self.assertIn("chatgpt_codex", providers)
        self.assertIn("opencode_go", providers)
        # The device-code flow must state its prerequisite and its warning.
        self.assertIn("security settings", payload["device_code"]["prerequisite"])
        self.assertIn("only at the official OpenAI URL", payload["device_code"]["anti_phishing"])

        status, payload, _ = self._owner(
            "/api/provider",
            {"action": "enroll_key", "provider_id": "opencode_go", "api_key": "test-key"},
        )
        self.assertEqual(status, 200)
        # The key is never echoed back.
        self.assertNotIn("test-key", json.dumps(payload))

    def test_step_5_each_provider_is_tested_independently(self) -> None:
        self._login()
        status, payload, _ = self._owner(
            "/api/provider", {"action": "test", "provider_id": FAKE_PROVIDER}
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])

    def test_step_6_model_ids_are_discovered_or_entered(self) -> None:
        self._login()
        status, payload, _ = self._owner(
            "/api/provider", {"action": "refresh", "provider_id": FAKE_PROVIDER}
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["model_count"], 1)
        # Discovery must not publish anything.
        status, models, _ = self.harness.request("GET", "/v1/models")
        self.assertEqual(models["data"], [])

    def test_step_7_models_are_explicitly_enabled(self) -> None:
        self._login()
        status, _, _ = self._owner(
            "/api/provider", {"action": "refresh", "provider_id": FAKE_PROVIDER}
        )
        self.assertEqual(status, 200)
        status, payload, _ = self._owner(
            "/api/model",
            {"action": "enable", "provider_id": FAKE_PROVIDER, "model_id": "gpt-6-luna"},
        )
        self.assertEqual(status, 200)
        # Still not published: enabling a model is not publishing an alias.
        status, models, _ = self.harness.request("GET", "/v1/models")
        self.assertEqual(models["data"], [])

    def test_steps_8_and_9_an_alias_with_a_primary_route(self) -> None:
        self._login()
        status, _, _ = self._owner(
            "/api/provider", {"action": "refresh", "provider_id": FAKE_PROVIDER}
        )
        self._owner(
            "/api/model",
            {"action": "enable", "provider_id": FAKE_PROVIDER, "model_id": "gpt-6-luna"},
        )
        status, payload, _ = self._owner(
            "/api/alias",
            {
                "action": "save",
                "name": "coding",
                "primary": {"provider": FAKE_PROVIDER, "model": "gpt-6-luna"},
                "fallbacks": [],
                "fallback_triggers": [],
                "fallback_authorized": False,
                "cooldown_seconds": 300,
            },
        )
        self.assertEqual(status, 200)

    def test_step_10_the_alias_is_published(self) -> None:
        self._prepare_published_alias()
        status, models, _ = self.harness.request("GET", "/v1/models")
        self.assertEqual(status, 200)
        self.assertEqual([entry["id"] for entry in models["data"]], ["coding"])

    def test_step_11_a_test_completion_succeeds(self) -> None:
        self._prepare_published_alias()
        status, payload, _ = self.harness.request(
            "POST",
            "/v1/chat/completions",
            body={"model": "coding", "messages": [{"role": "user", "content": "hello"}]},
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(self.harness.adapter.calls), 1)

    def test_step_12_the_serving_provider_is_visible_without_content(self) -> None:
        self._prepare_published_alias()
        self.harness.request(
            "POST",
            "/v1/chat/completions",
            body={"model": "coding", "messages": [{"role": "user", "content": "a secret prompt"}]},
        )
        events = self.harness.router._ledger.recent()
        self.assertEqual(events[0]["serving_provider"], FAKE_PROVIDER)
        self.assertEqual(events[0]["serving_model"], "gpt-6-luna")
        self.assertEqual(events[0]["requested_alias"], "coding")
        # The prompt is nowhere.
        self.assertNotIn("a secret prompt", json.dumps(events))
        for key in ("prompt", "messages", "content", "body"):
            self.assertNotIn(key, events[0])

    def test_step_13_the_deployed_release_is_visible(self) -> None:
        status, payload, _ = self.harness.request("GET", "/version")
        self.assertEqual(status, 200)
        self.assertEqual(
            sorted(payload.keys()),
            ["image_revision", "mcp_contract_version", "service", "source_revision", "source_tree"],
        )
        self.assertEqual(payload["source_revision"], "testcommit")
        self.assertTrue(payload["image_revision"].startswith("sha256:"))

    # --- fixture ----------------------------------------------------------

    def _prepare_published_alias(self) -> None:
        self._login()
        self._owner("/api/provider", {"action": "refresh", "provider_id": FAKE_PROVIDER})
        self._owner(
            "/api/model",
            {"action": "enable", "provider_id": FAKE_PROVIDER, "model_id": "gpt-6-luna"},
        )
        self._owner(
            "/api/alias",
            {
                "action": "save",
                "name": "coding",
                "primary": {"provider": FAKE_PROVIDER, "model": "gpt-6-luna"},
                "fallbacks": [],
                "fallback_triggers": [],
                "fallback_authorized": False,
                "cooldown_seconds": 300,
            },
        )
        status, _, _ = self._owner(
            "/api/alias", {"action": "publish", "name": "coding", "enabled": True}
        )
        self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main()
