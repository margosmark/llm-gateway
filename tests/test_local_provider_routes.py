"""Local providers as routes: capability gates, fallback consent, circuits.

Covers test-matrix cases 11-15, 18, 20 and 22-26, 28 of the T15 spec,
driving the real router, ledger and owner surface over loopback.

Honesty note (case 27): no Ollama, ``llama-server`` or LM Studio install is
available in this environment, so every server here is the scripted fixture
in ``tests/local_fixture.py``. Nothing in this file claims a real server or
version was exercised; that case stays open and is reported as untested.
"""

from __future__ import annotations

import io
import json
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gateway.adapters.base import AdapterError, ProviderRequest, ProviderResponse
from gateway.adapters.local_openai import FLAVORS, LocalOpenAIAdapter
from gateway.circuits import TRANSIENT_BACKOFF_MAX_SECONDS
from gateway.config import (
    ERROR_TRANSIENT,
    STATE_CONFIGURED_UNTESTED,
    STATE_NOT_CONFIGURED,
    Alias,
    ConfigStore,
    ModelEntry,
    ProviderState,
    RouteRef,
    default_provider_ids,
)
from gateway.core.capabilities import DIALECT_OPENAI_CHAT, DIALECT_OPENAI_RESPONSES
from gateway.costs import INVALID_USAGE_MARKER, price_for, stream_token_counters
from gateway.credentials import CredentialError, CredentialStore
from gateway.logging import RequestLogger
from gateway.routes import ValidationError, validate_route
from tests.local_fixture import ClosedPort, LocalFixture
from tests.test_end_to_end import FAKE_PROVIDER, FakeAdapter, GatewayHarness
from tests.test_image_input import ImageCapableAdapter, _alias_with_routes, _image_payload

LOCAL_MODEL = "local-test-model"


def _local_alias(
    primary: RouteRef,
    fallbacks: list[RouteRef] | None = None,
    *,
    triggers: list[str] | None = None,
    authorized: bool = True,
    confirmed: list[str] | None = None,
    name: str = "local",
) -> Alias:
    fallbacks = fallbacks or []
    return Alias(
        name=name,
        enabled=True,
        primary=primary,
        fallbacks=fallbacks,
        fallback_triggers=triggers or ["route_ineligible", "upstream_unavailable"],
        fallback_authorized=authorized,
        fallback_confirmed_providers=(
            confirmed if confirmed is not None else sorted({ref.provider for ref in fallbacks})
        ),
    )


class LocalRouteTestCase(unittest.TestCase):
    """A harness with the three local adapters registered and no endpoints."""

    def setUp(self) -> None:
        self.harness = GatewayHarness()
        self.addCleanup(self.harness.close)
        for provider_id in FLAVORS:
            self.harness.registry.register(
                LocalOpenAIAdapter(
                    provider_id,
                    self.harness.credentials.provider_handle(provider_id),
                    self.harness.config,
                )
            )

    # --- helpers --------------------------------------------------------

    def add_local(
        self,
        provider_id: str = "ollama",
        *,
        base_url: str | None = None,
        enabled: bool = True,
        model_kwargs: dict | None = None,
    ) -> None:
        with self.harness.config.mutate() as config:
            config.providers[provider_id] = ProviderState(
                provider_id=provider_id,
                kind=provider_id,
                state=STATE_CONFIGURED_UNTESTED if base_url else STATE_NOT_CONFIGURED,
                base_url=base_url,
                models={
                    LOCAL_MODEL: ModelEntry(
                        model_id=LOCAL_MODEL,
                        enabled=enabled,
                        dialect=DIALECT_OPENAI_CHAT,
                        source="discovered",
                        # Exactly what a catalogue read yields: no capability
                        # evidence on any of the three servers.
                        supports_function_tools=False,
                        supports_image_input=False,
                        supports_builtin_tools=False,
                        **(model_kwargs or {}),
                    )
                },
            )

    def add_alias(self, alias: Alias) -> None:
        with self.harness.config.mutate() as config:
            config.aliases[alias.name] = alias

    def event(self) -> dict:
        return self.harness.router._ledger.recent()[0]

    def approve(self, provider_id: str, field: str) -> None:
        """Approve one capability for one model, the way the console does."""
        with self.harness.config.mutate() as config:
            config.settings.setdefault("capability_approvals", {}).setdefault(provider_id, {})[
                LOCAL_MODEL
            ] = {field: {"value": True}}
        status, payload, _ = self.harness.request(
            "POST",
            "/owner/api/provider",
            body={"action": "refresh", "provider_id": provider_id},
        )
        self.assertEqual(status, 200, payload)


class CapabilityGateTests(LocalRouteTestCase):
    """Cases 11 and 12: the hard gates, and the payload after approval."""

    def test_an_image_request_is_excluded_while_image_input_is_unknown(self) -> None:
        fixture = LocalFixture()
        self.addCleanup(fixture.close)
        self.add_local("ollama", base_url=fixture.base_url)
        image_capable = ImageCapableAdapter(FAKE_PROVIDER, DIALECT_OPENAI_RESPONSES)
        self.harness.registry.register(image_capable)
        with self.harness.config.mutate() as config:
            config.providers[FAKE_PROVIDER].models["gpt-6-luna"].supports_image_input = True
        self.add_alias(
            _alias_with_routes(
                RouteRef("ollama", LOCAL_MODEL),
                [RouteRef(FAKE_PROVIDER, "gpt-6-luna")],
                name="local",
            )
        )

        status, _, _ = self.harness.request(
            "POST", "/v1/chat/completions", body=_image_payload("local")
        )
        self.assertEqual(status, 200)
        # The local server never saw the image request: the gate runs before
        # any provider is called.
        self.assertEqual(fixture.requests, [])
        self.assertEqual(
            {entry["reason"] for entry in self.event()["skipped"]},
            {"this model does not support image input"},
        )
        self.assertEqual(self.event()["serving_provider"], FAKE_PROVIDER)

    def test_the_image_gate_opens_only_after_an_owner_approval(self) -> None:
        fixture = LocalFixture()
        self.addCleanup(fixture.close)
        self.add_local("ollama", base_url=fixture.base_url)
        self.add_alias(_alias_with_routes(RouteRef("ollama", LOCAL_MODEL), [], name="local"))

        status, _, _ = self.harness.request(
            "POST", "/v1/chat/completions", body=_image_payload("local")
        )
        self.assertNotEqual(status, 200, "with no capable route the gateway fails closed")

        self.approve("ollama", "supports_image_input")
        status, _, _ = self.harness.request(
            "POST", "/v1/chat/completions", body=_image_payload("local")
        )
        self.assertEqual(status, 200)
        self.assertEqual(fixture.last()["path"], "/v1/chat/completions")
        self.assertIn("image_url", fixture.last()["body"].decode("utf-8"))
        chat_calls = [
            entry for entry in fixture.requests if entry["path"].endswith("/chat/completions")
        ]
        self.assertEqual(len(chat_calls), 1, "exactly one inference call")

    def test_a_tool_request_is_excluded_and_tool_choice_never_reaches_the_server(self) -> None:
        fixture = LocalFixture()
        self.addCleanup(fixture.close)
        self.add_local("llama_cpp", base_url=fixture.base_url)
        with self.harness.config.mutate() as config:
            config.providers[FAKE_PROVIDER].models["gpt-6-luna"].supports_function_tools = True
        self.add_alias(
            _alias_with_routes(
                RouteRef("llama_cpp", LOCAL_MODEL),
                [RouteRef(FAKE_PROVIDER, "gpt-6-luna")],
                name="local",
            )
        )

        tool_payload = {
            "model": "local",
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [
                {
                    "type": "function",
                    "function": {"name": "lookup", "parameters": {"type": "object", "properties": {}}},
                }
            ],
            "tool_choice": "auto",
        }

        status, _, _ = self.harness.request("POST", "/v1/chat/completions", body=tool_payload)
        self.assertEqual(status, 200, "the fallback serves while the local row cannot")
        self.assertEqual(fixture.requests, [], "tools are unknown, so the local server is never called")
        self.assertEqual(
            {entry["reason"] for entry in self.event()["skipped"]},
            {"this model does not support function tools"},
        )

        self.approve("llama_cpp", "supports_function_tools")
        status, _, _ = self.harness.request("POST", "/v1/chat/completions", body=tool_payload)
        self.assertEqual(status, 200)
        chat_calls = [
            entry for entry in fixture.requests if entry["path"].endswith("/chat/completions")
        ]
        self.assertEqual(len(chat_calls), 1, "exactly one inference call")
        body = chat_calls[0]["body"].decode("utf-8")
        sent = json.loads(body)
        self.assertIn("tools", sent, "the tool definitions must reach a server that supports them")
        # tool_choice is dropped by the adapter: none of the three documents
        # it, and a server that rejects unknown fields would 400 on it.
        self.assertNotIn("tool_choice", sent)
        self.assertNotIn("n", sent)
        self.assertNotIn("user", sent)
        self.assertNotIn("reasoning_effort", sent)
        self.assertNotIn("tool_choice", body)


class EffortSubstitutionTests(LocalRouteTestCase):
    """Case 13: an effort this row publishes is dropped, and said so."""

    def test_the_effort_is_dropped_and_the_substitution_is_recorded(self) -> None:
        fixture = LocalFixture()
        self.addCleanup(fixture.close)
        self.add_local("ollama", base_url=fixture.base_url)
        self.add_alias(_alias_with_routes(RouteRef("ollama", LOCAL_MODEL), [], name="local"))

        status, _, _ = self.harness.request(
            "POST",
            "/v1/responses",
            body={"model": "local", "input": "hi", "reasoning_effort": "high"},
        )
        self.assertEqual(status, 200, "a preference the row cannot honour must not exclude the route")

        sent = json.loads(fixture.last()["body"].decode("utf-8"))
        self.assertNotIn("reasoning_effort", sent)
        self.assertNotIn("reasoning", sent)

        degradations = self.event()["degradations"]
        self.assertTrue(degradations, "the substitution must be recorded")
        self.assertIn("reasoning effort", degradations[0]["reason"])
        self.assertEqual(degradations[0]["provider"], "ollama")


class FallbackConsentTests(LocalRouteTestCase):
    """Case 14: a local fallback is never taken silently."""

    def test_an_unconfirmed_local_fallback_is_skipped_not_taken(self) -> None:
        fixture = LocalFixture()
        self.addCleanup(fixture.close)
        self.add_local("ollama", base_url=fixture.base_url)
        failing = FakeAdapter(fail_with=AdapterError(ERROR_TRANSIENT, provider_id=FAKE_PROVIDER))
        self.harness.registry.register(failing)
        self.add_alias(
            _local_alias(
                RouteRef(FAKE_PROVIDER, "gpt-6-luna"),
                [RouteRef("ollama", LOCAL_MODEL)],
                triggers=["upstream_unavailable"],
                confirmed=[],  # the owner has not confirmed this provider
            )
        )

        status, payload, _ = self.harness.request(
            "POST", "/v1/chat/completions", body={"model": "local", "messages": [{"role": "user", "content": "hi"}]}
        )
        self.assertNotEqual(status, 200, payload)
        self.assertEqual(fixture.requests, [], "an unconfirmed provider must not be called")
        text = json.dumps(payload)
        self.assertIn("has not been confirmed by the owner", text)

    def test_a_confirmed_local_fallback_serves_the_request(self) -> None:
        fixture = LocalFixture()
        self.addCleanup(fixture.close)
        self.add_local("ollama", base_url=fixture.base_url)
        failing = FakeAdapter(fail_with=AdapterError(ERROR_TRANSIENT, provider_id=FAKE_PROVIDER))
        self.harness.registry.register(failing)
        self.add_alias(
            _local_alias(
                RouteRef(FAKE_PROVIDER, "gpt-6-luna"),
                [RouteRef("ollama", LOCAL_MODEL)],
                triggers=["upstream_unavailable"],
                confirmed=["ollama"],
            )
        )

        status, payload, _ = self.harness.request(
            "POST", "/v1/chat/completions", body={"model": "local", "messages": [{"role": "user", "content": "hi"}]}
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(len(fixture.requests), 1)
        event = self.event()
        self.assertTrue(event["fallback_used"])
        self.assertEqual(event["serving_provider"], "ollama")


class BridgeParityTests(LocalRouteTestCase):
    """Case 15: the same local server, buffered and streamed, through the bridge."""

    def _harness_with(self, **fixture_kwargs) -> LocalFixture:
        fixture = LocalFixture(**fixture_kwargs)
        self.addCleanup(fixture.close)
        self.add_local("ollama", base_url=fixture.base_url)
        self.add_alias(_local_alias(RouteRef("ollama", LOCAL_MODEL), [], name="local"))
        return fixture

    def test_a_buffered_response_is_converted_into_the_callers_dialect(self) -> None:
        self._harness_with()
        status, payload, _ = self.harness.request(
            "POST",
            "/v1/responses",
            body={"model": "local", "input": "hi"},
        )
        self.assertEqual(status, 200, payload)
        output = json.dumps(payload)
        self.assertIn("hello", output)
        self.assertEqual(payload.get("status"), "completed")

    def test_a_streamed_response_is_converted_into_the_callers_dialect(self) -> None:
        self._harness_with(
            stream_frames=[
                {
                    "id": "chatcmpl-fixture",
                    "object": "chat.completion.chunk",
                    "choices": [{"index": 0, "delta": {"content": "hello"}, "finish_reason": None}],
                },
                {
                    "id": "chatcmpl-fixture",
                    "object": "chat.completion.chunk",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8},
                },
            ]
        )
        status, payload, headers = self.harness.request(
            "POST",
            "/v1/responses",
            body={"model": "local", "input": "hi", "stream": True},
            raw=True,
        )
        self.assertEqual(status, 200)
        self.assertIn("text/event-stream", headers.get("Content-Type", ""))
        text = payload.decode("utf-8")
        self.assertIn("response.output_text.delta", text)
        self.assertIn("hello", text)
        self.assertIn("response.completed", text)

    def test_a_chat_caller_gets_chat_shapes_from_the_same_server(self) -> None:
        self._harness_with()
        status, payload, _ = self.harness.request(
            "POST",
            "/v1/chat/completions",
            body={"model": "local", "messages": [{"role": "user", "content": "hi"}]},
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["choices"][0]["message"]["content"], "hello")
        self.assertEqual(payload["object"], "chat.completion")


class UsageHonestyTests(LocalRouteTestCase):
    """Case 18: a refusal where there is none, silence where there is no number."""

    def test_a_chunk_with_no_choices_is_a_conversion_refusal(self) -> None:
        from gateway.bridge import ConversionError
        from gateway.bridge.aggregate import aggregate_chat

        # The refusal itself: a stream with no chunk carrying choices has
        # produced nothing to aggregate, so it is refused rather than
        # rendered as an empty completion.
        with self.assertRaises(ConversionError) as caught:
            aggregate_chat([b'data: {"id":"x","object":"chat.completion.chunk"}\n\n'])
        self.assertEqual(caught.exception.capability, "stream_termination")

        # And over the API a streamed caller sees no invented completion:
        # no text delta, no fabricated content.
        fixture = LocalFixture(
            stream_frames=[{"id": "chatcmpl-fixture", "object": "chat.completion.chunk"}]
        )
        self.addCleanup(fixture.close)
        self.add_local("ollama", base_url=fixture.base_url)
        self.add_alias(_local_alias(RouteRef("ollama", LOCAL_MODEL), [], name="local"))

        status, payload, _ = self.harness.request(
            "POST",
            "/v1/responses",
            body={"model": "local", "input": "hi", "stream": True},
            raw=True,
        )
        text = payload.decode("utf-8") if isinstance(payload, bytes) else json.dumps(payload)
        self.assertNotIn("response.output_text.delta", text)
        self.assertNotIn("hello", text)

    def test_missing_usage_is_recorded_as_missing_and_never_as_zero(self) -> None:
        fixture = LocalFixture(
            chat={
                "id": "chatcmpl-fixture",
                "object": "chat.completion",
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": "no usage here"}, "finish_reason": "stop"}
                ],
            }
        )
        self.addCleanup(fixture.close)
        self.add_local("ollama", base_url=fixture.base_url)
        self.add_alias(_local_alias(RouteRef("ollama", LOCAL_MODEL), [], name="local"))

        status, payload, _ = self.harness.request(
            "POST",
            "/v1/chat/completions",
            body={"model": "local", "messages": [{"role": "user", "content": "hi"}]},
        )
        self.assertEqual(status, 200, payload)
        event = self.event()
        self.assertIsNone(
            event.get("token_counters"), "an absent usage block must stay absent, never 0"
        )
        # There is no published price for a local model, so the cost can only
        # ever be "no published price" - never $0.00.
        self.assertIsNone(price_for(LOCAL_MODEL))

    def test_unusable_counters_are_flagged_not_invented(self) -> None:
        # A *reported* number outside the usable range is a refusal: it is
        # flagged, never silently replaced by a smaller number or by zero.
        frames = [
            b'data: {"choices":[{"delta":{}}],"usage":{"prompt_tokens":-5,"completion_tokens":10}}\n\n',
            b"data: [DONE]\n\n",
        ]
        counters = stream_token_counters(frames, DIALECT_OPENAI_CHAT)
        self.assertIsNotNone(counters)
        self.assertTrue(counters.get(INVALID_USAGE_MARKER))
        self.assertNotIn("prompt_tokens", counters)
        self.assertEqual(counters.get("completion_tokens"), 10)

        # A counter that was never a number is "not reported", not zero.
        unreported = stream_token_counters(
            [
                b'data: {"choices":[{"delta":{}}],"usage":{"prompt_tokens":"many","completion_tokens":null}}\n\n',
                b"data: [DONE]\n\n",
            ],
            DIALECT_OPENAI_CHAT,
        )
        self.assertIsNone(unreported)


class CircuitRecoveryTests(LocalRouteTestCase):
    """Case 20: refused connection -> transient -> bounded circuit -> cleared."""

    def test_a_refused_endpoint_opens_a_bounded_circuit_and_clearing_recovers(self) -> None:
        closed = ClosedPort()
        self.add_local("lm_studio", base_url=closed.base_url)  # refuses
        healthy = LocalFixture()
        self.addCleanup(healthy.close)
        self.add_local("ollama", base_url=healthy.base_url)  # serves
        failing = FakeAdapter(fail_with=AdapterError(ERROR_TRANSIENT, provider_id=FAKE_PROVIDER))
        self.harness.registry.register(failing)
        self.add_alias(
            _local_alias(
                RouteRef("lm_studio", LOCAL_MODEL),
                [RouteRef("ollama", LOCAL_MODEL)],
                triggers=["upstream_unavailable"],
                confirmed=["ollama"],
            )
        )

        body = {"model": "local", "messages": [{"role": "user", "content": "hi"}]}
        status, payload, _ = self.harness.request("POST", "/v1/chat/completions", body=body)
        self.assertEqual(status, 200, f"the healthy fallback must serve: {payload}")
        status, payload, _ = self.harness.request("POST", "/v1/chat/completions", body=body)
        self.assertEqual(status, 200, payload)

        circuits = self.harness.router.circuits
        remaining = max(
            circuits.remaining("lm_studio"),
            circuits.remaining("lm_studio", LOCAL_MODEL),
        )
        self.assertGreater(remaining, 0, "the refusing endpoint must be in cooldown")
        self.assertLessEqual(
            remaining, TRANSIENT_BACKOFF_MAX_SECONDS, "a transient circuit must be bounded"
        )
        details = circuits.details()
        error_classes = {row.get("error_class") for row in details.values()}
        self.assertIn(ERROR_TRANSIENT, error_classes)

        # The owner clears the cooldown, and the endpoint - now reachable -
        # serves as the primary again.
        with self.harness.config.mutate() as config:
            config.providers["lm_studio"].base_url = healthy.base_url
        status, payload, _ = self.harness.request(
            "POST",
            "/owner/api/provider",
            body={"action": "clear_cooldowns", "provider_id": "lm_studio"},
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(circuits.remaining("lm_studio"), 0)

        status, payload, _ = self.harness.request("POST", "/v1/chat/completions", body=body)
        self.assertEqual(status, 200, payload)
        self.assertEqual(self.event()["serving_provider"], "lm_studio")
        self.assertFalse(self.event()["fallback_used"])


class RegistrationAndIsolationTests(unittest.TestCase):
    """Cases 22 and 24: boot rows, no boot traffic, credential isolation."""

    def test_boot_creates_the_three_local_rows_without_one_outbound_call(self) -> None:
        from gateway.cli import build_application

        with tempfile.TemporaryDirectory() as tmp:
            # Any connection attempt at boot fails the test outright.
            with mock.patch.object(
                socket, "create_connection", side_effect=AssertionError("boot must not connect")
            ):
                app, config_store, _credentials, _router = build_application(
                    Path(tmp), "127.0.0.1", 8765
                )
            config = config_store.snapshot()
            for provider_id in FLAVORS:
                self.assertIn(provider_id, config.providers, provider_id)
                self.assertEqual(
                    config.providers[provider_id].state, STATE_NOT_CONFIGURED, provider_id
                )
                self.assertIsNone(config.providers[provider_id].base_url, provider_id)
            for provider_id in FLAVORS:
                self.assertIn(provider_id, default_provider_ids())
            self.assertIsNotNone(app)

    def test_the_local_handle_cannot_read_another_providers_credential(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = CredentialStore(Path(tmp))
            store.provider_handle("anthropic").write({"api_key": "anthropic-secret-value"})
            self.assertEqual(
                store.provider_handle("anthropic").read()["api_key"], "anthropic-secret-value"
            )
            self.assertNotEqual(
                store.provider_handle("ollama").read(),
                {"api_key": "anthropic-secret-value"},
            )
            for crafted in ("../../etc", "ollama/../anthropic", "not_a_provider", ""):
                with self.subTest(crafted=crafted):
                    with self.assertRaises(CredentialError):
                        store.provider_handle(crafted)

    def test_the_public_provider_clients_carry_no_peer_check(self) -> None:
        from gateway.adapters.anthropic import AnthropicAdapter
        from gateway.adapters.http_client import UpstreamClient

        self.assertIsNone(UpstreamClient()._pool.peer_check)
        with tempfile.TemporaryDirectory() as tmp:
            handle = CredentialStore(Path(tmp)).provider_handle("anthropic")
        adapter = AnthropicAdapter(handle)
        self.addCleanup(adapter._client.close)
        self.assertIsNone(adapter._client._pool.peer_check)


class RouteValidationTests(LocalRouteTestCase):
    """Case 23: a local route needs a discovered *and* enabled model."""

    def test_validate_route_refuses_until_the_model_is_discovered_and_enabled(self) -> None:
        registry = self.harness.registry
        config = self.harness.config

        with self.assertRaises(ValidationError):
            validate_route(config, registry, "ollama", LOCAL_MODEL, "primary")

        with self.harness.config.mutate() as snapshot:
            snapshot.providers["ollama"] = ProviderState(
                provider_id="ollama",
                kind="ollama",
                state=STATE_NOT_CONFIGURED,
                models={
                    LOCAL_MODEL: ModelEntry(
                        model_id=LOCAL_MODEL,
                        enabled=False,
                        dialect=DIALECT_OPENAI_CHAT,
                        source="discovered",
                    )
                },
            )
        with self.assertRaises(ValidationError) as caught:
            validate_route(config, registry, "ollama", LOCAL_MODEL, "primary")
        self.assertIn("not enabled", str(caught.exception))

        with self.harness.config.mutate() as snapshot:
            snapshot.providers["ollama"].models[LOCAL_MODEL].enabled = True
        ref = validate_route(config, registry, "ollama", LOCAL_MODEL, "primary")
        self.assertEqual(ref, RouteRef("ollama", LOCAL_MODEL))


class ExposureTests(unittest.TestCase):
    """Case 25: the endpoint is owner state, and only owner state."""

    def setUp(self) -> None:
        from tests.test_local_openai import LocalAdapterHarness

        self.log = io.StringIO()
        self.harness = LocalAdapterHarness(logger=RequestLogger(stream=self.log, enabled=True))
        self.addCleanup(self.harness.close)
        self.base_url = "http://192.168.1.77:11434"

    def _get(self, path: str, *, owner: bool = False):
        from gateway.http.core import Headers, Request

        from tests.owner_session import cookie_header

        headers = [("Host", "127.0.0.1:8765")]
        if owner:
            headers.append(("Cookie", cookie_header(self.harness.app)))
        request = Request(
            method="GET",
            path=path,
            raw_query="",
            headers=Headers(headers),
            body=b"",
            client_address=("127.0.0.1", 40000),
            server_port=8765,
        )
        response = self.harness.app.handle(request)
        return response.status, response.body

    def test_the_endpoint_is_owner_state_and_never_anything_public(self) -> None:
        status, _ = self.harness.set_endpoint("ollama", self.base_url)
        self.assertEqual(status, 200)

        owner_state = json.dumps(self.harness.state())
        self.assertIn(self.base_url, owner_state, "the owner must be able to see the endpoint")

        for path in (
            "/health",
            "/owner",
            "/owner/index.html",
            "/owner/app.js",
            "/owner/style.css",
            "/owner/api/auth/session",
            "/v1/models",
            "/v1/chat/completions",
        ):
            with self.subTest(path=path):
                status, body = self._get(path)
                self.assertNotIn(self.base_url, body.decode("utf-8", "replace"), path)

    def test_the_access_log_neither_names_the_endpoint_nor_a_token(self) -> None:
        from gateway.credentials import CredentialStore

        token = "log-secret-token-value"
        self.harness.credentials.provider_handle("ollama").write({"api_key": token})
        # Save and clear: both are owner mutations that must be logged by
        # action, and neither may name the value it stored.
        status, _ = self.harness.set_endpoint("ollama", self.base_url)
        self.assertEqual(status, 200)
        status, _ = self.harness.set_endpoint("ollama", "")
        self.assertEqual(status, 200)

        lines = [line for line in self.log.getvalue().splitlines() if line.strip()]
        self.assertTrue(lines, "the owner actions must be logged")
        text = "\n".join(lines)
        self.assertNotIn(self.base_url, text)
        self.assertNotIn("192.168.1.77", text)
        self.assertNotIn(token, text)
        self.assertIn("set_base_url", text, "the action itself is logged, not its value")


class ConfigRoundTripTests(unittest.TestCase):
    """Case 26: the endpoint survives persistence, including older files."""

    def test_base_url_round_trips_and_the_revision_moves(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            config = ConfigStore(path)
            with config.mutate() as snapshot:
                snapshot.providers["ollama"] = ProviderState(
                    provider_id="ollama",
                    kind="ollama",
                    state=STATE_CONFIGURED_UNTESTED,
                    base_url="http://127.0.0.1:11434",
                )
            self.assertEqual(
                config.snapshot().providers["ollama"].base_url, "http://127.0.0.1:11434"
            )
            reloaded = ConfigStore(path)
            self.assertEqual(
                reloaded.snapshot().providers["ollama"].base_url, "http://127.0.0.1:11434"
            )
            first_revision = reloaded.snapshot().revision
            with reloaded.mutate() as snapshot:
                snapshot.providers["ollama"].base_url = "http://127.0.0.1:8080"
            self.assertGreater(reloaded.snapshot().revision, first_revision)

    def test_a_config_written_before_endpoints_still_loads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "revision": 4,
                        "providers": {
                            "ollama": {"provider_id": "ollama", "kind": "ollama", "state": "not_configured"}
                        },
                        "aliases": {},
                        "settings": {},
                    }
                ),
                encoding="utf-8",
            )
            config = ConfigStore(path)
            provider = config.snapshot().providers["ollama"]
            self.assertIsNone(provider.base_url)
            self.assertEqual(provider.state, STATE_NOT_CONFIGURED)
            self.assertEqual(config.snapshot().revision, 4)


class ComposeLoopbackTests(unittest.TestCase):
    """Case 28: the shipped deployment can reach a loopback endpoint."""

    def test_the_gateway_service_keeps_host_networking(self) -> None:
        compose = Path(__file__).resolve().parents[1] / "compose.yaml"
        text = compose.read_text(encoding="utf-8")
        gateway_block = text.split("\n  gateway:", 1)[1]
        self.assertIn("network_mode: host", gateway_block)
        # The bridge-mode caveat is documented for the owner in the console,
        # not asserted against a container this test does not start.
        console = Path(__file__).resolve().parents[1] / "src/gateway/static/console.js"
        self.assertIn("bridge-networked", console.read_text(encoding="utf-8"))

    def test_a_loopback_endpoint_is_reached_by_the_real_client(self) -> None:
        fixture = LocalFixture()
        self.addCleanup(fixture.close)
        with tempfile.TemporaryDirectory() as tmp:
            config = ConfigStore(Path(tmp) / "config.json")
            with config.mutate() as snapshot:
                snapshot.providers["ollama"] = ProviderState(
                    provider_id="ollama",
                    kind="ollama",
                    state=STATE_CONFIGURED_UNTESTED,
                    base_url=fixture.base_url,
                )
            adapter = LocalOpenAIAdapter(
                "ollama", CredentialStore(Path(tmp)).provider_handle("ollama"), config
            )
            self.addCleanup(adapter._client.close)
            result = adapter.test_connection()
            self.assertTrue(result.ok, result.message)
            self.assertEqual(fixture.paths(), ["/v1/models", "/api/show"])


if __name__ == "__main__":
    unittest.main()
