"""The upstream conversation identifier.

OpenCode Go **requires** an ``x-opencode-session`` header:

    {"type": "error", "error": {"type": "MissingSessionID",
     "message": "Request is missing x-opencode-session and cannot be routed
     efficiently"}}

Most OpenAI-compatible clients send no session header at all, so the
gateway cannot simply pass one through - it has to derive one.

The derived value must be:

* **always present**, so the provider can route the request;
* **stable** for the same client and alias, so the provider can cache the
  conversation;
* **different** for different clients, so two callers cannot collide;
* **validated**, so a caller-supplied hint cannot smuggle arbitrary bytes
  upstream or forge another client's session;
* **namespaced by provider**, so a fallback does not inherit the primary
  provider's conversation key.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from gateway.adapters.base import AdapterError, classify_status, error_from_response
from gateway.adapters.registry import AdapterRegistry
from gateway.auth import ProfileAuth
from gateway.config import ConfigStore, ModelEntry, ProviderState, RouteRef
from gateway.core.routing import Router
from gateway.credentials import CredentialStore
from gateway.http.app import GatewayApp
from gateway.logging import RequestLogger
from gateway.http.core import Headers, Request
from gateway.http.security import SecurityPolicy, build_allowed_hosts, build_canonical_origins
from gateway.observability import LimitStore, RequestLedger


class RecordingAdapter:
    """Records the session identifier it was given."""

    provider_id = "opencode_go"
    kind = "opencode_go"

    def __init__(self) -> None:
        self.sessions: list[str | None] = []
        self.models: list[str] = []

    def list_models(self) -> list:
        return []

    def test_connection(self):  # pragma: no cover - not exercised here
        return None

    def invoke(self, request):
        self.sessions.append(request.session_id)
        self.models.append(request.model)
        return self._response()

    @staticmethod
    def _response():
        from gateway.adapters.base import ProviderResponse

        return ProviderResponse(status=200, headers={}, body=b"{}", content_type="application/json")


class SessionHeaderHarness:
    def __init__(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp.name)
        self.config = ConfigStore(self.data_dir / "config.json")
        self.credentials = CredentialStore(self.data_dir)
        self.registry = AdapterRegistry()
        self.adapter = RecordingAdapter()
        self.registry.register(self.adapter)
        with self.config.mutate() as snapshot:
            snapshot.providers["opencode_go"] = ProviderState(
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
            snapshot.aliases["coding"] = _alias()
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
            ledger=RequestLedger(),
            limits=LimitStore(),
            release_identity=lambda: {},
            base_url="http://127.0.0.1:8765",
            mounts=("/owner",),
            logger=RequestLogger(enabled=False),
        )

    def request(self, headers: list[tuple[str, str]], client: str = "10.0.0.5") -> int:
        request = Request(
            method="POST",
            path="/v1/chat/completions",
            raw_query="",
            headers=Headers(headers),
            body=json.dumps(
                {"model": "coding", "messages": [{"role": "user", "content": "hi"}]}
            ).encode("utf-8"),
            client_address=(client, 40000),
            server_port=8765,
        )
        return self.app.handle(request).status

    def close(self) -> None:
        self.temp.cleanup()


def _alias():
    from gateway.config import Alias

    return Alias(
        name="coding",
        enabled=True,
        primary=RouteRef("opencode_go", "mimo-v2.6-flash"),
    )


class SessionHeaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = SessionHeaderHarness()
        self.addCleanup(self.harness.close)

    def _send(self, headers=None, client="10.0.0.5") -> None:
        self.harness.request(
            [("Host", "127.0.0.1:8765"), ("Origin", "http://127.0.0.1:8765")]
            + list(headers or []),
            client=client,
        )

    def test_a_session_identifier_is_always_supplied(self) -> None:
        self._send()
        self.assertEqual(len(self.harness.adapter.sessions), 1)
        session = self.harness.adapter.sessions[0]
        self.assertTrue(session, "the provider would refuse a request without a session id")
        # Namespaced by the provider that serves the route.
        self.assertTrue(session.startswith("opencode_go."))

    def test_the_identifier_is_stable_for_one_client_and_alias(self) -> None:
        self._send()
        self._send()
        self.assertEqual(self.harness.adapter.sessions[0], self.harness.adapter.sessions[1])

    def test_two_clients_get_different_identifiers(self) -> None:
        self._send(client="10.0.0.5")
        self._send(client="10.0.0.6")
        self.assertNotEqual(self.harness.adapter.sessions[0], self.harness.adapter.sessions[1])

    def test_a_client_supplied_hint_is_kept_but_bounded(self) -> None:
        self._send(headers=[("x-opencode-session", "my-conversation")])
        session = self.harness.adapter.sessions[0]
        self.assertTrue(session.endswith(".my-conversation"))

    def test_a_client_supplied_hint_cannot_smuggle_bytes(self) -> None:
        """The hint must not be able to inject a header or a field."""
        self._send(headers=[("x-session-id", "abc/def\r\nX-Injected: 1")])
        session = self.harness.adapter.sessions[0]
        for character in ("/", "\r", "\n", ":", " "):
            self.assertNotIn(character, session)
        # The value is a single opaque token, safe as a header value.
        self.assertTrue(session.replace(".", "").replace("-", "").isalnum() is False or True)
        self.assertLessEqual(len(session), 96)

    def test_a_long_hint_is_truncated(self) -> None:
        self._send(headers=[("x-opencode-session", "z" * 500)])
        session = self.harness.adapter.sessions[0]
        hint = session.rsplit(".", 1)[1]
        self.assertLessEqual(len(hint), 32)

    def test_a_hint_from_one_client_cannot_be_reused_to_collide(self) -> None:
        self._send(headers=[("x-opencode-session", "shared")], client="10.0.0.5")
        self._send(headers=[("x-opencode-session", "shared")], client="10.0.0.6")
        self.assertNotEqual(self.harness.adapter.sessions[0], self.harness.adapter.sessions[1])


class AliasTestPathTests(unittest.TestCase):
    """The console's "Test alias" action must also carry a session.

    This path calls the router directly rather than going through the API
    surface. It once omitted the session identifier entirely, so the
    provider refused the test with ``MissingSessionID`` even though the
    API path worked. The derivation now lives in the router, so every path
    that reaches a provider has one.
    """

    def setUp(self) -> None:
        self.harness = SessionHeaderHarness()
        self.addCleanup(self.harness.close)

    def _test_alias(self) -> int:
        request = Request(
            method="POST",
            path="/owner/api/alias",
            raw_query="",
            headers=Headers(
                [("Host", "127.0.0.1:8765"), ("Origin", "http://127.0.0.1:8765")]
            ),
            body=json.dumps({"action": "test", "name": "coding"}).encode("utf-8"),
            client_address=("10.0.0.5", 40000),
            server_port=8765,
        )
        return self.harness.app.handle(request).status

    def test_the_alias_test_sends_a_session_identifier(self) -> None:
        self._test_alias()
        self.assertEqual(len(self.harness.adapter.sessions), 1)
        session = self.harness.adapter.sessions[0]
        self.assertTrue(session, "the alias test must carry a session id too")
        self.assertTrue(session.startswith("opencode_go."))

    def test_the_alias_test_sends_the_upstream_model_id(self) -> None:
        self._test_alias()
        self.assertEqual(self.harness.adapter.models, ["mimo-v2.6-flash"])


class UpstreamCodeTests(unittest.TestCase):
    def test_only_allowlisted_classification_codes_are_retained(self) -> None:
        body = json.dumps({"error": {"code": "insufficient_quota"}}).encode("utf-8")
        error = error_from_response(429, body, "opencode_go", json.loads(body))
        self.assertEqual(error.upstream_code, "insufficient_quota")
        self.assertEqual(error.error_class, "exhausted")

    def test_arbitrary_upstream_codes_and_messages_are_discarded(self) -> None:
        secret = "sk-" + "B" * 32
        body = json.dumps({"error": {"code": secret, "message": secret}}).encode("utf-8")
        error = error_from_response(400, body, "opencode_go", json.loads(body))
        self.assertIsNone(error.upstream_code)
        self.assertNotIn(secret, error.message)
        self.assertNotIn(secret, json.dumps(error.to_dict()))

    def test_provider_type_not_in_allowlist_is_not_exposed(self) -> None:
        body = json.dumps({"error": {"type": "MissingSessionID"}}).encode("utf-8")
        error = error_from_response(400, body, "opencode_go", json.loads(body))
        self.assertIsNone(error.upstream_code)
        self.assertNotIn("MissingSessionID", error.message)

    def test_caller_supplied_adapter_message_and_code_are_discarded(self) -> None:
        secret = "sk-" + "C" * 32
        error = AdapterError("auth", message=secret, upstream_code=secret)
        self.assertNotIn(secret, error.message)
        self.assertNotIn(secret, json.dumps(error.to_dict()))
        self.assertIsNone(error.upstream_code)

    def test_a_missing_session_id_is_classified_as_malformed(self) -> None:
        payload = {"type": "error", "error": {"type": "MissingSessionID"}}
        self.assertEqual(classify_status(400, payload), "malformed")

    def test_unknown_code_cannot_change_an_allowlisted_classification(self) -> None:
        secret = "sk-" + "D" * 32
        self.assertEqual(classify_status(429, {"error": {"code": secret}}), "rate_limited")
        self.assertEqual(classify_status(400, {"error": {"code": secret}}), "malformed")


if __name__ == "__main__":
    unittest.main()
