"""Isolated tests for the staged capability-discovery backend."""
import json
import tempfile
import unittest
from pathlib import Path

from gateway.capability_discovery import DiscoveryStore, row_fingerprint


class DiscoveryBackendTests(unittest.TestCase):
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

    def test_reported_usage_refusal_is_persisted_and_absent_usage_is_not(self) -> None:
        """SEC-R66-C information loss: stored evidence keeps the refusal record.

        A refused counter must persist as ``invalid_usage: true`` beside its
        nulls — "the provider reported a number this gateway will not carry"
        is evidence, and it is not the same record as a run whose provider
        reported no usage at all (which keeps no ``reported_usage`` key).
        A tampered non-bool marker value is dropped, never trusted.
        """
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "discovery.json"
            store = DiscoveryStore(path)
            store.record_fake_result("p", "m", [
                {"capability": "image_input", "status": "inconclusive",
                 "reported_usage": {"input_tokens": None, "output_tokens": 3,
                                    "invalid_usage": True}},
                {"capability": "function_tools", "status": "inconclusive",
                 "reported_usage": {"input_tokens": None, "output_tokens": None}},
            ], {"supports_image_input": False, "supports_function_tools": True}, 4)
            run = store.read("p", "m")["runs"][0]
            by_capability = {row["capability"]: row for row in run["results"]}
            self.assertEqual(
                by_capability["image_input"]["reported_usage"],
                {"input_tokens": None, "output_tokens": 3, "invalid_usage": True},
            )
            self.assertNotIn("reported_usage", by_capability["function_tools"])
            # The raw file must hold the marker too — not just the parsed view.
            self.assertIn('"invalid_usage": true', path.read_text())

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "discovery.json"
            store = DiscoveryStore(path)
            store.record_fake_result("p", "m", [
                {"capability": "image_input", "status": "inconclusive",
                 "reported_usage": {"input_tokens": None, "output_tokens": None,
                                    "invalid_usage": "true"}},
            ], {"supports_image_input": False, "supports_function_tools": True}, 4)
            run = store.read("p", "m")["runs"][0]
            self.assertNotIn("reported_usage", run["results"][0],
                             "a non-bool marker value is tampering, not evidence")

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
    def test_config_persistence_failure_does_not_consume_decision_and_retry_applies(self):
        from tests.test_end_to_end import GatewayHarness

        h = GatewayHarness()
        try:
            config = h.config.snapshot()
            row = config.providers["chatgpt_codex"].models["gpt-6-luna"].to_dict()
            proposal_id = h.app.discovery.record_fake_result("chatgpt_codex", "gpt-6-luna", [
                {"capability": "image_input", "status": "observed_supported"}], row, config.revision)
            proposal = h.app.discovery.proposal(proposal_id)
            assert proposal is not None
            request = {"proposal_id": proposal_id, "decision_id": "disk-failure-1",
                       "decisions": {"supports_image_input": "approved"},
                       "expected_revision": config.revision,
                       "expected_fingerprint": proposal["base_model_fingerprint"]}
            original_persist = h.config._persist
            h.config._persist = lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("synthetic disk full"))
            status, _, _ = h.request("POST", "/owner/api/capabilities/decision", request)
            self.assertEqual(status, 400)
            outcome = h.app.discovery.proposal(proposal_id)
            assert outcome is not None
            self.assertEqual(outcome["status"], "pending")
            self.assertEqual(outcome["fields"][0]["decision"], "pending")
            self.assertFalse(h.config.snapshot().providers["chatgpt_codex"].models["gpt-6-luna"].supports_image_input)
            h.config._persist = original_persist
            status, payload, _ = h.request("POST", "/owner/api/capabilities/decision", request)
            self.assertEqual(status, 200, payload)
            self.assertTrue(h.config.snapshot().providers["chatgpt_codex"].models["gpt-6-luna"].supports_image_input)
            self.assertEqual(h.app.discovery.proposal(proposal_id)["status"], "approved")
        finally:
            h.close()

    def test_sidecar_failure_is_recoverable_by_exact_retry_from_config_marker(self):
        from tests.test_end_to_end import GatewayHarness

        h = GatewayHarness()
        try:
            config = h.config.snapshot()
            row = config.providers["chatgpt_codex"].models["gpt-6-luna"].to_dict()
            proposal_id = h.app.discovery.record_fake_result("chatgpt_codex", "gpt-6-luna", [
                {"capability": "image_input", "status": "observed_supported"}], row, config.revision)
            proposal = h.app.discovery.proposal(proposal_id)
            assert proposal is not None
            request = {"proposal_id": proposal_id, "decision_id": "sidecar-failure-1",
                       "decisions": {"supports_image_input": "approved"},
                       "expected_revision": config.revision,
                       "expected_fingerprint": proposal["base_model_fingerprint"]}
            original_persist = h.app.discovery._persist
            h.app.discovery._persist = lambda: (_ for _ in ()).throw(OSError("synthetic sidecar failure"))
            status, _, _ = h.request("POST", "/owner/api/capabilities/decision", request)
            self.assertEqual(status, 400)
            self.assertTrue(h.config.snapshot().providers["chatgpt_codex"].models["gpt-6-luna"].supports_image_input)
            h.app.discovery._persist = original_persist
            # Simulate process restart: reload the durable sidecar, while the
            # config marker survives in config.json.
            h.app.discovery = DiscoveryStore(h.data_dir / "capability_discovery.json")
            status, payload, _ = h.request("POST", "/owner/api/capabilities/decision", request)
            self.assertEqual(status, 200, payload)
            replay = h.app.discovery.decision_replay(proposal_id, "sidecar-failure-1", request["decisions"])
            assert replay is not None
            self.assertEqual(replay["status"], "approved")
        finally:
            h.close()

    def test_expired_proposal_recovers_only_exact_config_committed_decision(self):
        from datetime import datetime, timedelta, timezone
        from tests.test_end_to_end import GatewayHarness

        h = GatewayHarness()
        try:
            config = h.config.snapshot()
            row = config.providers["chatgpt_codex"].models["gpt-6-luna"].to_dict()
            proposal_id = h.app.discovery.record_fake_result("chatgpt_codex", "gpt-6-luna", [
                {"capability": "image_input", "status": "observed_supported"}], row, config.revision)
            proposal = h.app.discovery.proposal(proposal_id)
            assert proposal is not None
            request = {"proposal_id": proposal_id, "decision_id": "expired-recovery",
                       "decisions": {"supports_image_input": "approved"},
                       "expected_revision": config.revision,
                       "expected_fingerprint": proposal["base_model_fingerprint"]}
            original_persist = h.app.discovery._persist
            h.app.discovery._persist = lambda: (_ for _ in ()).throw(OSError("synthetic sidecar failure"))
            status, _, _ = h.request("POST", "/owner/api/capabilities/decision", request)
            self.assertEqual(status, 400)
            h.app.discovery._persist = original_persist

            # Simulate downtime past TTL with config's durable decision marker intact.
            sidecar_path = h.data_dir / "capability_discovery.json"
            sidecar = json.loads(sidecar_path.read_text())
            item = next(x for x in sidecar["proposals"] if x["proposal_id"] == proposal_id)
            item["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
            sidecar_path.write_text(json.dumps(sidecar))
            h.app.discovery = DiscoveryStore(sidecar_path)
            status, payload, _ = h.request("POST", "/owner/api/capabilities/decision", request)
            self.assertEqual(status, 200, payload)
            self.assertEqual(payload["status"], "approved")
            self.assertEqual(h.app.discovery.decision_replay(proposal_id, request["decision_id"], request["decisions"]), payload)
            config_after = h.config.snapshot()
            provenance = config_after.settings["capability_approvals"]["chatgpt_codex"]["gpt-6-luna"]["supports_image_input"]
            self.assertEqual(provenance["decision_id"], request["decision_id"])
            self.assertEqual(provenance["proposal_id"], proposal_id)
            self.assertEqual(provenance["source"], "capability_discovery_observation")

            # Reusing the ID with a different payload, or creating a fresh
            # decision after expiry, cannot mutate config or the audit result.
            config_before = (h.data_dir / "config.json").read_bytes()
            conflicting = dict(request, decisions={"supports_image_input": "rejected"})
            status, _, _ = h.request("POST", "/owner/api/capabilities/decision", conflicting)
            self.assertEqual(status, 409)
            unauthorized = dict(request, decision_id="new-expired-decision")
            status, _, _ = h.request("POST", "/owner/api/capabilities/decision", unauthorized)
            self.assertEqual(status, 409)
            self.assertEqual((h.data_dir / "config.json").read_bytes(), config_before)
            self.assertEqual(h.app.discovery.proposal(proposal_id)["status"], "approved")
        finally:
            h.close()

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
        """R59: a tampered field record with no `classification` key must be
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
        """R59: `classification` is spec vocabulary (`documented|observed`), so
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


    def test_decision_refuses_expired_proposal_with_no_state_change(self):
        """CAP-105 / CAP-201 (security re-gate C3): expiry is a PRE-commit gate.

        An expired proposal carrying a fresh, correct CAS token and no config
        marker must be refused with zero state change. It used to be refused
        only inside decide(), i.e. AFTER the config transaction committed, so
        the refusal itself armed the exact recovery marker and the retry then
        returned 200 for the approval the owner had just been told was refused.
        """
        from datetime import UTC, datetime, timedelta

        from tests.test_end_to_end import GatewayHarness

        h = GatewayHarness()
        try:
            config = h.config.snapshot()
            row = config.providers["chatgpt_codex"].models["gpt-6-luna"].to_dict()
            proposal_id = h.app.discovery.record_fake_result("chatgpt_codex", "gpt-6-luna", [
                {"capability": "image_input", "status": "observed_supported"}], row, config.revision)

            def expire(proposal):
                proposal["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()

            self._tamper_sidecar(h, proposal_id, expire)
            proposal = h.app.discovery.proposal(proposal_id)
            assert proposal is not None
            before = h.config.snapshot().to_dict()
            before_disk = (h.data_dir / "config.json").read_bytes()
            request = {"proposal_id": proposal_id, "decision_id": "expired-refusal",
                       "decisions": {"supports_image_input": "approved"},
                       "expected_revision": config.revision,
                       "expected_fingerprint": proposal["base_model_fingerprint"]}
            status, payload, _ = h.request("POST", "/owner/api/capabilities/decision", request)
            self.assertEqual(status, 409, payload)
            # No config write of any kind: bytes, revision, capability value,
            # provenance row and the recovery marker all stay put.
            self.assertEqual((h.data_dir / "config.json").read_bytes(), before_disk)
            config_after = h.config.snapshot()
            self.assertEqual(config_after.to_dict(), before)
            self.assertEqual(config_after.revision, config.revision)
            self.assertEqual(
                config_after.providers["chatgpt_codex"].models["gpt-6-luna"].supports_image_input,
                row["supports_image_input"])
            self.assertIsNone(config_after.settings.get("capability_decisions", {}).get(proposal_id))
            self.assertEqual(
                config_after.settings.get("capability_approvals", {})
                .get("chatgpt_codex", {}).get("gpt-6-luna", {}), {})
            # No evidence row and no sidecar mutation: the proposal is still
            # pending and its field is still undecided.
            sidecar = json.loads((h.data_dir / "capability_discovery.json").read_text())
            self.assertEqual(sidecar.get("decisions"), [])
            outcome = h.app.discovery.proposal(proposal_id)
            assert outcome is not None
            self.assertEqual(outcome["status"], "pending")
            self.assertEqual(outcome["fields"][0]["decision"], "pending")
            # The refusal wrote no marker, so the EXACT retry is refused too --
            # an expired proposal can never be approved by a fresh decision.
            status, payload, _ = h.request("POST", "/owner/api/capabilities/decision", request)
            self.assertEqual(status, 409, payload)
            self.assertEqual((h.data_dir / "config.json").read_bytes(), before_disk)
            self.assertEqual(h.config.snapshot().revision, config.revision)
            self.assertEqual(h.config.snapshot().providers["chatgpt_codex"].models["gpt-6-luna"]
                             .supports_image_input, row["supports_image_input"])
            retry = h.app.discovery.proposal(proposal_id)
            assert retry is not None
            self.assertEqual(retry["status"], "pending")
        finally:
            h.close()

    def test_decision_refuses_non_vocabulary_decision_value_before_config_write(self):
        """Spec line 141: on validation failure none of the decisions apply.

        A value typo used to be refused by decide() only after the config
        commit, leaving a revision bump and a decision marker for a decision
        that never applied -- and burning the proposal, so the next legitimate
        decision got a 409.
        """
        from tests.test_end_to_end import GatewayHarness

        h = GatewayHarness()
        try:
            config = h.config.snapshot()
            row = config.providers["chatgpt_codex"].models["gpt-6-luna"].to_dict()
            proposal_id = h.app.discovery.record_fake_result("chatgpt_codex", "gpt-6-luna", [
                {"capability": "image_input", "status": "observed_supported"}], row, config.revision)
            proposal = h.app.discovery.proposal(proposal_id)
            assert proposal is not None
            before = h.config.snapshot().to_dict()
            before_disk = (h.data_dir / "config.json").read_bytes()
            status, payload, _ = h.request("POST", "/owner/api/capabilities/decision", {
                "proposal_id": proposal_id, "decision_id": "value-typo",
                "decisions": {"supports_image_input": "approvd"},
                "expected_revision": config.revision,
                "expected_fingerprint": proposal["base_model_fingerprint"],
            })
            self.assertEqual(status, 400, payload)
            self.assertEqual((h.data_dir / "config.json").read_bytes(), before_disk)
            config_after = h.config.snapshot()
            self.assertEqual(config_after.to_dict(), before)
            self.assertEqual(config_after.revision, config.revision)
            self.assertIsNone(config_after.settings.get("capability_decisions", {}).get(proposal_id))
            self.assertEqual(
                config_after.settings.get("capability_approvals", {})
                .get("chatgpt_codex", {}).get("gpt-6-luna", {}), {})
            sidecar = json.loads((h.data_dir / "capability_discovery.json").read_text())
            self.assertEqual(sidecar.get("decisions"), [])
            outcome = h.app.discovery.proposal(proposal_id)
            assert outcome is not None
            self.assertEqual(outcome["status"], "pending")
            # The malformed request must not have burned the proposal: the
            # next legitimate decision against the same snapshot still lands.
            status, payload, _ = h.request("POST", "/owner/api/capabilities/decision", {
                "proposal_id": proposal_id, "decision_id": "value-legit",
                "decisions": {"supports_image_input": "approved"},
                "expected_revision": config.revision,
                "expected_fingerprint": proposal["base_model_fingerprint"],
            })
            self.assertEqual(status, 200, payload)
            self.assertTrue(
                h.config.snapshot().providers["chatgpt_codex"].models["gpt-6-luna"].supports_image_input)
        finally:
            h.close()

    def test_decision_refuses_empty_decisions_map_before_config_write(self):
        """An empty map applies nothing, so it must not bump the revision or
        leave an empty approvals bucket behind (spec line 141)."""
        from tests.test_end_to_end import GatewayHarness

        h = GatewayHarness()
        try:
            config = h.config.snapshot()
            row = config.providers["chatgpt_codex"].models["gpt-6-luna"].to_dict()
            proposal_id = h.app.discovery.record_fake_result("chatgpt_codex", "gpt-6-luna", [
                {"capability": "image_input", "status": "observed_supported"}], row, config.revision)
            proposal = h.app.discovery.proposal(proposal_id)
            assert proposal is not None
            before = h.config.snapshot().to_dict()
            before_disk = (h.data_dir / "config.json").read_bytes()
            status, payload, _ = h.request("POST", "/owner/api/capabilities/decision", {
                "proposal_id": proposal_id, "decision_id": "empty-map",
                "decisions": {},
                "expected_revision": config.revision,
                "expected_fingerprint": proposal["base_model_fingerprint"],
            })
            self.assertEqual(status, 400, payload)
            self.assertEqual((h.data_dir / "config.json").read_bytes(), before_disk)
            config_after = h.config.snapshot()
            self.assertEqual(config_after.to_dict(), before)
            self.assertEqual(config_after.revision, config.revision)
            self.assertNotIn("gpt-6-luna",
                             config_after.settings.get("capability_approvals", {}).get("chatgpt_codex", {}))
            self.assertIsNone(config_after.settings.get("capability_decisions", {}).get(proposal_id))
            sidecar = json.loads((h.data_dir / "capability_discovery.json").read_text())
            self.assertEqual(sidecar.get("decisions"), [])
            outcome = h.app.discovery.proposal(proposal_id)
            assert outcome is not None
            self.assertEqual(outcome["status"], "pending")
        finally:
            h.close()

    def test_decision_refuses_mixed_valid_and_invalid_values_without_applying_either(self):
        """A valid field riding along with a garbage value must not be applied
        by the refused request (spec line 141: none are applied)."""
        from tests.test_end_to_end import GatewayHarness

        h = GatewayHarness()
        try:
            config = h.config.snapshot()
            row = config.providers["chatgpt_codex"].models["gpt-6-luna"].to_dict()
            proposal_id = h.app.discovery.record_fake_result("chatgpt_codex", "gpt-6-luna", [
                {"capability": "image_input", "status": "observed_supported"}], row, config.revision)

            def add_tools(proposal):
                proposal["fields"].append({"field": "supports_function_tools",
                                           "current_value": row["supports_function_tools"],
                                           "proposed_value": False, "evidence_refs": [],
                                           "classification": "observed", "decision": "pending"})

            self._tamper_sidecar(h, proposal_id, add_tools)
            proposal = h.app.discovery.proposal(proposal_id)
            assert proposal is not None
            before = h.config.snapshot().to_dict()
            before_disk = (h.data_dir / "config.json").read_bytes()
            status, payload, _ = h.request("POST", "/owner/api/capabilities/decision", {
                "proposal_id": proposal_id, "decision_id": "mixed-values",
                "decisions": {"supports_image_input": "approved",
                              "supports_function_tools": "nope"},
                "expected_revision": config.revision,
                "expected_fingerprint": proposal["base_model_fingerprint"],
            })
            self.assertEqual(status, 400, payload)
            self.assertEqual((h.data_dir / "config.json").read_bytes(), before_disk)
            config_after = h.config.snapshot()
            self.assertEqual(config_after.to_dict(), before)
            self.assertEqual(config_after.revision, config.revision)
            model = config_after.providers["chatgpt_codex"].models["gpt-6-luna"]
            self.assertEqual(model.supports_image_input, row["supports_image_input"])
            self.assertEqual(model.supports_function_tools, row["supports_function_tools"])
            self.assertEqual(
                config_after.settings.get("capability_approvals", {})
                .get("chatgpt_codex", {}).get("gpt-6-luna", {}), {})
            self.assertIsNone(config_after.settings.get("capability_decisions", {}).get(proposal_id))
            sidecar = json.loads((h.data_dir / "capability_discovery.json").read_text())
            self.assertEqual(sidecar.get("decisions"), [])
            outcome = h.app.discovery.proposal(proposal_id)
            assert outcome is not None
            self.assertEqual(outcome["status"], "pending")
        finally:
            h.close()

    def test_decision_refuses_over_long_ids_before_config_write(self):
        """Same root cause as the value/expiry findings: decide() bounds both
        ids, and that check used to run only after the config commit -- so an
        over-long decision_id was refused 400 WITH a revision bump and a
        marker. Id bounds are now validated before anything is written."""
        from tests.test_end_to_end import GatewayHarness

        h = GatewayHarness()
        try:
            config = h.config.snapshot()
            row = config.providers["chatgpt_codex"].models["gpt-6-luna"].to_dict()
            proposal_id = h.app.discovery.record_fake_result("chatgpt_codex", "gpt-6-luna", [
                {"capability": "image_input", "status": "observed_supported"}], row, config.revision)
            proposal = h.app.discovery.proposal(proposal_id)
            assert proposal is not None
            before = h.config.snapshot().to_dict()
            before_disk = (h.data_dir / "config.json").read_bytes()
            for field, value in (("proposal_id", "p" * 65), ("decision_id", "x" * 65)):
                with self.subTest(field=field):
                    request = {"proposal_id": proposal_id, "decision_id": "over-long",
                               "decisions": {"supports_image_input": "approved"},
                               "expected_revision": config.revision,
                               "expected_fingerprint": proposal["base_model_fingerprint"]}
                    request[field] = value
                    status, payload, _ = h.request("POST", "/owner/api/capabilities/decision", request)
                    self.assertEqual(status, 400, payload)
                    self.assertEqual((h.data_dir / "config.json").read_bytes(), before_disk)
                    self.assertEqual(h.config.snapshot().to_dict(), before)
                    self.assertEqual(h.config.snapshot().revision, config.revision)
                    self.assertIsNone(
                        h.config.snapshot().settings.get("capability_decisions", {}).get(proposal_id))
                    sidecar = json.loads((h.data_dir / "capability_discovery.json").read_text())
                    self.assertEqual(sidecar.get("decisions"), [])
                    outcome = h.app.discovery.proposal(proposal_id)
                    assert outcome is not None
                    self.assertEqual(outcome["status"], "pending")
        finally:
            h.close()


if __name__ == "__main__":
    unittest.main()
