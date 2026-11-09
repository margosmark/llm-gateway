"""Owner authentication and the optional model client API key.

Two boundaries, asserted apart on purpose:

* the owner console and every owner configuration API need a session
  cookie, issued by a one-time first-run setup or a sign-in;
* the ``/v1`` model surface needs a bearer key **only** when the owner
  has set one - keyless by default, so every existing client keeps
  working, and clearing the key restores exactly that.

Nothing here contacts a provider or reads a real credential store, and
no assertion depends on a number this file chose: the cookie flags, the
throttle window and the grace window are read from the modules that
implement them.
"""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from gateway.adapters.registry import AdapterRegistry
from gateway.auth import ProfileAuth
from gateway.cli import _reset_owner_password
from gateway.config import ConfigStore
from gateway.core.routing import Router
from gateway.credentials import CredentialStore
from gateway.http.app import GatewayApp
from gateway.http.core import Headers, Request
from gateway.http.security import (
    SecurityPolicy,
    build_allowed_hosts,
    build_canonical_origins,
)
from gateway.logging import RequestLogger
from gateway.observability import LimitStore, RequestLedger
from gateway.owner_auth import (
    CLIENT_KEY_GRACE_SECONDS,
    COOKIE_NAME,
    DEFAULT_PBKDF2_ITERATIONS,
    LOGIN_MAX_FAILURES,
    LOGIN_WINDOW_SECONDS,
    SESSION_TTL_SECONDS,
    SETUP_CODE_FILE_NAME,
    OwnerAuthStore,
)

HOST = "127.0.0.1"
PORT = 8765
OWNER_PASSWORD = "fixture-owner-passphrase"
NEW_PASSWORD = "a-different-passphrase"
CLIENT_KEY = "k-" + "a" * 40
ROTATED_KEY = "k-" + "b" * 40

#: The exact model-facing inventory: every path the client-key check
#: covers once a key is configured. Kept explicit so adding a model
#: endpoint without extending the boundary fails here.
MODEL_ENDPOINTS = (
    ("GET", "/v1/models"),
    ("POST", "/v1/chat/completions"),
    ("POST", "/v1/messages"),
    ("POST", "/v1/responses"),
    ("GET", "/v1/systemone/models"),
    ("GET", "/v1/alias-catalog"),
)


def set_cookie(headers) -> str | None:
    for key, value in headers:
        if key.lower() == "set-cookie" and value.startswith(f"{COOKIE_NAME}="):
            return value
    return None


def cookie_token(headers) -> str | None:
    header = set_cookie(headers)
    if header is None:
        return None
    return header.partition("=")[2].split(";", 1)[0]


def header(headers, name: str) -> str | None:
    for key, value in headers:
        if key.lower() == name.lower():
            return value
    return None


class OwnerAuthHarness:
    """A gateway with no server: requests go straight through ``handle``."""

    def __init__(self, *, cookie_secure: bool | None = False) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp.name)
        # A frozen clock the tests move by hand: throttle windows and the
        # client-key grace window are elapsed without sleeping.
        self.now = 1_800_000_000.0
        self.config = ConfigStore(self.data_dir / "config.json")
        self.credentials = CredentialStore(self.data_dir)
        self.registry = AdapterRegistry()
        policy = SecurityPolicy(
            allowed_hosts=build_allowed_hosts(HOST, PORT),
            canonical_origins=build_canonical_origins(HOST, PORT),
        )
        self.owner_auth = OwnerAuthStore(self.data_dir, clock=lambda: self.now)
        self.app = GatewayApp(
            config=self.config,
            credentials=self.credentials,
            router=Router(self.config, self.registry, ledger=RequestLedger()),
            registry=self.registry,
            profile_auth=ProfileAuth(self.config, self.credentials),
            policy=policy,
            ledger=RequestLedger(),
            limits=LimitStore(),
            release_identity=lambda: {},
            base_url=f"http://{HOST}:{PORT}",
            mounts=("/owner",),
            logger=RequestLogger(enabled=False),
            owner_auth=self.owner_auth,
            cookie_secure=cookie_secure,
        )
        # The plaintext one-time code, exactly as server startup would
        # print it: server-side only, never in an HTTP response.
        self.setup_code: str | None = self.app.setup_code_created

    def close(self) -> None:
        self.temp.cleanup()

    def request(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        *,
        cookie: str | None = None,
        headers: list[tuple[str, str]] | None = None,
        client: tuple[str, int] = (HOST, 40000),
    ):
        items: list[tuple[str, str]] = [
            ("Host", f"{HOST}:{PORT}"),
            ("Origin", f"http://{HOST}:{PORT}"),
        ]
        if cookie:
            items.append(("Cookie", f"{COOKIE_NAME}={cookie}"))
        items.extend(headers or [])
        if body is not None and not any(name.lower() == "content-type" for name, _ in items):
            # What a real JSON client sends: the owner surface refuses a
            # body that does not declare application/json.
            items.append(("Content-Type", "application/json"))
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

    def complete_setup(self, password: str = OWNER_PASSWORD) -> str:
        """Consume the one-time code and return the session token."""
        if self.setup_code is None:
            raise RuntimeError("fixture has no one-time setup code")
        status, _, headers = self.request(
            "POST",
            "/owner/api/auth/setup",
            {"username": "owner", "password": password, "setup_code": self.setup_code},
        )
        if status != 200:
            raise RuntimeError(f"fixture setup failed: {status}")
        token = cookie_token(headers)
        if token is None:
            raise RuntimeError("fixture setup issued no session cookie")
        return token

    def login(self, password: str = OWNER_PASSWORD, client: tuple[str, int] = (HOST, 40000)):
        return self.request(
            "POST",
            "/owner/api/auth/login",
            {"username": "owner", "password": password},
            client=client,
        )


class FirstRunSetupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = OwnerAuthHarness()
        self.addCleanup(self.harness.close)

    def test_a_fresh_gateway_has_no_account_and_no_default_credential(self) -> None:
        self.assertTrue(self.harness.owner_auth.setup_required)
        self.assertIsNotNone(self.harness.setup_code)

        status, payload, _ = self.harness.request("GET", "/owner/api/auth/session")
        self.assertEqual(status, 200)
        self.assertFalse(payload["authenticated"])
        self.assertTrue(payload["setup_required"])
        self.assertIsNone(payload["username"])

        # No admin/admin, no seeded anything: sign-in before setup is a
        # refusal that tells the caller to run setup instead.
        for username in ("admin", "owner"):
            status, payload, _ = self.harness.request(
                "POST",
                "/owner/api/auth/login",
                {"username": username, "password": username},
            )
            self.assertEqual(status, 409, payload)
            self.assertEqual(payload["error"]["type"], "setup_required")

    def test_the_setup_code_lives_on_the_host_and_never_in_a_response(self) -> None:
        code_path = self.harness.data_dir / SETUP_CODE_FILE_NAME
        self.assertTrue(code_path.exists())
        self.assertEqual(code_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.harness.owner_auth.read_setup_code(), self.harness.setup_code)

        probes = [
            self.harness.request("GET", "/owner/api/auth/session"),
            self.harness.request("GET", "/owner/"),
            self.harness.request(
                "POST",
                "/owner/api/auth/login",
                {"username": "owner", "password": "wrong-password"},
            ),
            self.harness.request(
                "POST",
                "/owner/api/auth/setup",
                {
                    "username": "owner",
                    "password": OWNER_PASSWORD,
                    "setup_code": "wrong-code",
                },
            ),
            self.harness.request("GET", "/version"),
        ]
        for _, payload, _ in probes:
            self.assertNotIn(str(self.harness.setup_code), json.dumps(payload))

    def test_setup_is_single_use_and_a_wrong_code_is_refused(self) -> None:
        status, payload, _ = self.harness.request(
            "POST",
            "/owner/api/auth/setup",
            {"username": "owner", "password": OWNER_PASSWORD, "setup_code": "not-the-code"},
        )
        self.assertEqual(status, 403, payload)
        self.assertEqual(payload["error"]["type"], "invalid_setup_code")
        self.assertTrue(self.harness.owner_auth.setup_required)

        token = self.harness.complete_setup()
        self.assertTrue(token)
        self.assertFalse(self.harness.owner_auth.setup_required)
        self.assertIsNone(self.harness.owner_auth.read_setup_code())

        # The consumed code cannot create a second account or a second
        # session; the store already has an owner.
        status, payload, _ = self.harness.request(
            "POST",
            "/owner/api/auth/setup",
            {
                "username": "intruder",
                "password": OWNER_PASSWORD,
                "setup_code": str(self.harness.setup_code),
            },
        )
        self.assertEqual(status, 409, payload)
        self.assertEqual(payload["error"]["type"], "already_configured")

    def test_setup_issues_an_httponly_session_cookie(self) -> None:
        status, _, headers = self.harness.request(
            "POST",
            "/owner/api/auth/setup",
            {
                "username": "owner",
                "password": OWNER_PASSWORD,
                "setup_code": str(self.harness.setup_code),
            },
        )
        self.assertEqual(status, 200)
        cookie = set_cookie(headers)
        self.assertIsNotNone(cookie)
        assert cookie is not None
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertIn("Path=/", cookie)
        self.assertIn("Max-Age=", cookie)
        # Plain-HTTP LAN listener: no Secure, because a browser would drop
        # the cookie. The Secure decision is tested separately.
        self.assertNotIn("Secure", cookie)
        # The response body carries no token either: script cannot read a
        # cookie it is not told about, and neither can a log scraper.
        status, payload, _ = self.harness.request("GET", "/owner/api/auth/session")
        self.assertEqual(status, 200)
        self.assertNotIn(cookie.split(";", 1)[0].partition("=")[2], json.dumps(payload))

    def test_owner_endpoints_require_a_session_while_the_shell_does_not(self) -> None:
        token = self.harness.complete_setup()

        # The login view's inputs: reachable before a session exists.
        for path in ("/owner/", "/owner/app.js", "/owner/style.css", "/owner/api/auth/session"):
            status, _, _ = self.harness.request("GET", path)
            self.assertEqual(status, 200, path)

        # Every owner API and read refuses without the cookie.
        for method, path, body in (
            ("GET", "/owner/api/state", None),
            ("GET", "/owner/api/leaderboard", None),
            ("POST", "/owner/api/profile", {"action": "save", "profile_id": "x", "name": "x"}),
            (
                "POST",
                "/owner/api/auth/password",
                {"current_password": OWNER_PASSWORD, "new_password": NEW_PASSWORD},
            ),
            ("POST", "/owner/api/auth/client-key", {"action": "clear"}),
            ("POST", "/owner/api/auth/logout", {}),
        ):
            status, payload, _ = self.harness.request(method, path, body)
            self.assertEqual(status, 401, f"{method} {path} -> {payload}")
            self.assertEqual(payload["error"]["type"], "authentication_required")

        # With the cookie they answer normally.
        status, payload, _ = self.harness.request("GET", "/owner/api/state", cookie=token)
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["authenticated"])
        self.assertEqual(payload["owner"], {"username": "owner"})

        # Non-owner surfaces were never part of this boundary.
        for path in ("/version", "/health"):
            status, _, _ = self.harness.request("GET", path)
            self.assertEqual(status, 200, path)


class SessionLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = OwnerAuthHarness()
        self.addCleanup(self.harness.close)
        self.token = self.harness.complete_setup()

    def test_logout_revokes_the_session_server_side(self) -> None:
        status, _, _ = self.harness.request("GET", "/owner/api/state", cookie=self.token)
        self.assertEqual(status, 200)

        status, _, headers = self.harness.request(
            "POST", "/owner/api/auth/logout", {}, cookie=self.token
        )
        self.assertEqual(status, 200)
        cleared = set_cookie(headers)
        self.assertIsNotNone(cleared)
        assert cleared is not None
        self.assertIn("Max-Age=0", cleared)

        # The browser dropping the cookie is cosmetic: the token itself is
        # dead the moment the server revokes it.
        status, payload, _ = self.harness.request("GET", "/owner/api/state", cookie=self.token)
        self.assertEqual(status, 401, payload)
        self.assertEqual(payload["error"]["type"], "authentication_required")

    def test_login_throttles_repeated_failures(self) -> None:
        for _ in range(LOGIN_MAX_FAILURES):
            status, payload, _ = self.harness.login(password="wrong-passphrase")
            self.assertEqual(status, 401, payload)
            self.assertEqual(payload["error"]["type"], "invalid_credentials")

        status, payload, headers = self.harness.login(password="wrong-passphrase")
        self.assertEqual(status, 429, payload)
        self.assertEqual(payload["error"]["type"], "login_throttled")
        retry_after = header(headers, "Retry-After")
        self.assertIsNotNone(retry_after)
        assert retry_after is not None
        self.assertGreater(float(retry_after), 0)

        # Throttling is not a bypass: even the right password waits.
        status, payload, _ = self.harness.login(password=OWNER_PASSWORD)
        self.assertEqual(status, 429, payload)

        # And it lifts when the window ages out.
        self.harness.now += LOGIN_WINDOW_SECONDS + 1
        status, payload, headers = self.harness.login(password=OWNER_PASSWORD)
        self.assertEqual(status, 200, payload)
        self.assertIsNotNone(cookie_token(headers))

    def test_throttling_is_also_keyed_by_client_address(self) -> None:
        # Distinct bogus usernames so only the address key accumulates.
        for index in range(LOGIN_MAX_FAILURES):
            status, _, _ = self.harness.request(
                "POST",
                "/owner/api/auth/login",
                {"username": f"intruder{index}", "password": "wrong-passphrase"},
                client=(HOST, 40000),
            )
            self.assertEqual(status, 401)

        # That address is now locked out, whatever account it names.
        status, payload, _ = self.harness.request(
            "POST",
            "/owner/api/auth/login",
            {"username": "owner", "password": OWNER_PASSWORD},
            client=(HOST, 40000),
        )
        self.assertEqual(status, 429, payload)

        # A different address is a different throttle key: one noisy client
        # must not lock the owner out of their own console.
        status, payload, headers = self.harness.login(
            password=OWNER_PASSWORD, client=("10.0.0.9", 41000)
        )
        self.assertEqual(status, 200, payload)
        self.assertIsNotNone(cookie_token(headers))

    def test_password_change_needs_the_current_password_and_revokes_others(self) -> None:
        other_headers = self.harness.login()[2]
        other_token = cookie_token(other_headers)
        self.assertIsNotNone(other_token)

        status, payload, _ = self.harness.request(
            "POST",
            "/owner/api/auth/password",
            {"current_password": "not-the-password", "new_password": NEW_PASSWORD},
            cookie=self.token,
        )
        self.assertEqual(status, 401, payload)
        self.assertEqual(payload["error"]["type"], "invalid_credentials")

        status, payload, _ = self.harness.request(
            "POST",
            "/owner/api/auth/password",
            {"current_password": OWNER_PASSWORD, "new_password": NEW_PASSWORD},
            cookie=self.token,
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["other_sessions_revoked"], 1)

        # The stolen cookie stops working; the session that noticed and
        # rotated the password keeps working.
        status, _, _ = self.harness.request("GET", "/owner/api/state", cookie=other_token)
        self.assertEqual(status, 401)
        status, _, _ = self.harness.request("GET", "/owner/api/state", cookie=self.token)
        self.assertEqual(status, 200)

        status, payload, headers = self.harness.login(password=NEW_PASSWORD)
        self.assertEqual(status, 200, payload)
        self.assertIsNotNone(cookie_token(headers))
        status, payload, _ = self.harness.login(password=OWNER_PASSWORD)
        self.assertEqual(status, 401, payload)

    def test_local_recovery_reads_the_password_from_stdin_and_revokes_sessions(self) -> None:
        with mock.patch("getpass.getpass", side_effect=[NEW_PASSWORD, NEW_PASSWORD]):
            status = _reset_owner_password(self.harness.data_dir)
        self.assertEqual(status, 0)

        # Every session is dead, the old password is gone, the new one works.
        status, _, _ = self.harness.request("GET", "/owner/api/state", cookie=self.token)
        self.assertEqual(status, 401)
        status, payload, _ = self.harness.login(password=OWNER_PASSWORD)
        self.assertEqual(status, 401, payload)
        status, payload, _ = self.harness.login(password=NEW_PASSWORD)
        self.assertEqual(status, 200, payload)

    def test_a_mismatched_recovery_confirmation_changes_nothing(self) -> None:
        with mock.patch("getpass.getpass", side_effect=[NEW_PASSWORD, "not-the-same"]):
            status = _reset_owner_password(self.harness.data_dir)
        self.assertEqual(status, 2)
        status, _, _ = self.harness.request("GET", "/owner/api/state", cookie=self.token)
        self.assertEqual(status, 200)


class CookieAttributeTests(unittest.TestCase):
    def test_secure_is_set_only_when_the_deployment_says_tls(self) -> None:
        secure = OwnerAuthHarness(cookie_secure=True)
        self.addCleanup(secure.close)
        secure.complete_setup()
        _, _, headers = secure.login()
        self.assertIn("Secure", set_cookie(headers) or "")

        plain = OwnerAuthHarness(cookie_secure=False)
        self.addCleanup(plain.close)
        plain.complete_setup()
        _, _, headers = plain.login()
        cookie = set_cookie(headers)
        self.assertIsNotNone(cookie)
        self.assertNotIn("Secure", cookie or "")

        # A client-controlled forwarded header must not flip the flag: the
        # cookie would then be dropped by a browser on plain HTTP.
        spoofed = OwnerAuthHarness(cookie_secure=False)
        self.addCleanup(spoofed.close)
        spoofed.complete_setup()
        _, _, headers = spoofed.request(
            "POST",
            "/owner/api/auth/login",
            {"username": "owner", "password": OWNER_PASSWORD},
            headers=[("X-Forwarded-Proto", "https")],
        )
        self.assertNotIn("Secure", set_cookie(headers) or "")

    def test_env_opt_in_marks_the_cookie_secure(self) -> None:
        with mock.patch.dict("os.environ", {"GATEWAY_COOKIE_SECURE": "1"}):
            harness = OwnerAuthHarness(cookie_secure=None)
            self.addCleanup(harness.close)
            self.assertTrue(harness.app.cookie_secure)
            harness.complete_setup()
            _, _, headers = harness.login()
            self.assertIn("Secure", set_cookie(headers) or "")


class ModelClientKeyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = OwnerAuthHarness()
        self.addCleanup(self.harness.close)
        self.token = self.harness.complete_setup()

    def client_key_action(self, body: dict, *, cookie: str | None = None):
        return self.harness.request(
            "POST", "/owner/api/auth/client-key", body, cookie=cookie or self.token
        )

    def test_the_model_api_is_keyless_by_default(self) -> None:
        self.assertFalse(self.harness.owner_auth.client_key_enabled)
        status, payload, _ = self.harness.request("GET", "/v1/models")
        self.assertEqual(status, 200, payload)
        for method, path in MODEL_ENDPOINTS:
            if (method, path) == ("GET", "/v1/models"):
                continue
            status, _, _ = self.harness.request(method, path, {})
            self.assertNotEqual(status, 401, f"{method} {path} must not demand a key")

    def test_set_requires_the_key_on_every_model_endpoint(self) -> None:
        status, payload, _ = self.client_key_action({"action": "set", "key": CLIENT_KEY})
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["client_key"]["enabled"])
        self.assertIn("does not modify any client configuration", payload["impact"])
        self.assertNotIn(CLIENT_KEY, json.dumps(payload))

        for method, path in MODEL_ENDPOINTS:
            status, payload, headers = self.harness.request(method, path, {})
            self.assertEqual(status, 401, f"{method} {path} -> {payload}")
            self.assertEqual(payload["error"]["type"], "invalid_api_key")
            self.assertIsNotNone(header(headers, "WWW-Authenticate"))
            self.assertNotIn(CLIENT_KEY, json.dumps(payload))

        # The right key opens every one of them; wrong keys do not.
        for method, path in MODEL_ENDPOINTS:
            status, payload, _ = self.harness.request(
                method,
                path,
                {} if method == "POST" else None,
                headers=[("Authorization", f"Bearer {CLIENT_KEY}")],
            )
            self.assertNotEqual(status, 401, f"{method} {path} with the key -> {payload}")
        status, payload, _ = self.harness.request(
            "GET", "/v1/models", headers=[("Authorization", "Bearer not-the-key")]
        )
        self.assertEqual(status, 401, payload)

        # Owner auth and the key are different boundaries: a session cookie
        # does not substitute for the key, and /version stays public.
        status, _, _ = self.harness.request("GET", "/version")
        self.assertEqual(status, 200)
        status, payload, _ = self.harness.request("GET", "/v1/models", cookie=self.token)
        self.assertEqual(status, 401, payload)

    def test_rotation_keeps_the_previous_key_valid_for_the_grace_window(self) -> None:
        status, _, _ = self.client_key_action({"action": "set", "key": CLIENT_KEY})
        self.assertEqual(status, 200)

        status, payload, _ = self.client_key_action({"action": "rotate", "key": ROTATED_KEY})
        self.assertEqual(status, 200, payload)
        grace = payload["client_key"]["previous_key_valid_for_seconds"]
        self.assertIsNotNone(grace)
        self.assertGreater(grace, 0)
        self.assertLessEqual(grace, CLIENT_KEY_GRACE_SECONDS)
        self.assertIn("previous key keeps working", payload["impact"])
        self.assertNotIn(ROTATED_KEY, json.dumps(payload))
        self.assertNotIn(CLIENT_KEY, json.dumps(payload))

        # Both keys work while the window is open - that is the point.
        for key in (CLIENT_KEY, ROTATED_KEY):
            status, _, _ = self.harness.request(
                "GET", "/v1/models", headers=[("Authorization", f"Bearer {key}")]
            )
            self.assertEqual(status, 200, key)

        self.harness.now += CLIENT_KEY_GRACE_SECONDS + 1
        status, _, _ = self.harness.request(
            "GET", "/v1/models", headers=[("Authorization", f"Bearer {CLIENT_KEY}")]
        )
        self.assertEqual(status, 401, "the rotated-out key must stop working")
        status, _, _ = self.harness.request(
            "GET", "/v1/models", headers=[("Authorization", f"Bearer {ROTATED_KEY}")]
        )
        self.assertEqual(status, 200)

    def test_clearing_the_key_restores_keyless_access(self) -> None:
        self.client_key_action({"action": "set", "key": CLIENT_KEY})
        status, _, _ = self.harness.request("GET", "/v1/models")
        self.assertEqual(status, 401)

        status, payload, _ = self.client_key_action({"action": "clear"})
        self.assertEqual(status, 200, payload)
        self.assertFalse(payload["client_key"]["enabled"])
        self.assertIn("keyless again", payload["impact"])

        status, payload, _ = self.harness.request("GET", "/v1/models")
        self.assertEqual(status, 200, payload)

        # And a fresh key can be set again afterwards.
        status, payload, _ = self.client_key_action({"action": "set", "key": CLIENT_KEY})
        self.assertEqual(status, 200, payload)

    def test_a_second_set_is_refused_and_tells_the_owner_to_rotate(self) -> None:
        self.client_key_action({"action": "set", "key": CLIENT_KEY})
        status, payload, _ = self.client_key_action({"action": "set", "key": ROTATED_KEY})
        self.assertEqual(status, 409, payload)
        self.assertEqual(payload["error"]["type"], "client_key_already_set")

    def test_no_secret_is_ever_echoed_by_a_read(self) -> None:
        self.client_key_action({"action": "set", "key": CLIENT_KEY})

        for method, path, body in (
            ("GET", "/owner/api/auth/session", None),
            ("GET", "/owner/api/state", None),
            ("POST", "/owner/api/auth/client-key", {"action": "set", "key": CLIENT_KEY}),
        ):
            status, payload, _ = self.harness.request(method, path, body, cookie=self.token)
            body_text = json.dumps(payload)
            self.assertNotIn(CLIENT_KEY, body_text, path)
            self.assertNotIn("pbkdf2", body_text, path)
            self.assertNotIn(self.token, body_text, path)

        # The stored file carries hashes, never the values.
        stored = (self.harness.data_dir / "owner_auth.json").read_bytes()
        self.assertNotIn(CLIENT_KEY.encode(), stored)
        self.assertNotIn(OWNER_PASSWORD.encode(), stored)
        self.assertNotIn(self.token.encode(), stored)

    def test_concurrent_rotations_leave_one_complete_record(self) -> None:
        self.client_key_action({"action": "set", "key": CLIENT_KEY})
        errors: list[BaseException] = []

        def rotate(index: int) -> None:
            try:
                self.harness.owner_auth.rotate_client_key(f"rotation-{index}-" + "c" * 32)
            except BaseException as exc:  # noqa: BLE001 - the test reports it
                errors.append(exc)

        threads = [threading.Thread(target=rotate, args=(i,)) for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        # The store re-loads as one coherent record: account kept, one
        # current key, no torn write.
        reloaded = OwnerAuthStore(self.harness.data_dir, clock=lambda: self.harness.now)
        self.assertFalse(reloaded.setup_required)
        self.assertTrue(reloaded.client_key_enabled)
        status = reloaded.client_key_status()
        self.assertIn("display_id", status)
        self.assertNotIn("key_hash", status)
        # The password still works after concurrent key writes.
        self.assertIsNotNone(reloaded.login("owner", OWNER_PASSWORD))


class SessionExpiryTests(unittest.TestCase):
    def test_an_expired_session_is_refused(self) -> None:
        harness = OwnerAuthHarness()
        self.addCleanup(harness.close)
        token = harness.complete_setup()
        status, _, _ = harness.request("GET", "/owner/api/state", cookie=token)
        self.assertEqual(status, 200)

        harness.now += SESSION_TTL_SECONDS + 1
        status, payload, _ = harness.request("GET", "/owner/api/state", cookie=token)
        self.assertEqual(status, 401, payload)
        self.assertEqual(payload["error"]["type"], "authentication_required")


class StoreHardeningTests(unittest.TestCase):
    def test_pbkdf2_work_factor_is_the_module_constant(self) -> None:
        from gateway.owner_auth import hash_password

        stored = hash_password(OWNER_PASSWORD)
        self.assertTrue(stored.startswith(f"pbkdf2_sha256${DEFAULT_PBKDF2_ITERATIONS}$"))

    def test_a_corrupt_store_fails_closed_to_setup_required(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            (data_dir / "owner_auth.json").write_text("{not json", encoding="utf-8")
            store = OwnerAuthStore(data_dir, clock=lambda: 0.0)
            self.assertTrue(store.setup_required)
            self.assertFalse(store.client_key_enabled)
            with self.assertRaises(Exception) as caught:
                store.login("owner", OWNER_PASSWORD)
            self.assertEqual(caught.exception.__class__.__name__, "OwnerAuthError")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
