"""T13: the plain OpenAI API-key provider, through the adapter seam.

Everything here uses synthetic values. No test contacts a real provider,
holds a real credential, or needs outbound network access: the provider is
either a scripted in-process double or a loopback HTTP server that only
this test talks to.

Coverage, by acceptance:

1. ``/v1/chat/completions`` and ``/v1/responses`` upstream on the buffered
   **and** streamed path - unit level (scripted double) and end to end
   (loopback fake OpenAI server, real ``UpstreamClient``, real SSE frames).
2. ``list_models()`` returns ``list[ModelInfo]`` and is exercised through
   the **generic** ``POST /owner/api/provider`` refresh handler.
3. Every upstream status and every documented upstream code maps into the
   shared error vocabulary; a raw status or body never leaks unclassified.
4. The discovered models carry the capability fields the router consults,
   and each refusal path fires: undocumented dialect, foreign dialect,
   unevidenced image input, absent credential.
5. The provider appears in the console and the state API and can be
   configured with a credential over HTTP.
6. Credential isolation: owner-only file, and the key never appears in an
   export-shaped response, the ledger, ``/version`` or a log line.
7. No implicit provider calls: page load, status, state, version and a
   configuration save produce zero upstream requests; only the explicit
   Test and Refresh actions produce one.
8. Refusal tests plus happy paths, all on the adapter seam.
"""

from __future__ import annotations

import io
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar

from gateway.adapters.base import (
    AdapterError,
    ProviderRequest,
    classify_status,
)
from gateway.adapters.http_client import UpstreamResponse
from gateway.adapters.openai_api import (
    PROVIDER_ID,
    SERVABLE_DIALECTS,
    OpenAIApiAdapter,
)
from gateway.adapters.registry import AdapterRegistry
from gateway.auth import ProfileAuth
from gateway.config import (
    ERROR_AUTH,
    ERROR_EXHAUSTED,
    ERROR_MALFORMED,
    ERROR_POLICY,
    ERROR_RATE_LIMITED,
    ERROR_ROUTE_GONE,
    ERROR_TRANSIENT,
    ERROR_UNKNOWN,
    Alias,
    ConfigStore,
    ModelEntry,
    ProviderState,
    RouteRef,
)
from gateway.core.capabilities import (
    DIALECT_ANTHROPIC_MESSAGES,
    DIALECT_GOOGLE_V1BETA,
    DIALECT_OPENAI_CHAT,
    DIALECT_OPENAI_RESPONSES,
    ModelInfo,
)
from gateway.core.routing import Router
from gateway.credentials import CredentialStore
from gateway.http.app import GatewayApp
from gateway.http.core import Headers, Request, Server
from gateway.http.security import (
    SecurityPolicy,
    build_allowed_hosts,
    build_canonical_origins,
)
from gateway.logging import RequestLogger
from gateway.observability import LimitStore, RequestLedger
from tests.owner_session import cookie_header

#: Synthetic values only. This key exists to prove isolation, never to
#: authenticate anywhere.
SYNTHETIC_KEY = "sk-synthetic-test-key-not-a-real-credential"

#: The shared vocabulary every adapter failure must land in.
SHARED_ERROR_CLASSES = frozenset(
    {
        ERROR_AUTH,
        ERROR_EXHAUSTED,
        ERROR_MALFORMED,
        ERROR_POLICY,
        ERROR_RATE_LIMITED,
        ERROR_TRANSIENT,
        ERROR_UNKNOWN,
        ERROR_ROUTE_GONE,
    }
)

UPSTREAM_SECRET = "upstream-body-text-must-never-appear"


class FakeUpstream:
    """Scripted provider double. Records every request it is asked to make."""

    def __init__(self, responses: list[UpstreamResponse] | None = None) -> None:
        self.responses = list(responses or [])
        self.requests: list[dict[str, Any]] = []

    def request(self, method, url, headers, body=None, stream=False, provider_id="", **kwargs):
        self.requests.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "body": body,
                "stream": stream,
                "kwargs": kwargs,
            }
        )
        if not self.responses:
            return UpstreamResponse(200, {}, b"{}", None, "application/json")
        return self.responses.pop(0)


def _response(status: int, payload: Any, stream: Any = None) -> UpstreamResponse:
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
    return UpstreamResponse(status, {}, body, stream, "application/json")


class AdapterTestCase(unittest.TestCase):
    """Scripted-upstream adapter harness."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = CredentialStore(Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _adapter(self, responses: list[UpstreamResponse] | None = None) -> tuple[OpenAIApiAdapter, FakeUpstream]:
        upstream = FakeUpstream(responses or [])
        adapter = OpenAIApiAdapter(self.store.provider_handle(PROVIDER_ID), client=upstream)
        return adapter, upstream

    def _with_key(self, key: str = SYNTHETIC_KEY) -> None:
        self.store.provider_handle(PROVIDER_ID).write({"api_key": key})


class CatalogueTests(AdapterTestCase):
    def test_the_catalogue_endpoint_is_the_documented_one(self) -> None:
        self._with_key()
        adapter, upstream = self._adapter([_response(200, {"data": [{"id": "gpt-6-mini"}]})])
        models = adapter.list_models()
        self.assertEqual([m.model_id for m in models], ["gpt-6-mini"])
        request = upstream.requests[0]
        self.assertEqual(request["method"], "GET")
        self.assertEqual(request["url"], "https://api.openai.com/v1/models")
        # A catalogue read is retry-safe: it has no side effect.
        self.assertTrue(request["kwargs"].get("retry_safe"))
        self.assertEqual(request["headers"]["Authorization"], f"Bearer {SYNTHETIC_KEY}")
        self.assertEqual(request["headers"]["User-Agent"], "personal-llm-gateway/2.0")

    def test_list_models_returns_model_info_with_every_published_fact_unknown(self) -> None:
        self._with_key()
        payload = {
            "object": "list",
            "data": [
                {"id": "gpt-6-luna", "object": "model", "created": 1, "owned_by": "openai"},
                {"id": "gpt-6-mini", "object": "model", "created": 2, "owned_by": "openai"},
            ],
        }
        adapter, _ = self._adapter([_response(200, payload)])
        models = adapter.list_models()
        self.assertEqual(len(models), 2)
        for model in models:
            self.assertIsInstance(model, ModelInfo)
            # The catalogue publishes no wire dialect, no window, no
            # limits and no retention class. All of it stays unknown
            # rather than inferred from the model id.
            self.assertEqual(model.dialect, "")
            self.assertIsNone(model.context_window)
            self.assertIsNone(model.context_limit)
            self.assertIsNone(model.max_output_tokens)
            self.assertIsNone(model.monthly_limit_usd)
            self.assertEqual(model.limits_source, "unknown")
            self.assertEqual(model.retention_class, "unknown")
            self.assertEqual(model.reasoning_efforts, ())
            self.assertEqual(model.source, "discovered")
            self.assertIsNone(model.notes)
            # Image input is a hard per-model capability and this
            # catalogue carries no evidence for any model.
            self.assertFalse(model.supports_image_input)
            self.assertFalse(model.supports_builtin_tools)
            # Both served endpoints document `stream` and `tools`.
            self.assertTrue(model.supports_streaming)
            self.assertTrue(model.supports_function_tools)

    def test_a_malformed_catalogue_body_yields_no_models(self) -> None:
        self._with_key()
        adapter, _ = self._adapter([_response(200, b"not json at all")])
        self.assertEqual(adapter.list_models(), [])

    def test_a_catalogue_body_without_data_yields_no_models(self) -> None:
        self._with_key()
        adapter, _ = self._adapter([_response(200, {"object": "list"})])
        self.assertEqual(adapter.list_models(), [])

    def test_a_refused_catalogue_read_is_classified_and_silent(self) -> None:
        self._with_key()
        body = {"error": {"message": UPSTREAM_SECRET, "type": "invalid_request_error"}}
        adapter, _ = self._adapter([_response(401, body)])
        with self.assertRaises(AdapterError) as ctx:
            adapter.list_models()
        self.assertEqual(ctx.exception.error_class, ERROR_AUTH)
        self.assertIn(ctx.exception.error_class, SHARED_ERROR_CLASSES)
        self.assertNotIn(UPSTREAM_SECRET, ctx.exception.message)

    def test_list_models_without_a_credential_refuses_locally(self) -> None:
        adapter, upstream = self._adapter()
        with self.assertRaises(AdapterError) as ctx:
            adapter.list_models()
        self.assertEqual(ctx.exception.error_class, ERROR_AUTH)
        self.assertIn(ctx.exception.provider_id, (PROVIDER_ID, None))
        # The refusal costs no upstream round trip.
        self.assertEqual(upstream.requests, [])


class InvocationTests(AdapterTestCase):
    def test_a_chat_dialect_request_reaches_chat_completions(self) -> None:
        self._with_key()
        payload = {"model": "gpt-6-mini", "messages": [{"role": "user", "content": "hi"}]}
        upstream_body = {"id": "cc1", "object": "chat.completion", "choices": []}
        adapter, upstream = self._adapter([_response(200, upstream_body)])
        response = adapter.invoke(
            ProviderRequest(model="gpt-6-mini", payload=payload, dialect=DIALECT_OPENAI_CHAT)
        )
        request = upstream.requests[0]
        self.assertEqual(request["method"], "POST")
        self.assertEqual(request["url"], "https://api.openai.com/v1/chat/completions")
        self.assertFalse(request["stream"])
        self.assertEqual(request["headers"]["Authorization"], f"Bearer {SYNTHETIC_KEY}")
        self.assertEqual(request["headers"]["Accept"], "application/json")
        # The payload travels unchanged: the router already substituted the
        # upstream model id, and the adapter is not a second conversion
        # point.
        self.assertEqual(json.loads(request["body"]), payload)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.json(), upstream_body)
        self.assertEqual(response.content_type, "application/json")
        self.assertEqual(response.dialect, DIALECT_OPENAI_CHAT)

    def test_a_responses_dialect_request_reaches_responses(self) -> None:
        self._with_key()
        payload = {"model": "gpt-6-luna", "input": "hi"}
        adapter, upstream = self._adapter([_response(200, {"id": "resp_1", "object": "response"})])
        response = adapter.invoke(
            ProviderRequest(model="gpt-6-luna", payload=payload, dialect=DIALECT_OPENAI_RESPONSES)
        )
        request = upstream.requests[0]
        self.assertEqual(request["url"], "https://api.openai.com/v1/responses")
        self.assertEqual(json.loads(request["body"]), payload)
        self.assertEqual(response.dialect, DIALECT_OPENAI_RESPONSES)

    def test_a_streamed_chat_request_is_forwarded_as_a_stream(self) -> None:
        self._with_key()
        frames = iter([b'data: {"choices":[]}\n\n', b"data: [DONE]\n\n"])
        adapter, upstream = self._adapter(
            [UpstreamResponse(200, {"content-type": "text/event-stream"}, None, frames, "text/event-stream")]
        )
        response = adapter.invoke(
            ProviderRequest(model="gpt-6-mini", payload={}, stream=True, dialect=DIALECT_OPENAI_CHAT)
        )
        request = upstream.requests[0]
        self.assertTrue(request["stream"])
        self.assertEqual(request["headers"]["Accept"], "text/event-stream")
        self.assertEqual(request["url"], "https://api.openai.com/v1/chat/completions")
        self.assertIs(response.stream, frames)
        self.assertIsNone(response.body)
        self.assertEqual(response.content_type, "text/event-stream")

    def test_a_streamed_responses_request_is_forwarded_as_a_stream(self) -> None:
        self._with_key()
        frames = iter([b'data: {"type":"response.completed"}\n\n'])
        adapter, upstream = self._adapter(
            [UpstreamResponse(200, {"content-type": "text/event-stream"}, None, frames, "text/event-stream")]
        )
        response = adapter.invoke(
            ProviderRequest(model="gpt-6-luna", payload={}, stream=True, dialect=DIALECT_OPENAI_RESPONSES)
        )
        self.assertTrue(upstream.requests[0]["stream"])
        self.assertEqual(upstream.requests[0]["url"], "https://api.openai.com/v1/responses")
        self.assertIs(response.stream, frames)

    def test_an_undocumented_dialect_is_refused_not_guessed(self) -> None:
        self._with_key()
        adapter, upstream = self._adapter()
        with self.assertRaises(AdapterError) as ctx:
            adapter.invoke(ProviderRequest(model="gpt-6-mini", payload={}, dialect=""))
        self.assertEqual(ctx.exception.error_class, ERROR_POLICY)
        self.assertIn("not documented", ctx.exception.message)
        self.assertEqual(upstream.requests, [])

    def test_a_foreign_dialect_is_refused_not_translated(self) -> None:
        self._with_key()
        adapter, upstream = self._adapter()
        for dialect in (DIALECT_ANTHROPIC_MESSAGES, DIALECT_GOOGLE_V1BETA, "systemone", "made-up"):
            with self.assertRaises(AdapterError) as ctx:
                adapter.invoke(ProviderRequest(model="m", payload={}, dialect=dialect))
            self.assertEqual(ctx.exception.error_class, ERROR_POLICY, dialect)
            self.assertIn("not served by the OpenAI API provider", ctx.exception.message)
        self.assertEqual(upstream.requests, [])

    def test_both_gateway_openai_dialects_are_servable(self) -> None:
        # The router converts between exactly these two; an adapter that
        # served only one of them would break the cross case.
        self.assertEqual(set(SERVABLE_DIALECTS), {DIALECT_OPENAI_CHAT, DIALECT_OPENAI_RESPONSES})

    def test_invoke_without_a_credential_refuses_locally(self) -> None:
        adapter, upstream = self._adapter()
        with self.assertRaises(AdapterError) as ctx:
            adapter.invoke(
                ProviderRequest(model="gpt-6-mini", payload={}, dialect=DIALECT_OPENAI_CHAT)
            )
        self.assertEqual(ctx.exception.error_class, ERROR_AUTH)
        self.assertEqual(upstream.requests, [])


class ErrorClassificationTests(AdapterTestCase):
    """Every failure the adapter can surface lands in the shared classes."""

    CASES: ClassVar[list[tuple[int, dict, str]]] = [
        (401, {}, ERROR_AUTH),
        (403, {}, ERROR_AUTH),
        (429, {}, ERROR_RATE_LIMITED),
        (429, {"error": {"code": "insufficient_quota"}}, ERROR_EXHAUSTED),
        (402, {}, ERROR_EXHAUSTED),
        (400, {}, ERROR_MALFORMED),
        (404, {}, ERROR_MALFORMED),
        (409, {}, ERROR_MALFORMED),
        (422, {}, ERROR_MALFORMED),
        (410, {}, ERROR_ROUTE_GONE),
        (410, {"error": {"code": "model_deprecated"}}, ERROR_ROUTE_GONE),
        (400, {"error": {"code": "model_not_found"}}, ERROR_ROUTE_GONE),
        (500, {}, ERROR_TRANSIENT),
        (503, {}, ERROR_TRANSIENT),
        (418, {}, ERROR_UNKNOWN),
    ]

    def test_each_upstream_outcome_maps_to_the_shared_vocabulary(self) -> None:
        for status, payload, expected in self.CASES:
            with self.subTest(status=status, payload=payload):
                self._with_key()
                body = {**payload, "error_message": UPSTREAM_SECRET}
                adapter, _ = self._adapter([_response(status, body)])
                with self.assertRaises(AdapterError) as ctx:
                    adapter.invoke(
                        ProviderRequest(model="m", payload={}, dialect=DIALECT_OPENAI_CHAT)
                    )
                error = ctx.exception
                self.assertEqual(error.error_class, expected)
                self.assertIn(error.error_class, SHARED_ERROR_CLASSES)
                # A raw status or body text never reaches the message
                # unclassified: the message is the fixed class text plus
                # gateway-authored metadata only.
                self.assertNotIn(UPSTREAM_SECRET, error.message)
                self.assertNotIn(str(status), error.message)

    def test_every_upstream_status_classifies_into_the_shared_classes(self) -> None:
        for status in range(400, 600):
            self.assertIn(classify_status(status), SHARED_ERROR_CLASSES, status)

    def test_the_error_dict_carries_the_class_not_only_the_status(self) -> None:
        self._with_key()
        adapter, _ = self._adapter([_response(503, {"error": {"message": UPSTREAM_SECRET}})])
        with self.assertRaises(AdapterError) as ctx:
            adapter.invoke(ProviderRequest(model="m", payload={}, dialect=DIALECT_OPENAI_CHAT))
        payload = ctx.exception.to_dict()
        self.assertEqual(payload["error_class"], ERROR_TRANSIENT)
        self.assertTrue(payload["retryable"])
        self.assertIn("upstream_code", payload)
        self.assertNotIn(UPSTREAM_SECRET, json.dumps(payload))


class ConnectionTests(AdapterTestCase):
    def test_a_test_without_a_credential_reports_not_configured(self) -> None:
        adapter, upstream = self._adapter()
        result = adapter.test_connection()
        self.assertFalse(result.ok)
        self.assertEqual(result.detail["reason"], "not_configured")
        self.assertEqual(upstream.requests, [])

    def test_a_successful_test_counts_the_models(self) -> None:
        self._with_key()
        adapter, _ = self._adapter([_response(200, {"data": [{"id": "a"}, {"id": "b"}]})])
        result = adapter.test_connection()
        self.assertTrue(result.ok)
        self.assertEqual(result.detail["model_count"], 2)

    def test_a_failing_test_reports_the_classified_failure(self) -> None:
        self._with_key()
        adapter, _ = self._adapter([_response(500, {"error": {"message": UPSTREAM_SECRET}})])
        result = adapter.test_connection()
        self.assertFalse(result.ok)
        self.assertEqual(result.detail["error_class"], ERROR_TRANSIENT)
        self.assertNotIn(UPSTREAM_SECRET, result.message)


# --- the owner surface over HTTP ---------------------------------------


class OwnerSurfaceHarness:
    """An in-process gateway holding the real adapter over a scripted upstream."""

    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.config = ConfigStore(self.root / "config.json")
        self.credentials = CredentialStore(self.root)
        self.upstream = FakeUpstream()
        self.registry = AdapterRegistry()
        self.adapter = OpenAIApiAdapter(
            self.credentials.provider_handle(PROVIDER_ID), client=self.upstream
        )
        self.registry.register(self.adapter)
        # Seed exactly what the CLI does for every registered provider.
        with self.config.mutate() as snapshot:
            snapshot.providers[PROVIDER_ID] = ProviderState(
                provider_id=PROVIDER_ID, kind=PROVIDER_ID
            )
        self.log_stream = io.StringIO()
        self.router = Router(self.config, self.registry, ledger=RequestLedger())
        policy = SecurityPolicy(
            allowed_hosts=build_allowed_hosts("127.0.0.1", 8765),
            canonical_origins=build_canonical_origins("127.0.0.1", 8765),
        )
        self.app = GatewayApp(
            config=self.config,
            credentials=self.credentials,
            router=self.router,
            registry=self.registry,
            profile_auth=ProfileAuth(self.config, self.credentials),
            policy=policy,
            ledger=self.router._ledger,
            limits=LimitStore(),
            release_identity=dict,
            base_url="http://127.0.0.1:8765",
            mounts=("/owner",),
            logger=RequestLogger(stream=self.log_stream, enabled=True),
        )
        self._session_cookie: str | None = None

    def close(self) -> None:
        self._tmp.cleanup()

    def request(self, method: str, path: str, body: Any = None):
        headers = [
            ("Host", "127.0.0.1:8765"),
            ("Origin", "http://127.0.0.1:8765"),
            ("Content-Type", "application/json"),
        ]
        if path.startswith("/owner/"):
            # Owner endpoints sit behind the sign-in gate; establish the
            # session once and reuse it, the way main's harnesses do.
            if self._session_cookie is None:
                self._session_cookie = cookie_header(self.app)
            headers.append(("Cookie", self._session_cookie))
        request = Request(
            method=method,
            path=path,
            raw_query="",
            headers=Headers(headers),
            body=json.dumps(body).encode("utf-8") if body is not None else b"",
            client_address=("127.0.0.1", 40000),
            server_port=8765,
        )
        response = self.app.handle(request)
        try:
            return response.status, json.loads(response.body.decode("utf-8"))
        except ValueError:
            return response.status, response.body.decode("utf-8", "replace")

    def refresh(self):
        return self.request(
            "POST", "/owner/api/provider", {"action": "refresh", "provider_id": PROVIDER_ID}
        )

    def enroll(self, key: str = SYNTHETIC_KEY):
        return self.request(
            "POST",
            "/owner/api/provider",
            {"action": "enroll_key", "provider_id": PROVIDER_ID, "api_key": key},
        )


class OwnerSurfaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = OwnerSurfaceHarness()

    def tearDown(self) -> None:
        self.harness.close()

    def test_the_provider_appears_in_the_state_api(self) -> None:
        status, state = self.harness.request("GET", "/owner/api/state")
        self.assertEqual(status, 200)
        providers = {p["provider_id"]: p for p in state["providers"]}
        self.assertIn(PROVIDER_ID, providers)
        provider = providers[PROVIDER_ID]
        self.assertEqual(provider["kind"], PROVIDER_ID)
        self.assertFalse(provider["credential_present"])
        self.assertEqual(provider["models"], [])

    def test_the_console_is_served_and_labels_the_provider(self) -> None:
        status, body = self.harness.request("GET", "/owner/")
        self.assertEqual(status, 200)
        self.assertIn("<html", body.lower())
        # The console must know how to render the new card.
        console_js = (
            Path(__file__).resolve().parents[1] / "src" / "gateway" / "static" / "console.js"
        ).read_text(encoding="utf-8")
        self.assertIn('providerId === "openai_api"', console_js)
        self.assertIn('"OpenAI API"', console_js)

    def test_a_key_can_be_enrolled_and_is_never_echoed(self) -> None:
        status, result = self.harness.enroll()
        self.assertEqual(status, 200, result)
        self.assertEqual(result, {"ok": True, "stored": True})
        self.assertNotIn(SYNTHETIC_KEY, json.dumps(result))
        described = self.harness.credentials.describe(PROVIDER_ID)
        self.assertTrue(described["present"])
        self.assertEqual(described["fields"], ["api_key"])
        self.assertNotIn(SYNTHETIC_KEY, json.dumps(described))
        _, state = self.harness.request("GET", "/owner/api/state")
        provider = next(p for p in state["providers"] if p["provider_id"] == PROVIDER_ID)
        self.assertTrue(provider["credential_present"])

    def test_configuration_reads_and_saves_never_call_the_provider(self) -> None:
        """Acceptance 7: no implicit provider calls, on any owner surface."""
        self.harness.enroll()
        self.harness.request("GET", "/owner/")
        self.harness.request("GET", "/version")
        self.harness.request("GET", "/owner/api/status")
        self.harness.request("GET", "/owner/api/state")
        # A configuration save: a hand-entered model, persisted.
        self.harness.request(
            "POST",
            "/owner/api/model",
            {
                "action": "manual",
                "provider_id": PROVIDER_ID,
                "model_id": "gpt-6-mini",
                "dialect": DIALECT_OPENAI_CHAT,
                "enabled": True,
            },
        )
        self.assertEqual(
            self.harness.upstream.requests,
            [],
            "a page load, a status read or a configuration save must never call the provider",
        )
        # The explicit Refresh action is what finally reaches out - once.
        status, result = self.harness.refresh()
        self.assertEqual(status, 200, result)
        self.assertEqual(len(self.harness.upstream.requests), 1)
        # ...and so does the explicit Test action - once more.
        status, _ = self.harness.request(
            "POST",
            "/owner/api/provider",
            {"action": "test", "provider_id": PROVIDER_ID},
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(self.harness.upstream.requests), 2)

    def test_the_generic_refresh_handler_discovers_the_models(self) -> None:
        self.harness.enroll()
        self.harness.upstream.responses = [
            _response(200, {"data": [{"id": "gpt-6-luna"}, {"id": "gpt-6-mini"}]})
        ]
        status, result = self.harness.refresh()
        self.assertEqual(status, 200, result)
        self.assertEqual(result["model_count"], 2)
        self.assertIn("observed_at", result)

        _, state = self.harness.request("GET", "/owner/api/state")
        provider = next(p for p in state["providers"] if p["provider_id"] == PROVIDER_ID)
        self.assertEqual(len(provider["models"]), 2)
        by_id = {m["model_id"]: m for m in provider["models"]}
        self.assertEqual(by_id["gpt-6-luna"]["source"], "discovered")
        # No dialect was published, so none was invented: the entry keeps
        # no dialect at all, which the router reads as undocumented.
        self.assertIsNone(by_id["gpt-6-luna"]["dialect"])
        self.assertEqual(by_id["gpt-6-luna"]["limits_source"], "unknown")
        self.assertTrue(provider["catalog_observed_at"])

    def test_a_refresh_without_a_credential_is_refused_as_auth(self) -> None:
        self.harness.upstream.responses = [_response(200, {"data": []})]
        status, result = self.harness.refresh()
        self.assertEqual(status, 502)
        self.assertEqual(result["error"]["type"], ERROR_AUTH)
        self.assertEqual(self.harness.upstream.requests, [])

    def test_a_key_that_the_upstream_rejects_is_classified_as_auth(self) -> None:
        self.harness.enroll("wrong-key")
        self.harness.upstream.responses = [
            _response(
                401,
                {"error": {"message": UPSTREAM_SECRET, "code": "invalid_api_key"}},
            )
        ]
        status, result = self.harness.refresh()
        self.assertEqual(status, 502)
        self.assertEqual(result["error"]["type"], ERROR_AUTH)
        self.assertNotIn(UPSTREAM_SECRET, json.dumps(result))


class CapabilityRoutingTests(unittest.TestCase):
    """The models carry the capability fields the router consults, and each
    refusal path exists: undocumented dialect, unevidenced image input,
    unsupported streaming - plus the effort preference that must NOT refuse.
    """

    def setUp(self) -> None:
        self.harness = OwnerSurfaceHarness()
        self.harness.enroll()
        self.harness.upstream.responses = [
            _response(200, {"data": [{"id": "gpt-6-mini"}]})
        ]
        status, _ = self.harness.refresh()
        self.assertEqual(status, 200)
        self.model = "gpt-6-mini"
        # A refresh stages models disabled; enable this one the way the
        # owner would, without yet choosing a dialect.
        status, result = self.harness.request(
            "POST",
            "/owner/api/model",
            {
                "action": "manual",
                "provider_id": PROVIDER_ID,
                "model_id": self.model,
                "enabled": True,
            },
        )
        self.assertEqual(status, 200, result)
        with self.harness.config.mutate() as config:
            config.aliases["probe"] = Alias(
                name="probe",
                enabled=True,
                primary=RouteRef(PROVIDER_ID, self.model),
            )

    def tearDown(self) -> None:
        self.harness.close()

    def _candidates(self, **kwargs):
        alias = self.harness.config.snapshot().aliases["probe"]
        return self.harness.router.candidates(
            alias, source_dialect=kwargs.pop("source_dialect", DIALECT_OPENAI_CHAT), **kwargs
        )

    def test_an_undocumented_dialect_excludes_the_route(self) -> None:
        candidate = self._candidates()[0]
        self.assertFalse(candidate.eligible)
        self.assertEqual(
            candidate.excluded_reason, "the wire dialect for this model is not documented"
        )

    def test_the_owner_dialect_edit_makes_the_route_eligible(self) -> None:
        status, result = self.harness.request(
            "POST",
            "/owner/api/model",
            {
                "action": "manual",
                "provider_id": PROVIDER_ID,
                "model_id": self.model,
                "dialect": DIALECT_OPENAI_CHAT,
                "enabled": True,
            },
        )
        self.assertEqual(status, 200, result)
        candidate = self._candidates()[0]
        self.assertTrue(candidate.eligible, candidate.excluded_reason)
        capability = candidate.capability
        self.assertEqual(capability.dialect, DIALECT_OPENAI_CHAT)
        self.assertTrue(capability.supports_streaming)
        self.assertTrue(capability.supports_function_tools)
        self.assertFalse(capability.supports_image_input)

    def test_an_image_request_is_refused_because_no_model_has_evidence(self) -> None:
        self._set_dialect()
        candidate = self._candidates(needs_image_input=True)[0]
        self.assertFalse(candidate.eligible)
        self.assertEqual(
            candidate.excluded_reason, "this model does not support image input"
        )

    def test_streaming_and_tools_stay_eligible(self) -> None:
        self._set_dialect()
        self.assertTrue(self._candidates(stream=True)[0].eligible)
        self.assertTrue(self._candidates(needs_tools=True)[0].eligible)

    def test_an_absent_reasoning_effort_is_a_droppable_preference(self) -> None:
        self._set_dialect()
        candidate = self._candidates(reasoning_effort="xhigh")[0]
        # Not excluded: an effort is a preference, and the substitution is
        # recorded rather than silent.
        self.assertTrue(candidate.eligible, candidate.excluded_reason)
        self.assertIn("will be dropped for this route", candidate.degraded_reason)

    def _set_dialect(self) -> None:
        status, result = self.harness.request(
            "POST",
            "/owner/api/model",
            {
                "action": "manual",
                "provider_id": PROVIDER_ID,
                "model_id": self.model,
                "dialect": DIALECT_OPENAI_CHAT,
                "enabled": True,
            },
        )
        self.assertEqual(status, 200, result)


class CredentialIsolationTests(unittest.TestCase):
    """Acceptance 6: the credential is stored owner-only and never escapes."""

    def setUp(self) -> None:
        self.harness = OwnerSurfaceHarness()
        status, _ = self.harness.enroll()
        self.assertEqual(status, 200)

    def tearDown(self) -> None:
        self.harness.close()

    def test_the_credential_file_is_owner_only(self) -> None:
        path = self.harness.root / "credentials" / "openai_api.json"
        self.assertTrue(path.is_file())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(
            (self.harness.root / "credentials").stat().st_mode & 0o777, 0o700
        )

    def test_state_status_and_version_never_carry_the_key(self) -> None:
        for path in ("/owner/api/state", "/owner/api/status", "/version", "/owner/"):
            _, body = self.harness.request("GET", path)
            text = body if isinstance(body, str) else json.dumps(body)
            self.assertNotIn(SYNTHETIC_KEY, text, path)

    def test_the_configuration_and_the_ledger_never_carry_the_key(self) -> None:
        config_text = (self.harness.root / "config.json").read_text(encoding="utf-8")
        self.assertNotIn(SYNTHETIC_KEY, config_text)
        ledger = self.harness.router._ledger.recent()
        self.assertNotIn(SYNTHETIC_KEY, json.dumps(ledger))

    def test_a_log_line_never_carries_the_key(self) -> None:
        # Exercise the surfaces that produce log lines: a page load, a
        # status read and the credential-enrolling mutation itself.
        self.harness.request("GET", "/owner/")
        self.harness.request("GET", "/owner/api/status")
        self.harness.request(
            "POST",
            "/owner/api/provider",
            {"action": "enroll_key", "provider_id": PROVIDER_ID, "api_key": SYNTHETIC_KEY},
        )
        log_text = self.harness.log_stream.getvalue()
        self.assertNotIn(SYNTHETIC_KEY, log_text)
        for line in filter(None, log_text.splitlines()):
            record = json.loads(line)
            self.assertNotIn("api_key", record)
            self.assertNotIn("Authorization", line)

    def test_a_handle_is_bound_to_one_provider(self) -> None:
        # Structural isolation: another provider's handle cannot read this
        # provider's file.
        other = self.harness.credentials.provider_handle("opencode_go")
        self.assertIsNone(other.read())
        self.assertEqual(other.store_id, "opencode_go")
        self.assertEqual(self.harness.credentials.list_store_ids(), [PROVIDER_ID])
        self.assertTrue(self.harness.credentials.permissions_are_owner_only(PROVIDER_ID))

    def test_disconnect_removes_only_this_credential(self) -> None:
        self.harness.credentials.provider_handle("opencode_go").write(
            {"api_key": "other-provider-key"}
        )
        status, _ = self.harness.request(
            "POST",
            "/owner/api/provider",
            {"action": "disconnect", "provider_id": PROVIDER_ID},
        )
        self.assertEqual(status, 200)
        self.assertIsNone(self.harness.credentials.provider_handle(PROVIDER_ID).read())
        self.assertEqual(
            self.harness.credentials.provider_handle("opencode_go").read(),
            {"api_key": "other-provider-key"},
        )
        _, state = self.harness.request("GET", "/owner/api/state")
        provider = next(p for p in state["providers"] if p["provider_id"] == PROVIDER_ID)
        self.assertFalse(provider["credential_present"])
        self.assertEqual(provider["state"], "not_configured")


# --- end to end over loopback -------------------------------------------


def _free_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


CHAT_CHUNK_FRAMES = (
    b'data: {"id":"cc1","object":"chat.completion.chunk","model":"gpt-6-mini",'
    b'"choices":[{"index":0,"delta":{"role":"assistant","content":"Hello"},"finish_reason":null}]}\n\n'
    b'data: {"id":"cc1","object":"chat.completion.chunk","model":"gpt-6-mini",'
    b'"choices":[{"index":0,"delta":{},"finish_reason":"stop"}],'
    b'"usage":{"prompt_tokens":5,"completion_tokens":3,"total_tokens":8}}\n\n'
    b"data: [DONE]\n\n"
)

RESPONSES_FRAMES = (
    b'data: {"type":"response.output_text.delta","delta":"Hello"}\n\n'
    b'data: {"type":"response.completed","response":{"id":"resp_1",'
    b'"usage":{"input_tokens":1,"output_tokens":2}}}\n\n'
)

CHAT_BUFFERED = {
    "id": "chatcmpl-1",
    "object": "chat.completion",
    "model": "gpt-6-mini",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "hello from upstream"},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
}

RESPONSES_BUFFERED = {
    "id": "resp_1",
    "object": "response",
    "model": "gpt-6-luna",
    "status": "completed",
    "output": [
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "hello from upstream"}],
        }
    ],
    "usage": {"input_tokens": 5, "output_tokens": 3},
}


class _FakeOpenAIHandler(BaseHTTPRequestHandler):
    """A loopback stand-in for api.openai.com. Only this test talks to it."""

    protocol_version = "HTTP/1.0"

    def log_message(self, *args) -> None:
        pass

    @property
    def records(self) -> list[dict[str, Any]]:
        return self.server.records  # type: ignore[attr-defined]

    def _authorized(self) -> bool:
        return (
            self.headers.get("Authorization")
            == f"Bearer {self.server.api_key}"  # type: ignore[attr-defined]
        )

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_sse(self, frames: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.wfile.write(frames)
        self.wfile.flush()

    def do_GET(self) -> None:
        self.records.append(
            {
                "method": "GET",
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
            }
        )
        if self.path != "/v1/models":
            self._send_json(404, {"error": {"message": "not found", "type": "invalid_request_error"}})
            return
        if not self._authorized():
            self._send_json(
                401,
                {
                    "error": {
                        "message": "Incorrect API key provided",
                        "type": "invalid_request_error",
                        "code": "invalid_api_key",
                    }
                },
            )
            return
        self._send_json(
            200,
            {
                "object": "list",
                "data": [{"id": "gpt-6-luna", "object": "model"}, {"id": "gpt-6-mini", "object": "model"}],
            },
        )

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        record = {
            "method": "POST",
            "path": self.path,
            "authorization": self.headers.get("Authorization"),
            "accept": self.headers.get("Accept"),
            "body": json.loads(body.decode("utf-8")) if body else None,
        }
        self.records.append(record)
        if not self._authorized():
            self._send_json(
                401,
                {
                    "error": {
                        "message": "Incorrect API key provided",
                        "type": "invalid_request_error",
                        "code": "invalid_api_key",
                    }
                },
            )
            return
        stream = bool(record["body"] and record["body"].get("stream"))
        if self.path == "/v1/chat/completions":
            if stream:
                self._send_sse(CHAT_CHUNK_FRAMES)
            else:
                self._send_json(200, CHAT_BUFFERED)
            return
        if self.path == "/v1/responses":
            if stream:
                self._send_sse(RESPONSES_FRAMES)
            else:
                self._send_json(200, RESPONSES_BUFFERED)
            return
        self._send_json(404, {"error": {"message": "not found", "type": "invalid_request_error"}})


class FakeOpenAIServer:
    def __init__(self, api_key: str = SYNTHETIC_KEY) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeOpenAIHandler)
        self.server.api_key = api_key  # type: ignore[attr-defined]
        self.server.records = []  # type: ignore[attr-defined]
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def records(self) -> list[dict[str, Any]]:
        return self.server.records  # type: ignore[attr-defined]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class EndToEndHarness:
    """The real gateway over loopback, talking to the fake OpenAI server
    through the real ``UpstreamClient``."""

    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.upstream = FakeOpenAIServer()
        self.port = _free_port()

        self.config = ConfigStore(self.root / "config.json")
        self.credentials = CredentialStore(self.root)
        self.credentials.provider_handle(PROVIDER_ID).write({"api_key": SYNTHETIC_KEY})
        self.registry = AdapterRegistry()
        self.adapter = OpenAIApiAdapter(
            self.credentials.provider_handle(PROVIDER_ID), base_url=self.upstream.url
        )
        self.registry.register(self.adapter)

        with self.config.mutate() as snapshot:
            snapshot.providers[PROVIDER_ID] = ProviderState(
                provider_id=PROVIDER_ID,
                kind=PROVIDER_ID,
                state="connected",
                models={
                    "gpt-6-mini": ModelEntry(
                        model_id="gpt-6-mini",
                        enabled=True,
                        dialect=DIALECT_OPENAI_CHAT,
                        source="discovered",
                    ),
                    "gpt-6-luna": ModelEntry(
                        model_id="gpt-6-luna",
                        enabled=True,
                        dialect=DIALECT_OPENAI_RESPONSES,
                        source="discovered",
                    ),
                },
            )
            snapshot.aliases["chaty"] = Alias(
                name="chaty", enabled=True, primary=RouteRef(PROVIDER_ID, "gpt-6-mini")
            )
            snapshot.aliases["respy"] = Alias(
                name="respy", enabled=True, primary=RouteRef(PROVIDER_ID, "gpt-6-luna")
            )

        self.router = Router(self.config, self.registry, ledger=RequestLedger())
        policy = SecurityPolicy(
            allowed_hosts=build_allowed_hosts("127.0.0.1", self.port),
            canonical_origins=build_canonical_origins("127.0.0.1", self.port),
        )
        self.app = GatewayApp(
            config=self.config,
            credentials=self.credentials,
            router=self.router,
            registry=self.registry,
            profile_auth=ProfileAuth(self.config, self.credentials),
            policy=policy,
            ledger=self.router._ledger,
            limits=LimitStore(),
            release_identity=dict,
            base_url=f"http://127.0.0.1:{self.port}",
            mounts=("/owner",),
            logger=RequestLogger(enabled=False),
        )
        self.server = Server("127.0.0.1", self.port, self.app.handle)
        self.server.start_background()

    def close(self) -> None:
        self.server.stop()
        self.upstream.close()
        self._tmp.cleanup()

    def request(self, path: str, body: Any, raw: bool = False):
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            url=f"http://127.0.0.1:{self.port}{path}", data=data, method="POST"
        )
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                payload = response.read()
                headers = {key.lower(): value for key, value in response.headers.items()}
                if raw:
                    return response.status, payload, headers
                return response.status, json.loads(payload.decode("utf-8")), headers
        except urllib.error.HTTPError as exc:
            with exc:
                payload = exc.read()
            if raw:
                return exc.code, payload, {}
            return exc.code, json.loads(payload.decode("utf-8")), {}


def _strict_frames(payload: bytes) -> list[bytes]:
    """Parse SSE the way a strict client does: frames are data lines plus
    the blank line that ends them."""
    frames = [chunk for chunk in payload.split(b"\n\n") if chunk.strip()]
    for frame in frames:
        for line in frame.splitlines():
            if line.strip() and not line.startswith((b"data:", b"event:", b":")):
                raise AssertionError(f"non-SSE line in frame: {line!r}")
    return frames


class EndToEndTests(unittest.TestCase):
    """The real transport, both dialects, buffered and streamed."""

    def setUp(self) -> None:
        self.harness = EndToEndHarness()

    def tearDown(self) -> None:
        self.harness.close()

    def test_a_buffered_chat_request_passes_through_its_own_dialect(self) -> None:
        status, payload, _ = self.harness.request(
            "/v1/chat/completions",
            {"model": "chaty", "messages": [{"role": "user", "content": "hi"}]},
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["choices"][0]["message"]["content"], "hello from upstream")
        record = self.harness.upstream.records[-1]
        self.assertEqual(record["path"], "/v1/chat/completions")
        self.assertEqual(record["authorization"], f"Bearer {SYNTHETIC_KEY}")
        # The alias never reaches the provider: the upstream model id does.
        self.assertEqual(record["body"]["model"], "gpt-6-mini")
        self.assertFalse(record["body"].get("stream"))

    def test_a_streamed_chat_request_passes_through_strict_frames(self) -> None:
        status, payload, headers = self.harness.request(
            "/v1/chat/completions",
            {"model": "chaty", "messages": [{"role": "user", "content": "hi"}], "stream": True},
            raw=True,
        )
        self.assertEqual(status, 200)
        self.assertIn("text/event-stream", headers.get("content-type", ""))
        frames = _strict_frames(payload)
        self.assertTrue(any(b"chat.completion.chunk" in frame for frame in frames))
        self.assertTrue(any(b"finish_reason" in frame for frame in frames))
        self.assertEqual(frames[-1], b"data: [DONE]")
        record = self.harness.upstream.records[-1]
        self.assertTrue(record["body"].get("stream"))
        self.assertEqual(record["accept"], "text/event-stream")

    def test_a_buffered_responses_request_passes_through_its_own_dialect(self) -> None:
        status, payload, _ = self.harness.request(
            "/v1/responses", {"model": "respy", "input": "hi"}
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["object"], "response")
        self.assertEqual(payload["output"][0]["content"][0]["text"], "hello from upstream")
        record = self.harness.upstream.records[-1]
        self.assertEqual(record["path"], "/v1/responses")
        self.assertEqual(record["body"]["model"], "gpt-6-luna")

    def test_a_streamed_responses_request_passes_through_strict_frames(self) -> None:
        status, payload, headers = self.harness.request(
            "/v1/responses", {"model": "respy", "input": "hi", "stream": True}, raw=True
        )
        self.assertEqual(status, 200)
        self.assertIn("text/event-stream", headers.get("content-type", ""))
        frames = _strict_frames(payload)
        self.assertTrue(any(b"response.output_text.delta" in frame for frame in frames))
        self.assertTrue(any(b"response.completed" in frame for frame in frames))

    def test_a_chat_client_is_converted_on_the_buffered_path(self) -> None:
        # The route speaks Responses; the client speaks Chat Completions.
        # The response must come back converted, not verbatim (trap 4).
        status, payload, _ = self.harness.request(
            "/v1/chat/completions",
            {"model": "respy", "messages": [{"role": "user", "content": "hi"}]},
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["object"], "chat.completion")
        self.assertEqual(payload["choices"][0]["message"]["content"], "hello from upstream")
        record = self.harness.upstream.records[-1]
        self.assertEqual(record["path"], "/v1/responses")
        self.assertIn("input", record["body"])

    def test_a_chat_client_is_converted_on_the_streamed_path(self) -> None:
        status, payload, headers = self.harness.request(
            "/v1/chat/completions",
            {
                "model": "respy",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
            raw=True,
        )
        self.assertEqual(status, 200)
        self.assertIn("text/event-stream", headers.get("content-type", ""))
        text = payload.decode("utf-8", "replace")
        # Converted into Chat chunks, with a proper terminator - the
        # upstream's Responses events must not leak through.
        frames = _strict_frames(payload)
        self.assertTrue(any(b"chat.completion.chunk" in frame for frame in frames))
        self.assertIn("Hello", text)
        self.assertEqual(frames[-1], b"data: [DONE]")
        self.assertNotIn(b"response.output_text.delta", payload)

    def test_a_rejected_key_is_classified_over_the_real_transport(self) -> None:
        self.harness.credentials.provider_handle(PROVIDER_ID).write(
            {"api_key": "definitely-wrong-key"}
        )
        with self.assertRaises(AdapterError) as ctx:
            self.harness.adapter.list_models()
        self.assertEqual(ctx.exception.error_class, ERROR_AUTH)
        self.assertIn(ctx.exception.error_class, SHARED_ERROR_CLASSES)


if __name__ == "__main__":
    unittest.main()
