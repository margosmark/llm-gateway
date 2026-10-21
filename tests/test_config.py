"""Tests for the configuration store."""

from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from gateway.config import (
    ACCESS_AUTHENTICATED,
    ACCESS_LAN_NOAUTH,
    STATE_CONNECTED,
    STATE_NOT_CONFIGURED,
    Alias,
    ConfigStore,
    GatewayConfig,
    ModelEntry,
    ProviderState,
    RouteRef,
    utc_now_iso,
)


class ConfigStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "config.json"
        self.store = ConfigStore(self.path)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_fresh_install_is_empty(self) -> None:
        config = self.store.snapshot()
        self.assertEqual(config.providers, {})
        self.assertEqual(config.aliases, {})
        self.assertEqual(config.profiles, {})
        self.assertFalse(config.initialized)
        self.assertEqual(config.revision, 0)

    def test_mutation_bumps_revision_and_persists(self) -> None:
        with self.store.mutate() as config:
            config.initialized = True
            config.access_mode = ACCESS_AUTHENTICATED

        reloaded = ConfigStore(self.path).snapshot()
        self.assertTrue(reloaded.initialized)
        self.assertEqual(reloaded.access_mode, ACCESS_AUTHENTICATED)
        self.assertEqual(reloaded.revision, 1)

    def test_revision_increases_monotonically(self) -> None:
        seen = []
        for _ in range(3):
            with self.store.mutate() as config:
                config.settings["touched"] = True
            seen.append(self.store.snapshot().revision)
        self.assertEqual(seen, [1, 2, 3])

    def test_failed_mutation_does_not_persist(self) -> None:
        try:
            with self.store.mutate() as config:
                config.initialized = True
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        self.assertFalse(self.store.snapshot().initialized)
        self.assertEqual(self.store.snapshot().revision, 0)

    def test_snapshot_is_cached_and_stable_across_mutations(self) -> None:
        original = self.store.snapshot()
        self.assertIs(self.store.snapshot(), original)
        with self.store.mutate() as config:
            config.initialized = True
        updated = self.store.snapshot()
        self.assertIsNot(updated, original)
        self.assertTrue(updated.initialized)
        self.assertFalse(original.initialized)

    def test_failed_persist_keeps_published_snapshot_unchanged(self) -> None:
        original = self.store.snapshot()
        with mock.patch.object(self.store, "_persist", side_effect=OSError("disk unavailable")):
            with self.assertRaises(OSError):
                with self.store.mutate() as config:
                    config.initialized = True
        self.assertIs(self.store.snapshot(), original)
        self.assertFalse(self.store.snapshot().initialized)

    def test_reader_never_observes_half_applied_transaction(self) -> None:
        started = threading.Event()
        release = threading.Event()
        observed: list[tuple[bool, bool, int]] = []

        def mutate() -> None:
            with self.store.mutate() as config:
                config.initialized = True
                started.set()
                self.assertTrue(release.wait(timeout=2))
                config.access_mode = ACCESS_AUTHENTICATED

        writer = threading.Thread(target=mutate)
        writer.start()
        self.assertTrue(started.wait(timeout=2))
        reader_done = threading.Event()

        def read_snapshot() -> None:
            snapshot = self.store.snapshot()
            observed.append((snapshot.initialized, snapshot.access_mode == ACCESS_AUTHENTICATED, snapshot.revision))
            reader_done.set()

        reader = threading.Thread(target=read_snapshot)
        reader.start()
        self.assertFalse(reader_done.wait(timeout=0.05))
        release.set()
        writer.join(timeout=2)
        reader.join(timeout=2)
        self.assertFalse(writer.is_alive())
        self.assertFalse(reader.is_alive())
        self.assertEqual(observed, [(True, True, 1)])
        self.assertEqual(self.store.snapshot().revision, 1)

    def test_config_file_is_owner_only(self) -> None:
        with self.store.mutate() as config:
            config.initialized = True
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)


class AliasTests(unittest.TestCase):
    def test_round_trips_through_dict(self) -> None:
        alias = Alias(
            name="coding",
            enabled=True,
            primary=RouteRef("chatgpt_codex", "gpt-6-luna"),
            fallbacks=[RouteRef("opencode_go", "gpt-6-luna"), RouteRef("opencode_go", "mimo-v2.6-flash")],
            fallback_triggers=["exhausted"],
            fallback_authorized=True,
            cooldown_seconds=600,
            budget=12.5,
        )
        restored = Alias.from_dict(alias.to_dict())
        assert restored is not None
        self.assertEqual(restored.name, "coding")
        self.assertEqual(restored.primary, RouteRef("chatgpt_codex", "gpt-6-luna"))
        self.assertEqual(len(restored.fallbacks), 2)
        self.assertEqual(restored.fallbacks[1].model, "mimo-v2.6-flash")
        self.assertTrue(restored.fallback_authorized)
        self.assertEqual(restored.cooldown_seconds, 600)
        self.assertEqual(restored.budget, 12.5)

    def test_all_routes_lists_primary_first(self) -> None:
        alias = Alias(
            name="fast",
            primary=RouteRef("a", "m1"),
            fallbacks=[RouteRef("b", "m2")],
        )
        self.assertEqual([r.model for r in alias.all_routes()], ["m1", "m2"])

    def test_unknown_trigger_names_are_dropped(self) -> None:
        alias = Alias.from_dict({"name": "x", "fallback_triggers": ["exhausted", "nonsense"]})
        assert alias is not None
        self.assertEqual(alias.fallback_triggers, ["exhausted"])

    def test_route_ref_requires_both_provider_and_model(self) -> None:
        self.assertIsNone(RouteRef.from_dict({"provider": "p"}))
        self.assertIsNone(RouteRef.from_dict({"model": "m"}))
        self.assertIsNone(RouteRef.from_dict("not a dict"))

    def test_route_ref_never_ambiguous(self) -> None:
        # The same model string at two providers stays distinguishable.
        a = RouteRef("chatgpt_codex", "gpt-6-luna")
        b = RouteRef("opencode_go", "gpt-6-luna")
        self.assertNotEqual(a.label(), b.label())
        self.assertIn("chatgpt_codex", a.label())


class ProviderStateTests(unittest.TestCase):
    def test_default_state_is_not_configured(self) -> None:
        provider = ProviderState("opencode_go", "opencode_go")
        self.assertEqual(provider.state, STATE_NOT_CONFIGURED)

    def test_round_trips_models(self) -> None:
        provider = ProviderState(
            provider_id="opencode_go",
            kind="opencode_go",
            state=STATE_CONNECTED,
            models={
                "gpt-6-luna": ModelEntry(
                    "gpt-6-luna", enabled=True, dialect="openai_responses",
                    context_window=272000, context_limit=258400, max_output_tokens=32768,
                )
            },
        )
        restored = ProviderState.from_dict(provider.to_dict())
        assert restored is not None
        self.assertEqual(restored.state, STATE_CONNECTED)
        self.assertTrue(restored.models["gpt-6-luna"].enabled)
        self.assertEqual(restored.models["gpt-6-luna"].dialect, "openai_responses")
        self.assertEqual(restored.models["gpt-6-luna"].max_output_tokens, 32768)

    def test_unknown_state_falls_back_to_not_configured(self) -> None:
        restored = ProviderState.from_dict(
            {"provider_id": "p", "kind": "p", "state": "bogus"}
        )
        assert restored is not None
        self.assertEqual(restored.state, STATE_NOT_CONFIGURED)


class ModelEntryFlagTests(unittest.TestCase):
    def test_capability_flags_are_never_coerced(self) -> None:
        # Security regression: the string "false" (or an int, or null) must
        # not become True through bool() coercion on any parsing path.
        entry = ModelEntry.from_dict({
            "model_id": "m", "enabled": "true",
            "supports_image_input": "false", "supports_builtin_tools": 1,
            "supports_streaming": None, "limits_known": "yes",
        })
        assert entry is not None
        self.assertFalse(entry.enabled)
        self.assertFalse(entry.supports_image_input)
        self.assertFalse(entry.supports_builtin_tools)
        self.assertTrue(entry.supports_streaming)  # declared default restored, not coerced
        self.assertFalse(entry.limits_known)


class PublishedAliasTests(unittest.TestCase):
    def test_only_enabled_aliases_with_a_primary_are_published(self) -> None:
        config = GatewayConfig()
        config.aliases["a"] = Alias(name="a", enabled=True, primary=RouteRef("p", "m"))
        config.aliases["b"] = Alias(name="b", enabled=False, primary=RouteRef("p", "m"))
        config.aliases["c"] = Alias(name="c", enabled=True)
        self.assertEqual([a.name for a in config.published_aliases()], ["a"])


if __name__ == "__main__":
    unittest.main()
