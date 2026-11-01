"""End-to-end tests over real HTTP.

These exercise the whole application: the security policy, the API surface,
the console, the routing core, and the fallback rules. The provider is a
fake adapter - no test ever contacts a real provider.
"""

from __future__ import annotations

import json
import socket
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from unittest.mock import patch

from gateway.adapters.base import (
    AdapterError,
    ProviderRequest,
    ProviderResponse,
    TestResult,
)
from gateway.adapters.registry import AdapterRegistry
from gateway.auth import ProfileAuth
from gateway.config import ConfigStore, ModelEntry, Profile, ProviderState, RouteRef
from gateway.core.routing import Router
from gateway.credentials import CredentialStore
from gateway.http.app import GatewayApp
from gateway.http.core import Response, Server
from gateway.http.security import (
    SecurityPolicy,
    build_allowed_hosts,
    build_canonical_origins,
)
from gateway.logging import RequestLogger
from gateway.observability import LimitStore, RequestEvent, RequestLedger, utc_now_iso

FAKE_PROVIDER = "chatgpt_codex"


class FakeAdapter:
    """A provider that answers from a scripted response.

    It records what it was asked for, so tests can assert that the gateway
    converted the request correctly and did not call a provider it should
    not have called.
    """

    def __init__(
        self,
        provider_id: str = FAKE_PROVIDER,
        dialect: str = "openai_responses",
        fail_with: AdapterError | None = None,
        stream_frames: list[bytes] | None = None,
    ) -> None:
        self.provider_id = provider_id
        self.kind = provider_id
        self.dialect = dialect
        self.fail_with = fail_with
        self.stream_frames = stream_frames or []
        self.calls: list[ProviderRequest] = []
        self.model_list_calls = 0
        #: When set, the provider answers with a stream even for a client
        #: that asked for a single object - a streaming-only upstream.
        self.always_stream = False

    def list_models(self) -> list:
        self.model_list_calls += 1
        from gateway.core.capabilities import ModelInfo

        return [
            ModelInfo(
                model_id="gpt-6-luna",
                dialect="openai_responses",
                reasoning_efforts=("none", "low", "medium", "high", "xhigh", "max"),
                retention_class="30_days",
                monthly_limit_usd=15.0,
                limits_source="published_estimate",
            )
        ]

    def test_connection(self) -> TestResult:
        return TestResult(ok=True, state="connected", message="fake provider reachable")

    def invoke(self, request: ProviderRequest) -> ProviderResponse:
        self.calls.append(request)
        if self.fail_with is not None:
            raise self.fail_with
        if request.stream or self.always_stream:
            return ProviderResponse(
                status=200,
                headers={},
                body=None,
                stream=iter(self.stream_frames),
                content_type="text/event-stream",
                dialect=self.dialect,
            )
        body = json.dumps(
            {
                "id": "resp_fake",
                "object": "response",
                "model": request.model,
                "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "ok"}]}],
            }
        ).encode("utf-8")
        return ProviderResponse(
            status=200, headers={}, body=body, content_type="application/json", dialect=self.dialect
        )


def free_port(host: str = "127.0.0.1") -> int:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    endpoint = (host, 0, 0, 0) if family == socket.AF_INET6 else (host, 0)
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        sock.bind(endpoint)
        return int(sock.getsockname()[1])


class SystemOneAdapter:
    provider_id = "opencode_console"
    kind = "opencode_console"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.model_list_calls = 0

    def list_models(self) -> list:
        self.model_list_calls += 1
        return []

    def test_connection(self) -> TestResult:
        return TestResult(ok=True, state="connected", message="synthetic Jev reachable")

    def invoke_systemone(self, payload: dict[str, Any]) -> bytes:
        self.calls.append(payload)
        return json.dumps({"model": payload["model"], "answers": {"phase": {"type": "choice", "choice": "plan", "confidence": 0.9, "probabilities": {"plan": 0.9}}}, "usage": {"input_tokens": 1, "output_tokens": 1}}).encode()


class GatewayHarness:
    """Builds a gateway over loopback for a test."""

    def __init__(self, adapter: FakeAdapter | None = None, host: str = "127.0.0.1") -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.host = host
        self.authority = f"[{host}]" if ":" in host else host
        self.port = free_port(host)

        self.config = ConfigStore(self.data_dir / "config.json")
        self.credentials = CredentialStore(self.data_dir)
        self.registry = AdapterRegistry()
        self.adapter = adapter or FakeAdapter()
        self.registry.register(self.adapter)
        self.registry.register(FakeAdapter(provider_id="opencode_go", dialect="openai_chat"))
        self.jev = SystemOneAdapter()
        self.registry.register(self.jev)

        with self.config.mutate() as snapshot:
            snapshot.providers[FAKE_PROVIDER] = ProviderState(
                provider_id=FAKE_PROVIDER,
                kind=FAKE_PROVIDER,
                state="connected",
                models={
                    "gpt-6-luna": ModelEntry(
                        model_id="gpt-6-luna",
                        enabled=True,
                        dialect="openai_responses",
                        source="discovered",
                    )
                },
            )
            snapshot.providers["opencode_go"] = ProviderState(
                provider_id="opencode_go",
                kind="opencode_go",
                state="not_configured",
            )
            snapshot.providers["opencode_console"] = ProviderState(
                provider_id="opencode_console",
                kind="opencode_console",
                state="connected",
                models={"jev-1.13": ModelEntry(model_id="jev-1.13", enabled=True, dialect="systemone", source="discovered")},
            )

        self.router = Router(self.config, self.registry, ledger=RequestLedger())
        self.limits = LimitStore()
        self.profile_auth = ProfileAuth(self.config, self.credentials)

        policy = SecurityPolicy(
            allowed_hosts=build_allowed_hosts(self.host, self.port),
            canonical_origins=build_canonical_origins(self.host, self.port),
        )
        self.app = GatewayApp(
            config=self.config,
            credentials=self.credentials,
            router=self.router,
            registry=self.registry,
            profile_auth=self.profile_auth,
            policy=policy,
            ledger=self.router._ledger,
            limits=self.limits,
            release_identity=lambda: {
                "service": "personal-llm-gateway",
                "source_revision": "testcommit",
                "source_tree": "testtree",
                "image_revision": "sha256:" + "a" * 64,
            },
            base_url=f"http://{self.authority}:{self.port}",
            mounts=("/owner",),
            logger=RequestLogger(enabled=False),
        )

        self.server = Server(self.host, self.port, self.app.handle)
        self.server.start_background()

    def close(self) -> None:
        self.server.stop()
        self._tmp.cleanup()

    # --- requests --------------------------------------------------------

    def request(
        self,
        method: str,
        path: str,
        body: Any = None,
        headers: dict[str, str] | None = None,
        port: int | None = None,
        raw: bool = False,
    ):
        url = f"http://{self.authority}:{port or self.port}{path}"
        data = body if isinstance(body, bytes) else (json.dumps(body).encode("utf-8") if body is not None else None)
        request = urllib.request.Request(url=url, data=data, method=method)
        request.add_header("Host", f"{self.authority}:{port or self.port}")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        # A browser always sends Origin on a same-origin mutation, and the
        # gateway requires it on the browser surfaces. A caller that supplies
        # its own Origin keeps it.
        if path.startswith("/owner/") and not any(
            key.lower() == "origin" for key in (headers or {})
        ):
            request.add_header("Origin", f"http://{self.authority}:{port or self.port}")
        def collect(response) -> dict[str, Any]:
            headers = {key: value for key, value in response.headers.items()}
            # Preserve cookie headers for any response under test.
            headers["set-cookie"] = list(response.headers.get_all("Set-Cookie") or [])
            return headers

        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                payload = response.read()
                if raw:
                    return response.status, payload, collect(response)
                return response.status, json.loads(payload.decode("utf-8")) if payload else None, collect(response)
        except urllib.error.HTTPError as exc:
            with exc:
                payload = exc.read()
            if raw:
                return exc.code, payload, collect(exc)
            try:
                return exc.code, json.loads(payload.decode("utf-8")), collect(exc)
            except ValueError:
                return exc.code, payload.decode("utf-8", "replace"), collect(exc)


class ProviderConcurrencyTests(unittest.TestCase):
    def test_requests_over_provider_cap_are_refused_and_capacity_recovers(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        count_lock = threading.Lock()
        started = 0

        class BlockingAdapter(FakeAdapter):
            def invoke(self, request: ProviderRequest) -> ProviderResponse:
                nonlocal started
                with count_lock:
                    started += 1
                    if started == 4:
                        entered.set()
                release.wait(timeout=5)
                return super().invoke(request)

        adapter = BlockingAdapter()
        harness = GatewayHarness(adapter)
        self.addCleanup(harness.close)
        with harness.config.mutate() as config:
            config.aliases["coding"] = _alias()
        results: list[int] = []

        def first_request() -> None:
            status, _, _ = harness.request(
                "POST", "/v1/responses", {"model": "coding", "input": "synthetic"}
            )
            results.append(status)

        workers = [threading.Thread(target=first_request) for _ in range(4)]
        for worker in workers:
            worker.start()
        self.assertTrue(entered.wait(timeout=3))
        status, payload, _ = harness.request(
            "POST", "/v1/responses", {"model": "coding", "input": "synthetic"}
        )
        self.assertEqual(status, 502)
        self.assertEqual(payload["error"]["type"], "rate_limited")
        self.assertIn("local concurrency limit", payload["error"]["message"])
        self.assertEqual(len(adapter.calls), 0)

        release.set()
        for worker in workers:
            worker.join(timeout=3)
            self.assertFalse(worker.is_alive())
        self.assertEqual(results, [200] * 4)
        status, _, _ = harness.request(
            "POST", "/v1/responses", {"model": "coding", "input": "synthetic"}
        )
        self.assertEqual(status, 200)

    def test_capacity_is_independent_per_provider(self) -> None:
        from gateway.provider_concurrency import ProviderConcurrency

        cap = ProviderConcurrency(1)
        cap.acquire("provider-a")
        cap.acquire("provider-b")
        with self.assertRaises(Exception) as raised:
            cap.acquire("provider-a")
        self.assertEqual(raised.exception.error_class, "rate_limited")
        cap.release("provider-a")
        cap.acquire("provider-a")
        cap.release("provider-a")
        cap.release("provider-b")

    def test_streaming_response_holds_capacity_until_consumed(self) -> None:
        from gateway.provider_concurrency import ProviderConcurrency, _CappedAdapter

        class StreamingAdapter:
            provider_id = "stream-provider"

            def invoke(self, request: ProviderRequest) -> ProviderResponse:
                return ProviderResponse(
                    status=200,
                    stream=iter((b"one", b"two")),
                    content_type="text/event-stream",
                    dialect="openai_responses",
                )

        limits = ProviderConcurrency(1)
        adapter = _CappedAdapter(StreamingAdapter(), limits)
        response = adapter.invoke(ProviderRequest(model="m", payload={}, stream=True))
        with self.assertRaises(Exception) as raised:
            limits.acquire("stream-provider")
        self.assertEqual(raised.exception.error_class, "rate_limited")
        self.assertEqual(list(response.stream or ()), [b"one", b"two"])
        limits.acquire("stream-provider")
        limits.release("stream-provider")

    def test_invalid_limit_is_refused(self) -> None:
        from gateway.provider_concurrency import ProviderConcurrency

        for limit in (0, -1, True, 1.5):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                ProviderConcurrency(limit)


class MetadataTests(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = GatewayHarness()
        self.addCleanup(self.harness.close)

    def test_ipv6_loopback_server_serves_canonical_mutation(self) -> None:
        try:
            harness = GatewayHarness(host="::1")
        except OSError as exc:
            self.skipTest(f"IPv6 loopback unavailable: {exc}")
        self.addCleanup(harness.close)
        self.harness = harness
        status, payload, _ = harness.request(
            "GET", "/version",
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(harness.app.base_url, f"http://[::1]:{harness.port}")
        status, payload, _ = harness.request(
            "POST", "/owner/api/model",
            body={"action": "enable", "provider_id": "chatgpt_codex", "model_id": "gpt-6-luna"},
        )
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["ok"])

    def test_ipv6_ula_listener_binds_when_host_owns_address(self) -> None:
        host = "fd12:3456::1"
        try:
            port = free_port(host)
        except OSError as exc:
            self.skipTest(f"test host does not own ULA: {exc}")
        server = Server(host, port, lambda request: Response(200))
        try:
            server.start_background()
        except OSError as exc:
            self.skipTest(f"IPv6 ULA bind unavailable: {exc}")
        self.addCleanup(server.stop)
        self.assertEqual(server.actual_port, port)

class MetadataDetailsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = GatewayHarness()
        self.addCleanup(self.harness.close)

    def test_version_has_the_fixed_four_key_schema(self) -> None:
        status, payload, _ = self.harness.request("GET", "/version")
        self.assertEqual(status, 200)
        self.assertEqual(
            sorted(payload.keys()),
            ["image_revision", "service", "source_revision", "source_tree"],
        )
        self.assertEqual(payload["service"], "personal-llm-gateway")

    def test_version_exposes_no_environment_or_configuration(self) -> None:
        _, payload, _ = self.harness.request("GET", "/version")
        text = json.dumps(payload)
        self.assertNotIn("config", text.lower().replace("configuration", ""))
        self.assertNotIn("credential", text.lower())

    def test_health_is_static_and_anonymous(self) -> None:
        status, payload, _ = self.harness.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"status": "ok"})

    def test_metrics_uses_prometheus_format_and_rejects_untrusted_headers(self) -> None:
        status, payload, headers = self.harness.request("GET", "/metrics", raw=True)
        self.assertEqual(status, 200)
        self.assertIn("text/plain; version=0.0.4", next(value for key, value in headers.items() if key.lower() == "content-type"))
        self.assertIn(b"gateway_requests_total", payload)
        status, _, _ = self.harness.request("GET", "/metrics", headers={"X-Forwarded-For": "203.0.113.9"})
        self.assertEqual(status, 400)
        status, _, _ = self.harness.request("GET", "/metrics", headers={"Host": "evil.example"})
        self.assertEqual(status, 403)
        status, _, _ = self.harness.request("POST", "/metrics", headers={"Origin": "https://evil.example"}, raw=True)
        self.assertEqual(status, 405)
        self.assertNotIn("allow", headers)
        self.assertNotIn("client_address", payload.decode())
        self.assertNotIn("credential", payload.decode())
        self.assertNotIn("prompt", payload.decode())
        self.assertNotIn("token", payload.decode())
        from prometheus_client.parser import text_string_to_metric_families
        self.assertTrue(list(text_string_to_metric_families(payload.decode())))

    def test_metrics_is_not_routed_from_owner_mount(self) -> None:
        status, _, _ = self.harness.request("GET", "/owner/metrics")
        self.assertEqual(status, 404)

    def test_metrics_forwarded_and_host_refusals_follow_shared_policy(self) -> None:
        status, payload, _ = self.harness.request("GET", "/metrics", headers={"X-Forwarded-Host": "bad.example"})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["type"], "forwarded_header_rejected")
        status, payload, _ = self.harness.request("GET", "/metrics", headers={"Host": "bad.example"})
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"]["type"], "host_mismatch")

    def test_metrics_emits_cooldown_gauge_from_router_circuits(self) -> None:
        # Wiring-level regression for QA-W2B-1: handle_metrics must read the
        # router's real circuit store. With an open circuit, GET /metrics
        # must carry a gateway_cooldown_remaining_seconds{route="..."} sample.
        router = self.harness.router
        self.assertIsNotNone(router)
        router.circuits.record_failure(FAKE_PROVIDER, "gpt-5-codex", "rate_limited", 30)
        open_circuits = router.circuits.snapshot()
        self.assertIn(f"{FAKE_PROVIDER}/gpt-5-codex", open_circuits)
        status, payload, _ = self.harness.request("GET", "/metrics", raw=True)
        self.assertEqual(status, 200)
        text = payload.decode("utf-8")
        self.assertIn(
            f'gateway_cooldown_remaining_seconds{{route="{FAKE_PROVIDER}/gpt-5-codex"}} ',
            text,
        )


class SecurityProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = GatewayHarness()
        self.addCleanup(self.harness.close)

    def test_forwarded_header_is_rejected(self) -> None:
        status, payload, _ = self.harness.request(
            "GET", "/version", headers={"X-Forwarded-For": "203.0.113.9"}
        )
        self.assertEqual(status, 400)

    def test_forwarded_header_is_rejected_on_owner_mutation(self) -> None:
        status, payload, _ = self.harness.request(
            "POST", "/owner/api/settings", body={"action": "save"},
            headers={"X-Forwarded-Host": "evil.example"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["type"], "forwarded_header_rejected")

    def test_mismatched_host_is_rejected(self) -> None:
        status, payload, _ = self.harness.request("GET", "/version", headers={"Host": "evil.example.com"})
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"]["type"], "host_mismatch")

    def test_setup_surface_is_removed(self) -> None:
        status, payload, _ = self.harness.request("GET", "/setup/api/state")
        self.assertEqual(status, 404)

    def test_full_state_is_available_without_a_session(self) -> None:
        status, payload, _ = self.harness.request("GET", "/owner/api/state")
        self.assertEqual(status, 200)
        self.assertFalse(payload["authenticated"])
        self.assertTrue(payload["can_mutate"])
        self.assertTrue(payload["can_enroll_credentials"])

    def test_safe_status_is_available_without_a_session(self) -> None:
        status, payload, _ = self.harness.request("GET", "/owner/api/status")
        self.assertEqual(status, 200)
        # The safe status must not carry the disclosure class: no profile
        # records, no configuration revision, no request ledger.
        self.assertNotIn("profiles", payload)
        self.assertNotIn("revision", payload)
        self.assertNotIn("ledger", payload)
        self.assertNotIn("limits", payload)
        self.assertNotIn("aliases", payload)

    def test_login_and_passphrase_route_are_removed(self) -> None:
        status, payload, _ = self.harness.request(
            "POST", "/owner/api/session", body={"passphrase": "synthetic-only"}
        )
        self.assertEqual(status, 404)

    def test_console_is_served(self) -> None:
        status, payload, headers = self.harness.request("GET", "/owner/", raw=True)
        self.assertEqual(status, 200)
        self.assertIn(b"Personal LLM Gateway", payload)
        self.assertIn("text/html", headers.get("Content-Type", ""))
        status, script, _ = self.harness.request("GET", "/owner/app.js", raw=True)
        self.assertEqual(status, 200)
        self.assertIn(b"Trusted LAN admin mode ", script)
        self.assertNotIn(b"/api/session", script)

    def test_console_html_carries_no_secret(self) -> None:
        _, payload, _ = self.harness.request("GET", "/owner/", raw=True)
        self.assertNotIn(b"sk-", payload)
        self.assertNotIn(b"Bearer ", payload)


class ApiSurfaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = GatewayHarness()
        self.addCleanup(self.harness.close)
        with self.harness.config.mutate() as config:
            config.aliases["coding"] = _alias()

    def test_empty_models_catalog_explains_itself(self) -> None:
        with self.harness.config.mutate() as config:
            config.aliases["coding"].enabled = False
        status, payload, _ = self.harness.request("GET", "/v1/models")
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"], [])
        self.assertIn("notice", payload)
        self.assertIn("publish", payload["notice"].lower())

    def test_fresh_anonymous_models_and_no_route_chat_never_call_provider(self) -> None:
        with self.harness.config.mutate() as config:
            config.aliases.clear()
            config.profiles.clear()
        status, payload, _ = self.harness.request("GET", "/v1/models")
        self.assertEqual(status, 200)
        self.assertEqual(payload["object"], "list")
        self.assertEqual(payload["data"], [])
        status, payload, _ = self.harness.request(
            "POST", "/v1/chat/completions",
            body={"model": "missing-route", "messages": [{"role": "user", "content": "synthetic"}]},
        )
        self.assertEqual(status, 404)
        self.assertIn("error", payload)
        self.assertEqual(self.harness.adapter.calls, [])

    def test_published_alias_appears_in_models(self) -> None:
        with self.harness.config.mutate() as config:
            config.aliases["coding"].enabled = True
        status, payload, _ = self.harness.request("GET", "/v1/models")
        self.assertEqual(status, 200)
        self.assertEqual([entry["id"] for entry in payload["data"]], ["coding"])
        self.assertNotIn("notice", payload)

    def test_api_remains_anonymous_even_when_legacy_access_mode_is_authenticated(self) -> None:
        with self.harness.config.mutate() as config:
            config.access_mode = "authenticated"
            config.profiles["restricted-client"] = Profile(
                profile_id="restricted-client", name="Restricted", allowed_aliases=[]
            )
            config.aliases["coding"].enabled = True
        status, payload, _ = self.harness.request("GET", "/v1/models")
        self.assertEqual(status, 200)
        self.assertEqual([entry["id"] for entry in payload["data"]], ["coding"])

    def test_provider_secret_in_error_code_never_reaches_api_state_or_ledger(self) -> None:
        secret = "sk-" + "E" * 32
        durable_ledger_path = self.harness.data_dir / "request-ledger.jsonl"
        durable_ledger = RequestLedger(persistence_path=durable_ledger_path)
        self.addCleanup(durable_ledger.close)
        self.harness.router._ledger = durable_ledger
        self.harness.app.ledger = durable_ledger
        self.harness.adapter.fail_with = AdapterError("auth", message=secret, upstream_code=secret)
        with self.harness.config.mutate() as config:
            config.aliases["coding"].enabled = True
        status, response, _ = self.harness.request(
            "POST", "/v1/chat/completions",
            body={"model": "coding", "messages": [{"role": "user", "content": "synthetic"}]},
        )
        self.assertEqual(status, 502)
        state_status, state, _ = self.harness.request("GET", "/owner/api/state")
        self.assertEqual(state_status, 200)
        combined = json.dumps({"response": response, "state": state, "ledger": self.harness.router._ledger.recent()})
        self.assertNotIn(secret, combined)
        self.assertEqual(response["error"]["type"], "auth")
        self.assertEqual(response["error"]["detail"]["attempts"][0]["error_class"], "auth")

    def test_completion_is_routed_and_attributed(self) -> None:
        with self.harness.config.mutate() as config:
            config.aliases["coding"].enabled = True
        status, payload, _ = self.harness.request(
            "POST",
            "/v1/chat/completions",
            body={"model": "coding", "messages": [{"role": "user", "content": "hi"}]},
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(self.harness.adapter.calls), 1)
        call = self.harness.adapter.calls[0]
        self.assertEqual(call.model, "gpt-6-luna")
        self.assertEqual(call.dialect, "openai_responses")

    def test_chat_reasoning_disabled_does_not_require_reasoning_capability(self) -> None:
        with self.harness.config.mutate() as config:
            config.providers[FAKE_PROVIDER].models["gpt-6-luna"].reasoning_efforts = []
        status, payload, _ = self.harness.request(
            "POST",
            "/v1/chat/completions",
            body={
                "model": "coding",
                "messages": [{"role": "user", "content": "hi"}],
                "reasoning": {"enabled": False, "effort": "none"},
            },
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(len(self.harness.adapter.calls), 1)
        self.assertNotIn("reasoning_effort", self.harness.adapter.calls[0].payload)

    def test_preview_converts_request_without_provider_call_or_ledger_entry(self) -> None:
        before_events = self.harness.router._ledger.totals()["requests"]
        status, result, headers = self.harness.request(
            "POST", "/owner/api/preview",
            {"model": "coding", "messages": [{"role": "user", "content": "preview marker"}], "stream": False},
        )
        self.assertEqual(status, 200)
        self.assertEqual(result["dialect"], "openai_responses")
        self.assertEqual(result["model"], "gpt-6-luna")
        self.assertNotIn("coding", json.dumps(result["payload"]))
        self.assertEqual(result["payload"]["model"], "gpt-6-luna")
        self.assertEqual(result["payload"]["input"][0]["content"][0]["text"], "preview marker")
        self.assertTrue(result["session_id"].startswith(FAKE_PROVIDER + "."))
        self.assertEqual(self.harness.adapter.calls, [])
        self.assertEqual(self.harness.router._ledger.totals()["requests"], before_events)
        self.assertEqual(headers.get("Cache-Control"), "no-store")

    def test_preview_refuses_unpublished_alias(self) -> None:
        status, result, _ = self.harness.request(
            "POST", "/owner/api/preview", {"model": "missing", "messages": []}
        )
        self.assertEqual(status, 404)
        self.assertEqual(result["error"]["type"], "unknown_model")
        self.assertEqual(self.harness.adapter.calls, [])

    def test_chat_request_is_converted_to_the_responses_dialect(self) -> None:
        with self.harness.config.mutate() as config:
            config.aliases["coding"].enabled = True
        self.harness.request(
            "POST",
            "/v1/chat/completions",
            body={"model": "coding", "messages": [{"role": "user", "content": "hi"}]},
        )
        payload = self.harness.adapter.calls[0].payload
        self.assertIn("input", payload)
        self.assertEqual(payload["input"][0]["content"][0]["text"], "hi")

    def test_unknown_model_is_refused(self) -> None:
        status, payload, _ = self.harness.request(
            "POST",
            "/v1/chat/completions",
            body={"model": "nope", "messages": []},
        )
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["type"], "unknown_model")

    def test_malformed_json_is_refused(self) -> None:
        status, payload, _ = self.harness.request("POST", "/v1/chat/completions", body=None)
        self.assertIn(status, (400, 404))

    def test_streaming_completion_emits_sse(self) -> None:
        with self.harness.config.mutate() as config:
            config.aliases["coding"].enabled = True
        self.harness.adapter.stream_frames = [
            b'data: {"type":"response.output_text.delta","delta":"Hello"}\n\n',
            b'data: {"type":"response.completed","response":{"id":"resp_1","usage":{"input_tokens":1,"output_tokens":2}}}\n\n',
        ]
        status, payload, headers = self.harness.request(
            "POST",
            "/v1/chat/completions",
            body={"model": "coding", "messages": [{"role": "user", "content": "hi"}], "stream": True},
            raw=True,
        )
        self.assertEqual(status, 200)
        self.assertIn("text/event-stream", headers.get("Content-Type", ""))
        text = payload.decode("utf-8")
        # The upstream is Responses; the client asked for Chat Completions,
        # so the stream is converted into Chat chunks.
        self.assertIn("chat.completion.chunk", text)
        self.assertIn("Hello", text)
        self.assertIn("data: [DONE]", text)

    def test_responses_stream_passes_through_in_its_own_dialect(self) -> None:
        with self.harness.config.mutate() as config:
            config.aliases["coding"].enabled = True
        self.harness.adapter.stream_frames = [
            b'data: {"type":"response.output_text.delta","delta":"Hello"}\n\n',
            b'data: {"type":"response.completed","response":{"id":"resp_1","usage":{"input_tokens":1,"output_tokens":2}}}\n\n',
        ]
        status, payload, headers = self.harness.request(
            "POST",
            "/v1/responses",
            body={
                "model": "coding",
                "input": [{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}],
                "stream": True,
            },
            raw=True,
        )
        self.assertEqual(status, 200)
        text = payload.decode("utf-8")
        self.assertIn("response.output_text.delta", text)
        self.assertIn("response.completed", text)

    def test_ledger_records_the_serving_provider(self) -> None:
        with self.harness.config.mutate() as config:
            config.aliases["coding"].enabled = True
        self.harness.adapter.invoke = lambda request: ProviderResponse(
            status=200, body=b'{"usage":{"input_tokens":9,"output_tokens":4,"total_tokens":13}}'
        )
        self.harness.request(
            "POST",
            "/v1/chat/completions",
            body={"model": "coding", "messages": [{"role": "user", "content": "hi"}]},
        )
        events = self.harness.router._ledger.recent()
        self.assertTrue(events)
        self.assertEqual(events[0]["requested_alias"], "coding")
        self.assertEqual(events[0]["serving_provider"], FAKE_PROVIDER)
        self.assertEqual(events[0]["serving_model"], "gpt-6-luna")
        self.assertEqual(events[0]["token_counters"], {"prompt_tokens": 9, "completion_tokens": 4, "total_tokens": 13})

    def test_over_budget_refuses_before_provider_call_with_actionable_details(self) -> None:
        with self.harness.config.mutate() as config:
            config.aliases["coding"].enabled = True
            config.aliases["coding"].budget = 0.01
        from gateway.budget import month_window
        from gateway.observability import RequestEvent
        # Anchor the seed to the active UTC month so this budget test remains
        # meaningful as wall-clock time advances. An explicit reported zero
        # cached counter keeps spend known; without it the cost is Unknown.
        month_start, month_end = month_window()
        seeded_at = month_start.isoformat().replace("+00:00", "Z")
        expected_reset_at = month_end.strftime("%Y-%m-%dT%H:%M:%SZ")
        self.harness.router._ledger.record(RequestEvent(
            "req_prior", seeded_at, "coding", serving_provider="opencode_go",
            serving_model="gpt-6-luna", status=200,
            token_counters={"prompt_tokens": 1_000_000, "completion_tokens": 0, "cached_read_tokens": 0}))

        status, payload, _ = self.harness.request("POST", "/v1/chat/completions",
            body={"model": "coding", "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(status, 429)
        self.assertIn("$0.01", payload["error"]["message"])
        self.assertIn("$0.10", payload["error"]["message"])
        self.assertIn(expected_reset_at, payload["error"]["message"])
        self.assertEqual(self.harness.adapter.calls, [])

    def test_streamed_usage_is_amended_into_request_ledger(self) -> None:
        with self.harness.config.mutate() as config:
            config.aliases["coding"].enabled = True
        self.harness.adapter.stream_frames = [
            b'data: {"type":"response.completed","response":{"usage":{"input_tokens":7,"output_tokens":2}}}\n\n'
        ]
        self.harness.request(
            "POST", "/v1/responses",
            body={"model": "coding", "input": "hi", "stream": True}, raw=True,
        )
        event = self.harness.router._ledger.recent()[0]
        self.assertEqual(event["token_counters"], {"prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9})
        self.assertNotIn("content", event)


def _alias():
    from gateway.config import Alias

    return Alias(
        name="coding",
        enabled=True,
        primary=RouteRef(FAKE_PROVIDER, "gpt-6-luna"),
    )


class AliasGroupHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = GatewayHarness()
        self.addCleanup(self.harness.close)
        with self.harness.config.mutate() as config:
            config.aliases["coding"] = _alias()
            config.aliases["coding"].tag = "team"
            config.aliases["coding"].enabled = False
            config.aliases["coding"].fallbacks = [RouteRef("opencode_go", "private-model")]
            config.aliases["coding"].fallback_authorized = True
            config.aliases["writing"] = _alias()
            config.aliases["writing"].name = "writing"
            config.aliases["writing"].enabled = False
            config.aliases["writing"].tag = "team"
            config.aliases["other"] = _alias()
            config.aliases["other"].name = "other"
            config.aliases["other"].enabled = False
            config.aliases["other"].tag = "solo"

    def test_group_publish_and_disable_only_toggle_tag_members(self) -> None:
        before = self.harness.config.snapshot().aliases
        status, result, _ = self.harness.request("POST", "/owner/api/alias-group", {"action": "publish", "tag": "team"})
        self.assertEqual(status, 200)
        self.assertEqual(result["aliases"], ["coding", "writing"])
        published = self.harness.config.snapshot().aliases
        self.assertTrue(published["coding"].enabled)
        self.assertTrue(published["writing"].enabled)
        self.assertFalse(published["other"].enabled)
        self.assertEqual(published["coding"].fallbacks, before["coding"].fallbacks)
        self.assertEqual(published["coding"].fallback_authorized, before["coding"].fallback_authorized)
        self.assertEqual(published["coding"].tag, "team")
        status, result, _ = self.harness.request("POST", "/owner/api/alias-group", {"action": "disable", "tag": "team"})
        self.assertEqual(status, 200)
        self.assertFalse(self.harness.config.snapshot().aliases["coding"].enabled)
        self.assertFalse(self.harness.config.snapshot().aliases["writing"].enabled)
        self.assertFalse(self.harness.config.snapshot().aliases["other"].enabled)

    def test_non_object_group_payload_is_rejected_without_mutation(self) -> None:
        before = self.harness.config.snapshot().aliases
        status, _result, _ = self.harness.request("POST", "/owner/api/alias-group", ["publish", "team"])
        self.assertEqual(status, 400)
        after = self.harness.config.snapshot().aliases
        self.assertEqual(after, before)

    def test_group_refusal_does_not_partially_publish(self) -> None:
        with self.harness.config.mutate() as config:
            config.aliases["writing"].primary = None
        status, _result, _ = self.harness.request("POST", "/owner/api/alias-group", {"action": "publish", "tag": "team"})
        self.assertEqual(status, 400)
        self.assertFalse(self.harness.config.snapshot().aliases["coding"].enabled)
        self.assertFalse(self.harness.config.snapshot().aliases["writing"].enabled)
        status, _result, _ = self.harness.request("POST", "/owner/api/alias-group", {"action": "publish", "tag": "../team"})
        self.assertEqual(status, 400)


class FallbackTests(unittest.TestCase):
    """The fallback rules, which are the easiest thing to get wrong."""

    def _harness_with_fallback(self, adapter: FakeAdapter, **alias_kwargs) -> GatewayHarness:
        harness = GatewayHarness(adapter)
        # The fallback provider needs its own adapter, or the route would be
        # excluded before the fallback policy was ever consulted.
        harness.registry.register(FakeAdapter(provider_id="opencode_go", dialect="openai_chat"))
        with harness.config.mutate() as config:
            config.providers["opencode_go"] = ProviderState(
                provider_id="opencode_go",
                kind="opencode_go",
                state="connected",
                models={
                    "mimo-v2.6-flash": ModelEntry(
                        model_id="mimo-v2.6-flash",
                        enabled=True,
                        dialect="openai_chat",
                        source="discovered",
                    )
                },
            )
            alias = _alias()
            alias.fallbacks = [RouteRef("opencode_go", "mimo-v2.6-flash")]
            for key, value in alias_kwargs.items():
                setattr(alias, key, value)
            config.aliases["coding"] = alias
        return harness

    def test_fallback_is_refused_by_default(self) -> None:
        adapter = FakeAdapter(fail_with=AdapterError("exhausted", provider_id=FAKE_PROVIDER))
        harness = self._harness_with_fallback(adapter)
        self.addCleanup(harness.close)
        status, payload, _ = harness.request(
            "POST",
            "/v1/chat/completions",
            body={"model": "coding", "messages": [{"role": "user", "content": "hi"}]},
        )
        self.assertEqual(status, 502)
        self.assertIn("not authorised", payload["error"]["message"])

    def test_fallback_runs_when_authorised_and_confirmed(self) -> None:
        adapter = FakeAdapter(fail_with=AdapterError("exhausted", provider_id=FAKE_PROVIDER))
        harness = self._harness_with_fallback(
            adapter,
            fallback_authorized=True,
            fallback_confirmed_providers=["opencode_go"],
            fallback_triggers=["exhausted"],
        )
        self.addCleanup(harness.close)
        status, payload, _ = harness.request(
            "POST",
            "/v1/chat/completions",
            body={"model": "coding", "messages": [{"role": "user", "content": "hi"}]},
        )
        self.assertEqual(status, 200)

    def test_fallback_needs_the_provider_confirmed(self) -> None:
        adapter = FakeAdapter(fail_with=AdapterError("exhausted", provider_id=FAKE_PROVIDER))
        harness = self._harness_with_fallback(
            adapter, fallback_authorized=True, fallback_triggers=["exhausted"]
        )
        self.addCleanup(harness.close)
        status, payload, _ = harness.request(
            "POST",
            "/v1/chat/completions",
            body={"model": "coding", "messages": [{"role": "user", "content": "hi"}]},
        )
        self.assertEqual(status, 502)
        self.assertIn("has not been confirmed", payload["error"]["message"])

    def test_fallback_needs_the_trigger_enabled(self) -> None:
        adapter = FakeAdapter(fail_with=AdapterError("exhausted", provider_id=FAKE_PROVIDER))
        harness = self._harness_with_fallback(
            adapter,
            fallback_authorized=True,
            fallback_confirmed_providers=["opencode_go"],
        )
        self.addCleanup(harness.close)
        status, payload, _ = harness.request(
            "POST",
            "/v1/chat/completions",
            body={"model": "coding", "messages": [{"role": "user", "content": "hi"}]},
        )
        self.assertEqual(status, 502)
        self.assertIn("trigger is not enabled", payload["error"]["message"])

    def test_auth_failure_never_falls_back_by_default(self) -> None:
        adapter = FakeAdapter(fail_with=AdapterError("auth", provider_id=FAKE_PROVIDER))
        harness = self._harness_with_fallback(
            adapter,
            fallback_authorized=True,
            fallback_confirmed_providers=["opencode_go"],
            fallback_triggers=["exhausted", "rate_limited", "upstream_unavailable"],
        )
        self.addCleanup(harness.close)
        status, payload, _ = harness.request(
            "POST",
            "/v1/chat/completions",
            body={"model": "coding", "messages": [{"role": "user", "content": "hi"}]},
        )
        self.assertEqual(status, 502)
        self.assertIn("safe fallback policy", payload["error"]["message"])

    def test_tools_never_trigger_a_fallback(self) -> None:
        adapter = FakeAdapter(fail_with=AdapterError("exhausted", provider_id=FAKE_PROVIDER))
        harness = self._harness_with_fallback(
            adapter,
            fallback_authorized=True,
            fallback_confirmed_providers=["opencode_go"],
            fallback_triggers=["exhausted"],
        )
        self.addCleanup(harness.close)
        status, payload, _ = harness.request(
            "POST",
            "/v1/chat/completions",
            body={
                "model": "coding",
                "messages": [{"role": "user", "content": "hi"}],
                "tools": [{"type": "function", "function": {"name": "f", "parameters": {"type": "object"}}}],
            },
        )
        self.assertEqual(status, 502)
        self.assertIn("side effect", payload["error"]["message"])

    def test_plain_429_is_rate_limiting_not_exhaustion(self) -> None:
        from gateway.adapters.base import classify_status

        self.assertEqual(classify_status(429, {"error": {"code": "rate_limit_exceeded"}}), "rate_limited")
        self.assertEqual(classify_status(429, {"error": {"code": "insufficient_quota"}}), "exhausted")
        self.assertEqual(classify_status(429, {}), "rate_limited")


class SystemOneApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = GatewayHarness()
        self.addCleanup(self.harness.close)

    def test_systemone_uses_only_enabled_native_model_and_preserves_response(self) -> None:
        request = {"model": "jev-1.13", "state": "synthetic request content", "questions": {"phase": {"type": "choice", "criteria": {"plan": "planning"}}}}
        status, payload, _ = self.harness.request("POST", "/v1/systemone", body=request)
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["answers"]["phase"]["choice"], "plan")
        self.assertEqual(self.harness.jev.calls, [request])

    def test_systemone_refuses_images_and_never_calls_provider(self) -> None:
        status, payload, _ = self.harness.request("POST", "/v1/systemone", body={"model": "jev-1.13", "state": "synthetic", "assets": {"image": {}}, "questions": {"q": {"type": "noul"}}})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["type"], "malformed_request")
        self.assertEqual(self.harness.jev.calls, [])

    def test_systemone_refuses_disabled_or_unknown_model_without_provider_call(self) -> None:
        status, payload, _ = self.harness.request("POST", "/v1/systemone", body={"model": "not-enabled", "state": "synthetic", "questions": {"q": {"type": "noul"}}})
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["type"], "unknown_model")
        self.assertEqual(self.harness.jev.calls, [])

    def test_systemone_catalogue_is_saved_metadata_not_an_upstream_call(self) -> None:
        status, payload, _ = self.harness.request("GET", "/v1/systemone/models")
        self.assertEqual(status, 200)
        self.assertEqual(payload["models"][0]["name"], "jev-1.13")
        self.assertEqual(self.harness.jev.model_list_calls, 0)


class ConsoleApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = GatewayHarness()
        self.addCleanup(self.harness.close)

    def _login(self) -> dict[str, str]:
        """Compatibility helper: trusted-LAN console needs no login headers."""
        return {}

    def test_owner_console_mutates_same_origin_without_session_or_csrf(self) -> None:
        status, payload, _ = self.harness.request(
            "POST", "/owner/api/model",
            body={"action": "enable", "provider_id": "chatgpt_codex", "model_id": "gpt-6-luna"},
        )
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["ok"])

    def test_state_loads_published_prices_once_for_many_priced_ledger_rows(self) -> None:
        for index in range(40):
            self.harness.router._ledger.record(RequestEvent(
                request_id=f"price-cache-{index}", timestamp=utc_now_iso(),
                requested_alias="sample", serving_provider="opencode_go",
                serving_model="claude-haiku-5-5", token_counters={"prompt_tokens": 100, "completion_tokens": 10, "cached_read_tokens": 0},
            ))
        from gateway.costs import published_prices
        with patch("gateway.http.app.published_prices", wraps=published_prices) as load_prices:
            status, payload, _ = self.harness.request("GET", "/owner/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(load_prices.call_count, 1)
        matching = [row for row in payload["ledger"] if row["request_id"].startswith("price-cache-")]
        self.assertEqual(len(matching), 25)
        self.assertTrue(all(row["cost"]["amount_usd"] is not None for row in matching))
        self.assertIn("sample", payload["usage_costs"]["alias"])

    def test_state_reports_usage_costs_and_durability(self) -> None:
        status, payload, _ = self.harness.request("GET", "/owner/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(payload["usage_costs"]["window_hours"], 24)
        self.assertEqual(set(payload["usage_costs"]) - {"window_hours", "coverage"}, {"alias", "provider", "model"})
        self.assertIn("enabled", payload["ledger_durability"])
        self.assertFalse(payload["ledger_durability"]["billing_system"])
        self.assertEqual(set(payload["request_statistics"]), {"hour", "day", "month"})
        self.assertEqual(payload["request_statistics"]["day"]["window_hours"], 24)
        self.assertEqual(set(payload["request_statistics"]["day"]) - {"window", "window_hours"}, {"alias", "provider", "model"})
        health = payload["provider_health"]
        # One health row per configured provider, sorted by id — the exact
        # provider set is whatever this build registers (main has grown one).
        self.assertEqual(
            [row["provider_id"] for row in health],
            sorted(provider["provider_id"] for provider in payload["providers"]),
        )
        self.assertEqual(health[0]["connection_state"], "connected")
        self.assertEqual(health[0]["model_count"], 1)
        self.assertIsNone(health[0]["recent_success_rate"])
        self.assertEqual(health[0]["recent_requests"], 0)

    def test_state_reports_the_zero_state(self) -> None:
        status, payload, _ = self.harness.request("GET", "/owner/api/state")
        self.assertEqual(status, 200)
        self.assertFalse(payload["initialized"])
        self.assertEqual(len(payload["checklist"]), 7)
        self.assertEqual(payload["checklist"][0]["title"], "Connect a provider")

    def test_owner_can_mutate_in_lan_noauth_mode(self) -> None:
        status, payload, _ = self.harness.request(
            "POST",
            "/owner/api/model",
            body={"action": "enable", "provider_id": "chatgpt_codex", "model_id": "gpt-6-luna"},
        )
        self.assertEqual(status, 200, payload)

    def test_alias_state_round_trips_phase_routing_policy(self) -> None:
        policy = {
            "enabled": True,
            "routes": {
                "plan": {"provider": "chatgpt_codex", "model": "gpt-6-luna", "effort": "high"},
                "implement": {"provider": "chatgpt_codex", "model": "gpt-6-luna"},
                "validate": {"provider": "chatgpt_codex", "model": "gpt-6-luna", "effort": "medium"},
            },
            "default_route": {"provider": "chatgpt_codex", "model": "gpt-6-luna"},
            "inference": {
                "enabled": True, "provider": "opencode_console", "model": "jev-1.13",
                "minimum_confidence": 0.91, "on_uncertain": "default",
            },
        }
        status, payload, _ = self.harness.request("POST", "/owner/api/alias", body={
            "action": "save", "name": "phase-test",
            "primary": {"provider": "chatgpt_codex", "model": "gpt-6-luna"},
            "phase_routing": policy,
        })
        self.assertEqual(status, 200, payload)
        status, state, _ = self.harness.request("GET", "/owner/api/state")
        self.assertEqual(status, 200)
        alias = next(item for item in state["aliases"] if item["name"] == "phase-test")
        saved_policy = alias["phase_routing"]
        self.assertEqual(saved_policy["enabled"], policy["enabled"])
        self.assertEqual(set(saved_policy["routes"]), {"plan", "implement", "validate"})
        for phase, route in policy["routes"].items():
            self.assertEqual(saved_policy["routes"][phase], {
                "phase": phase,
                "route": {"provider": route["provider"], "model": route["model"]},
                **({"effort": route["effort"]} if "effort" in route else {}),
            })
        self.assertEqual(saved_policy["default_route"], policy["default_route"])
        self.assertEqual(saved_policy["inference"], policy["inference"])

    def test_phase_route_validation_refusal_does_not_change_alias(self) -> None:
        original = {
            "enabled": True,
            "routes": {"plan": {"provider": "chatgpt_codex", "model": "gpt-6-luna"}},
            "default_route": None,
            "inference": {"enabled": False, "provider": "opencode_console", "model": "", "minimum_confidence": 0.8, "on_uncertain": "default"},
        }
        save = {"action": "save", "name": "phase-test", "primary": {"provider": "chatgpt_codex", "model": "gpt-6-luna"}, "phase_routing": original}
        self.assertEqual(self.harness.request("POST", "/owner/api/alias", body=save)[0], 200)
        bad = dict(save)
        bad["phase_routing"] = {**original, "routes": {"invented": {"provider": "chatgpt_codex", "model": "gpt-6-luna"}}}
        status, payload, _ = self.harness.request("POST", "/owner/api/alias", body=bad)
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["field"], "phase_routing.routes")
        state = self.harness.request("GET", "/owner/api/state")[1]
        alias = next(item for item in state["aliases"] if item["name"] == "phase-test")
        self.assertEqual(alias["phase_routing"]["routes"]["plan"]["route"], {
            "provider": original["routes"]["plan"]["provider"],
            "model": original["routes"]["plan"]["model"],
        })
        self.assertEqual(alias["phase_routing"]["enabled"], original["enabled"])
        self.assertNotIn("request content", json.dumps(state).lower())

    def test_console_displays_phase_routing_fields_and_server_field_errors(self) -> None:
        status, body, _ = self.harness.request("GET", "/owner/app.js", raw=True)
        self.assertEqual(status, 200)
        source = body.decode()
        for text in ("Per-alias phase routing", "Enable phase routing", "Jev phase inference", "minimum_confidence", "phase_routing: phaseRouting", "serverError.field"):
            self.assertIn(text, source)
        self.assertIn('["plan", "implement", "validate"]', source)
        self.assertIn("m.enabled", source)

    def test_cross_origin_mutation_is_rejected(self) -> None:
        status, payload, _ = self.harness.request(
            "POST", "/owner/api/model", body={"action": "enable", "provider_id": "x", "model_id": "y"},
            headers={"Origin": "http://evil.example.com"},
        )
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"]["type"], "origin_mismatch")

    def test_missing_origin_on_browser_mutation_is_rejected(self) -> None:
        status, payload, _ = self.harness.request(
            "POST", "/owner/api/model", body={"action": "enable"}, headers={"Origin": "null"}
        )
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"]["type"], "origin_mismatch")

    def test_credential_enrolment_is_available_on_the_console(self) -> None:
        with self.harness.config.mutate() as config:
            config.access_mode = "authenticated"
        status, payload, _ = self.harness.request(
            "POST",
            "/owner/api/provider",
            body={"action": "enroll_key", "provider_id": "opencode_go", "api_key": "not-a-real-key"},
        )
        self.assertEqual(status, 200, payload)
        self.assertNotIn("not-a-real-key", json.dumps(payload))

    def test_authenticated_access_mode_cannot_be_enabled(self) -> None:
        status, payload, _ = self.harness.request(
            "POST", "/owner/api/settings",
            body={"action": "access_mode", "access_mode": "authenticated"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["type"], "unsupported_access_mode")
        self.assertEqual(self.harness.config.snapshot().access_mode, "lan_noauth")

    def test_refresh_publishes_limits_with_a_source_and_confidence(self) -> None:
        status, _, _ = self.harness.request(
            "POST", "/owner/api/provider",
            body={"action": "refresh", "provider_id": FAKE_PROVIDER},
        )
        self.assertEqual(status, 200)
        status, state, _ = self.harness.request("GET", "/owner/api/state")
        self.assertEqual(status, 200)
        limits = state["limits"]
        self.assertTrue(limits)
        for entry in limits:
            self.assertIn("source", entry)
            self.assertIn("observed_at", entry)
            self.assertIn("confidence", entry)
            self.assertNotEqual(entry["source"], "provider_reported")

    def test_state_never_contains_credential_material(self) -> None:
        _, payload, _ = self.harness.request("GET", "/owner/api/state")
        text = json.dumps(payload)
        self.assertNotIn("api_key", text.replace("api_key_", ""))
        self.assertNotIn("Bearer", text)


if __name__ == "__main__":
    unittest.main()
