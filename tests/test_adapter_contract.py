"""The shared adapter contract.

Regression cover for **R8**: the ChatGPT/Codex adapter's ``list_models()``
returned a list of strings, while the generic refresh handler needs
``ModelInfo`` attributes. A normal ChatGPT refresh therefore hit the
generic 500 path instead of populating the model list.

The contract under test:

* every adapter's ``list_models()`` returns ``list[ModelInfo]``;
* the wire dialect is right for each provider - Responses for
  ChatGPT/Codex, and the published per-model dialect for OpenCode Go;
* the **generic** refresh handler works for both real adapters, over HTTP.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from gateway.adapters.base import ProviderRequest, ProviderResponse
from gateway.adapters.chatgpt_codex import ChatGPTCodexAdapter
from gateway.adapters.http_client import UpstreamResponse
from gateway.adapters.opencode_go import OpenCodeGoAdapter
from gateway.adapters.registry import AdapterRegistry
from gateway.auth import ProfileAuth
from gateway.config import ConfigStore
from gateway.core.capabilities import DIALECT_OPENAI_RESPONSES, ModelInfo
from gateway.core.routing import Router
from gateway.credentials import CredentialStore
from gateway.http.app import GatewayApp
from gateway.http.core import Headers, Request
from gateway.http.security import SecurityPolicy, build_allowed_hosts, build_canonical_origins
from gateway.logging import RequestLogger
from gateway.observability import LimitStore, RequestLedger


class ScriptedUpstream:
    """A provider double that returns a scripted body."""

    def __init__(self, body: bytes) -> None:
        self.body = body
        self.requests: list[dict] = []

    def request(self, method, url, headers, body=None, stream=False, provider_id="", **kwargs):
        self.requests.append({"method": method, "url": url, "headers": headers})
        return UpstreamResponse(200, {}, self.body, None, "application/json")


class AdapterContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_the_codex_catalogue_shape_is_parsed(self) -> None:
        """The Codex catalogue identifies a model by its ``slug``.

        A parser that looks for ``id`` finds nothing and reports an empty
        catalogue - which is exactly what a silent refresh did.
        """
        payload = json.dumps(
            {
                "models": [
                    {
                        "slug": "gpt-6-luna",
                        "display_name": "GPT-6 Luna",
                        "supported_in_api": True,
                        "visibility": "list",
                        "context_window": 1050000,
                        "supported_reasoning_levels": [
                            {"effort": "none", "description": "no reasoning"},
                            {"effort": "medium", "description": "balanced"},
                            {"effort": "max", "description": "deepest"},
                        ],
                    },
                    {
                        "slug": "gpt-6-sol",
                        "supported_in_api": False,
                        "visibility": "hide",
                    },
                ]
            }
        ).encode()
        store = self._store()
        store.provider_handle("chatgpt_codex").write({"access_token": "synthetic"})
        adapter = ChatGPTCodexAdapter(
            store.provider_handle("chatgpt_codex"), client=ScriptedUpstream(payload)
        )
        models = adapter.list_models()
        by_id = {m.model_id: m for m in models}
        self.assertIn("gpt-6-luna", by_id)
        self.assertEqual(by_id["gpt-6-luna"].dialect, DIALECT_OPENAI_RESPONSES)
        self.assertEqual(by_id["gpt-6-luna"].context_window, 1050000)
        self.assertEqual(
            by_id["gpt-6-luna"].reasoning_efforts, ("none", "medium", "max")
        )
        # A model the account cannot use through the API is not offered.
        self.assertNotIn("gpt-6-sol", by_id)

    def test_codex_image_support_is_transcribed_per_exact_slug(self) -> None:
        """The Codex catalogue publishes no image field, so it is transcribed.

        ``gpt-5.6-terra`` is documented by OpenAI as accepting image input;
        ``gpt-6-luna`` has no such evidence, so it stays false. Image support
        is never inferred from the Responses dialect or a model family.
        """
        payload = json.dumps({
            "models": [
                {"slug": "gpt-5.6-terra", "supported_in_api": True},
                {"slug": "gpt-6-luna", "supported_in_api": True},
            ]
        }).encode()
        store = self._store()
        store.provider_handle("chatgpt_codex").write({"access_token": "synthetic"})
        adapter = ChatGPTCodexAdapter(
            store.provider_handle("chatgpt_codex"), client=ScriptedUpstream(payload)
        )
        by_id = {m.model_id: m for m in adapter.list_models()}
        self.assertTrue(by_id["gpt-5.6-terra"].supports_image_input)
        self.assertIn("developers.openai.com", by_id["gpt-5.6-terra"].notes or "")
        # Unevidenced stays false, and says so rather than claiming support.
        self.assertFalse(by_id["gpt-6-luna"].supports_image_input)
        self.assertNotIn("developers.openai.com", by_id["gpt-6-luna"].notes or "")

    def test_an_openai_shaped_catalogue_is_still_parsed(self) -> None:
        payload = json.dumps({"data": [{"id": "gpt-6-luna"}]}).encode()
        store = self._store()
        store.provider_handle("chatgpt_codex").write({"access_token": "synthetic"})
        adapter = ChatGPTCodexAdapter(
            store.provider_handle("chatgpt_codex"), client=ScriptedUpstream(payload)
        )
        models = adapter.list_models()
        self.assertEqual([m.model_id for m in models], ["gpt-6-luna"])

    def test_the_chatgpt_adapter_returns_model_info(self) -> None:
        payload = json.dumps({"models": [{"slug": "gpt-6-luna"}, {"slug": "gpt-6-sol"}]}).encode()
        store = self._store()
        store.provider_handle("chatgpt_codex").write({"access_token": "synthetic"})
        adapter = ChatGPTCodexAdapter(
            store.provider_handle("chatgpt_codex"),
            client=ScriptedUpstream(payload),
        )
        models = adapter.list_models()
        self.assertTrue(models)
        for model in models:
            self.assertIsInstance(model, ModelInfo)
            self.assertEqual(model.dialect, DIALECT_OPENAI_RESPONSES)
        self.assertEqual({m.model_id for m in models}, {"gpt-6-luna", "gpt-6-sol"})

    def test_the_chatgpt_adapter_reports_unknown_account_facts_as_unknown(self) -> None:
        payload = json.dumps({"models": [{"id": "gpt-6-luna"}]}).encode()
        store = self._store()
        store.provider_handle("chatgpt_codex").write({"access_token": "synthetic"})
        adapter = ChatGPTCodexAdapter(
            store.provider_handle("chatgpt_codex"),
            client=ScriptedUpstream(payload),
        )
        model = adapter.list_models()[0]
        self.assertIsNone(model.context_window)
        self.assertEqual(model.retention_class, "unknown")
        self.assertEqual(model.limits_source, "unknown")

    def test_the_model_list_request_carries_the_client_version(self) -> None:
        """The Codex client appends its version, and the backend expects it."""
        from gateway.adapters.chatgpt_codex import CHATGPT_CLIENT_VERSION

        store = self._store()
        store.provider_handle("chatgpt_codex").write({"access_token": "synthetic"})
        upstream = ScriptedUpstream(json.dumps({"models": [{"id": "gpt-6-luna"}]}).encode())
        adapter = ChatGPTCodexAdapter(
            store.provider_handle("chatgpt_codex"), client=upstream
        )
        adapter.list_models()
        url = upstream.requests[0]["url"]
        self.assertIn("/models", url)
        self.assertIn(f"client_version={CHATGPT_CLIENT_VERSION}", url)

    def test_the_client_version_is_a_whole_semantic_version(self) -> None:
        from gateway.adapters.chatgpt_codex import CHATGPT_CLIENT_VERSION

        self.assertRegex(CHATGPT_CLIENT_VERSION, r"^\d+\.\d+\.\d+$")

    def test_the_opencode_go_adapter_returns_model_info(self) -> None:
        payload = json.dumps({"data": [{"id": "gpt-6-luna"}, {"id": "mimo-v2.6-flash"}]}).encode()
        adapter = OpenCodeGoAdapter(
            self._store().provider_handle("opencode_go"),
            client=ScriptedUpstream(payload),
        )
        models = {m.model_id: m for m in adapter.list_models()}
        self.assertEqual(models["gpt-6-luna"].dialect, "openai_responses")
        self.assertEqual(models["mimo-v2.6-flash"].dialect, "openai_chat")

    def test_the_generic_refresh_handler_works_for_anthropic(self) -> None:
        from gateway.adapters.anthropic import AnthropicAdapter

        store = self._store()
        store.provider_handle("anthropic").write({"api_key": "synthetic"})
        upstream = ScriptedUpstream(json.dumps({"data": [{"id": "claude-synthetic"}]}).encode())
        adapter = AnthropicAdapter(store.provider_handle("anthropic"), client=upstream)
        app = self._app([adapter])
        status, result = self._refresh(app, "anthropic")
        self.assertEqual(status, 200, result)
        self.assertEqual(result["model_count"], 1)
        state = app.config.snapshot().providers["anthropic"]
        self.assertEqual(state.models["claude-synthetic"].dialect, "anthropic_messages")
        self.assertEqual(len(upstream.requests), 1)

    def _store(self) -> CredentialStore:
        return CredentialStore(self.root)

    # --- the generic refresh handler, over HTTP -------------------------

    def _app(self, adapters) -> GatewayApp:
        config = ConfigStore(self.root / "config.json")
        credentials = CredentialStore(self.root)
        registry = AdapterRegistry()
        for adapter in adapters:
            registry.register(adapter)
        router = Router(config, registry, ledger=RequestLedger())
        policy = SecurityPolicy(
            allowed_hosts=build_allowed_hosts("127.0.0.1", 8765),
            canonical_origins=build_canonical_origins("127.0.0.1", 8765),
        )
        return GatewayApp(
            config=config,
            credentials=credentials,
            router=router,
            registry=registry,
            profile_auth=ProfileAuth(config, credentials),
            policy=policy,
            ledger=RequestLedger(),
            limits=LimitStore(),
            release_identity=lambda: {},
            base_url="http://127.0.0.1:8765",
            mounts=("/owner",),
            logger=RequestLogger(enabled=False),
        )

    def _refresh(self, app: GatewayApp, provider_id: str):
        request = Request(
            method="POST",
            path="/owner/api/provider",
            raw_query="",
            headers=Headers(
                [("Host", "127.0.0.1:8765"), ("Origin", "http://127.0.0.1:8765")]
            ),
            body=json.dumps({"action": "refresh", "provider_id": provider_id}).encode("utf-8"),
            client_address=("127.0.0.1", 40000),
            server_port=8765,
        )
        response = app.handle(request)
        return response.status, json.loads(response.body.decode("utf-8"))

    def test_the_generic_refresh_handler_works_for_the_chatgpt_adapter(self) -> None:
        store = self._store()
        store.provider_handle("chatgpt_codex").write({"access_token": "synthetic"})
        payload = json.dumps({"models": [{"slug": "gpt-6-luna"}]}).encode()
        adapter = ChatGPTCodexAdapter(
            store.provider_handle("chatgpt_codex"), client=ScriptedUpstream(payload)
        )
        app = self._app([adapter])
        status, result = self._refresh(app, "chatgpt_codex")
        self.assertEqual(status, 200, result)
        self.assertEqual(result["model_count"], 1)

        state = app.handle(
            Request(
                method="GET",
                path="/owner/api/state",
                raw_query="",
                headers=Headers([("Host", "127.0.0.1:8765")]),
                body=b"",
                client_address=("127.0.0.1", 40000),
                server_port=8765,
            )
        )
        providers = json.loads(state.body.decode("utf-8"))["providers"]
        chatgpt = [p for p in providers if p["provider_id"] == "chatgpt_codex"][0]
        self.assertEqual(len(chatgpt["models"]), 1)
        self.assertEqual(chatgpt["models"][0]["model_id"], "gpt-6-luna")
        self.assertEqual(chatgpt["models"][0]["dialect"], DIALECT_OPENAI_RESPONSES)

    def test_the_generic_refresh_handler_works_for_the_opencode_go_adapter(self) -> None:
        store = self._store()
        store.provider_handle("opencode_go").write({"api_key": "synthetic"})
        payload = json.dumps({"data": [{"id": "gpt-6-luna"}, {"id": "mimo-v2.6-flash"}]}).encode()
        adapter = OpenCodeGoAdapter(
            store.provider_handle("opencode_go"), client=ScriptedUpstream(payload)
        )
        app = self._app([adapter])
        status, result = self._refresh(app, "opencode_go")
        self.assertEqual(status, 200, result)
        self.assertEqual(result["model_count"], 2)


if __name__ == "__main__":
    unittest.main()
