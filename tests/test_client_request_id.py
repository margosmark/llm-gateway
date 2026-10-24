"""Regression tests for non-authoritative client request correlation IDs."""

from __future__ import annotations

import unittest

from gateway.http.request_id import MAX_CLIENT_REQUEST_ID_LENGTH, validate_client_request_id
from gateway.observability import RequestLedger
from tests.test_end_to_end import FAKE_PROVIDER, GatewayHarness
from gateway.config import Alias, RouteRef


class ClientRequestIdValidationTests(unittest.TestCase):
    def test_accepts_bounded_safe_token(self) -> None:
        value = "client.req-42:abc_9"
        self.assertEqual(validate_client_request_id(value), value)
        self.assertEqual(
            validate_client_request_id("a" * MAX_CLIENT_REQUEST_ID_LENGTH),
            "a" * MAX_CLIENT_REQUEST_ID_LENGTH,
        )

    def test_refuses_invalid_values(self) -> None:
        for value in ("", " leading", "trailing ", "line\nbreak", "tab\tvalue", "x/y", "é", "a" * (MAX_CLIENT_REQUEST_ID_LENGTH + 1)):
            with self.subTest(value=repr(value)), self.assertRaises(ValueError):
                validate_client_request_id(value)


class ClientRequestIdIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = GatewayHarness()
        self.addCleanup(self.harness.close)
        with self.harness.config.mutate() as config:
            config.aliases["correlate"] = Alias(
                name="correlate",
                enabled=True,
                primary=RouteRef(FAKE_PROVIDER, "gpt-6-luna"),
            )

    def test_records_client_id_beside_gateway_authoritative_id(self) -> None:
        status, _, _ = self.harness.request(
            "POST",
            "/v1/chat/completions",
            body={"model": "correlate", "messages": [{"role": "user", "content": "hi"}]},
            headers={"X-Request-Id": "customer-42"},
        )
        self.assertEqual(status, 200)
        ledger = self.harness.router._ledger
        assert isinstance(ledger, RequestLedger)
        event = ledger.recent(limit=1)[0]
        self.assertEqual(event["client_request_id"], "customer-42")
        self.assertTrue(event["request_id"].startswith("req_"))
        self.assertNotEqual(event["request_id"], event["client_request_id"])
        self.assertTrue(ledger.amend(event["request_id"], token_counters={"prompt_tokens": 3}))
        self.assertEqual(ledger.recent(limit=1)[0]["client_request_id"], "customer-42")

    def test_invalid_client_id_is_rejected_and_not_echoed_or_recorded(self) -> None:
        status, body, _ = self.harness.request(
            "POST",
            "/v1/chat/completions",
            body={"model": "correlate", "messages": [{"role": "user", "content": "hi"}]},
            headers={"X-Request-Id": "bad/id-secret"},
        )
        self.assertEqual(status, 400)
        self.assertNotIn("bad", str(body))
        ledger = self.harness.router._ledger
        assert isinstance(ledger, RequestLedger)
        self.assertEqual(ledger.recent(limit=10), [])
