"""Isolated tests for the staged capability-discovery backend."""
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


if __name__ == "__main__":
    unittest.main()
