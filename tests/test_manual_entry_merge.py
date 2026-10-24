"""A manual model entry must merge, not replace.

Regression cover for **R27**: the `manual` action rebuilt the whole
``ModelEntry`` instead of merging into it, so the provider-published fields
were silently discarded. A manual dialect edit on a discovered
``gpt-6-luna`` took the context limit from 258,400 to **None** — so the
alias's limit became Unknown — and emptied the reasoning levels, so a
reasoning request was dropped as a degradation instead of honoured. The
action still returned ``{"ok": true}``.

This is a documented workflow: the README sends the owner to "enter it
manually and set the dialect" when a model shows an empty dialect. The fix
for a missing dialect must not destroy the rest of the model.
"""

from __future__ import annotations

import unittest

from gateway.config import ModelEntry, ProviderState
from tests.test_end_to_end import FAKE_PROVIDER, GatewayHarness

DISCOVERED = ModelEntry(
    model_id="gpt-6-luna",
    enabled=True,
    dialect="openai_responses",
    source="discovered",
    context_window=272000,
    context_limit=258400,
    reasoning_efforts=["none", "low", "medium", "high", "xhigh", "max"],
    monthly_limit_usd=15.0,
    limits_source="published_estimate",
    limits_observed_at="2026-10-19T00:00:00Z",
    retention_class="30_days",
    supports_streaming=True,
    supports_function_tools=True,
    supports_builtin_tools=True,
    supports_image_input=False,
)


class ManualEntryMergeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = GatewayHarness()
        self.addCleanup(self.harness.close)
        with self.harness.config.mutate() as config:
            config.providers[FAKE_PROVIDER] = ProviderState(
                provider_id=FAKE_PROVIDER,
                kind=FAKE_PROVIDER,
                state="connected",
                models={"gpt-6-luna": DISCOVERED},
            )

    def _manual(self, body: dict):
        return self.harness.request(
            "POST",
            "/owner/api/model",
            body={"action": "manual", "provider_id": FAKE_PROVIDER, "model_id": "gpt-6-luna", **body},
        )

    def _entry(self):
        return self.harness.config.snapshot().providers[FAKE_PROVIDER].models["gpt-6-luna"]

    def test_a_manual_dialect_edit_preserves_the_published_metadata(self) -> None:
        status, payload, _ = self._manual({"dialect": "openai_chat"})
        self.assertEqual(status, 200, payload)

        entry = self._entry()
        self.assertEqual(entry.dialect, "openai_chat", "the dialect the owner supplied")
        self.assertEqual(entry.context_window, 272000)
        self.assertEqual(entry.context_limit, 258400)
        self.assertEqual(
            list(entry.reasoning_efforts),
            ["none", "low", "medium", "high", "xhigh", "max"],
        )
        self.assertEqual(entry.monthly_limit_usd, 15.0)
        self.assertEqual(entry.limits_source, "published_estimate")
        self.assertEqual(entry.limits_observed_at, "2026-10-19T00:00:00Z")
        self.assertEqual(entry.retention_class, "30_days")

    def test_a_manual_dialect_edit_preserves_the_capability_flags(self) -> None:
        self._manual({"dialect": "openai_chat"})
        entry = self._entry()
        self.assertTrue(entry.supports_streaming)
        self.assertTrue(entry.supports_function_tools)
        self.assertTrue(entry.supports_builtin_tools)
        self.assertFalse(entry.supports_image_input)

    def test_a_manual_edit_cannot_clear_a_confirmed_image_capability(self) -> None:
        """A confirmed image capability survives a manual entry.

        The capability is proven against the provider's own contract, so an
        owner correcting a missing dialect or rewriting the notes must not
        silently blank it: the route would stop being selected for images
        while looking healthy in the console. Only the flag is held here -
        ``notes`` is a field the owner explicitly supplies, so replacing it
        is the point of the form, not a defect.
        """
        with self.harness.config.mutate() as config:
            entry = config.providers[FAKE_PROVIDER].models["gpt-6-luna"]
            entry.supports_image_input = True

        for body in (
            {"dialect": "openai_chat"},
            {"notes": "owner note"},
            {"enabled": True},
        ):
            with self.subTest(body=body):
                status, payload, _ = self._manual(body)
                self.assertEqual(status, 200, payload)
                entry = self._entry()
                self.assertTrue(
                    entry.supports_image_input,
                    f"a manual edit {body} must not clear a confirmed image capability",
                )

    def test_a_manual_edit_still_records_the_owner_supplied_fields(self) -> None:
        """The merge preserves capability without ignoring the owner."""
        self._manual({"dialect": "openai_chat", "notes": "owner note"})
        entry = self._entry()
        self.assertEqual(entry.dialect, "openai_chat")
        self.assertEqual(entry.notes, "owner note")
        self.assertEqual(entry.source, "manual")

    def test_a_manual_dialect_edit_preserves_the_enabled_state(self) -> None:
        self._manual({"dialect": "openai_chat"})
        self.assertTrue(self._entry().enabled)

    def test_a_manual_entry_may_explicitly_disable_a_model(self) -> None:
        self._manual({"dialect": "openai_chat", "enabled": False})
        self.assertFalse(self._entry().enabled)

    def test_a_new_manual_model_is_created_and_enabled(self) -> None:
        status, payload, _ = self.harness.request(
            "POST",
            "/owner/api/model",
            body={
                "action": "manual",
                "provider_id": FAKE_PROVIDER,
                "model_id": "brand-new-model",
                "dialect": "openai_chat",
            },
        )
        self.assertEqual(status, 200, payload)
        entry = self.harness.config.snapshot().providers[FAKE_PROVIDER].models["brand-new-model"]
        self.assertTrue(entry.enabled, "a manually entered model must be usable")
        self.assertEqual(entry.dialect, "openai_chat")
        self.assertEqual(entry.source, "manual")
        # A model the provider does not publish has no published facts to keep.
        self.assertIsNone(entry.context_limit)
        self.assertEqual(entry.limits_source, "unknown")

    def test_the_alias_limit_survives_a_manual_dialect_edit(self) -> None:
        """The owner's symptom: the alias's limit became Unknown."""
        with self.harness.config.mutate() as config:
            from gateway.config import Alias, RouteRef

            config.aliases["coding"] = Alias(
                name="coding",
                enabled=True,
                primary=RouteRef(FAKE_PROVIDER, "gpt-6-luna"),
            )
        self._manual({"dialect": "openai_chat"})

        status, payload, _ = self.harness.request("GET", "/owner/api/state")
        self.assertEqual(status, 200)
        alias = [a for a in payload["aliases"] if a["name"] == "coding"][0]
        limit = alias["context_limit"]
        self.assertTrue(limit["known"], "the alias limit must survive a manual dialect edit")
        self.assertEqual(limit["limit"], 258400)


if __name__ == "__main__":
    unittest.main()
