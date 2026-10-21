from __future__ import annotations

import unittest

from prometheus_client.parser import text_string_to_metric_families

from gateway.metrics import render_metrics
from gateway.observability import RequestEvent, RequestLedger


class MetricsTests(unittest.TestCase):
    def test_valid_exposition_metadata_only_and_cooldown(self):
        ledger = RequestLedger()
        ledger.record(RequestEvent(request_id="r1", timestamp="2026-10-20T00:00:00Z", requested_alias="alias", status=200, latency_ms=25, fallback_used=True))
        ledger.record(RequestEvent(request_id="r2", timestamp="2026-10-20T00:00:01Z", requested_alias="alias", status=502, error_class="upstream_error", latency_ms=300))
        text = render_metrics(ledger, {"provider/model": 42})
        families = {family.name: family for family in text_string_to_metric_families(text)}
        self.assertIn("gateway_requests", families)
        self.assertIn("gateway_errors", families)
        self.assertIn("gateway_request_latency_milliseconds", families)
        self.assertIn("gateway_fallbacks", families)
        self.assertIn("gateway_cooldown_remaining_seconds", families)
        self.assertIn('route="provider/model"', text)
        self.assertIn('error_class="upstream_error"', text)
        for secret in ("prompt", "messages", "completion", "credential", "client_address"):
            self.assertNotIn(secret, text)
        self.assertNotIn("token", text)

    def test_empty_ledger_is_valid_and_no_cooldown_series(self):
        text = render_metrics(None)
        self.assertEqual(list(text_string_to_metric_families(text))[0].name, "gateway_requests")
        self.assertNotIn('route=', text)


if __name__ == "__main__":
    unittest.main()
