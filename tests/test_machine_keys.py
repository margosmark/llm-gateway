"""Console-generated machine keys for MCP and other automation.

Three credentials, asserted apart on purpose:

* the **owner session cookie** (human console sign-in),
* the optional **model client key** (the ``/v1`` surface, keyless by
  default),
* the **machine key** (this file): console-generated, shown exactly once,
  hash-only on disk, scoped ``read`` or ``write``, revocable at once.

Everything here runs against an in-process harness: no server, no
provider call, no production data directory, no lifecycle command. The
cases map to the acceptance matrix in ``gateway-mcp-spec.md``
(t_2fce4e14): A2, A3, A6, A7, A7a, A7b, A7c, A21 and A23.
"""
from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path

from gateway.adapters.registry import AdapterRegistry
from gateway.auth import ProfileAuth
from gateway.config import ConfigStore
from gateway.core.routing import Router
from gateway.credentials import CredentialStore
from gateway.http.app import (
    MACHINE_KEY_READ_PATHS,
    MACHINE_KEY_WRITE_PATHS,
    GatewayApp,
)
from gateway.http.core import Headers, Request
from gateway.http.security import (
    SecurityPolicy,
    bearer_authorization,
    build_allowed_hosts,
    build_canonical_origins,
)
from gateway.logging import RequestLogger
from gateway.observability import LimitStore, RequestLedger
from gateway.owner_auth import (
    COOKIE_NAME,
    MACHINE_KEY_FILE_NAME,
    MACHINE_KEY_PEPPER_FILE_NAME,
    MACHINE_KEY_PREFIX,
    MACHINE_KEY_SCOPES,
    MachineKeyStore,
    OwnerAuthStore,
)
from gateway.stores import read_json

HOST = "127.0.0.1"
PORT = 8765
OWNER_PASSWORD = "fixture-owner-passphrase"
READ_KEY_NAME = "laptop-mcp"
WRITE_KEY_NAME = "ci-mcp"


def header(headers, name: str) -> str | None:
    for key, value in headers:
        if key.lower() == name.lower():
            return value
    return None


def cookie_token(headers) -> str | None:
    raw = header(headers, "set-cookie")
    if raw is None or not raw.startswith(f"{COOKIE_NAME}="):
        return None
    return raw.partition("=")[2].split(";", 1)[0]


class MachineKeyHarness:
    """An in-process gateway whose requests are built by hand.

    Unlike the console fixtures, requests here carry no ``Origin`` unless
    a test asks for one: that is the machine-client shape under test.
    """

    def __init__(self, *, log_stream: io.StringIO | None = None) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp.name)
        self.now = 1_800_000_000.0
        self.config = ConfigStore(self.data_dir / "config.json")
        self.credentials = CredentialStore(self.data_dir)
        self.registry = AdapterRegistry()
        policy = SecurityPolicy(
            allowed_hosts=build_allowed_hosts(HOST, PORT),
            canonical_origins=build_canonical_origins(HOST, PORT),
        )
        self.owner_auth = OwnerAuthStore(self.data_dir, clock=lambda: self.now)
        self.machine_keys = MachineKeyStore(self.data_dir, clock=lambda: self.now)
        self.log_stream = log_stream if log_stream is not None else io.StringIO()
        self.app = GatewayApp(
            config=self.config,
            credentials=self.credentials,
            router=Router(self.config, self.registry, ledger=RequestLedger()),
            registry=self.registry,
            profile_auth=ProfileAuth(self.config, self.credentials),
            policy=policy,
            ledger=RequestLedger(),
            limits=LimitStore(),
            release_identity=dict,
            base_url=f"http://{HOST}:{PORT}",
            mounts=("/owner",),
            logger=RequestLogger(stream=self.log_stream),
            owner_auth=self.owner_auth,
            machine_keys=self.machine_keys,
            cookie_secure=False,
        )
        self.setup_code: str | None = self.app.setup_code_created
        self.session: str | None = None

    def close(self) -> None:
        self.temp.cleanup()

    # --- requests --------------------------------------------------------

    def send(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        *,
        authorization: str | None = None,
        cookie: str | None = None,
        origin: str | None = None,
        extra_headers: list[tuple[str, str]] | None = None,
        client: tuple[str, int] = (HOST, 40000),
    ):
        items: list[tuple[str, str]] = []
        extras = list(extra_headers or [])
        # A test that overrides Host sends it itself; otherwise the
        # canonical authority is the first (and only) Host header.
        if not any(name.lower() == "host" for name, _ in extras):
            items.append(("Host", f"{HOST}:{PORT}"))
        if origin is not None:
            items.append(("Origin", origin))
        if authorization is not None:
            items.append(("Authorization", authorization))
        if cookie is not None:
            items.append(("Cookie", f"{COOKIE_NAME}={cookie}"))
        items.extend(extras)
        request = Request(
            method=method,
            path=path,
            raw_query="",
            headers=Headers(items),
            body=json.dumps(body).encode("utf-8") if body is not None else b"",
            client_address=client,
            server_port=PORT,
        )
        response = self.app.handle(request)
        try:
            payload = json.loads(response.body.decode("utf-8"))
        except ValueError:
            payload = {}
        return response.status, payload, response.headers

    def browser(
        self, method: str, path: str, body: dict | None = None, *, cookie: str | None = None, **kwargs
    ):
        """A console-shaped request: canonical Origin, optional session."""
        return self.send(
            method,
            path,
            body,
            cookie=cookie if cookie is not None else self.session,
            origin=f"http://{HOST}:{PORT}",
            **kwargs,
        )

    def machine(self, method: str, path: str, key: str, body: dict | None = None, **kwargs):
        """The machine-client shape: bearer, no Origin, no cookie."""
        return self.send(method, path, body, authorization=f"Bearer {key}", **kwargs)

    # --- fixtures --------------------------------------------------------

    def sign_in(self) -> str:
        status, payload, headers = self.send(
            "POST",
            "/owner/api/auth/setup",
            {"username": "owner", "password": OWNER_PASSWORD, "setup_code": self.setup_code},
            origin=f"http://{HOST}:{PORT}",
        )
        if status != 200:
            raise RuntimeError(f"fixture setup failed: {status} {payload}")
        token = cookie_token(headers)
        if token is None:
            raise RuntimeError("fixture setup issued no session cookie")
        self.session = token
        return token

    def create_key(self, name: str, scope: str) -> tuple[str, str, str]:
        """Create through the real endpoint; returns (display_id, value, ...)."""
        status, payload, _ = self.browser(
            "POST", "/owner/api/machine-keys", {"action": "create", "name": name, "scope": scope}
        )
        if status != 200:
            raise RuntimeError(f"fixture key creation failed: {status} {payload}")
        created = payload["machine_key"]
        return created["display_id"], created["key"], created["scope"]

    def logs(self) -> list[dict]:
        records = []
        for line in self.log_stream.getvalue().splitlines():
            try:
                records.append(json.loads(line))
            except ValueError:
                continue
        return records


class StoreTests(unittest.TestCase):
    """The store itself: hash-only, one-time value, immediate revoke."""

    def setUp(self) -> None:
        self.harness = MachineKeyHarness()
        self.addCleanup(self.harness.close)
        self.harness.sign_in()

    def test_create_returns_the_value_once_and_the_store_holds_only_a_hash(self) -> None:
        display_id, value, scope = self.harness.create_key(READ_KEY_NAME, "read")
        self.assertTrue(value.startswith(MACHINE_KEY_PREFIX))
        self.assertEqual(scope, "read")

        store_path = self.harness.data_dir / MACHINE_KEY_FILE_NAME
        self.assertTrue(store_path.exists())
        self.assertEqual(store_path.stat().st_mode & 0o777, 0o600)
        raw = store_path.read_text(encoding="utf-8")
        self.assertNotIn(value, raw, "the plaintext must never touch disk")
        self.assertIn(display_id, raw)

        pepper_path = self.harness.data_dir / MACHINE_KEY_PEPPER_FILE_NAME
        self.assertTrue(pepper_path.exists())
        self.assertEqual(pepper_path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(value, pepper_path.read_text(encoding="utf-8"))

        # The list is exactly four fields and never carries the value.
        status, payload, _ = self.harness.browser("GET", "/owner/api/machine-keys")
        self.assertEqual(status, 200, payload)
        rows = payload["machine_keys"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            sorted(rows[0].keys()),
            ["display_id", "last_used_at", "name", "scope"],
        )
        self.assertEqual(rows[0]["display_id"], display_id)
        self.assertEqual(rows[0]["name"], READ_KEY_NAME)
        self.assertNotIn(value, json.dumps(payload))

        # Nothing else can read it back: no endpoint returns the value.
        for path in ("/owner/api/state", "/owner/api/status", "/owner/api/machine-keys"):
            _, body, _ = self.harness.browser("GET", path)
            self.assertNotIn(value, json.dumps(body), path)

    def test_rotate_keeps_the_display_id_and_kills_the_previous_value(self) -> None:
        display_id, old_value, _ = self.harness.create_key(WRITE_KEY_NAME, "write")
        status, payload, _ = self.harness.browser(
            "POST", "/owner/api/machine-keys", {"action": "rotate", "display_id": display_id}
        )
        self.assertEqual(status, 200, payload)
        rotated = payload["machine_key"]
        self.assertEqual(rotated["display_id"], display_id)
        self.assertNotEqual(rotated["key"], old_value)
        self.assertTrue(rotated["key"].startswith(MACHINE_KEY_PREFIX))

        status, _, _ = self.harness.machine("GET", "/owner/api/state", old_value)
        self.assertEqual(status, 401, "the previous value must be dead immediately")
        status, _, _ = self.harness.machine("GET", "/owner/api/state", rotated["key"])
        self.assertEqual(status, 200)

    def test_revoke_deletes_the_record_and_takes_effect_at_once(self) -> None:
        display_id, value, _ = self.harness.create_key(WRITE_KEY_NAME, "write")
        status, _, _ = self.harness.machine("GET", "/owner/api/state", value)
        self.assertEqual(status, 200)

        status, payload, _ = self.harness.browser(
            "POST", "/owner/api/machine-keys", {"action": "revoke", "display_id": display_id}
        )
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["revoked"])

        # No grace window, no cached acceptance: the very next request.
        status, payload, _ = self.harness.machine("GET", "/owner/api/state", value)
        self.assertEqual(status, 401, payload)
        self.assertEqual(payload["error"]["type"], "authentication_required")

        _, body, _ = self.harness.browser("GET", "/owner/api/machine-keys")
        self.assertEqual(body["machine_keys"], [])

        # The record - and with it the only accepted hash - is gone from disk.
        stored = read_json(self.harness.data_dir / MACHINE_KEY_FILE_NAME, {})
        self.assertEqual(stored.get("keys"), {})

    def test_last_used_is_recorded_and_list_exposes_only_the_four_fields(self) -> None:
        _display_id, value, _ = self.harness.create_key(READ_KEY_NAME, "read")
        _, payload, _ = self.harness.browser("GET", "/owner/api/machine-keys")
        self.assertIsNone(payload["machine_keys"][0]["last_used_at"])

        self.harness.now += 120.0
        status, _, _ = self.harness.machine("GET", "/owner/api/state", value)
        self.assertEqual(status, 200)

        _, payload, _ = self.harness.browser("GET", "/owner/api/machine-keys")
        row = payload["machine_keys"][0]
        self.assertEqual(sorted(row.keys()), ["display_id", "last_used_at", "name", "scope"])
        self.assertEqual(row["last_used_at"], self.harness.now)

    def test_a_hand_edited_store_record_never_authenticates(self) -> None:
        display_id, value, _ = self.harness.create_key(READ_KEY_NAME, "read")
        store_path = self.harness.data_dir / MACHINE_KEY_FILE_NAME
        data = read_json(store_path, {})
        # A forged record without a hash, and a scope outside the enum.
        data["keys"]["mk_deadbeef"] = {"name": "forged", "scope": "write"}
        data["keys"][display_id]["scope"] = "root"
        store_path.write_text(json.dumps(data), encoding="utf-8")

        status, _, _ = self.harness.machine("GET", "/owner/api/state", value)
        self.assertEqual(status, 401, "a record with an invalid scope must be dropped")

    def test_create_validates_name_and_scope(self) -> None:
        for name, scope, expected in (
            ("", "read", "invalid_machine_key_name"),
            ("ok", "root", "invalid_scope"),
            ("ok", None, "invalid_scope"),
            ("a" * 65, "read", "invalid_machine_key_name"),
        ):
            status, payload, _ = self.harness.browser(
                "POST", "/owner/api/machine-keys", {"action": "create", "name": name, "scope": scope}
            )
            self.assertEqual(status, 400, payload)
            self.assertEqual(payload["error"]["type"], expected)
        self.assertEqual(set(MACHINE_KEY_SCOPES), {"read", "write"})


class MachineRequestShapeTests(unittest.TestCase):
    """The exact accepted request shape, and what falls back to the cookie."""

    def setUp(self) -> None:
        self.harness = MachineKeyHarness()
        self.addCleanup(self.harness.close)
        self.harness.sign_in()
        self.read_id, self.read_key, _ = self.harness.create_key(READ_KEY_NAME, "read")
        self.write_id, self.write_key, _ = self.harness.create_key(WRITE_KEY_NAME, "write")

    def test_bearer_parsing_is_exactly_two_fields_with_a_case_insensitive_scheme(self) -> None:
        # Accepted: the scheme is case-insensitive and any whitespace run
        # separates the two fields.
        for value in (f"bEaReR {self.read_key}", f"Bearer\t{self.read_key}"):
            status, payload, _ = self.harness.send(
                "GET", "/owner/api/state", authorization=value
            )
            self.assertEqual(status, 200, (value, payload))

        # Refused as a machine request - each of these falls through to the
        # ordinary cookie path, and there is no cookie on these requests.
        refused = [
            "Bearer",
            "Bearer   ",
            f"Bearer {self.read_key} extra",
            f"Basic {self.read_key}",
            f"{self.read_key}",
        ]
        for value in refused:
            status, payload, _ = self.harness.send(
                "GET", "/owner/api/state", authorization=value
            )
            self.assertEqual(status, 401, (value, payload))
            self.assertEqual(payload["error"]["message"], "owner sign-in required", (value, payload))
            self.assertNotIn("machine", json.dumps(payload))

    def test_machine_requests_need_no_origin_and_no_cookie(self) -> None:
        status, payload, _ = self.harness.machine("GET", "/owner/api/state", self.read_key)
        self.assertEqual(status, 200, payload)

    def test_a_hostile_origin_does_not_stop_a_machine_mutation(self) -> None:
        status, payload, _ = self.harness.machine(
            "POST",
            "/owner/api/settings",
            self.write_key,
            {"action": "initialize"},
            origin="https://evil.example",
        )
        self.assertEqual(status, 200, payload)

    def test_cookie_mutations_still_require_the_canonical_origin(self) -> None:
        # A23: the browser CSRF guard is untouched by the machine path.
        status, payload, _ = self.harness.send(
            "POST",
            "/owner/api/settings",
            {"action": "initialize"},
            cookie=self.harness.session,
            origin=None,
        )
        self.assertEqual(status, 403, payload)
        self.assertEqual(payload["error"]["type"], "origin_required")

        status, payload, _ = self.harness.send(
            "POST",
            "/owner/api/settings",
            {"action": "initialize"},
            cookie=self.harness.session,
            origin="https://evil.example",
        )
        self.assertEqual(status, 403, payload)
        self.assertEqual(payload["error"]["type"], "origin_mismatch")

        status, payload, _ = self.harness.browser(
            "POST", "/owner/api/settings", {"action": "initialize"}
        )
        self.assertEqual(status, 200, payload)

    def test_host_and_forwarded_checks_run_before_machine_authentication(self) -> None:
        status, payload, _ = self.harness.send(
            "GET",
            "/owner/api/state",
            authorization=f"Bearer {self.read_key}",
            extra_headers=[("Host", "evil.example")],
        )
        self.assertEqual(status, 403, payload)
        self.assertEqual(payload["error"]["type"], "host_mismatch")

        for name in ("x-forwarded-for", "x-real-ip", "forwarded"):
            status, payload, _ = self.harness.send(
                "GET",
                "/owner/api/state",
                authorization=f"Bearer {self.read_key}",
                extra_headers=[(name, "10.0.0.9")],
            )
            self.assertEqual(status, 400, (name, payload))
            self.assertEqual(payload["error"]["type"], "forwarded_header_rejected")

    def test_a_machine_request_may_not_carry_a_session_cookie(self) -> None:
        status, payload, _ = self.harness.machine(
            "GET", "/owner/api/state", self.read_key, cookie=self.harness.session
        )
        self.assertEqual(status, 403, payload)
        self.assertEqual(payload["error"]["type"], "cookie_rejected")

    def test_a_session_request_without_a_bearer_is_unchanged(self) -> None:
        status, payload, _ = self.harness.browser("GET", "/owner/api/state")
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["can_mutate"])
        self.assertTrue(payload["authenticated"])
        self.assertEqual(payload["owner"]["username"], "owner")

    def test_bearer_authorization_helper_matches_the_wire_shape(self) -> None:
        from gateway.http.core import Request as Req

        def req(value: str | None):
            items = [] if value is None else [("Authorization", value)]
            return Req(
                method="GET",
                path="/owner/api/state",
                raw_query="",
                headers=Headers(items),
                body=b"",
                client_address=(HOST, 40000),
                server_port=PORT,
            )

        self.assertIsNone(bearer_authorization(req(None)))
        self.assertEqual(bearer_authorization(req("Bearer abc")), "abc")
        self.assertEqual(bearer_authorization(req("bearer abc")), "abc")
        self.assertIsNone(bearer_authorization(req("Bearer ")))
        self.assertIsNone(bearer_authorization(req("Bearer a b")))
        self.assertIsNone(bearer_authorization(req("Basic abc")))


class ScopeAndSurfaceTests(unittest.TestCase):
    """Read vs write scope, and which endpoints a machine key may reach."""

    def setUp(self) -> None:
        self.harness = MachineKeyHarness()
        self.addCleanup(self.harness.close)
        self.harness.sign_in()
        self.read_id, self.read_key, _ = self.harness.create_key(READ_KEY_NAME, "read")
        self.write_id, self.write_key, _ = self.harness.create_key(WRITE_KEY_NAME, "write")

    def test_a_read_key_reads_every_machine_readable_endpoint(self) -> None:
        for suffix in sorted(MACHINE_KEY_READ_PATHS):
            status, payload, _ = self.harness.machine(
                "GET", "/owner" + suffix, self.read_key
            )
            self.assertEqual(status, 200, (suffix, payload))

    def test_a_read_key_is_refused_on_every_mutating_owner_endpoint(self) -> None:
        before = self.harness.config.snapshot().revision
        for suffix in sorted(MACHINE_KEY_WRITE_PATHS):
            status, payload, _ = self.harness.machine(
                "POST", "/owner" + suffix, self.read_key, {"action": "initialize"}
            )
            self.assertEqual(status, 403, (suffix, payload))
            self.assertEqual(payload["error"]["type"], "scope_required", suffix)
        self.assertEqual(
            self.harness.config.snapshot().revision, before, "no write may half-apply"
        )

    def test_a_write_key_mutates_and_reads(self) -> None:
        status, payload, _ = self.harness.machine(
            "POST", "/owner/api/settings", self.write_key, {"action": "initialize"}
        )
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["ok"])

        status, payload, _ = self.harness.machine("GET", "/owner/api/state", self.write_key)
        self.assertEqual(status, 200, payload)
        # can_mutate follows the key's scope; credential enrolment never does.
        self.assertTrue(payload["can_mutate"])
        self.assertFalse(payload["can_enroll_credentials"])
        self.assertFalse(payload["authenticated"])

    def test_every_session_only_endpoint_refuses_a_machine_key(self) -> None:
        session_only = [
            ("GET", "/owner/", None),
            ("GET", "/owner/index.html", None),
            ("GET", "/owner/api/auth/session", None),
            ("POST", "/owner/api/auth/logout", {}),
            ("POST", "/owner/api/auth/password", {"current_password": "x", "new_password": "y"}),
            ("POST", "/owner/api/auth/client-key", {"action": "clear"}),
            ("GET", "/owner/api/machine-keys", None),
            ("POST", "/owner/api/machine-keys", {"action": "create", "name": "x", "scope": "read"}),
            ("GET", "/owner/api/backup/export", None),
            ("POST", "/owner/api/backup/import", {}),
            ("POST", "/owner/api/preview", {"model": "x"}),
            ("POST", "/owner/api/profile", {"action": "issue_token", "profile_id": "p"}),
        ]
        for method, path, body in session_only:
            for key in (self.read_key, self.write_key):
                status, payload, _ = self.harness.machine(method, path, key, body)
                self.assertEqual(status, 403, (method, path, payload))
                self.assertEqual(
                    payload["error"]["type"], "machine_key_forbidden", (method, path)
                )

    def test_console_pages_and_auth_endpoints_stay_out_of_machine_reach(self) -> None:
        status, payload, _ = self.harness.machine("GET", "/owner/", self.read_key)
        self.assertEqual(status, 403, payload)
        self.assertEqual(payload["error"]["type"], "machine_key_forbidden")

        # A machine key never signs anyone in: login with a valid password
        # and a bearer is refused before the handler runs, so no cookie.
        status, payload, headers = self.harness.machine(
            "POST",
            "/owner/api/auth/login",
            self.write_key,
            {"username": "owner", "password": OWNER_PASSWORD},
        )
        self.assertEqual(status, 403, payload)
        self.assertIsNone(cookie_token(headers))

        # Without a bearer the console shell is exactly as public as before.
        status, _, _ = self.harness.send("GET", "/owner/")
        self.assertEqual(status, 200)

    def test_machine_key_responses_never_issue_a_session_cookie(self) -> None:
        probes = [
            ("GET", "/owner/api/state", self.read_key, None),
            ("POST", "/owner/api/settings", self.write_key, {"action": "initialize"}),
            ("GET", "/owner/api/state", self.read_key + "-wrong", None),
            ("POST", "/owner/api/alias", self.read_key, {"action": "save"}),
        ]
        for method, path, key, body in probes:
            _, _, headers = self.harness.machine(method, path, key, body)
            self.assertIsNone(cookie_token(headers), (method, path))

        # And no machine request leaves a session behind for a later call.
        status, payload, _ = self.harness.send("GET", "/owner/api/state")
        self.assertEqual(status, 401, payload)
        self.assertEqual(payload["error"]["type"], "authentication_required")

    def test_credential_enrolment_is_refused_even_with_a_write_key(self) -> None:
        status, payload, _ = self.harness.machine(
            "POST",
            "/owner/api/provider",
            self.write_key,
            {"action": "enroll_key", "provider_id": "anthropic", "api_key": "sk-machine-probe"},
        )
        self.assertEqual(status, 403, payload)
        self.assertEqual(payload["error"]["type"], "machine_key_forbidden")
        self.assertIsNone(self.harness.credentials.handle("anthropic").read())
        self.assertNotIn("sk-machine-probe", json.dumps(payload))

    def test_state_cache_does_not_share_bodies_across_principals(self) -> None:
        # A session render and a machine render must not be served to
        # each other: the token includes both mutation verdicts.
        _, session_body, _ = self.harness.browser("GET", "/owner/api/state")
        self.assertEqual(session_body["owner"]["username"], "owner")

        _, machine_body, _ = self.harness.machine(
            "GET", "/owner/api/state", self.write_key
        )
        self.assertIsNone(machine_body["owner"]["username"])
        self.assertFalse(machine_body["authenticated"])

        _, again, _ = self.harness.browser("GET", "/owner/api/state")
        self.assertEqual(again["owner"]["username"], "owner")


class KeyConfusionTests(unittest.TestCase):
    """A21: no crossover between the three credentials."""

    def setUp(self) -> None:
        self.harness = MachineKeyHarness()
        self.addCleanup(self.harness.close)
        self.harness.sign_in()
        self.read_id, self.read_key, _ = self.harness.create_key(READ_KEY_NAME, "read")
        self.write_id, self.write_key, _ = self.harness.create_key(WRITE_KEY_NAME, "write")

    def test_a_machine_key_never_authorizes_the_model_surface(self) -> None:
        for key in (self.read_key, self.write_key):
            status, payload, _ = self.harness.machine("GET", "/v1/models", key)
            self.assertEqual(status, 401, payload)
            self.assertEqual(payload["error"]["type"], "invalid_api_key")

        # Keyless /v1 stays keyless: no Authorization, no machine key.
        status, _, _ = self.harness.send("GET", "/v1/models")
        self.assertEqual(status, 200)

    def test_a_model_client_key_never_authorizes_owner_routes(self) -> None:
        client_key = "ck-" + "a" * 40
        status, payload, _ = self.harness.browser(
            "POST", "/owner/api/auth/client-key", {"action": "set", "key": client_key}
        )
        self.assertEqual(status, 200, payload)

        status, payload, _ = self.harness.send(
            "GET", "/owner/api/state", authorization=f"Bearer {client_key}"
        )
        self.assertEqual(status, 401, payload)
        self.assertEqual(payload["error"]["type"], "authentication_required")

        # The model surface still accepts it, and a machine key still does not.
        status, _, _ = self.harness.send(
            "GET", "/v1/models", authorization=f"Bearer {client_key}"
        )
        self.assertEqual(status, 200)
        status, payload, _ = self.harness.machine("GET", "/v1/models", self.write_key)
        self.assertEqual(status, 401, payload)
        self.assertEqual(payload["error"]["type"], "invalid_api_key")


class AuditAndLeakageTests(unittest.TestCase):
    """A7c + the audit rule: display id and action, never the value."""

    def setUp(self) -> None:
        self.harness = MachineKeyHarness()
        self.addCleanup(self.harness.close)
        self.harness.sign_in()
        self.read_id, self.read_key, _ = self.harness.create_key(READ_KEY_NAME, "read")
        self.write_id, self.write_key, _ = self.harness.create_key(WRITE_KEY_NAME, "write")

    def test_every_machine_mutation_is_logged_with_display_id_and_action(self) -> None:
        status, _, _ = self.harness.machine(
            "POST", "/owner/api/settings", self.write_key, {"action": "initialize"}
        )
        self.assertEqual(status, 200)

        status, _, _ = self.harness.machine(
            "POST", "/owner/api/settings", self.read_key, {"action": "initialize"}
        )
        self.assertEqual(status, 403)

        records = [r for r in self.harness.logs() if r.get("machine_key_id")]
        self.assertEqual(len(records), 2, records)
        self.assertEqual({r["machine_key_id"] for r in records}, {self.write_id, self.read_id})
        for record in records:
            self.assertTrue(record.get("action"), record)
            self.assertEqual(record["method"], "POST")
            self.assertEqual(record["path"], "/owner/api/settings")

    def test_reads_do_not_carry_an_audit_identity_and_nothing_carries_the_value(self) -> None:
        status, _, _ = self.harness.machine("GET", "/owner/api/state", self.read_key)
        self.assertEqual(status, 200)
        status, _, _ = self.harness.machine("GET", "/owner/api/state", "gwmk_not-a-real-key")
        self.assertEqual(status, 401)

        text = self.harness.log_stream.getvalue()
        self.assertNotIn(self.read_key, text)
        self.assertNotIn(self.write_key, text)
        self.assertNotIn("gwmk_not-a-real-key", text)
        self.assertNotIn("machine_key_id", text)

    def test_no_surface_ever_echoes_a_key_value(self) -> None:
        probes = [
            ("GET", "/owner/api/state", None),
            ("GET", "/owner/api/status", None),
            ("GET", "/owner/api/machine-keys", None),
            ("GET", "/version", None),
            ("GET", "/health", None),
            ("GET", "/owner/api/auth/session", None),
        ]
        for method, path, _ in probes:
            _, payload, _ = self.harness.browser(method, path)
            blob = json.dumps(payload)
            self.assertNotIn(self.read_key, blob, path)
            self.assertNotIn(self.write_key, blob, path)

        # A refused authentication must not echo the presented value.
        presented = "gwmk_" + "z" * 40
        status, payload, headers = self.harness.machine("GET", "/owner/api/state", presented)
        self.assertEqual(status, 401)
        self.assertNotIn(presented, json.dumps(payload))
        self.assertNotIn(presented, str(headers))

        # Error paths on the machine surface stay equally quiet.
        status, payload, _ = self.harness.machine(
            "POST", "/owner/api/settings", self.read_key, {"action": "initialize"}
        )
        self.assertEqual(status, 403)
        self.assertNotIn(self.read_key, json.dumps(payload))

        # And the log stayed clean through all of it.
        text = self.harness.log_stream.getvalue()
        self.assertNotIn(self.read_key, text)
        self.assertNotIn(self.write_key, text)


class CompatibilityTests(unittest.TestCase):
    """A23: the human console keeps working exactly as it did."""

    def setUp(self) -> None:
        self.harness = MachineKeyHarness()
        self.addCleanup(self.harness.close)
        self.harness.sign_in()

    def test_session_only_mutations_still_need_a_session(self) -> None:
        status, payload, _ = self.harness.send(
            "POST",
            "/owner/api/settings",
            {"action": "initialize"},
            origin=f"http://{HOST}:{PORT}",
        )
        self.assertEqual(status, 401, payload)
        self.assertEqual(payload["error"]["type"], "authentication_required")

    def test_setup_login_logout_and_password_flow_are_unchanged(self) -> None:
        status, payload, headers = self.harness.browser("POST", "/owner/api/auth/logout")
        self.assertEqual(status, 200, payload)
        self.assertIsNotNone(cookie_token(headers))

        status, payload, headers = self.harness.browser(
            "POST",
            "/owner/api/auth/login",
            {"username": "owner", "password": OWNER_PASSWORD},
        )
        self.assertEqual(status, 200, payload)
        token = cookie_token(headers)
        self.assertIsNotNone(token)

        status, payload, _ = self.harness.browser(
            "POST",
            "/owner/api/auth/password",
            {"current_password": OWNER_PASSWORD, "new_password": "another-good-passphrase"},
            cookie=token,
        )
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["ok"])

    def test_the_state_payload_still_describes_the_client_key_not_machine_keys(self) -> None:
        self.harness.create_key(READ_KEY_NAME, "read")
        _, payload, _ = self.harness.browser("GET", "/owner/api/state")
        self.assertIn("client_key", payload["access"])
        self.assertNotIn("machine_keys", json.dumps(payload))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
