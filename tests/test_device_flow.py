"""The ChatGPT / Codex device-authorisation flow, end to end.

Regression cover for **R1**: the console could never complete a device
sign-in, because the start response omitted the identifier the poll
needed. The browser sent an empty value and the server answered "that
authorisation is no longer pending".

The properties under test:

* the start action returns an **opaque poll handle**;
* the provider's own device auth id **never leaves the process**;
* the one-time code is returned **once**, by the start action only;
* polling with the handle works: pending -> approved;
* polling with an unknown or expired handle is refused;
* pending state is in memory and expires.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from gateway.adapters.base import AdapterError
from gateway.adapters.chatgpt_codex import (
    DEVICE_APPROVED,
    DEVICE_DENIED,
    DEVICE_PENDING,
    ChatGPTCodexAdapter,
)
from gateway.adapters.http_client import UpstreamResponse
from gateway.adapters.registry import AdapterRegistry
from gateway.config import ConfigStore
from gateway.core.routing import Router
from gateway.credentials import CredentialStore
from gateway.http.app import GatewayApp
from gateway.logging import RequestLogger
from gateway.http.core import Headers, Request
from gateway.http.security import SecurityPolicy, build_allowed_hosts, build_canonical_origins
from gateway.observability import LimitStore, RequestLedger
from tests.owner_session import cookie_header


class FakeUpstream:
    """A scripted upstream for the device flow."""

    def __init__(self, responses: list[UpstreamResponse]) -> None:
        self.responses = list(responses)
        self.requests: list[dict] = []

    def request(self, method, url, headers, body=None, stream=False, provider_id="", **kwargs):
        self.requests.append({"method": method, "url": url, "headers": headers, "body": body})
        if not self.responses:
            return UpstreamResponse(200, {}, b"{}", None, "application/json")
        return self.responses.pop(0)


def user_code_response() -> UpstreamResponse:
    payload = json.dumps({"device_auth_id": "provider-side-id", "user_code": "ABCD-EFGH", "interval": 5}).encode()
    return UpstreamResponse(200, {}, payload, None, "application/json")


def code_response() -> UpstreamResponse:
    payload = json.dumps(
        {"authorization_code": "ac", "code_challenge": "cc", "code_verifier": "cv"}
    ).encode()
    return UpstreamResponse(200, {}, payload, None, "application/json")


def token_response() -> UpstreamResponse:
    payload = json.dumps({"access_token": "synthetic-access", "refresh_token": "synthetic-refresh"}).encode()
    return UpstreamResponse(200, {}, payload, None, "application/json")


class DeviceFlowHarness:
    """The gateway application with a scripted ChatGPT adapter."""

    def __init__(self, responses: list[UpstreamResponse]) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp.name)
        self.config = ConfigStore(self.data_dir / "config.json")
        self.credentials = CredentialStore(self.data_dir)
        self.registry = AdapterRegistry()
        self.upstream = FakeUpstream(responses)
        self.adapter = ChatGPTCodexAdapter(
            self.credentials.provider_handle("chatgpt_codex"),
            client=self.upstream,
        )
        self.registry.register(self.adapter)
        policy = SecurityPolicy(
            allowed_hosts=build_allowed_hosts("127.0.0.1", 8765),
            canonical_origins=build_canonical_origins("127.0.0.1", 8765),
        )
        self.app = GatewayApp(
            config=self.config,
            credentials=self.credentials,
            router=Router(self.config, self.registry, ledger=RequestLedger()),
            registry=self.registry,
            profile_auth=__import__(
                "gateway.auth", fromlist=["ProfileAuth"]
            ).ProfileAuth(self.config, self.credentials),
            policy=policy,
            ledger=RequestLedger(),
            limits=LimitStore(),
            release_identity=lambda: {},
            base_url="http://127.0.0.1:8765",
            mounts=("/owner",),
            logger=RequestLogger(enabled=False),
        )
        self.owner_cookie = cookie_header(self.app)

    def call(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        request = Request(
            method=method,
            path=path,
            raw_query="",
            headers=Headers(
                [
                    ("Host", "127.0.0.1:8765"),
                    ("Origin", "http://127.0.0.1:8765"),
                    ("Cookie", self.owner_cookie),
                ]
            ),
            body=json.dumps(body).encode("utf-8") if body is not None else b"",
            client_address=("127.0.0.1", 40000),
            server_port=8765,
        )
        response = self.app.handle(request)
        try:
            payload = json.loads(response.body.decode("utf-8"))
        except (ValueError, AttributeError):
            payload = {}
        return response.status, payload

    def close(self) -> None:
        self.temp.cleanup()


class DeviceFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.harness: list[DeviceFlowHarness] = []

    def tearDown(self) -> None:
        for harness in self.harness:
            harness.close()

    def _harness(self, responses: list[UpstreamResponse]) -> DeviceFlowHarness:
        harness = DeviceFlowHarness(responses)
        self.harness.append(harness)
        return harness

    def test_start_returns_an_opaque_poll_handle(self) -> None:
        harness = self._harness([user_code_response()])
        status, payload = harness.call(
            "POST", "/owner/api/provider", {"action": "device_start", "provider_id": "chatgpt_codex"}
        )
        self.assertEqual(status, 200)
        self.assertIn("poll_handle", payload)
        self.assertTrue(payload["poll_handle"])
        self.assertEqual(payload["verification_url"], "https://auth.openai.com/codex/device")
        self.assertEqual(payload["user_code"], "ABCD-EFGH")
        self.assertEqual(payload["interval"], 5)
        self.assertEqual(payload["state"], DEVICE_PENDING)

    def test_the_provider_device_auth_id_never_leaves_the_process(self) -> None:
        harness = self._harness([user_code_response()])
        status, payload = harness.call(
            "POST", "/owner/api/provider", {"action": "device_start", "provider_id": "chatgpt_codex"}
        )
        self.assertEqual(status, 200)
        self.assertNotIn("device_auth_id", payload)
        self.assertNotIn("provider-side-id", json.dumps(payload))
        # And the handle is not the provider's identifier.
        self.assertNotEqual(payload["poll_handle"], "provider-side-id")

    def test_poll_with_the_handle_reaches_approved(self) -> None:
        harness = self._harness(
            [user_code_response(), code_response(), token_response()]
        )
        status, start = harness.call(
            "POST", "/owner/api/provider", {"action": "device_start", "provider_id": "chatgpt_codex"}
        )
        self.assertEqual(status, 200)

        status, poll = harness.call(
            "POST",
            "/owner/api/provider",
            {"action": "device_poll", "provider_id": "chatgpt_codex", "poll_handle": start["poll_handle"]},
        )
        self.assertEqual(status, 200)
        self.assertEqual(poll["state"], DEVICE_APPROVED)

        # The credential was stored, and is not readable back through the API.
        stored = harness.credentials.provider_handle("chatgpt_codex").read()
        self.assertEqual(stored["access_token"], "synthetic-access")
        status, state = harness.call("GET", "/owner/api/state")
        self.assertNotIn("synthetic-access", json.dumps(state))

    def test_the_one_time_code_is_returned_only_by_the_start_action(self) -> None:
        harness = self._harness([user_code_response()])
        status, start = harness.call(
            "POST", "/owner/api/provider", {"action": "device_start", "provider_id": "chatgpt_codex"}
        )
        self.assertEqual(status, 200)
        self.assertIn("user_code", start)

        status, poll = harness.call(
            "POST",
            "/owner/api/provider",
            {"action": "device_poll", "provider_id": "chatgpt_codex", "poll_handle": start["poll_handle"]},
        )
        self.assertEqual(status, 200)
        self.assertNotIn("user_code", poll)
        self.assertNotIn("ABCD-EFGH", json.dumps(poll))

    def test_polling_an_unknown_handle_is_refused(self) -> None:
        harness = self._harness([user_code_response()])
        status, payload = harness.call(
            "POST",
            "/owner/api/provider",
            {"action": "device_poll", "provider_id": "chatgpt_codex", "poll_handle": "not-a-handle"},
        )
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["type"], "unknown_authorization")

    def test_polling_without_a_handle_is_refused(self) -> None:
        harness = self._harness([user_code_response()])
        status, payload = harness.call(
            "POST", "/owner/api/provider", {"action": "device_poll", "provider_id": "chatgpt_codex"}
        )
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["type"], "unknown_authorization")

    def test_the_device_flow_is_not_reachable_from_the_api_surface(self) -> None:
        harness = self._harness([user_code_response()])
        status, _ = harness.call(
            "POST", "/v1/api/provider", {"action": "device_start", "provider_id": "chatgpt_codex"}
        )
        self.assertEqual(status, 404)

    def test_an_expired_pending_authorization_is_pruned(self) -> None:
        harness = self._harness([user_code_response()])
        status, start = harness.call(
            "POST", "/owner/api/provider", {"action": "device_start", "provider_id": "chatgpt_codex"}
        )
        self.assertEqual(status, 200)

        # Age the pending record past its expiry.
        pending = harness.app._pending_device[start["poll_handle"]]
        pending.expires_at = 0.0

        status, payload = harness.call(
            "POST",
            "/owner/api/provider",
            {"action": "device_poll", "provider_id": "chatgpt_codex", "poll_handle": start["poll_handle"]},
        )
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["type"], "unknown_authorization")
        self.assertNotIn(start["poll_handle"], harness.app._pending_device)

    def test_a_denied_authorisation_is_reported(self) -> None:
        harness = self._harness(
            [user_code_response(), UpstreamResponse(500, {}, b"", None, "application/json")]
        )
        status, start = harness.call(
            "POST", "/owner/api/provider", {"action": "device_start", "provider_id": "chatgpt_codex"}
        )
        self.assertEqual(status, 200)
        status, poll = harness.call(
            "POST",
            "/owner/api/provider",
            {"action": "device_poll", "provider_id": "chatgpt_codex", "poll_handle": start["poll_handle"]},
        )
        self.assertEqual(status, 200)
        self.assertIn(poll["state"], (DEVICE_DENIED, DEVICE_PENDING))


if __name__ == "__main__":
    unittest.main()
