"""Isolated tests for the staged capability-discovery backend."""
import json
import tempfile
import unittest
from pathlib import Path

from gateway.capability_discovery import DiscoveryStore, preview_for, row_fingerprint


class DiscoveryBackendTests(unittest.TestCase):
    def test_preview_is_exact_bounded_and_blocks_unknown_price(self):
        p = preview_for("provider-a", "model-x", ["text_generation", "image_input", "function_tools"], 7)
        self.assertFalse(p["available"])
        self.assertEqual((p["provider_id"], p["model_id"], p["base_revision"]), ("provider-a", "model-x", 7))
        self.assertEqual(p["maximum_requests"], 3)
        self.assertEqual((p["retries"], p["concurrency"], p["streaming"]), (0, 1, False))
        self.assertIsNone(p["price"]["amount"])
        self.assertTrue(p["estimate_is_not_a_bill_guarantee"])

    def test_preview_rejects_duplicates_unknown_and_empty(self):
        for probes in ([], ["text_generation", "text_generation"], ["magic"]):
            with self.subTest(probes=probes), self.assertRaises(ValueError):
                preview_for("p", "m", probes, 1)

    def test_fake_evidence_is_sanitized_persisted_and_exactly_scoped(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "discovery.json"
            store = DiscoveryStore(path)
            proposal = store.record_fake_result("p", "same", [
                {"capability": "image_input", "status": "observed_supported", "raw": "MUST_NOT_PERSIST"},
                {"capability": "function_tools", "status": "inconclusive", "raw": "MUST_NOT_PERSIST"},
            ], {"supports_image_input": False, "supports_function_tools": True}, 4)
            data = path.read_text()
            self.assertNotIn("MUST_NOT_PERSIST", data)
            self.assertEqual(len(store.read("p", "same")["runs"]), 1)
            self.assertEqual(store.read("other", "same")["runs"], [])
            proposal_obj = store.proposal(proposal)
            self.assertEqual([x["field"] for x in proposal_obj["fields"]], ["supports_image_input"])
            self.assertEqual(proposal_obj["fields"][0]["decision"], "pending")
            loaded = DiscoveryStore(path)
            self.assertEqual(loaded.proposal(proposal), proposal_obj)

    def test_field_decision_idempotent_and_conflict_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            store = DiscoveryStore(Path(directory) / "d.json")
            row = {"supports_image_input": False}
            proposal = store.record_fake_result("p", "m", [{"capability": "image_input", "status": "observed_supported"}], row, 5)
            fp = row_fingerprint(row)
            result = store.decide(proposal, "decision-1", {"supports_image_input": "approved"}, 5, fp)
            self.assertEqual(store.decide(proposal, "decision-1", {"supports_image_input": "approved"}, 5, fp), result)
            with self.assertRaises(RuntimeError):
                store.decide(proposal, "decision-2", {"supports_image_input": "rejected"}, 5, fp)

    def test_stale_revision_or_fingerprint_conflicts(self):
        for revision, fingerprint in ((8, "x"), (7, "x")):
            with self.subTest(revision=revision):
                with tempfile.TemporaryDirectory() as directory:
                    store = DiscoveryStore(Path(directory) / "d.json")
                    row = {"supports_image_input": False}
                    proposal = store.record_fake_result("p", "m", [{"capability": "image_input", "status": "observed_supported"}], row, 7)
                    with self.assertRaises(RuntimeError):
                        store.decide(proposal, "d", {"supports_image_input": "approved"}, revision, fingerprint)
                    self.assertEqual(store.proposal(proposal)["status"], "conflicted")

    def test_decide_refuses_field_outside_capability_vocabulary(self):
        """S-1 (store layer): the sidecar is untrusted, so a tampered proposal
        field name must not be recordable even when the caller bypasses HTTP."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "d.json"
            store = DiscoveryStore(path)
            row = {"supports_image_input": False, "dialect": "openai_responses"}
            proposal_id = store.record_fake_result("p", "m", [
                {"capability": "image_input", "status": "observed_supported"}], row, 5)
            # Tamper the persisted sidecar the way a hostile writer would.
            data = json.loads(path.read_text())
            for item in data["proposals"]:
                if item.get("proposal_id") == proposal_id:
                    item["fields"].append({"field": "dialect", "current_value": "openai_responses",
                                           "proposed_value": "systemone", "evidence_refs": [],
                                           "classification": "observed", "decision": "pending"})
            path.write_text(json.dumps(data))
            tampered = DiscoveryStore(path)
            fingerprint = row_fingerprint(row)
            with self.assertRaises(ValueError):
                tampered.decide(proposal_id, "oov", {"dialect": "approved"}, 5, fingerprint)
            # Nothing was recorded: the proposal is still pending.
            still_pending = tampered.proposal(proposal_id)
            assert still_pending is not None
            self.assertEqual(still_pending["status"], "pending")
            # The legitimate capability vocabulary still decides normally; the
            # tampered field stays pending, so the proposal is only partial.
            result = tampered.decide(proposal_id, "legit", {"supports_image_input": "approved"}, 5, fingerprint)
            self.assertEqual(result["status"], "partially_approved")


class CapabilityApprovalHttpTests(unittest.TestCase):
    def test_decision_endpoint_updates_only_approved_field(self):
        from tests.test_end_to_end import GatewayHarness

        h = GatewayHarness()
        try:
            config = h.config.snapshot()
            row = config.providers["chatgpt_codex"].models["gpt-6-luna"].to_dict()
            proposal_id = h.app.discovery.record_fake_result("chatgpt_codex", "gpt-6-luna", [
                {"capability": "image_input", "status": "observed_supported"},
                {"capability": "function_tools", "status": "observed_unsupported"},
            ], row, config.revision)
            proposal = h.app.discovery.proposal(proposal_id)
            status, payload, _ = h.request("POST", "/owner/api/capabilities/decision", {
                "proposal_id": proposal_id, "decision_id": "http-decision-1",
                "decisions": {"supports_image_input": "approved"},
                "expected_revision": config.revision,
                "expected_fingerprint": proposal["base_model_fingerprint"],
            })
            self.assertEqual(status, 200, payload)
            updated = h.config.snapshot().providers["chatgpt_codex"].models["gpt-6-luna"]
            self.assertTrue(updated.supports_image_input)
            self.assertTrue(updated.supports_function_tools)
            self.assertTrue(updated.enabled)
            revision_after = h.config.snapshot().revision
            replay_status, replay, _ = h.request("POST", "/owner/api/capabilities/decision", {
                "proposal_id": proposal_id, "decision_id": "http-decision-1",
                "decisions": {"supports_image_input": "approved"},
                "expected_revision": config.revision, "expected_fingerprint": proposal["base_model_fingerprint"],
            })
            self.assertEqual(replay_status, 200, replay)
            self.assertEqual(replay["decisions"], payload["decisions"])
            self.assertEqual(h.config.snapshot().revision, revision_after)
            # Unselected fields remain decidable against the same snapshot.
            current_proposal = h.app.discovery.proposal(proposal_id)
            current_row = h.config.snapshot().providers["chatgpt_codex"].models["gpt-6-luna"].to_dict()
            status, second, _ = h.request("POST", "/owner/api/capabilities/decision", {
                "proposal_id": proposal_id, "decision_id": "http-decision-2",
                "decisions": {"supports_function_tools": "approved"},
                "expected_revision": revision_after,
                "expected_fingerprint": row_fingerprint(current_row),
            })
            self.assertEqual(current_proposal["base_revision"], revision_after)
            self.assertEqual(status, 200, second)
            self.assertEqual(second["status"], "approved")
        finally:
            h.close()

    def test_stale_http_decision_conflicts_without_config_mutation(self):
        from tests.test_end_to_end import GatewayHarness

        h = GatewayHarness()
        try:
            config = h.config.snapshot()
            row = config.providers["chatgpt_codex"].models["gpt-6-luna"].to_dict()
            proposal_id = h.app.discovery.record_fake_result("chatgpt_codex", "gpt-6-luna", [
                {"capability": "image_input", "status": "observed_supported"}], row, config.revision)
            proposal = h.app.discovery.proposal(proposal_id)
            before = h.config.snapshot().to_dict()
            status, payload, _ = h.request("POST", "/owner/api/capabilities/decision", {
                "proposal_id": proposal_id, "decision_id": "stale",
                "decisions": {"supports_image_input": "approved"},
                "expected_revision": config.revision + 1,
                "expected_fingerprint": proposal["base_model_fingerprint"],
            })
            self.assertEqual(status, 409, payload)
            self.assertEqual(h.config.snapshot().to_dict(), before)
            outcome = h.app.discovery.proposal(proposal_id)
            assert outcome is not None
            self.assertEqual(outcome["status"], "conflicted")
        finally:
            h.close()

    @staticmethod
    def _tamper_sidecar(h, proposal_id, mutate):
        """Rewrite the 0600 evidence sidecar as a hostile writer would, then
        reload it through the real loader (the S-1 attack path)."""
        path = h.data_dir / "capability_discovery.json"
        data = json.loads(path.read_text())
        for item in data["proposals"]:
            if item.get("proposal_id") == proposal_id:
                mutate(item)
        path.write_text(json.dumps(data))
        h.app.discovery = DiscoveryStore(path)

    def test_decision_refuses_field_outside_capability_vocabulary(self):
        from tests.test_end_to_end import GatewayHarness

        h = GatewayHarness()
        try:
            config = h.config.snapshot()
            row = config.providers["chatgpt_codex"].models["gpt-6-luna"].to_dict()
            proposal_id = h.app.discovery.record_fake_result("chatgpt_codex", "gpt-6-luna", [
                {"capability": "image_input", "status": "observed_supported"}], row, config.revision)

            def add_tampered_field(proposal):
                proposal["fields"].append({"field": "dialect", "current_value": row["dialect"],
                                           "proposed_value": "systemone", "evidence_refs": [],
                                           "classification": "observed", "decision": "pending"})

            self._tamper_sidecar(h, proposal_id, add_tampered_field)
            proposal = h.app.discovery.proposal(proposal_id)
            assert proposal is not None
            before = h.config.snapshot().to_dict()
            before_disk = (h.data_dir / "config.json").read_bytes()

            # An unknown field is rejected outright, never silently ignored.
            status, payload, _ = h.request("POST", "/owner/api/capabilities/decision", {
                "proposal_id": proposal_id, "decision_id": "oov-decision",
                "decisions": {"dialect": "approved"},
                "expected_revision": config.revision,
                "expected_fingerprint": proposal["base_model_fingerprint"],
            })
            self.assertEqual(status, 400, payload)

            # Mixing a valid field with an unknown one is refused whole, so an
            # out-of-vocabulary key can never ride along with a valid write.
            status, payload, _ = h.request("POST", "/owner/api/capabilities/decision", {
                "proposal_id": proposal_id, "decision_id": "mixed-decision",
                "decisions": {"supports_image_input": "approved", "dialect": "approved"},
                "expected_revision": config.revision,
                "expected_fingerprint": proposal["base_model_fingerprint"],
            })
            self.assertEqual(status, 400, payload)

            # Config is byte-identical in memory and on disk: no setattr, no
            # provenance row, no revision bump, no recorded decision.
            self.assertEqual(h.config.snapshot().to_dict(), before)
            self.assertEqual((h.data_dir / "config.json").read_bytes(), before_disk)
            model = h.config.snapshot().providers["chatgpt_codex"].models["gpt-6-luna"]
            self.assertEqual(model.dialect, row["dialect"])
            self.assertEqual(model.supports_image_input, row["supports_image_input"])
            approvals = h.config.snapshot().settings.get("capability_approvals", {})
            self.assertEqual(approvals.get("chatgpt_codex", {}).get("gpt-6-luna", {}), {})
            outcome = h.app.discovery.proposal(proposal_id)
            assert outcome is not None
            self.assertEqual(outcome["status"], "pending")
        finally:
            h.close()

    def test_decision_refuses_non_boolean_proposed_value(self):
        from tests.test_end_to_end import GatewayHarness

        h = GatewayHarness()
        try:
            config = h.config.snapshot()
            row = config.providers["chatgpt_codex"].models["gpt-6-luna"].to_dict()
            proposal_id = h.app.discovery.record_fake_result("chatgpt_codex", "gpt-6-luna", [
                {"capability": "image_input", "status": "observed_supported"}], row, config.revision)

            def corrupt_value(proposal):
                proposal["fields"][0]["proposed_value"] = "not-a-bool"

            self._tamper_sidecar(h, proposal_id, corrupt_value)
            proposal = h.app.discovery.proposal(proposal_id)
            assert proposal is not None
            before = h.config.snapshot().to_dict()
            before_disk = (h.data_dir / "config.json").read_bytes()
            status, payload, _ = h.request("POST", "/owner/api/capabilities/decision", {
                "proposal_id": proposal_id, "decision_id": "non-bool-decision",
                "decisions": {"supports_image_input": "approved"},
                "expected_revision": config.revision,
                "expected_fingerprint": proposal["base_model_fingerprint"],
            })
            self.assertEqual(status, 400, payload)
            # Never coerced, never persisted: the boolean field keeps its value
            # and the string never reaches config.json.
            self.assertEqual(h.config.snapshot().to_dict(), before)
            disk = (h.data_dir / "config.json").read_bytes()
            self.assertEqual(disk, before_disk)
            self.assertNotIn(b"not-a-bool", disk)
            model = h.config.snapshot().providers["chatgpt_codex"].models["gpt-6-luna"]
            self.assertEqual(model.supports_image_input, row["supports_image_input"])
            approvals = h.config.snapshot().settings.get("capability_approvals", {})
            self.assertEqual(approvals.get("chatgpt_codex", {}).get("gpt-6-luna", {}), {})
            outcome = h.app.discovery.proposal(proposal_id)
            assert outcome is not None
            self.assertEqual(outcome["status"], "pending")
        finally:
            h.close()

    def test_decision_refuses_missing_classification_before_sidecar_write(self):
        """R56: a tampered field record with no `classification` key must be
        refused before decide() persists, so the 0600 evidence sidecar never
        records an approval that never applied and never burns the proposal."""
        from tests.test_end_to_end import GatewayHarness

        h = GatewayHarness()
        try:
            config = h.config.snapshot()
            row = config.providers["chatgpt_codex"].models["gpt-6-luna"].to_dict()
            proposal_id = h.app.discovery.record_fake_result("chatgpt_codex", "gpt-6-luna", [
                {"capability": "image_input", "status": "observed_supported"}], row, config.revision)

            def drop_classification(proposal):
                proposal["fields"][0].pop("classification", None)

            self._tamper_sidecar(h, proposal_id, drop_classification)
            proposal = h.app.discovery.proposal(proposal_id)
            assert proposal is not None
            before = h.config.snapshot().to_dict()
            before_disk = (h.data_dir / "config.json").read_bytes()
            status, payload, _ = h.request("POST", "/owner/api/capabilities/decision", {
                "proposal_id": proposal_id, "decision_id": "missing-classification-decision",
                "decisions": {"supports_image_input": "approved"},
                "expected_revision": config.revision,
                "expected_fingerprint": proposal["base_model_fingerprint"],
            })
            self.assertEqual(status, 400, payload)
            # Config never changed: no setattr, no provenance, no revision bump.
            self.assertEqual(h.config.snapshot().to_dict(), before)
            self.assertEqual((h.data_dir / "config.json").read_bytes(), before_disk)
            approvals = h.config.snapshot().settings.get("capability_approvals", {})
            self.assertEqual(approvals.get("chatgpt_codex", {}).get("gpt-6-luna", {}), {})
            # The evidence store recorded nothing and the proposal is still
            # decidable by a later legitimate request (not burned/resolved).
            sidecar = json.loads((h.data_dir / "capability_discovery.json").read_text())
            self.assertEqual(sidecar.get("decisions"), [])
            outcome = h.app.discovery.proposal(proposal_id)
            assert outcome is not None
            self.assertEqual(outcome["status"], "pending")
            self.assertEqual(outcome["fields"][0]["decision"], "pending")
        finally:
            h.close()

    def test_decision_refuses_out_of_vocabulary_classification(self):
        """R56: `classification` is spec vocabulary (`documented|observed`), so
        a tampered value is refused before decide() persists anything, exactly
        like an out-of-vocabulary field name or a non-boolean value."""
        from tests.test_end_to_end import GatewayHarness

        h = GatewayHarness()
        try:
            config = h.config.snapshot()
            row = config.providers["chatgpt_codex"].models["gpt-6-luna"].to_dict()
            proposal_id = h.app.discovery.record_fake_result("chatgpt_codex", "gpt-6-luna", [
                {"capability": "image_input", "status": "observed_supported"}], row, config.revision)

            def fake_classification(proposal):
                proposal["fields"][0]["classification"] = "totally_verified"

            self._tamper_sidecar(h, proposal_id, fake_classification)
            proposal = h.app.discovery.proposal(proposal_id)
            assert proposal is not None
            before = h.config.snapshot().to_dict()
            before_disk = (h.data_dir / "config.json").read_bytes()
            status, payload, _ = h.request("POST", "/owner/api/capabilities/decision", {
                "proposal_id": proposal_id, "decision_id": "bogus-classification-decision",
                "decisions": {"supports_image_input": "approved"},
                "expected_revision": config.revision,
                "expected_fingerprint": proposal["base_model_fingerprint"],
            })
            self.assertEqual(status, 400, payload)
            self.assertEqual(h.config.snapshot().to_dict(), before)
            self.assertEqual((h.data_dir / "config.json").read_bytes(), before_disk)
            sidecar = json.loads((h.data_dir / "capability_discovery.json").read_text())
            self.assertEqual(sidecar.get("decisions"), [])
            outcome = h.app.discovery.proposal(proposal_id)
            assert outcome is not None
            self.assertEqual(outcome["status"], "pending")
        finally:
            h.close()


if __name__ == "__main__":
    unittest.main()
