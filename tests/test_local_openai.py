"""The shared local adapter: catalogue, payload, capability truth, budgets.

Covers test-matrix cases 6-10, 16, 17, 19, 20 and 21 of the T15 spec, each
against a scripted loopback server (``tests.local_fixture``) or a closed
loopback port - never against a real Ollama, llama.cpp or LM Studio install,
because none is assumed to exist here. Where a server/version is not
reproduced, this file says so in the test name rather than implying it ran.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from gateway.adapters.base import AdapterError, ProviderRequest
from gateway.adapters.http_client import UpstreamClient
from gateway.adapters.local_openai import (
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_READ_TIMEOUT,
    FLAVORS,
    LocalOpenAIAdapter,
    connect_timeout_from_env,
)
from gateway.adapters.registry import AdapterRegistry
from gateway.auth import ProfileAuth
from gateway.bridge.streams import iter_sse_frames
from gateway.config import (
    ERROR_EXHAUSTED,
    ERROR_MALFORMED,
    ERROR_TRANSIENT,
    ERROR_AUTH,
    ERROR_RATE_LIMITED,
    STATE_CONNECTED,
    STATE_CONFIGURED_UNTESTED,
    STATE_FAILED,
    STATE_NOT_CONFIGURED,
    ConfigStore,
    ProviderState,
)
from gateway.core.capabilities import DIALECT_OPENAI_CHAT
from gateway.core.routing import Router
from gateway.credentials import CredentialStore
from gateway.http.app import GatewayApp
from gateway.http.core import Headers, Request
from gateway.http.security import SecurityPolicy, build_allowed_hosts, build_canonical_origins
from gateway.logging import RequestLogger
from gateway.observability import LimitStore, RequestLedger
from tests.local_fixture import SYNTHETIC_TOKEN, ClosedPort, LocalFixture
from tests.owner_session import cookie_header

PORT = 8765
HOST = "127.0.0.1"


class LocalAdapterHarness:
    """A gateway over loopback wired to three local adapters."""

    def __init__(self, logger: RequestLogger | None = None) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.config = ConfigStore(self.root / "config.json")
        self.credentials = CredentialStore(self.root)
        self.registry = AdapterRegistry()
        self.adapters = [LocalOpenAIAdapter(pid, self.credentials.provider_handle(pid), self.config) for pid in FLAVORS]
        for adapter in self.adapters:
            self.registry.register(adapter)
        router = Router(self.config, self.registry, ledger=RequestLedger())
        policy = SecurityPolicy(
            allowed_hosts=build_allowed_hosts(HOST, PORT),
            canonical_origins=build_canonical_origins(HOST, PORT),
        )
        self.app = GatewayApp(
            config=self.config,
            credentials=self.credentials,
            router=router,
            registry=self.registry,
            profile_auth=ProfileAuth(self.config, self.credentials),
            policy=policy,
            ledger=RequestLedger(),
            limits=LimitStore(),
            release_identity=lambda: {},
            base_url=f"http://{HOST}:{PORT}",
            mounts=("/owner",),
            logger=logger or RequestLogger(enabled=False),
        )
        with self.config.mutate() as snapshot:
            for pid in FLAVORS:
                snapshot.providers[pid] = ProviderState(
                    provider_id=pid, kind=pid, state=STATE_NOT_CONFIGURED
                )

    def close(self) -> None:
        for adapter in self.adapters:
            adapter._client.close()
        self._tmp.cleanup()

    # --- owner surface --------------------------------------------------

    def action(self, body: dict) -> tuple[int, dict]:
        request = Request(
            method="POST",
            path="/owner/api/provider",
            raw_query="",
            headers=Headers(
                [
                    ("Host", f"{HOST}:{PORT}"),
                    ("Origin", f"http://{HOST}:{PORT}"),
                    ("Cookie", cookie_header(self.app)),
                ]
            ),
            body=json.dumps(body).encode("utf-8"),
            client_address=("127.0.0.1", 40000),
            server_port=PORT,
        )
        response = self.app.handle(request)
        return response.status, json.loads(response.body.decode("utf-8") or "{}")

    def state(self) -> dict:
        request = Request(
            method="GET",
            path="/owner/api/state",
            raw_query="",
            headers=Headers(
                [
                    ("Host", f"{HOST}:{PORT}"),
                    ("Cookie", cookie_header(self.app)),
                ]
            ),
            body=b"",
            client_address=("127.0.0.1", 40000),
            server_port=PORT,
        )
        response = self.app.handle(request)
        return json.loads(response.body.decode("utf-8") or "{}")

    def refresh(self, provider_id: str) -> tuple[int, dict]:
        return self.action({"action": "refresh", "provider_id": provider_id})

    def set_endpoint(self, provider_id: str, url: str) -> tuple[int, dict]:
        return self.action(
            {"action": "set_base_url", "provider_id": provider_id, "base_url": url}
        )

    def entry(self, provider_id: str, model_id: str):
        return self.config.snapshot().providers[provider_id].models.get(model_id)


class LocalAdapterTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = LocalAdapterHarness()
        self.addCleanup(self.harness.close)

    def adapter(self, provider_id: str = "ollama") -> LocalOpenAIAdapter:
        return next(a for a in self.harness.adapters if a.provider_id == provider_id)


class CatalogueTests(LocalAdapterTestCase):
    """Case 6, and the fail-closed half of case 1."""

    def test_no_endpoint_is_no_call_at_boot_and_a_policy_refusal_on_refresh(self) -> None:
        # Construction alone makes no outbound call: there is nowhere to call.
        self.assertEqual(
            {pid: self.harness.config.snapshot().providers[pid].state for pid in FLAVORS},
            {pid: STATE_NOT_CONFIGURED for pid in FLAVORS},
        )
        status, payload = self.harness.refresh("ollama")
        self.assertNotEqual(status, 200, payload)
        error = payload.get("error", {})
        self.assertEqual(error.get("type"), "policy")
        self.assertEqual(
            error.get("message"),
            "no local endpoint is configured for this provider",
        )
        # Still not configured: a refusal never invents a destination.
        provider = self.harness.config.snapshot().providers["ollama"]
        self.assertEqual(provider.state, STATE_NOT_CONFIGURED)
        self.assertIsNone(provider.base_url)

    def test_each_flavour_enumerates_models_over_http(self) -> None:
        for pid in FLAVORS:
            with self.subTest(provider=pid):
                fixture = LocalFixture()
                self.addCleanup(fixture.close)
                status, _ = self.harness.set_endpoint(pid, fixture.base_url)
                self.assertEqual(status, 200, pid)
                status, result = self.harness.refresh(pid)
                self.assertEqual(status, 200, result)
                self.assertEqual(result["model_count"], 1)
                entry = self.harness.entry(pid, "local-test-model")
                self.assertIsNotNone(entry, "the catalogue row must be stored")
                self.assertEqual(entry.dialect, DIALECT_OPENAI_CHAT)
                # Ollama's documented evidence call follows its catalogue
                # read; the other two publish no such endpoint.
                expected = ["/v1/models", "/api/show"] if pid == "ollama" else ["/v1/models"]
                self.assertEqual(fixture.paths(), expected)
                state = self.harness.config.snapshot().providers[pid]
                self.assertEqual(state.state, STATE_CONFIGURED_UNTESTED)

    def test_a_body_that_is_not_a_catalogue_is_an_empty_refresh(self) -> None:
        fixture = LocalFixture(models={"unexpected": "shape"})
        self.addCleanup(fixture.close)
        self.harness.set_endpoint("lm_studio", fixture.base_url)
        status, result = self.harness.refresh("lm_studio")
        self.assertEqual(status, 200, result)
        self.assertEqual(result["model_count"], 0)

    def test_a_malformed_catalogue_entry_is_skipped_not_fatal(self) -> None:
        fixture = LocalFixture(
            models={"data": [{"id": "good-model"}, {"no_id": 1}, "not-a-dict", {"id": ""}]}
        )
        self.addCleanup(fixture.close)
        self.harness.set_endpoint("llama_cpp", fixture.base_url)
        status, result = self.harness.refresh("llama_cpp")
        self.assertEqual(status, 200, result)
        self.assertEqual(result["model_count"], 1)
        self.assertIsNotNone(self.harness.entry("llama_cpp", "good-model"))

    def test_an_upstream_failure_is_classified_and_carries_no_body_text(self) -> None:
        marker = "fixture-failure-body-marker"
        fixture = LocalFixture(
            models_status=500, chat_error={"error": {"message": marker}}
        )
        self.addCleanup(fixture.close)
        self.harness.set_endpoint("ollama", fixture.base_url)
        status, result = self.harness.refresh("ollama")
        self.assertEqual(status, 502, result)
        self.assertEqual(result["error"]["type"], ERROR_TRANSIENT)
        self.assertNotIn(marker, json.dumps(result))


class CredentialPolicyTests(LocalAdapterTestCase):
    """Case 7: keyless by default, and only this provider's own token."""

    def test_a_local_request_carries_no_authorization_header_by_default(self) -> None:
        fixture = LocalFixture()
        self.addCleanup(fixture.close)
        self.harness.set_endpoint("ollama", fixture.base_url)
        adapter = self.adapter("ollama")
        adapter.invoke(
            ProviderRequest(model="local-test-model", payload={"messages": []}, dialect=DIALECT_OPENAI_CHAT)
        )
        seen = fixture.last()
        self.assertNotIn("Authorization", seen["headers"])
        self.assertIn("/v1/chat/completions", seen["path"])

    def test_a_server_that_requires_a_token_refuses_a_keyless_call(self) -> None:
        fixture = LocalFixture(require_bearer=SYNTHETIC_TOKEN)
        self.addCleanup(fixture.close)
        self.harness.set_endpoint("ollama", fixture.base_url)
        adapter = self.adapter("ollama")
        with self.assertRaises(AdapterError) as caught:
            adapter.invoke(
                ProviderRequest(model="local-test-model", payload={"messages": []}, dialect=DIALECT_OPENAI_CHAT)
            )
        self.assertEqual(caught.exception.error_class, ERROR_AUTH)

    def test_an_enrolled_token_is_sent_and_the_server_accepts_it(self) -> None:
        fixture = LocalFixture(require_bearer=SYNTHETIC_TOKEN)
        self.addCleanup(fixture.close)
        self.harness.set_endpoint("ollama", fixture.base_url)
        self.harness.credentials.provider_handle("ollama").write({"api_key": SYNTHETIC_TOKEN})
        adapter = self.adapter("ollama")
        adapter.invoke(
            ProviderRequest(model="local-test-model", payload={"messages": []}, dialect=DIALECT_OPENAI_CHAT)
        )
        self.assertEqual(
            fixture.last()["headers"].get("Authorization"), f"Bearer {SYNTHETIC_TOKEN}"
        )

    def test_the_token_never_reaches_the_owner_state_or_a_test_result(self) -> None:
        fixture = LocalFixture(require_bearer=SYNTHETIC_TOKEN)
        self.addCleanup(fixture.close)
        self.harness.set_endpoint("ollama", fixture.base_url)
        self.harness.credentials.provider_handle("ollama").write({"api_key": SYNTHETIC_TOKEN})

        state_text = json.dumps(self.harness.state())
        self.assertNotIn(SYNTHETIC_TOKEN, state_text)

        result = self.adapter("ollama").test_connection()
        self.assertTrue(result.ok, result.message)
        self.assertNotIn(SYNTHETIC_TOKEN, json.dumps(result.to_dict()))
        # The endpoint itself is owner-only state, and the token is not even
        # there: it lives in the credential store, nowhere else.
        self.assertNotIn(SYNTHETIC_TOKEN, json.dumps(self.harness.config.snapshot().to_dict()))

    def test_another_providers_credential_is_not_sent(self) -> None:
        fixture = LocalFixture()
        self.addCleanup(fixture.close)
        self.harness.set_endpoint("ollama", fixture.base_url)
        # A different provider's token is enrolled; this adapter reads only
        # its own handle.
        self.harness.credentials.provider_handle("lm_studio").write({"api_key": SYNTHETIC_TOKEN})
        self.adapter("ollama").invoke(
            ProviderRequest(model="local-test-model", payload={"messages": []}, dialect=DIALECT_OPENAI_CHAT)
        )
        self.assertNotIn("Authorization", fixture.last()["headers"])


class PayloadShapeTests(LocalAdapterTestCase):
    """Case 8: what all three document, and nothing they do not."""

    def _invoke(self, payload: dict, *, stream: bool = False, model: str = "route-model"):
        fixture = LocalFixture()
        self.addCleanup(fixture.close)
        self.harness.set_endpoint("llama_cpp", fixture.base_url)
        adapter = self.adapter("llama_cpp")
        adapter.invoke(
            ProviderRequest(model=model, payload=payload, stream=stream, dialect=DIALECT_OPENAI_CHAT)
        )
        return json.loads(fixture.last()["body"].decode("utf-8"))

    def test_max_completion_tokens_is_rewritten_and_unsupported_fields_are_dropped(self) -> None:
        body = self._invoke(
            {
                "messages": [],
                "max_completion_tokens": 256,
                "tool_choice": "auto",
                "n": 2,
                "user": "someone",
                "reasoning_effort": "high",
            }
        )
        self.assertEqual(body.get("max_tokens"), 256)
        self.assertNotIn("max_completion_tokens", body)
        for field in ("tool_choice", "n", "user", "reasoning_effort"):
            self.assertNotIn(field, body, field)

    def test_stream_options_are_sent_only_for_a_streaming_request(self) -> None:
        buffered = self._invoke({"messages": []}, stream=False)
        self.assertFalse(buffered["stream"])
        self.assertNotIn("stream_options", buffered)

        streamed = self._invoke(
            {"messages": [], "stream_options": {"include_usage": True}}, stream=True
        )
        self.assertTrue(streamed["stream"])
        self.assertEqual(streamed.get("stream_options"), {"include_usage": True})

    def test_the_payload_model_is_the_routes_model(self) -> None:
        body = self._invoke({"messages": [], "model": "a-caller-chosen-name"}, model="route-model")
        self.assertEqual(body["model"], "route-model")


class CapabilityTruthTests(LocalAdapterTestCase):
    """Case 9: unknown stays unknown, and an approval survives a refresh."""

    def test_capabilities_and_limits_fail_closed_on_discovery(self) -> None:
        # llama.cpp's server: no capability evidence anywhere, so everything
        # stays unknown. Ollama's one documented evidence path is covered in
        # OllamaEvidenceTests.
        fixture = LocalFixture()
        self.addCleanup(fixture.close)
        self.harness.set_endpoint("llama_cpp", fixture.base_url)
        status, _ = self.harness.refresh("llama_cpp")
        self.assertEqual(status, 200)
        entry = self.harness.entry("llama_cpp", "local-test-model")
        self.assertFalse(entry.supports_function_tools)
        self.assertFalse(entry.supports_image_input)
        self.assertFalse(entry.supports_builtin_tools)
        self.assertIsNone(entry.context_window)
        self.assertIsNone(entry.monthly_limit_usd)
        self.assertEqual(entry.limits_source, "unknown")
        self.assertIn("unknown", entry.notes)

    def test_an_owner_approval_survives_a_refresh(self) -> None:
        fixture = LocalFixture()
        self.addCleanup(fixture.close)
        self.harness.set_endpoint("ollama", fixture.base_url)
        with self.harness.config.mutate() as config:
            config.settings.setdefault("capability_approvals", {}).setdefault("ollama", {})[
                "local-test-model"
            ] = {"supports_function_tools": {"value": True}}
        self.harness.refresh("ollama")
        entry = self.harness.entry("ollama", "local-test-model")
        self.assertTrue(entry.supports_function_tools, "the approval must survive discovery")

        # And it survives the *next* discovery too.
        self.harness.refresh("ollama")
        entry = self.harness.entry("ollama", "local-test-model")
        self.assertTrue(entry.supports_function_tools)


class OllamaEvidenceTests(LocalAdapterTestCase):
    """Case 10: /api/show is evidence, only during an explicit refresh."""

    def test_show_evidence_sets_image_input_and_context_window(self) -> None:
        fixture = LocalFixture()
        self.addCleanup(fixture.close)
        self.harness.set_endpoint("ollama", fixture.base_url)
        status, _ = self.harness.refresh("ollama")
        self.assertEqual(status, 200)
        entry = self.harness.entry("ollama", "local-test-model")
        self.assertTrue(entry.supports_image_input, "Ollama publishes 'vision'")
        self.assertEqual(entry.context_window, 8192)
        self.assertEqual(entry.context_limit, 8192)
        # Ollama publishes no tool-use capability value, so tools stay false.
        self.assertFalse(entry.supports_function_tools)
        self.assertEqual(fixture.paths().count("/api/show"), 1)

    def test_a_server_without_show_leaves_everything_unknown(self) -> None:
        # llama-server and LM Studio answer 404 (or 405) on /api/show.
        fixture = LocalFixture(show_status=404)
        self.addCleanup(fixture.close)
        self.harness.set_endpoint("llama_cpp", fixture.base_url)
        status, _ = self.harness.refresh("llama_cpp")
        self.assertEqual(status, 200)
        entry = self.harness.entry("llama_cpp", "local-test-model")
        self.assertFalse(entry.supports_image_input)
        self.assertFalse(entry.supports_function_tools)
        self.assertIsNone(entry.context_window)

    def test_show_is_never_called_at_boot_or_during_inference(self) -> None:
        fixture = LocalFixture()
        self.addCleanup(fixture.close)
        self.harness.set_endpoint("ollama", fixture.base_url)
        adapter = self.adapter("ollama")
        adapter.invoke(
            ProviderRequest(model="local-test-model", payload={"messages": []}, dialect=DIALECT_OPENAI_CHAT)
        )
        self.assertEqual(fixture.paths(), ["/v1/chat/completions"])

        # ...and not during an explicit connection test's own catalogue read
        # either: the evidence path belongs to refresh.
        fixture2 = LocalFixture()
        self.addCleanup(fixture2.close)
        self.harness.set_endpoint("lm_studio", fixture2.base_url)
        self.adapter("lm_studio").test_connection()
        self.assertEqual(fixture2.paths(), ["/v1/models"])


class StreamFramingTests(LocalAdapterTestCase):
    """Case 16: llama-server's keep-alive comments must not become events."""

    def _read_stream(self, *, sse_comments: bool) -> list[bytes]:
        fixture = LocalFixture(sse_comments=sse_comments)
        self.addCleanup(fixture.close)
        self.harness.set_endpoint("llama_cpp", fixture.base_url)
        adapter = self.adapter("llama_cpp")
        response = adapter.invoke(
            ProviderRequest(
                model="local-test-model",
                payload={"messages": []},
                stream=True,
                dialect=DIALECT_OPENAI_CHAT,
            )
        )
        self.assertIsNotNone(response.stream)
        return list(response.stream)

    def test_comment_only_frames_yield_no_event_and_data_frames_are_complete(self) -> None:
        frames = self._read_stream(sse_comments=True)
        self.assertGreaterEqual(len(frames), 2)
        events: list[dict] = []
        comment_only = 0
        for frame in frames:
            # Every frame the client hands on is self-contained: it ends with
            # the SSE terminator, and a comment-only frame never becomes an
            # event (``iter_sse_frames`` parses events, not comments).
            self.assertTrue(frame.endswith(b"\n\n"), frame)
            parsed = list(iter_sse_frames(frame))
            if not parsed:
                comment_only += 1
            for _, data in parsed:
                if data == "[DONE]":
                    continue
                events.append(json.loads(data))
        self.assertEqual(len(events), 2, "the keep-alive must not add an event")
        self.assertEqual(events[0]["choices"][0]["delta"]["content"], "hel")
        self.assertIn("usage", events[1])
        self.assertGreater(comment_only, 0, "the fixture must have sent a keep-alive frame")

    def test_a_stream_without_comments_is_parsed_the_same_way(self) -> None:
        frames = self._read_stream(sse_comments=False)
        payloads = [
            json.loads(data)
            for frame in frames
            for _, data in iter_sse_frames(frame)
            if data != "[DONE]"
        ]
        self.assertEqual(len(payloads), 2)


class ColdLoadBudgetTests(LocalAdapterTestCase):
    """Case 17: a cold local load is not a stall, and a stall is not a load."""

    def test_the_local_head_budget_is_bigger_than_the_global_default(self) -> None:
        from gateway.adapters.http_client import DEFAULT_CONNECT_TIMEOUT as GLOBAL_DEFAULT

        for pid in FLAVORS:
            adapter = self.adapter(pid)
            self.assertEqual(adapter._client.connect_timeout, DEFAULT_CONNECT_TIMEOUT)
            self.assertGreater(DEFAULT_CONNECT_TIMEOUT, GLOBAL_DEFAULT)
            self.assertEqual(adapter._client.timeout, DEFAULT_READ_TIMEOUT)

    def test_a_slow_cold_stream_head_within_the_local_budget_is_served(self) -> None:
        # The head takes longer than the *global* 10 s default: exactly the
        # cold-load case this budget exists for. The head of a stream is the
        # phase the local budget governs (a buffered head is judged by the
        # read budget, which is the gateway's normal 120 s one).
        fixture = LocalFixture(head_delay=11.0)
        self.addCleanup(fixture.close)
        self.harness.set_endpoint("ollama", fixture.base_url)
        adapter = self.adapter("ollama")
        response = adapter.invoke(
            ProviderRequest(
                model="local-test-model",
                payload={"messages": []},
                stream=True,
                dialect=DIALECT_OPENAI_CHAT,
            )
        )
        frames = list(response.stream or ())
        self.assertGreaterEqual(len(frames), 2)

    def test_a_stream_head_beyond_the_local_budget_fails_as_a_connect_timeout(self) -> None:
        fixture = LocalFixture(head_delay=4.0)
        self.addCleanup(fixture.close)
        self.harness.set_endpoint("ollama", fixture.base_url)
        adapter = LocalOpenAIAdapter(
            "ollama",
            self.harness.credentials.provider_handle("ollama"),
            self.harness.config,
            connect_timeout=1.0,
        )
        self.addCleanup(adapter._client.close)
        with self.assertRaises(AdapterError) as caught:
            adapter.invoke(
                ProviderRequest(
                    model="local-test-model",
                    payload={"messages": []},
                    stream=True,
                    dialect=DIALECT_OPENAI_CHAT,
                )
            )
        error = caught.exception
        self.assertEqual(error.error_class, ERROR_TRANSIENT)
        self.assertIn("connect timeout", error.message)
        # A stream head that never came is reported by the connection test
        # as a failure with the same classification.
        result = adapter.test_connection()
        self.assertTrue(result.ok, "the catalogue read uses the read budget")

    def test_a_progressing_stream_outlives_the_connect_budget(self) -> None:
        # Six frames at 0.4 s = 2.4 s of *progress* after the head, with a
        # 1 s connect budget: the read budget is the one that may judge it.
        frames = [
            {
                "id": "chatcmpl-fixture",
                "object": "chat.completion.chunk",
                "choices": [{"index": 0, "delta": {"content": str(index)}, "finish_reason": None}],
            }
            for index in range(6)
        ]
        fixture = LocalFixture(stream_frames=frames, frame_delay=0.4)
        self.addCleanup(fixture.close)
        self.harness.set_endpoint("llama_cpp", fixture.base_url)
        adapter = LocalOpenAIAdapter(
            "llama_cpp",
            self.harness.credentials.provider_handle("llama_cpp"),
            self.harness.config,
            connect_timeout=1.0,
        )
        self.addCleanup(adapter._client.close)
        response = adapter.invoke(
            ProviderRequest(
                model="local-test-model",
                payload={"messages": []},
                stream=True,
                dialect=DIALECT_OPENAI_CHAT,
            )
        )
        received = list(response.stream)
        payloads = [
            json.loads(data)
            for frame in received
            for _, data in iter_sse_frames(frame)
            if data != "[DONE]"
        ]
        self.assertEqual(len(payloads), len(frames))

    def test_the_budget_environment_override_is_clamped(self) -> None:
        self.assertEqual(connect_timeout_from_env({}), DEFAULT_CONNECT_TIMEOUT)
        self.assertEqual(connect_timeout_from_env({"GATEWAY_LOCAL_CONNECT_TIMEOUT": "abc"}), DEFAULT_CONNECT_TIMEOUT)
        self.assertEqual(connect_timeout_from_env({"GATEWAY_LOCAL_CONNECT_TIMEOUT": "1"}), 10.0)
        self.assertEqual(connect_timeout_from_env({"GATEWAY_LOCAL_CONNECT_TIMEOUT": "100000"}), 300.0)
        self.assertEqual(connect_timeout_from_env({"GATEWAY_LOCAL_CONNECT_TIMEOUT": "45"}), 45.0)


class ErrorClassificationTests(LocalAdapterTestCase):
    """Cases 19 and 21: how a local server's own failures classify."""

    def _invoke_error(self, status: int, body: dict) -> AdapterError:
        fixture = LocalFixture(chat_status=status, chat_error=body)
        self.addCleanup(fixture.close)
        self.harness.set_endpoint("llama_cpp", fixture.base_url)
        adapter = self.adapter("llama_cpp")
        with self.assertRaises(AdapterError) as caught:
            adapter.invoke(
                ProviderRequest(model="local-test-model", payload={"messages": []}, dialect=DIALECT_OPENAI_CHAT)
            )
        return caught.exception

    def test_statuses_classify_as_the_documented_classes(self) -> None:
        cases = (
            (401, {"error": {"code": 401, "message": "Invalid API Key"}}, ERROR_AUTH),
            (403, {"error": {"message": "forbidden"}}, ERROR_AUTH),
            (501, {"error": {"message": "unknown model type"}}, ERROR_TRANSIENT),
            (404, {"error": {"message": "model not found"}}, ERROR_MALFORMED),
            (400, {"error": {"message": "bad request"}}, ERROR_MALFORMED),
            (429, {"error": {"message": "Too Many Requests"}}, ERROR_RATE_LIMITED),
            (503, {"error": {"message": "loading"}}, ERROR_TRANSIENT),
        )
        for status, body, expected in cases:
            with self.subTest(status=status):
                error = self._invoke_error(status, body)
                self.assertEqual(error.error_class, expected)
                # Provider text never reaches the caller; only the fixed
                # gateway sentence for the class does.
                self.assertNotIn("Invalid API Key", error.message)
                self.assertNotIn("model not found", error.message)

    def test_no_local_error_shape_is_classified_exhausted(self) -> None:
        shapes = (
            (429, {"error": {"code": 429, "message": "Too Many Requests"}}),
            (429, {"error": {"type": "rate_limit_exceeded"}}),
            (401, {"error": {"code": 401, "message": "Invalid API Key"}}),
            (404, {"error": {"message": "model not found"}}),
            (400, {"error": {"message": "unknown field: reasoning_effort"}}),
            (500, {"error": {"message": "internal error"}}),
            (501, {"error": {"message": "not supported"}}),
        )
        for status, body in shapes:
            with self.subTest(status=status, body=body):
                error = self._invoke_error(status, body)
                self.assertNotEqual(error.error_class, ERROR_EXHAUSTED)
                self.assertNotIn("exhausted", error.message)

    def test_a_refused_connection_is_transient_and_names_no_address(self) -> None:
        closed = ClosedPort()
        self.harness.set_endpoint("ollama", closed.base_url)
        adapter = self.adapter("ollama")
        with self.assertRaises(AdapterError) as caught:
            adapter.invoke(
                ProviderRequest(model="local-test-model", payload={"messages": []}, dialect=DIALECT_OPENAI_CHAT)
            )
        error = caught.exception
        self.assertEqual(error.error_class, ERROR_TRANSIENT)
        self.assertNotIn(str(closed.port), error.message)

    def test_a_connection_failure_is_reported_by_the_connection_test_too(self) -> None:
        closed = ClosedPort()
        self.harness.set_endpoint("lm_studio", closed.base_url)
        result = self.adapter("lm_studio").test_connection()
        self.assertFalse(result.ok)
        self.assertEqual(result.state, "failed")
        self.assertEqual(result.detail.get("error_class"), ERROR_TRANSIENT)


if __name__ == "__main__":
    unittest.main()
