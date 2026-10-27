"""Tests for the provider adapters.

The upstream transport is faked. No test contacts a provider.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from gateway.adapters.base import (
    AdapterError,
    ProviderRequest,
    classify_status,
    error_from_response,
)
from gateway.adapters.chatgpt_codex import (
    DEVICE_APPROVED,
    DEVICE_PENDING,
    ChatGPTCodexAdapter,
)
from gateway.adapters.opencode_go import OpenCodeGoAdapter, _default_snapshot_path
from gateway.adapters.opencode_console import OpenCodeConsoleAdapter
from gateway.adapters.http_client import UpstreamResponse
from gateway.core.capabilities import (
    DIALECT_ANTHROPIC_MESSAGES,
    DIALECT_GOOGLE_V1BETA,
    DIALECT_OPENAI_CHAT,
    DIALECT_OPENAI_RESPONSES,
)
from gateway.credentials import CredentialStore


class FakeUpstream:
    """Records requests and returns scripted responses."""

    def __init__(self, responses: list[UpstreamResponse] | None = None) -> None:
        self.responses = list(responses or [])
        self.requests: list[dict] = []

    def request(self, method, url, headers, body=None, stream=False, provider_id="", **kwargs):
        self.requests.append({"method": method, "url": url, "headers": headers, "body": body})
        if not self.responses:
            return UpstreamResponse(200, {}, b"{}", None, "application/json")
        return self.responses.pop(0)


class SnapshotTests(unittest.TestCase):
    def test_the_snapshot_is_found_in_both_layouts(self) -> None:
        # Regression: the snapshot lives beside the source tree in one layout
        # and beside the package in the container layout. Both must resolve.
        path = _default_snapshot_path()
        self.assertTrue(path.is_file(), f"the catalogue snapshot was not found at {path}")

    def test_the_snapshot_covers_every_live_id(self) -> None:
        snapshot = json.loads(_default_snapshot_path().read_text(encoding="utf-8"))
        models = snapshot["models"]
        # 38 live ids (observed 2026-10-25) plus two rows the provider has
        # stopped serving, which stay in the catalogue as deprecated.
        self.assertEqual(len(models), 40)
        self.assertEqual(snapshot["model_count"], 40)
        # The nine ids the provider serves but does not document.
        unknown = sorted(k for k, v in models.items() if not v["dialect"])
        self.assertEqual(
            unknown,
            [
                "glm-5.1",
                "grok-4.6",
                "iceman-contributor-free",
                "kimi-k2.6",
                "minimax-m2.5",
                "minimax-m2.7",
                "omen-alpha",
                "qwen3.6-plus",
                "qwen3.7-max",
            ],
        )

    def test_the_snapshot_limits_match_the_published_table(self) -> None:
        snapshot = json.loads(_default_snapshot_path().read_text(encoding="utf-8"))
        models = snapshot["models"]
        # Regression: v2 recorded DeepSeek V4 Pro as $80. It is $15.
        self.assertEqual(models["deepseek-v4-pro"]["monthly_limit_usd"], 15.0)
        self.assertEqual(models["deepseek-v4.1-flash"]["monthly_limit_usd"], 80.0)
        self.assertEqual(models["gpt-6-luna"]["monthly_limit_usd"], 15.0)
        self.assertEqual(models["mimo-v2.6-flash"]["monthly_limit_usd"], 60.0)
        self.assertEqual(models["minimax-m3.1-flash"]["monthly_limit_usd"], 20.0)
        self.assertNotIn("minimax-m3.1-flash-free", models)

    def test_the_snapshot_keeps_model_provider_context_and_thinking_evidence_separate(self) -> None:
        models = json.loads(_default_snapshot_path().read_text(encoding="utf-8"))["models"]
        mimo = models["mimo-v2.6-flash"]
        self.assertEqual(mimo["context_window"], 1_048_576)
        self.assertEqual(mimo["context_limit"], 1_048_576)
        self.assertEqual(mimo["max_output_tokens"], 131_072)
        self.assertEqual(mimo["context_source"], "https://platform.xiaomimimo.com/docs/en-US/quick-start/model")
        minimax = models["minimax-m3.1-flash"]
        self.assertEqual(minimax["context_window"], 1_000_000)
        self.assertEqual(minimax["reasoning_efforts"], ["low", "medium", "high", "xhigh", "max"])
        # MiniMax publishes no maximum output, so the 512,000-token one is
        # transcribed from the OpenCode data page, which is what
        # ``context_source`` therefore names for this row.
        self.assertEqual(minimax["max_output_tokens"], 512_000)
        self.assertEqual(minimax["context_source"],
                         "https://opencode.ai/data/minimax/minimax-m3-1-flash")

    def test_the_snapshot_marks_the_free_models(self) -> None:
        snapshot = json.loads(_default_snapshot_path().read_text(encoding="utf-8"))
        models = snapshot["models"]
        for model_id in ("beam-free", "longcat-2.5-preview-free"):
            self.assertIsNone(models[model_id]["monthly_limit_usd"])
            self.assertEqual(models[model_id]["limits_source"], "published_estimate")

    def test_the_snapshot_marks_the_training_permitted_models(self) -> None:
        snapshot = json.loads(_default_snapshot_path().read_text(encoding="utf-8"))
        models = snapshot["models"]
        for model_id in ("gemini-4-argon-contributor", "muse-spark-1.2-contributor", "muse-spark-1.3-contributor"):
            self.assertEqual(models[model_id]["retention_class"], "training_permitted")
        self.assertEqual(models["gpt-6-luna"]["retention_class"], "30_days")
        self.assertEqual(models["mimo-v2.6-flash"]["retention_class"], "0_days")


class OpenCodeGoAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = CredentialStore(Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _adapter(self, responses=None) -> OpenCodeGoAdapter:
        return OpenCodeGoAdapter(
            self.store.provider_handle("opencode_go"),
            client=FakeUpstream(responses or []),
        )

    def test_catalogue_ids_are_joined_to_the_published_metadata(self) -> None:
        payload = json.dumps({"data": [
            {"id": "gpt-6-luna"}, {"id": "mimo-v2.6-flash"},
            {"id": "minimax-m3.1-flash"}, {"id": "omen-alpha"},
        ]}).encode()
        adapter = self._adapter([UpstreamResponse(200, {}, payload, None, "application/json")])
        models = adapter.list_models()
        by_id = {m.model_id: m for m in models}
        self.assertEqual(by_id["gpt-6-luna"].dialect, DIALECT_OPENAI_RESPONSES)
        self.assertEqual(by_id["gpt-6-luna"].monthly_limit_usd, 15.0)
        self.assertEqual(by_id["gpt-6-luna"].retention_class, "30_days")
        self.assertEqual(by_id["mimo-v2.6-flash"].dialect, DIALECT_OPENAI_CHAT)
        self.assertEqual(by_id["mimo-v2.6-flash"].context_limit, 1_048_576)
        self.assertEqual(by_id["mimo-v2.6-flash"].max_output_tokens, 131_072)
        self.assertEqual(by_id["minimax-m3.1-flash"].context_limit, 1_000_000)
        self.assertEqual(by_id["minimax-m3.1-flash"].reasoning_efforts, ("low", "medium", "high", "xhigh", "max"))
        # A model the provider serves but does not document stays unknown.
        self.assertEqual(by_id["omen-alpha"].dialect, "")
        self.assertIsNone(by_id["omen-alpha"].monthly_limit_usd)
        self.assertEqual(by_id["omen-alpha"].limits_source, "unknown")

    def test_the_catalogue_endpoint_is_the_documented_one(self) -> None:
        adapter = self._adapter([UpstreamResponse(200, {}, b'{"data":[]}', None, "application/json")])
        adapter.list_models()
        self.assertEqual(adapter._client.requests[0]["url"], "https://opencode.ai/inference/go/v1/models")

    def test_a_model_with_no_published_dialect_is_refused(self) -> None:
        adapter = self._adapter()
        with self.assertRaises(AdapterError) as ctx:
            adapter.endpoint_for("omen-alpha")
        self.assertIn("not documented", ctx.exception.message)

    def test_a_model_absent_from_the_snapshot_is_refused(self) -> None:
        adapter = self._adapter()
        with self.assertRaises(AdapterError) as ctx:
            adapter.endpoint_for("some-model-that-was-never-published")
        self.assertIn("no published endpoint", ctx.exception.message)

    def test_a_non_openai_dialect_is_refused_rather_than_guessed(self) -> None:
        adapter = self._adapter()
        self.assertEqual(adapter.endpoint_for("claude-haiku-5-5")[0], DIALECT_ANTHROPIC_MESSAGES)
        with self.assertRaises(AdapterError) as ctx:
            adapter.invoke(ProviderRequest(model="claude-haiku-5-5", payload={}))
        self.assertIn("does not translate", ctx.exception.message)

    def test_the_google_dialect_is_refused_rather_than_guessed(self) -> None:
        adapter = self._adapter()
        self.assertEqual(adapter.endpoint_for("gemini-4-argon-contributor")[0], DIALECT_GOOGLE_V1BETA)
        with self.assertRaises(AdapterError):
            adapter.invoke(ProviderRequest(model="gemini-4-argon-contributor", payload={}))

    def test_request_hygiene_headers_are_set(self) -> None:
        adapter = self._adapter([UpstreamResponse(200, {}, b"{}", None, "application/json")])
        self.store.provider_handle("opencode_go").write({"api_key": "test-key-not-real"})
        adapter.invoke(ProviderRequest(model="gpt-6-luna", payload={"a": 1}, session_id="session-42"))
        headers = adapter._client.requests[0]["headers"]
        self.assertEqual(headers["x-opencode-session"], "session-42")
        self.assertEqual(headers["User-Agent"], "personal-llm-gateway/2.0")
        self.assertEqual(headers["Authorization"], "Bearer test-key-not-real")

    def test_no_authorization_header_without_a_key(self) -> None:
        adapter = self._adapter([UpstreamResponse(200, {}, b"{}", None, "application/json")])
        adapter.invoke(ProviderRequest(model="gpt-6-luna", payload={}))
        self.assertNotIn("Authorization", adapter._client.requests[0]["headers"])

    def test_a_test_without_a_credential_reports_not_configured(self) -> None:
        adapter = self._adapter()
        result = adapter.test_connection()
        self.assertFalse(result.ok)
        self.assertEqual(result.detail["reason"], "not_configured")


class OpenCodeConsoleAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = CredentialStore(Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_catalogue_and_systemone_use_the_documented_native_contract(self) -> None:
        upstream = FakeUpstream([
            UpstreamResponse(200, {}, b'{"object":"list","data":[{"id":"jev-1.13","object":"model"},{"id":"gpt-6-luna","object":"model"}]}', None, "application/json"),
            UpstreamResponse(200, {}, b'{"model":"jev-1.13","answers":{},"usage":{"input_tokens":1,"output_tokens":1}}', None, "application/json"),
        ])
        self.store.provider_handle("opencode_console").write({"api_key": "synthetic-test-key"})
        adapter = OpenCodeConsoleAdapter(self.store.provider_handle("opencode_console"), client=upstream)
        models = adapter.list_models()
        self.assertEqual(models[0].model_id, "jev-1.13")
        self.assertEqual(models[0].dialect, "systemone")
        body = adapter.invoke_systemone({"model": "jev-1.13", "state": "synthetic state", "questions": {"q": {"type": "noul"}}})
        self.assertIn(b'"usage"', body)
        self.assertEqual(upstream.requests[0]["url"], "https://opencode.ai/inference/v1/models")
        self.assertEqual(upstream.requests[1]["url"], "https://opencode.ai/inference/v1/systemone")
        self.assertEqual(upstream.requests[1]["headers"]["Authorization"], "Bearer synthetic-test-key")

    def test_test_refuses_without_a_credential(self) -> None:
        adapter = OpenCodeConsoleAdapter(self.store.provider_handle("opencode_console"), client=FakeUpstream())
        result = adapter.test_connection()
        self.assertFalse(result.ok)
        self.assertEqual(result.detail["reason"], "not_configured")


class DeviceCodeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = CredentialStore(Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _adapter(self, responses=None) -> ChatGPTCodexAdapter:
        return ChatGPTCodexAdapter(
            self.store.provider_handle("chatgpt_codex"),
            client=FakeUpstream(responses),
        )

    def test_the_user_code_request_uses_the_documented_contract(self) -> None:
        payload = json.dumps({"device_auth_id": "did", "user_code": "ABCD-EFGH", "interval": 5}).encode()
        adapter = self._adapter([UpstreamResponse(200, {}, payload, None, "application/json")])
        authorization = adapter.start_device_authorization()
        request = adapter._client.requests[0]
        self.assertEqual(request["method"], "POST")
        self.assertEqual(request["url"], "https://auth.openai.com/api/accounts/deviceauth/usercode")
        self.assertEqual(json.loads(request["body"]), {"client_id": adapter._client_id})
        self.assertEqual(authorization.verification_url, "https://auth.openai.com/codex/device")
        self.assertEqual(authorization.interval, 5)
        self.assertEqual(authorization.state, DEVICE_PENDING)

    def test_polling_keeps_waiting_on_403(self) -> None:
        user_code = json.dumps({"device_auth_id": "did", "user_code": "ABCD-EFGH", "interval": 5}).encode()
        adapter = self._adapter(
            [
                UpstreamResponse(200, {}, user_code, None, "application/json"),
                UpstreamResponse(403, {}, b"", None, "application/json"),
            ]
        )
        authorization = adapter.start_device_authorization()
        authorization = adapter.poll_device_authorization(authorization)
        self.assertEqual(authorization.state, DEVICE_PENDING)
        self.assertEqual(adapter._client.requests[1]["url"], "https://auth.openai.com/api/accounts/deviceauth/token")

    def test_a_successful_poll_exchanges_and_stores_the_token(self) -> None:
        code_payload = json.dumps(
            {"authorization_code": "ac", "code_challenge": "cc", "code_verifier": "cv"}
        ).encode()
        token_payload = json.dumps(
            {"id_token": "header." + "e30" + ".sig", "access_token": "at", "refresh_token": "rt"}
        ).encode()
        user_code = json.dumps({"device_auth_id": "did", "user_code": "ABCD-EFGH", "interval": 5}).encode()
        adapter = self._adapter(
            [
                UpstreamResponse(200, {}, user_code, None, "application/json"),
                UpstreamResponse(200, {}, code_payload, None, "application/json"),
                UpstreamResponse(200, {}, token_payload, None, "application/json"),
            ]
        )
        authorization = adapter.start_device_authorization()
        authorization = adapter.poll_device_authorization(authorization)
        self.assertEqual(authorization.state, DEVICE_APPROVED)

        stored = self.store.provider_handle("chatgpt_codex").read()
        self.assertEqual(stored["access_token"], "at")
        self.assertEqual(stored["refresh_token"], "rt")

    def test_the_token_exchange_uses_the_documented_form(self) -> None:
        code_payload = json.dumps(
            {"authorization_code": "ac", "code_challenge": "cc", "code_verifier": "cv"}
        ).encode()
        token_payload = json.dumps({"access_token": "at"}).encode()
        user_code = json.dumps({"device_auth_id": "did", "user_code": "ABCD-EFGH", "interval": 5}).encode()
        adapter = self._adapter(
            [
                UpstreamResponse(200, {}, user_code, None, "application/json"),
                UpstreamResponse(200, {}, code_payload, None, "application/json"),
                UpstreamResponse(200, {}, token_payload, None, "application/json"),
            ]
        )
        authorization = adapter.start_device_authorization()
        adapter.poll_device_authorization(authorization)
        exchange = adapter._client.requests[2]
        self.assertEqual(exchange["url"], "https://auth.openai.com/oauth/token")
        from urllib.parse import parse_qs

        form = {k: v[0] for k, v in parse_qs(exchange["body"].decode()).items()}
        self.assertEqual(form["grant_type"], "authorization_code")
        self.assertEqual(form["code"], "ac")
        self.assertEqual(form["code_verifier"], "cv")
        self.assertEqual(form["redirect_uri"], "https://auth.openai.com/deviceauth/callback")

    def test_the_upstream_is_the_documented_codex_base(self) -> None:
        adapter = self._adapter([UpstreamResponse(200, {}, b"{}", None, "application/json")])
        self.store.provider_handle("chatgpt_codex").write({"access_token": "at"})
        adapter.invoke(ProviderRequest(model="gpt-6-luna", payload={}))
        self.assertEqual(
            adapter._client.requests[0]["url"],
            "https://chatgpt.com/backend-api/codex/responses",
        )

    def test_disconnect_removes_the_local_credential_only(self) -> None:
        adapter = self._adapter()
        self.store.provider_handle("chatgpt_codex").write({"access_token": "at"})
        self.assertTrue(adapter.disconnect())
        self.assertIsNone(self.store.provider_handle("chatgpt_codex").read())


class ClassificationTests(unittest.TestCase):
    def test_a_plain_429_is_rate_limiting(self) -> None:
        self.assertEqual(classify_status(429, {}), "rate_limited")

    def test_a_structured_exhaustion_code_is_exhaustion(self) -> None:
        self.assertEqual(classify_status(429, {"error": {"code": "insufficient_quota"}}), "exhausted")

    def test_auth_failures_are_auth(self) -> None:
        self.assertEqual(classify_status(401, {}), "auth")
        self.assertEqual(classify_status(403, {}), "auth")

    def test_malformed_requests_are_malformed(self) -> None:
        self.assertEqual(classify_status(400, {}), "malformed")
        self.assertEqual(classify_status(404, {}), "malformed")

    def test_server_errors_are_transient(self) -> None:
        self.assertEqual(classify_status(500, {}), "transient")
        self.assertEqual(classify_status(503, {}), "transient")

    def test_an_unclassifiable_failure_is_unknown(self) -> None:
        self.assertEqual(classify_status(418, {}), "unknown")

    def test_policy_refusals_are_policy(self) -> None:
        self.assertEqual(
            classify_status(403, {"error": {"code": "unsupported_country_region_territory"}}),
            "policy",
        )

    def test_the_error_message_never_echoes_the_body(self) -> None:
        body = json.dumps({"error": {"message": "secret prompt text", "code": "bad_request"}}).encode()
        error = error_from_response(400, body, "p", json.loads(body))
        self.assertNotIn("secret prompt text", error.message)
        self.assertNotIn("bad_request", error.message)
        self.assertEqual(error.error_class, "malformed")


if __name__ == "__main__":
    unittest.main()
