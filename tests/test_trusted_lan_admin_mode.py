"""Independent trusted-LAN administration regressions over synthetic HTTP."""
from __future__ import annotations

import json
import socket
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from gateway.adapters.base import ProviderRequest, ProviderResponse, TestResult
from gateway.adapters.registry import AdapterRegistry
from gateway.auth import ProfileAuth
from gateway.config import ConfigStore
from gateway.core.routing import Router
from gateway.credentials import CredentialStore
from gateway.http.app import GatewayApp
from gateway.logging import RequestLogger
from gateway.http.core import Server
from gateway.http.security import (
    FORWARDED_HEADER_NAMES,
    SecurityPolicy,
    build_allowed_hosts,
    build_canonical_origins,
)
from gateway.observability import LimitStore, RequestEvent, RequestLedger
from tests.owner_session import cookie_header


class MockAdapter:
    """Transport-free provider double; invoking it is observable in the test."""

    provider_id = "synthetic"
    kind = "synthetic"

    def __init__(self) -> None:
        self.calls: list[ProviderRequest] = []

    def list_models(self) -> list:
        return []

    def test_connection(self) -> TestResult:
        return TestResult(ok=True, state="connected", message="synthetic")

    def invoke(self, request: ProviderRequest) -> ProviderResponse:
        self.calls.append(request)
        return ProviderResponse(status=200, headers={}, body=b"{}", content_type="application/json")


class TrustedLanHarness:
    """Fresh single-listener app served on an explicit loopback address."""

    def __init__(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp.name)
        self.config = ConfigStore(self.data_dir / "config.json")
        self.credentials = CredentialStore(self.data_dir)
        self.registry = AdapterRegistry()
        self.adapter = MockAdapter()
        self.registry.register(self.adapter)
        self.ledger = RequestLedger()
        self.router = Router(self.config, self.registry, ledger=self.ledger)
        self.port = self._free_port()
        host = "127.0.0.1"
        policy = SecurityPolicy(
            allowed_hosts=build_allowed_hosts(host, self.port),
            canonical_origins=build_canonical_origins(host, self.port),
        )
        self.app = GatewayApp(
            config=self.config,
            credentials=self.credentials,
            router=self.router,
            registry=self.registry,
            profile_auth=ProfileAuth(self.config, self.credentials),
            policy=policy,
            ledger=self.ledger,
            limits=LimitStore(),
            release_identity=lambda: {
                "service": "personal-llm-gateway",
                "source_revision": "synthetic",
                "source_tree": "synthetic",
                "image_revision": "unknown",
            },
            base_url=f"http://{host}:{self.port}",
            mounts=("/owner",),
            logger=RequestLogger(enabled=False),
        )
        # Every owner endpoint sits behind a session cookie now, so the
        # fixture signs in the same way a console browser would.
        self.owner_cookie = cookie_header(self.app)
        self.server = Server(host, self.port, self.app.handle)
        self.server.start_background()

    @staticmethod
    def _free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    def close(self) -> None:
        self.server.stop()
        self.temp.cleanup()

    def request(
        self,
        method: str,
        path: str,
        body: Any = None,
        headers: dict[str, str] | None = None,
        *,
        add_origin: bool = True,
        auth: bool = True,
    ) -> tuple[int, bytes, dict[str, str]]:
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Host", f"127.0.0.1:{self.port}")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        if auth:
            request.add_header("Cookie", self.owner_cookie)
        if add_origin and path.startswith("/owner/") and method in {"POST", "PUT", "PATCH", "DELETE"}:
            request.add_header("Origin", f"http://127.0.0.1:{self.port}")
        for name, value in (headers or {}).items():
            request.add_header(name, value)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, response.read(), dict(response.headers.items())
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, exc.read(), dict(exc.headers.items())


class TrustedLanAdminModeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = TrustedLanHarness()
        self.addCleanup(self.harness.close)

    def test_fresh_owner_console_is_direct_and_has_no_setup_surface(self) -> None:
        status, body, headers = self.harness.request("GET", "/owner/")
        self.assertEqual(status, 200)
        self.assertIn(b"Personal LLM Gateway", body)
        self.assertNotIn("set-cookie", {key.lower() for key in headers})
        self.assertNotIn(b"/api/session", body)
        self.assertEqual(self.harness.app.mounts, ("/owner",))
        self.assertEqual(self.harness.app._routes.match("GET", "/setup/")[0], None)
        self.assertEqual(self.harness.app._routes.match("GET", "/owner/api/session")[0], None)

    def test_canonical_same_origin_configuration_mutation_needs_an_owner_session(self) -> None:
        # Same-origin checks are not the only boundary any more: a mutation
        # without a session cookie never reaches its handler.
        status, body, _ = self.harness.request(
            "POST",
            "/owner/api/profile",
            {"action": "save", "profile_id": "unauthenticated", "name": "Blocked"},
            auth=False,
        )
        self.assertEqual(status, 401, body)
        self.assertIn(b"authentication_required", body)
        self.assertNotIn("unauthenticated", self.harness.config.snapshot().profiles)

        status, body, headers = self.harness.request(
            "POST",
            "/owner/api/profile",
            {"action": "save", "profile_id": "synthetic-profile", "name": "Synthetic"},
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body), {"ok": True, "profile_id": "synthetic-profile"})
        # A signed-in mutation does not mint or refresh a session cookie.
        self.assertNotIn("set-cookie", {key.lower() for key in headers})
        self.assertIn("synthetic-profile", self.harness.config.snapshot().profiles)

    def test_cross_site_mutation_and_hostile_origin_are_rejected(self) -> None:
        status, body, _ = self.harness.request(
            "POST",
            "/owner/api/profile",
            {"action": "save", "profile_id": "blocked", "name": "Blocked"},
            {"Origin": "https://attacker.invalid"},
        )
        self.assertEqual(status, 403)
        self.assertIn(b"origin_mismatch", body)
        self.assertNotIn("blocked", self.harness.config.snapshot().profiles)

        status, body, _ = self.harness.request(
            "GET", "/owner/", {"ignored": True}, {"Host": "attacker.invalid"}
        )
        self.assertEqual(status, 403)
        self.assertIn(b"host_mismatch", body)
        status, body, _ = self.harness.request(
            "POST",
            "/owner/api/profile",
            {"action": "save", "profile_id": "host-blocked", "name": "Blocked"},
            {"Host": "attacker.invalid"},
        )
        self.assertEqual(status, 403)
        self.assertIn(b"host_mismatch", body)
        self.assertNotIn("host-blocked", self.harness.config.snapshot().profiles)

    def test_forwarded_headers_are_rejected_on_read_and_mutation(self) -> None:
        for name in FORWARDED_HEADER_NAMES:
            with self.subTest(header=name, path="read"):
                status, body, _ = self.harness.request("GET", "/v1/models", headers={name: "attacker.invalid"})
                self.assertEqual(status, 400)
                self.assertIn(b"forwarded_header_rejected", body)
            with self.subTest(header=name, path="mutation"):
                status, body, _ = self.harness.request(
                    "POST",
                    "/owner/api/settings",
                    {"action": "initialize"},
                    {name: "attacker.invalid"},
                )
                self.assertEqual(status, 400)
                self.assertIn(b"forwarded_header_rejected", body)
        self.assertFalse(self.harness.config.snapshot().initialized)

    def test_fresh_anonymous_models_and_no_route_chat_are_openai_json_without_transport(self) -> None:
        status, body, headers = self.harness.request("GET", "/v1/models")
        self.assertEqual(status, 200)
        self.assertIn("application/json", headers.get("Content-Type", ""))
        self.assertEqual(json.loads(body)["object"], "list")
        self.assertEqual(json.loads(body)["data"], [])

        status, body, headers = self.harness.request(
            "POST",
            "/v1/chat/completions",
            {"model": "unpublished", "messages": [{"role": "user", "content": "synthetic"}]},
        )
        self.assertGreaterEqual(status, 400)
        self.assertLess(status, 500)
        self.assertIn("application/json", headers.get("Content-Type", ""))
        self.assertIn("error", json.loads(body))
        self.assertEqual(self.harness.adapter.calls, [])

    def test_warning_and_synthetic_secret_redaction_across_reads_errors_version_and_ledger(self) -> None:
        secret = "SYNTHETIC-ONLY-SECRET-7f13"
        self.harness.credentials.provider_handle("opencode_go").write({"api_key": secret})
        status, console, _ = self.harness.request("GET", "/owner/app.js")
        self.assertEqual(status, 200)
        self.assertIn(b"Owner sign-in protects this console", console)
        self.assertIn(b"keyless by default", console)

        status, state, _ = self.harness.request("GET", "/owner/api/state")
        self.assertEqual(status, 200)
        self.assertNotIn(secret.encode(), state)
        with self.harness.config.mutate():
            pass
        self.assertNotIn(secret.encode(), (self.harness.data_dir / "config.json").read_bytes())

        status, error, _ = self.harness.request("POST", "/owner/api/profile", {"action": "unknown-synthetic-action"})
        self.assertGreaterEqual(status, 400)
        self.assertNotIn(secret.encode(), error)
        status, version, _ = self.harness.request("GET", "/version")
        self.assertEqual(status, 200)
        self.assertNotIn(secret.encode(), version)

        self.harness.request(
            "POST",
            "/v1/chat/completions",
            {"model": "missing", "messages": [{"role": "user", "content": "synthetic"}]},
        )
        self.harness.ledger.record(
            RequestEvent(request_id="req_synthetic", timestamp="synthetic", requested_alias="missing")
        )
        status, state, _ = self.harness.request("GET", "/owner/api/state")
        self.assertEqual(status, 200)
        self.assertNotIn(secret.encode(), state)
        events = self.harness.ledger.recent()
        self.assertTrue(events)
        self.assertNotIn(secret, json.dumps(events))
        self.assertNotIn(secret, json.dumps(self.harness.ledger.totals()))
        self.assertEqual(self.harness.adapter.calls, [])


if __name__ == "__main__":
    unittest.main()
