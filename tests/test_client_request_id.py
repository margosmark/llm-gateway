"""Regression tests for non-authoritative client request correlation IDs."""

from __future__ import annotations

import http.client
import json
import unittest

from gateway.config import Alias, RouteRef
from gateway.http.app import CLIENT_REQUEST_ID_PATHS
from gateway.http.request_id import (
    MAX_CLIENT_REQUEST_ID_LENGTH,
    validate_client_request_id,
)
from gateway.observability import RequestLedger
from tests.test_anthropic_surface import _build_harness as _build_anthropic_harness
from tests.test_end_to_end import FAKE_PROVIDER, GatewayHarness
from tests.test_messages_handler import VALID


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


#: A native System One call: its ``state`` is caller content the ledger must
#: never carry, so it doubles as the metadata-only canary.
SYSTEMONE_BODY = {
    "model": "jev-1.13",
    "state": "client state text that must never be recorded",
    "questions": {"q": {"type": "noul"}},
}


class ClientRequestIdSurfaceCoverageTests(unittest.TestCase):
    """T27 holds on every inference surface, not only the OpenAI pair.

    QA (pack-d): ``POST /v1/messages`` and ``POST /v1/systemone`` used to
    reach their handlers without the ``X-Request-Id`` gate, so a malformed id
    was accepted and a valid one was never recorded while
    ``docs/RUNBOOK.md`` claimed the rule "for inference requests".
    """

    def test_messages_records_the_client_id_beside_the_gateway_id(self) -> None:
        harness = _build_anthropic_harness()
        self.addCleanup(harness.close)
        status, _, _ = harness.request(
            "POST",
            "/v1/messages",
            body=VALID,
            headers={"X-Request-Id": "customer-messages"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(harness.adapter.calls), 1)
        ledger = harness.router._ledger
        assert isinstance(ledger, RequestLedger)
        event = ledger.recent(limit=1)[0]
        self.assertEqual(event["client_request_id"], "customer-messages")
        self.assertTrue(event["request_id"].startswith("req_"))
        self.assertNotEqual(event["request_id"], event["client_request_id"])
        self.assertEqual(event["status"], 200)

    def test_messages_refuses_a_malformed_client_id_before_any_provider_call(self) -> None:
        harness = _build_anthropic_harness()
        self.addCleanup(harness.close)
        status, payload, _ = harness.request(
            "POST",
            "/v1/messages",
            body=VALID,
            headers={"X-Request-Id": "bad/id-secret"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["type"], "malformed_request")
        self.assertNotIn("bad", str(payload))
        self.assertEqual(harness.adapter.calls, [])
        ledger = harness.router._ledger
        assert isinstance(ledger, RequestLedger)
        self.assertEqual(ledger.recent(limit=10), [])

    def test_systemone_records_the_client_id_beside_the_gateway_id(self) -> None:
        harness = GatewayHarness()
        self.addCleanup(harness.close)
        status, _, _ = harness.request(
            "POST",
            "/v1/systemone",
            body=SYSTEMONE_BODY,
            headers={"X-Request-Id": "customer-systemone"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(harness.jev.calls), 1)
        ledger = harness.router._ledger
        assert isinstance(ledger, RequestLedger)
        event = ledger.recent(limit=1)[0]
        self.assertEqual(event["client_request_id"], "customer-systemone")
        self.assertTrue(event["request_id"].startswith("req_"))
        self.assertNotEqual(event["request_id"], event["client_request_id"])
        self.assertEqual(event["status"], 200)
        self.assertEqual(event["serving_provider"], "opencode_console")
        # Metadata only: the caller's state never reaches the ledger.
        self.assertNotIn("client state text", str(event))

    def test_systemone_refuses_a_malformed_client_id_without_calling_the_provider(self) -> None:
        harness = GatewayHarness()
        self.addCleanup(harness.close)
        status, payload, _ = harness.request(
            "POST",
            "/v1/systemone",
            body=SYSTEMONE_BODY,
            headers={"X-Request-Id": "bad/id-secret"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["type"], "malformed_request")
        self.assertNotIn("bad", str(payload))
        self.assertEqual(harness.jev.calls, [])
        ledger = harness.router._ledger
        assert isinstance(ledger, RequestLedger)
        self.assertEqual(ledger.recent(limit=10), [])

    def test_repeated_client_id_headers_are_refused_on_every_inference_surface(self) -> None:
        """A repeated header is rejected before any handler, on all four paths.

        The body is deliberately meaningless: the gate runs after routing and
        before authorization, so a repeated id must be the answer whatever the
        payload says.
        """
        harness = GatewayHarness()
        self.addCleanup(harness.close)
        body = json.dumps({"model": "does-not-matter"}).encode("utf-8")
        for path in CLIENT_REQUEST_ID_PATHS:
            with self.subTest(path=path):
                connection = http.client.HTTPConnection(harness.authority, harness.port, timeout=10)
                self.addCleanup(connection.close)
                connection.putrequest("POST", path, skip_host=True, skip_accept_encoding=True)
                connection.putheader("Host", f"{harness.authority}:{harness.port}")
                connection.putheader("Content-Type", "application/json")
                connection.putheader("Content-Length", str(len(body)))
                connection.putheader("X-Request-Id", "first-value")
                connection.putheader("X-Request-Id", "second-value")
                connection.endheaders(body)
                response = connection.getresponse()
                payload = json.loads(response.read().decode("utf-8"))
                self.assertEqual(response.status, 400, payload)
                self.assertEqual(payload["error"]["type"], "malformed_request")
                self.assertEqual(payload["error"]["message"], "invalid X-Request-Id")
                self.assertNotIn("first-value", str(payload))
