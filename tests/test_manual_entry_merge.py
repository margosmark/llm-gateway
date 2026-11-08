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

The same discipline is covered here from end to end:

* the action reports **what changed**, **what survived**, and every
  **conflict** with a published value and how it was resolved;
* the provenance of each owner-supplied value is kept on the entry, so it
  survives a restart and a later refresh;
* a ``refresh`` merges in the other direction — an owner's edit survives
  while the provider publishes nothing for that field, and is reported as
  superseded when the provider publishes something else;
* the ``enable`` and ``disable`` actions touch only the enabled flag;
* the state the console reads carries the merged entry, its overrides, and
  a limit snapshot that agrees with it;
* an unknown provider, an invalid model id and an unknown dialect are
  refused, with nothing persisted.
"""

from __future__ import annotations

import unittest
from typing import Any

from gateway.config import ModelEntry, ProviderState
from gateway.core.capabilities import ModelInfo
from tests.test_end_to_end import FAKE_PROVIDER, FakeAdapter, GatewayHarness

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

    def test_a_new_manual_model_records_only_what_the_owner_supplied(self) -> None:
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
        self.assertTrue(payload["created"])
        self.assertEqual(payload["changed"], {"dialect": {"from": None, "to": "openai_chat"}})
        entry = self.harness.config.snapshot().providers[FAKE_PROVIDER].models["brand-new-model"]
        self.assertEqual(entry.owner_overrides, {"dialect": {"owner": "openai_chat", "published": None}})
        # "entered manually" is a marker, not a value the owner wrote, so it
        # is not recorded as one and does not shadow a later publication.
        self.assertEqual(entry.notes, "entered manually")

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


    def test_the_response_says_what_changed_and_what_survived(self) -> None:
        """The owner is told what actually happened, not just "ok"."""
        status, payload, _ = self._manual({"dialect": "openai_chat"})
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["created"])
        self.assertEqual(
            payload["changed"],
            {"dialect": {"from": "openai_responses", "to": "openai_chat"}},
        )
        preserved = payload["preserved"]
        self.assertEqual(preserved["context_window"], 272000)
        self.assertEqual(preserved["context_limit"], 258400)
        self.assertEqual(
            preserved["reasoning_efforts"],
            ["none", "low", "medium", "high", "xhigh", "max"],
        )
        self.assertEqual(preserved["monthly_limit_usd"], 15.0)
        self.assertEqual(preserved["limits_source"], "published_estimate")
        self.assertEqual(preserved["limits_observed_at"], "2026-10-19T00:00:00Z")
        self.assertTrue(preserved["supports_builtin_tools"])
        self.assertFalse(preserved["supports_image_input"])

    def test_a_conflicting_dialect_is_reported_and_the_published_value_is_kept(self) -> None:
        """A published fact is never dropped silently."""
        status, payload, _ = self._manual({"dialect": "openai_chat"})
        self.assertEqual(status, 200, payload)
        conflicts = payload["conflicts"]
        self.assertEqual(len(conflicts), 1, payload)
        self.assertEqual(conflicts[0]["field"], "dialect")
        self.assertEqual(conflicts[0]["published"], "openai_responses")
        self.assertEqual(conflicts[0]["owner"], "openai_chat")
        self.assertIn("resolution", conflicts[0])
        # The published value is not lost: it is kept on the entry, so the
        # disagreement survives a restart and the next refresh can report it
        # rather than silently revert it.
        entry = self._entry()
        self.assertEqual(
            entry.owner_overrides["dialect"],
            {"owner": "openai_chat", "published": "openai_responses"},
        )
        self.assertEqual(entry.dialect, "openai_chat", "the owner's explicit value is the effective one")

    def test_an_action_that_changed_nothing_says_so(self) -> None:
        before = self._entry().observed_at
        status, payload, _ = self._manual({})
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["changed"], {}, "nothing changed, and the response says so")
        self.assertEqual(payload["conflicts"], [])
        self.assertFalse(payload["created"])
        # An action that changed nothing also writes nothing.
        self.assertEqual(self._entry().observed_at, before)

    def test_a_value_matching_the_published_one_is_not_a_conflict(self) -> None:
        status, payload, _ = self._manual({"dialect": "openai_responses"})
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["changed"], {})
        self.assertEqual(payload["conflicts"], [])
        self.assertEqual(
            self._entry().owner_overrides["dialect"],
            {"owner": "openai_responses", "published": "openai_responses"},
        )

    def test_the_manual_action_refuses_an_unknown_provider(self) -> None:
        status, payload, _ = self.harness.request(
            "POST",
            "/owner/api/model",
            body={"action": "manual", "provider_id": "not-configured", "model_id": "m-1", "dialect": "openai_chat"},
        )
        self.assertEqual(status, 404, payload)
        self.assertEqual(payload["error"]["type"], "unknown_provider")
        # Nothing is created: an unknown provider id used to be turned into a
        # "manual" provider holding an entry nothing could route.
        self.assertNotIn("not-configured", self.harness.config.snapshot().providers)

    def test_the_manual_action_refuses_an_invalid_model_id(self) -> None:
        status, payload, _ = self.harness.request(
            "POST",
            "/owner/api/model",
            body={"action": "manual", "provider_id": FAKE_PROVIDER, "model_id": "not a model", "dialect": "openai_chat"},
        )
        self.assertEqual(status, 400, payload)
        self.assertEqual(payload["error"]["type"], "invalid_model")
        self.assertEqual(payload["error"]["field"], "model_id")
        self.assertNotIn("not a model", self.harness.config.snapshot().providers[FAKE_PROVIDER].models)

    def test_the_manual_action_refuses_an_unknown_dialect(self) -> None:
        status, payload, _ = self._manual({"dialect": "openai_v9"})
        self.assertEqual(status, 400, payload)
        self.assertEqual(payload["error"]["type"], "invalid_model")
        self.assertEqual(payload["error"]["field"], "dialect")
        self.assertEqual(self._entry().dialect, "openai_responses", "a refused edit is not applied")

    def test_the_console_state_reads_the_merged_entry(self) -> None:
        """The console must not report a limit the model row has lost."""
        self._manual({"dialect": "openai_chat"})
        status, payload, _ = self.harness.request("GET", "/owner/api/state")
        self.assertEqual(status, 200)
        provider = next(p for p in payload["providers"] if p["provider_id"] == FAKE_PROVIDER)
        model = next(m for m in provider["models"] if m["model_id"] == "gpt-6-luna")
        self.assertEqual(model["dialect"], "openai_chat")
        self.assertEqual(model["monthly_limit_usd"], 15.0)
        self.assertEqual(model["limits_source"], "published_estimate")
        self.assertEqual(model["context_limit"], 258400)
        self.assertEqual(
            model["owner_overrides"]["dialect"],
            {"owner": "openai_chat", "published": "openai_responses"},
        )
        # The Status view reads published limits from the snapshot store, so
        # the store is re-published with the merged entry and the two views
        # cannot disagree about whether the limit exists.
        snapshots = [
            row
            for row in payload["limits"]
            if row["provider"] == FAKE_PROVIDER and row["window"] == "monthly:gpt-6-luna"
        ]
        self.assertEqual(len(snapshots), 1, payload["limits"])
        self.assertEqual(snapshots[0]["value"], 15.0)
        self.assertEqual(snapshots[0]["source"], "published_estimate")

    def test_enable_and_disable_report_their_change_and_preserve_the_rest(self) -> None:
        """The same discipline for the other owner action that edits a model."""
        status, payload, _ = self.harness.request(
            "POST",
            "/owner/api/model",
            body={"action": "disable", "provider_id": FAKE_PROVIDER, "model_id": "gpt-6-luna"},
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["changed"], {"enabled": {"from": True, "to": False}})
        self.assertEqual(payload["conflicts"], [])
        self.assertEqual(payload["preserved"]["context_limit"], 258400)
        entry = self._entry()
        self.assertFalse(entry.enabled)
        self.assertEqual(entry.dialect, "openai_responses")
        self.assertEqual(entry.monthly_limit_usd, 15.0)
        self.assertEqual(list(entry.reasoning_efforts), ["none", "low", "medium", "high", "xhigh", "max"])

        status, payload, _ = self.harness.request(
            "POST",
            "/owner/api/model",
            body={"action": "enable", "provider_id": FAKE_PROVIDER, "model_id": "gpt-6-luna"},
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["changed"], {"enabled": {"from": False, "to": True}})
        self.assertTrue(self._entry().enabled)


class ScriptedAdapter(FakeAdapter):
    """A provider that publishes exactly the catalogue it is given."""

    def __init__(self, catalogue: list[ModelInfo], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.catalogue = catalogue

    def list_models(self) -> list:
        self.model_list_calls += 1
        return list(self.catalogue)


def _published(dialect: str, notes: str | None = None) -> ModelInfo:
    return ModelInfo(
        model_id="gpt-6-luna",
        dialect=dialect,
        context_window=272000,
        context_limit=258400,
        reasoning_efforts=("none", "low", "medium", "high", "xhigh", "max"),
        supports_builtin_tools=True,
        retention_class="30_days",
        monthly_limit_usd=15.0,
        limits_source="published_estimate",
        limits_observed_at="2026-10-19T00:00:00Z",
        notes=notes,
    )


class RefreshMergeTests(unittest.TestCase):
    """The same merge discipline applied wherever an owner action edits a
    discovered model - including the refresh that reads the model back."""

    def setUp(self) -> None:
        self.catalogue: list[ModelInfo] = [_published("")]
        self.harness = GatewayHarness(adapter=ScriptedAdapter(self.catalogue))
        self.addCleanup(self.harness.close)
        with self.harness.config.mutate() as config:
            config.providers[FAKE_PROVIDER] = ProviderState(
                provider_id=FAKE_PROVIDER,
                kind=FAKE_PROVIDER,
                state="connected",
                models={"gpt-6-luna": ModelEntry(**DISCOVERED.__dict__)},
            )

    def _entry(self) -> ModelEntry:
        return self.harness.config.snapshot().providers[FAKE_PROVIDER].models["gpt-6-luna"]

    def _edit(self, body: dict) -> tuple[int, dict]:
        return self.harness.request(
            "POST",
            "/owner/api/model",
            body={"action": "manual", "provider_id": FAKE_PROVIDER, "model_id": "gpt-6-luna", **body},
        )

    def _refresh(self) -> tuple[int, dict]:
        return self.harness.request(
            "POST",
            "/owner/api/provider",
            body={"action": "refresh", "provider_id": FAKE_PROVIDER},
        )

    def test_a_refresh_keeps_an_owner_edit_the_provider_does_not_publish(self) -> None:
        """The documented workflow: the model has no dialect, the owner sets one."""
        status, payload, _ = self._edit({"dialect": "openai_chat", "notes": "the owner's note"})
        self.assertEqual(status, 200, payload)
        self.assertEqual(self._entry().dialect, "openai_chat")

        status, payload, _ = self._refresh()
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["owner_overrides_superseded"], [])

        entry = self._entry()
        self.assertEqual(entry.dialect, "openai_chat", "the owner's edit survives a refresh that publishes nothing")
        self.assertEqual(entry.notes, "the owner's note")
        self.assertEqual(entry.owner_overrides["dialect"], {"owner": "openai_chat", "published": None})
        self.assertEqual(entry.owner_overrides["notes"], {"owner": "the owner's note", "published": None})
        self.assertEqual(entry.source, "discovered")
        # Everything the refresh did publish is still the entry's.
        self.assertEqual(entry.context_window, 272000)
        self.assertEqual(entry.context_limit, 258400)
        self.assertEqual(entry.monthly_limit_usd, 15.0)
        self.assertEqual(entry.limits_source, "published_estimate")

    def test_a_refresh_reports_the_override_it_took_back(self) -> None:
        """A published fact the provider now states wins - but never silently."""
        self.catalogue[:] = [_published("openai_responses")]
        status, payload, _ = self._edit({"dialect": "openai_chat"})
        self.assertEqual(status, 200, payload)
        self.assertEqual(self._entry().dialect, "openai_chat")

        status, payload, _ = self._refresh()
        self.assertEqual(status, 200, payload)
        self.assertEqual(
            payload["owner_overrides_superseded"],
            [
                {
                    "model_id": "gpt-6-luna",
                    "field": "dialect",
                    "owner": "openai_chat",
                    "published": "openai_responses",
                    "resolution": "the published value stands; the owner's value was replaced and is reported here",
                }
            ],
        )
        entry = self._entry()
        self.assertEqual(entry.dialect, "openai_responses")
        self.assertEqual(entry.owner_overrides, {}, "the disagreement is settled, so nothing is left to protect")

    def test_a_refresh_keeps_a_model_the_owner_disabled(self) -> None:
        """The enabled flag is an owner preference, not a published fact."""
        status, payload, _ = self.harness.request(
            "POST",
            "/owner/api/model",
            body={"action": "disable", "provider_id": FAKE_PROVIDER, "model_id": "gpt-6-luna"},
        )
        self.assertEqual(status, 200, payload)
        status, payload, _ = self._refresh()
        self.assertEqual(status, 200, payload)
        self.assertFalse(self._entry().enabled)


class OwnerOverridePersistenceTests(unittest.TestCase):
    """The provenance must survive the config round-trip, and be validated."""

    def test_owner_overrides_survive_a_state_reload(self) -> None:
        entry = ModelEntry(
            model_id="gpt-6-luna",
            dialect="openai_chat",
            source="discovered",
            owner_overrides={"dialect": {"owner": "openai_chat", "published": "openai_responses"}},
        )
        reloaded = ModelEntry.from_dict(entry.to_dict())
        self.assertIsNotNone(reloaded)
        self.assertEqual(
            reloaded.owner_overrides,
            {"dialect": {"owner": "openai_chat", "published": "openai_responses"}},
        )

    def test_an_override_record_for_an_unknown_field_is_dropped(self) -> None:
        """A hand-edited state file cannot smuggle a record for a field."""
        entry = ModelEntry.from_dict(
            {
                "model_id": "gpt-6-luna",
                "owner_overrides": {
                    "dialect": {"owner": "openai_chat", "published": "openai_responses"},
                    "context_limit": {"owner": 999},
                    "notes": "not a record",
                },
            }
        )
        self.assertIsNotNone(entry)
        self.assertEqual(
            entry.owner_overrides,
            {"dialect": {"owner": "openai_chat", "published": "openai_responses"}},
        )


if __name__ == "__main__":
    unittest.main()
