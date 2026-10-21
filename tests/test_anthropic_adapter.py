from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from gateway.adapters.anthropic import AnthropicAdapter
from gateway.adapters.base import AdapterError, ProviderRequest
from gateway.adapters.http_client import UpstreamResponse
from gateway.config import ERROR_AUTH, ERROR_EXHAUSTED, ERROR_MALFORMED, ERROR_POLICY, ERROR_RATE_LIMITED, ERROR_TRANSIENT
from gateway.credentials import CredentialStore


class FakeTransport:
    def __init__(self, responses=()):
        self.responses = list(responses)
        self.requests = []

    def request(self, method, url, headers, body=None, stream=False, provider_id="", **kwargs):
        self.requests.append((method, url, headers, body, stream, provider_id))
        return self.responses.pop(0)


class AnthropicAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.credentials = CredentialStore(Path(self.temp.name)).provider_handle("anthropic")
        self.credentials.write({"api_key": "synthetic-key"})

    def tearDown(self):
        self.temp.cleanup()

    def test_buffered_and_streamed_messages_requests(self):
        frames = iter([b'event: message_start\ndata: {"type":"message_start"}\n\n'])
        transport = FakeTransport([UpstreamResponse(200, {}, b'{"type":"message","content":[]}', None, "application/json"), UpstreamResponse(200, {}, None, frames, "text/event-stream")])
        adapter = AnthropicAdapter(self.credentials, transport)
        buffered = adapter.invoke(ProviderRequest(model="claude-synthetic", payload={"max_tokens": 8, "messages": []}, dialect="anthropic_messages"))
        streamed = adapter.invoke(ProviderRequest(model="claude-synthetic", payload={"max_tokens": 8, "messages": []}, stream=True, dialect="anthropic_messages"))
        self.assertEqual(buffered.json()["type"], "message")
        self.assertEqual(next(streamed.stream), b'event: message_start\ndata: {"type":"message_start"}\n\n')
        self.assertEqual([r[1] for r in transport.requests], ["https://api.anthropic.com/v1/messages"] * 2)
        self.assertEqual(transport.requests[0][2]["x-api-key"], "synthetic-key")
        self.assertEqual(transport.requests[0][2]["anthropic-version"], "2023-06-01")
        self.assertFalse(json.loads(transport.requests[0][3])["stream"])
        self.assertTrue(json.loads(transport.requests[1][3])["stream"])

    def test_only_native_dialect_is_accepted(self):
        adapter = AnthropicAdapter(self.credentials, FakeTransport())
        with self.assertRaises(AdapterError):
            adapter.invoke(ProviderRequest(model="claude-synthetic", payload={}, dialect="openai_chat"))

    def test_shared_error_classes(self):
        cases = [(402, None, ERROR_EXHAUSTED), (429, None, ERROR_RATE_LIMITED), (503, None, ERROR_TRANSIENT), (401, None, ERROR_AUTH), (403, {"error":{"type":"access_denied"}}, ERROR_POLICY), (400, None, ERROR_MALFORMED)]
        for status, payload, expected in cases:
            with self.subTest(status=status):
                body = json.dumps(payload or {}).encode()
                transport = FakeTransport([UpstreamResponse(status, {}, body, None, "application/json")])
                with self.assertRaises(AdapterError) as caught:
                    AnthropicAdapter(self.credentials, transport).invoke(ProviderRequest(model="claude-synthetic", payload={}, dialect="anthropic_messages"))
                self.assertEqual(caught.exception.error_class, expected)

    def test_model_list_contract_and_safe_unknown_capabilities(self):
        transport = FakeTransport([UpstreamResponse(200, {}, b'{"data":[{"id":"claude-synthetic"}]}', None, "application/json")])
        models = AnthropicAdapter(self.credentials, transport).list_models()
        self.assertEqual(len(models), 1)
        self.assertEqual(models[0].dialect, "anthropic_messages")
        self.assertEqual(models[0].monthly_limit_usd, None)
        self.assertEqual(models[0].context_window, None)


if __name__ == "__main__":
    unittest.main()
