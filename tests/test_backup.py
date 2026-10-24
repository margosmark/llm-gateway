from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from gateway.backup import export_config, import_config
from gateway.config import ConfigStore, Profile, RouteRef
from gateway.identifiers import ValidationError
from tests.test_end_to_end import FAKE_PROVIDER, GatewayHarness


class BackupHttpTests(unittest.TestCase):
    def setUp(self):
        self.harness = GatewayHarness()
        self.addCleanup(self.harness.close)

    def test_export_omits_token_hashes_and_secret_adjacent_fields(self):
        with self.harness.config.mutate() as config:
            config.profiles["laptop"] = Profile(profile_id="laptop", name="Laptop", has_token=True)
            config.settings["private_note"] = "synthetic-secret-marker"
        status, result, _ = self.harness.request("GET", "/owner/api/backup/export")
        self.assertEqual(status, 200)
        serialized = json.dumps(result)
        self.assertNotIn("token", serialized.lower())
        self.assertNotIn("synthetic-secret-marker", serialized)

    def test_import_rejects_non_boolean_capability_flags(self):
        # Security regression: a capability flag that is a string ("false"),
        # an int or null must be rejected outright - never coerced to True.
        _, result, _ = self.harness.request("GET", "/owner/api/backup/export")
        export = result["export"]
        clone = ConfigStore(Path(self.harness._tmp.name) / "clone-strict.json")
        app = self.harness.app
        for flag, value in (("supports_image_input", "false"),
                            ("enabled", "true"),
                            ("supports_builtin_tools", 1),
                            ("limits_known", None)):
            with self.subTest(flag=flag, value=value):
                payload = json.loads(json.dumps(export))
                provider = next(iter(payload["config"]["providers"].values()))
                model = next(iter(provider["models"].values()))
                model[flag] = value
                with self.assertRaises(ValidationError):
                    import_config(payload, clone.snapshot(), app.registry)

    def test_roundtrip_and_reimport_are_idempotent(self):
        with self.harness.config.mutate() as config:
            config.aliases["coding"] = __import__("gateway.config", fromlist=["Alias"]).Alias(
                name="coding", enabled=True, primary=RouteRef(FAKE_PROVIDER, "gpt-6-luna"))
        _, result, _ = self.harness.request("GET", "/owner/api/backup/export")
        export = result["export"]
        clone = ConfigStore(Path(self.harness._tmp.name) / "clone.json")
        app = self.harness.app
        imported = __import__("gateway.backup", fromlist=["import_config"]).import_config(export, clone.snapshot(), app.registry)
        clone.replace(imported)
        self.assertEqual(export_config(clone.snapshot())["config"], export["config"])
        clone.replace(__import__("gateway.backup", fromlist=["import_config"]).import_config(export, clone.snapshot(), app.registry))
        self.assertEqual(export_config(clone.snapshot())["config"], export["config"])

    def test_bad_identifier_and_route_refusals_do_not_write_bytes(self):
        _, result, _ = self.harness.request("GET", "/owner/api/backup/export")
        original = self.harness.config.path.read_bytes()
        for mutate in (
            lambda x: x["config"]["profiles"].update({"../escape": {"profile_id": "../escape", "name": "bad"}}),
            lambda x: x["config"]["aliases"].update({"bad": {"name": "bad", "enabled": True, "primary": {"provider": FAKE_PROVIDER, "model": "missing-model"}, "fallbacks": []}}),
        ):
            payload = json.loads(json.dumps(result["export"]))
            mutate(payload)
            status, _, _ = self.harness.request("POST", "/owner/api/backup/import", body=payload)
            self.assertEqual(status, 400)
            self.assertEqual(self.harness.config.path.read_bytes(), original)

    def test_snapshot_worker_writes_to_state_volume_only_when_enabled(self):
        from gateway.snapshots import SnapshotWorker
        store = self.harness.config
        worker = SnapshotWorker(store, 0)
        worker.start()
        worker.stop()
        self.assertFalse((store.path.parent / "snapshots").exists())
        with store.mutate() as config:
            config.settings["snapshot_interval_seconds"] = 1
        worker = SnapshotWorker(store, 1)
        worker.start()
        import time
        deadline = time.monotonic() + 3
        snapshot_dir = store.path.parent / "snapshots"
        while time.monotonic() < deadline and not list(snapshot_dir.glob("config-*.json")) if snapshot_dir.exists() else time.monotonic() < deadline:
            time.sleep(0.05)
        worker.stop()
        files = list(snapshot_dir.glob("config-*.json"))
        self.assertEqual(len(files), 1)
        self.assertEqual(json.loads(files[0].read_text())["settings"]["snapshot_interval_seconds"], 1)

    def test_snapshot_interval_defaults_off_and_is_surfaced(self):
        _, result, _ = self.harness.request("GET", "/owner/api/state")
        self.assertEqual(result["snapshot_interval_seconds"], 0)
        status, _, _ = self.harness.request("POST", "/owner/api/settings", body={"action": "snapshot_interval", "seconds": 3600})
        self.assertEqual(status, 200)
        _, result, _ = self.harness.request("GET", "/owner/api/state")
        self.assertEqual(result["snapshot_interval_seconds"], 3600)


if __name__ == "__main__":
    unittest.main()
