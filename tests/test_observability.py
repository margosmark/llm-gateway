"""Tests for metadata-only observability."""

from __future__ import annotations

import unittest

from gateway.observability import (
    CONFIDENCE_NONE,
    SOURCE_PUBLISHED_ESTIMATE,
    SOURCE_UNKNOWN,
    LimitSnapshot,
    LimitStore,
    RequestEvent,
    RequestLedger,
    redact,
    scan_for_secrets,
)


class LedgerTests(unittest.TestCase):
    def test_records_the_requested_alias_and_the_serving_provider(self) -> None:
        ledger = RequestLedger()
        ledger.record(
            RequestEvent(
                request_id="r1",
                timestamp="2026-10-19T00:00:00Z",
                requested_alias="coding",
                serving_provider="chatgpt_codex",
                serving_model="gpt-6-luna",
                status=200,
                latency_ms=120,
            )
        )
        recent = ledger.recent()
        self.assertEqual(len(recent), 1)
        self.assertEqual(recent[0]["requested_alias"], "coding")
        self.assertEqual(recent[0]["serving_provider"], "chatgpt_codex")

    def test_event_has_no_content_fields(self) -> None:
        ledger = RequestLedger()
        ledger.record(
            RequestEvent(
                request_id="r1",
                timestamp="2026-10-19T00:00:00Z",
                requested_alias="coding",
                status=200,
            )
        )
        keys = set(ledger.recent()[0].keys())
        for forbidden in ("prompt", "messages", "content", "response", "body", "api_key", "token"):
            self.assertNotIn(forbidden, keys, f"ledger must not carry {forbidden}")

    def test_ring_is_bounded_and_eviction_is_counted(self) -> None:
        ledger = RequestLedger(capacity=3)
        for index in range(5):
            ledger.record(
                RequestEvent(
                    request_id=f"r{index}",
                    timestamp="2026-10-19T00:00:00Z",
                    requested_alias="a",
                    status=200,
                )
            )
        self.assertEqual(len(ledger.recent(limit=100)), 3)
        self.assertEqual(ledger.evicted, 2)

    def test_totals_count_errors_and_fallbacks(self) -> None:
        ledger = RequestLedger()
        ledger.record(RequestEvent("r1", "t", "a", status=200))
        ledger.record(RequestEvent("r2", "t", "a", error_class="rate_limited", fallback_used=True))
        ledger.record(RequestEvent("r3", "t", "a", error_class="exhausted"))
        totals = ledger.totals()
        self.assertEqual(totals["requests"], 3)
        self.assertEqual(totals["errors"], 2)
        self.assertEqual(totals["fallbacks"], 1)
        self.assertEqual(totals["rate_limited"], 1)
        self.assertEqual(totals["exhausted"], 1)

    def test_recent_is_newest_first(self) -> None:
        ledger = RequestLedger()
        for index in range(3):
            ledger.record(RequestEvent(f"r{index}", "t", f"alias{index}", status=200))
        self.assertEqual([e["requested_alias"] for e in ledger.recent()], ["alias2", "alias1", "alias0"])


class LimitTests(unittest.TestCase):
    def test_unknown_snapshot_keeps_the_value_none(self) -> None:
        snapshot = LimitSnapshot.unknown("chatgpt_codex", "5h", note="no official API")
        self.assertIsNone(snapshot.value)
        self.assertIsNone(snapshot.limit)
        self.assertEqual(snapshot.source, SOURCE_UNKNOWN)
        self.assertEqual(snapshot.confidence, CONFIDENCE_NONE)

    def test_unknown_is_not_zero(self) -> None:
        snapshot = LimitSnapshot.unknown("chatgpt_codex", "5h")
        self.assertNotEqual(snapshot.value, 0)
        self.assertNotEqual(snapshot.limit, 0)

    def test_snapshot_carries_source_and_observation_time(self) -> None:
        snapshot = LimitSnapshot(
            provider="opencode_go",
            window="monthly",
            value=15.0,
            limit=15.0,
            source=SOURCE_PUBLISHED_ESTIMATE,
        )
        data = snapshot.to_dict()
        self.assertEqual(data["source"], SOURCE_PUBLISHED_ESTIMATE)
        self.assertTrue(data["observed_at"])
        self.assertEqual(data["confidence"], CONFIDENCE_NONE)

    def test_store_keeps_the_latest_snapshot_per_window(self) -> None:
        store = LimitStore()
        store.put(LimitSnapshot("p", "5h", value=10.0))
        store.put(LimitSnapshot("p", "5h", value=20.0))
        self.assertEqual(len(store.all()), 1)
        self.assertEqual(store.get("p", "5h").value, 20.0)

    def test_store_keeps_windows_separate(self) -> None:
        store = LimitStore()
        store.put(LimitSnapshot("p", "5h"))
        store.put(LimitSnapshot("p", "weekly"))
        self.assertEqual(len(store.all()), 2)


class RedactionTests(unittest.TestCase):
    def test_openai_style_key_is_redacted(self) -> None:
        text = "using sk-abcdefghijklmnopqrstuvwxyz012345 to call"
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz012345", redact(text))
        self.assertIn("[redacted]", redact(text))

    def test_bearer_token_is_redacted(self) -> None:
        text = "Authorization: Bearer abcdefghijklmnop"
        self.assertNotIn("abcdefghijklmnop", redact(text))

    def test_device_code_is_redacted(self) -> None:
        self.assertNotIn("ABCD-EFGH", redact("code ABCD-EFGH used"))

    def test_gateway_key_header_is_redacted(self) -> None:
        self.assertNotIn("sekritvalue", redact("X-Gateway-Key: sekritvalue"))

    def test_scanner_reports_pattern_names(self) -> None:
        found = scan_for_secrets("token sk-abcdefghijklmnopqrstuv")
        self.assertIn("openai_key", found)

    def test_clean_text_is_not_flagged(self) -> None:
        self.assertEqual(scan_for_secrets("a normal log line with no secrets"), [])

    def test_redaction_preserves_ordinary_text(self) -> None:
        text = "provider chatgpt_codex served gpt-6-luna in 120ms"
        self.assertEqual(redact(text), text)


if __name__ == "__main__":
    unittest.main()
