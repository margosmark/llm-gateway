"""``owner_console_auth``: the owner-console session gate, and its opt-out.

The flag turns a security control OFF, so the failure this suite exists to
prove impossible is failing open *by accident*: a typo, a missing key, a
wrong type or a drifted default must never leave an unauthenticated admin
console on the LAN.

Covered here:

* the four stored states (absent / invalid / true / false) plus a hostile
  parse, through the real ``GatewayConfig.from_dict`` round-trip;
* session and state truth in both modes, including the persistent
  machine-readable marker when the gate is off;
* the critical fence: the machine key (``gateway-owner-mcp``) and the model
  client key behave *identically* in both modes;
* no secret material in any owner response, in either mode;
* the route table itself: identical in both modes, every non-public owner
  route refused by the gate when it is on, none refused by the gate when it
  is off.

This file adds to the suite; it does not modify an existing test.
"""
from __future__ import annotations

import json
import unittest
from typing import ClassVar

from gateway.config import GatewayConfig
from tests.owner_session import FIXTURE_PASSWORD
from tests.test_end_to_end import GatewayHarness

# Values a careless operator (or a careless config) can produce. Every one of
# them must leave the gate ON. The "helpful" spellings - "false", "no", 0 -
# are the hostile cases: they read as "off" to a human and must not read as
# "off" to the parser.
HOSTILE_VALUES = [
    "no", "No", "NO", "off", "disabled", "false", "False", "FALSE",
    0, 1, 2, -1, 0.0, "", "null", "None", "none",
    [], [False], [0], {}, {"value": False}, {"owner_console_auth": False},
    {"nested": {"owner_console_auth": False}},
]

# The client key validator wants 32-256 characters; keep these long enough
# that a length complaint cannot mask the behaviour under test.
CLIENT_KEY = "sk-client-key-fence-0123456789abcdef0123456789abcdef"


def error_type(payload) -> str | None:
    """The ``type`` of a gateway error, in either envelope it uses."""
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    if isinstance(error, dict):
        return error.get("type")
    if isinstance(error, str):
        return error
    return payload.get("type")


def error_message(payload) -> str | None:
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    if isinstance(error, dict):
        return error.get("message")
    return payload.get("message")


def request(harness: GatewayHarness, method: str, path: str, body=None, **kwargs):
    """``harness.request`` that survives a non-JSON 200.

    The harness only decodes a non-JSON body on the error path, and the
    console's static and CSV routes answer 200 with text.
    """
    try:
        return harness.request(method, path, body, **kwargs)
    except json.JSONDecodeError:
        if method != "GET":
            raise
        status, raw, headers = harness.request(method, path, body, raw=True, **kwargs)
        return status, raw.decode("utf-8", "replace"), headers


def gate_state(harness: GatewayHarness, value, *, absent: bool = False):
    """Store ``value`` through the real ConfigStore round-trip."""
    if not absent:
        with harness.config.mutate() as config:
            config.owner_console_auth = value
    return harness


class FailClosedParseTests(unittest.TestCase):
    """The acceptance item: only literal ``false`` ever opens the console."""

    def test_the_default_is_on(self) -> None:
        self.assertIs(GatewayConfig().owner_console_auth, True)

    def test_absent_key_means_auth_required(self) -> None:
        self.assertIs(GatewayConfig.from_dict({}).owner_console_auth, True)

    def test_only_literal_false_disables_the_gate(self) -> None:
        self.assertIs(
            GatewayConfig.from_dict({"owner_console_auth": False}).owner_console_auth, False
        )
        self.assertIs(
            GatewayConfig.from_dict({"owner_console_auth": True}).owner_console_auth, True
        )

    def test_every_hostile_value_fails_closed(self) -> None:
        for value in HOSTILE_VALUES:
            with self.subTest(value=repr(value)):
                config = GatewayConfig.from_dict({"owner_console_auth": value})
                self.assertIs(config.owner_console_auth, True, repr(value))

    def test_a_hostile_payload_never_raises_past_the_gate(self) -> None:
        # An exception thrown while parsing must not become a config that
        # skips the gate; from_dict has to answer for any payload it is given.
        for value in HOSTILE_VALUES + [[[[[[False]]]]], {"a": {"b": {"c": [None, False]}}}]:
            with self.subTest(value=repr(value)):
                config = GatewayConfig.from_dict({"owner_console_auth": value})
                self.assertIs(config.owner_console_auth, True)

    def test_a_deliberate_opt_out_survives_persistence(self) -> None:
        # The operator's own choice must not be quietly re-enabled on reload,
        # and a config written without the key must stay fail-closed.
        config = GatewayConfig.from_dict({"owner_console_auth": False})
        reloaded = GatewayConfig.from_dict(json.loads(json.dumps(config.to_dict())))
        self.assertIs(reloaded.owner_console_auth, False)
        default = GatewayConfig.from_dict(
            json.loads(json.dumps(GatewayConfig.from_dict({}).to_dict()))
        )
        self.assertIs(default.owner_console_auth, True)


class GateMatrixTests(unittest.TestCase):
    """Each stored state, against the live session and state endpoints."""

    # (stored value, gate off?)
    STATES: ClassVar[list[tuple[object, bool]]] = [
        (None, False),   # absent - fail closed
        ("no", False),   # hostile - fail closed
        (0, False),      # hostile - fail closed
        (True, False),   # the default, exactly today's behaviour
        (False, True),   # the opt-out
    ]

    def _harness(self, value) -> GatewayHarness:
        harness = GatewayHarness()
        self.addCleanup(harness.close)
        gate_state(harness, value, absent=value is None)
        return harness

    def test_the_gate_refuses_an_anonymous_owner_request_unless_the_flag_is_false(self) -> None:
        for value, gate_off in self.STATES:
            with self.subTest(value=repr(value)):
                harness = self._harness(value)
                status, payload, _ = request(harness, "GET", "/owner/api/state", auth=False)
                if gate_off:
                    self.assertEqual(status, 200)
                    self.assertEqual(payload["console_auth"], "disabled")
                    # Never claims a session it did not establish.
                    self.assertIs(payload["authenticated"], False)
                    self.assertIsNone(payload["owner"]["username"])
                else:
                    self.assertEqual(status, 401)
                    self.assertEqual(error_type(payload), "authentication_required")
                    self.assertEqual(error_message(payload), "owner sign-in required")

    def test_the_session_endpoint_reports_the_mode_and_the_session_truthfully(self) -> None:
        for value, gate_off in self.STATES:
            with self.subTest(value=repr(value)):
                harness = self._harness(value)
                status, session, _ = request(
                    harness, "GET", "/owner/api/auth/session", auth=False
                )
                self.assertEqual(status, 200)
                self.assertEqual(
                    session["console_auth"], "disabled" if gate_off else "enabled"
                )
                # Session truth, not mode verdict: nobody signed in here.
                self.assertIs(session["authenticated"], False)
                self.assertIsNone(session["username"])

                signed_in, session, _ = request(
                    harness, "GET", "/owner/api/auth/session", auth=True
                )
                self.assertEqual(signed_in, 200)
                self.assertIs(session["authenticated"], True)
                self.assertEqual(session["username"], "owner")

    def test_a_signed_in_owner_keeps_working_in_both_modes(self) -> None:
        for value, gate_off in self.STATES:
            with self.subTest(value=repr(value)):
                harness = self._harness(value)
                status, state, _ = request(harness, "GET", "/owner/api/state", auth=True)
                self.assertEqual(status, 200)
                self.assertIs(state["authenticated"], True)
                self.assertEqual(state["owner"]["username"], "owner")
                self.assertTrue(state["can_mutate"])
                if gate_off:
                    self.assertEqual(state["console_auth"], "disabled")
                    self.assertIn("sign-in is OFF", state["access_mode_description"])
                else:
                    # The default payload stays exactly as the MCP contract
                    # pins it: no extra key, no version bump.
                    self.assertNotIn("console_auth", state)
                    self.assertNotIn("sign-in is OFF", state["access_mode_description"])


class RouteTableTests(unittest.TestCase):
    """The diff must be the gate, not new surface."""

    @staticmethod
    def _routes(harness: GatewayHarness):
        return sorted(
            {
                (method, route.name)
                for route in harness.app._routes._routes
                for method in sorted(route.methods)
                # The gate runs before the handler for every method; HEAD and
                # OPTIONS add nothing but urllib noise here.
                if method not in {"HEAD", "OPTIONS"}
            }
        )

    @classmethod
    def _owner_routes(cls, harness: GatewayHarness):
        return [
            (method, path) for method, path in cls._routes(harness)
            if path.startswith("/owner")
        ]

    def test_the_flag_does_not_add_or_remove_a_route(self) -> None:
        on = GatewayHarness()
        self.addCleanup(on.close)
        off = GatewayHarness()
        self.addCleanup(off.close)
        gate_state(off, False)
        self.assertEqual(self._routes(on), self._routes(off))
        self.assertTrue(self._owner_routes(on))

    def test_with_the_gate_on_every_non_public_owner_route_refuses_an_anonymous_call(self) -> None:
        harness = GatewayHarness()
        self.addCleanup(harness.close)
        public = set(harness.app._public_owner_paths)
        checked = 0
        for method, path in self._owner_routes(harness):
            if path in public:
                continue
            with self.subTest(method=method, path=path):
                status, payload, _ = request(
                    harness, method, path, {} if method == "POST" else None, auth=False
                )
                self.assertEqual(status, 401)
                self.assertEqual(error_type(payload), "authentication_required")
                self.assertEqual(error_message(payload), "owner sign-in required")
                checked += 1
        self.assertGreater(checked, 20, "the owner route table shrank")

    def test_with_the_gate_off_no_owner_route_is_refused_by_the_gate(self) -> None:
        harness = GatewayHarness()
        self.addCleanup(harness.close)
        gate_state(harness, False)
        for method, path in self._owner_routes(harness):
            with self.subTest(method=method, path=path):
                status, payload, _ = request(
                    harness, method, path, {} if method == "POST" else None, auth=False
                )
                refused = (
                    status == 401
                    and error_type(payload) == "authentication_required"
                    and error_message(payload) == "owner sign-in required"
                )
                self.assertFalse(
                    refused,
                    f"{method} {path} still hit the session gate with the flag off "
                    f"({status} {payload!r})",
                )

    def test_the_public_surface_is_the_same_in_both_modes(self) -> None:
        # The sign-in view's own assets must stay public in both modes, or
        # the console stops loading; and the auth endpoints stay public and
        # stay useless without credentials.
        expected_api = {
            "/owner/api/auth/login", "/owner/api/auth/session", "/owner/api/auth/setup",
        }
        for gate_off in (False, True):
            with self.subTest(gate_off=gate_off):
                harness = GatewayHarness()
                self.addCleanup(harness.close)
                if gate_off:
                    gate_state(harness, False)
                public = set(harness.app._public_owner_paths)
                self.assertEqual(
                    {path for path in public if path.startswith("/owner/api/")},
                    expected_api,
                )
                for asset in ("/owner", "/owner/", "/owner/index.html",
                              "/owner/app.js", "/owner/style.css"):
                    self.assertIn(asset, public)

                status, payload, _ = request(
                    harness, "POST", "/owner/api/auth/login",
                    {"username": "", "password": ""}, auth=False,
                )
                # Refused by the credential check, never by the session
                # gate - in either mode.
                self.assertEqual(status, 401)
                self.assertEqual(error_type(payload), "invalid_credentials")
                self.assertNotEqual(error_message(payload), "owner sign-in required")


class MachineKeyFenceTests(unittest.TestCase):
    """Critical fence: the machine key is a different boundary, flag or no."""

    def _harness(self, gate_off: bool) -> GatewayHarness:
        harness = GatewayHarness()
        self.addCleanup(harness.close)
        if gate_off:
            gate_state(harness, False)
        return harness

    @staticmethod
    def _write_body() -> dict:
        return {"alias": "fence", "provider": "fake", "model": "synthetic",
                "upstream": "fake"}

    @classmethod
    def _probes(cls, harness: GatewayHarness, read_key: str, write_key: str):
        """Every machine-key probe, as comparable (status, type, message)."""
        results = {}

        def record(name, *args, **kwargs):
            status, payload, _ = request(harness, *args, **kwargs)
            results[name] = (status, error_type(payload), error_message(payload))
            return status, payload

        record(
            "unknown_key_write",
            "POST", "/owner/api/alias", cls._write_body(),
            headers={"Authorization": "Bearer not-a-real-key"}, auth=False,
        )
        record(
            "read_scope_write",
            "POST", "/owner/api/alias", cls._write_body(),
            headers={"Authorization": "Bearer " + read_key}, auth=False,
        )
        record(
            "write_scope_initialize",
            "POST", "/owner/api/settings",
            {"action": "initialize", "provider": "fake", "model": "synthetic"},
            headers={"Authorization": "Bearer " + write_key}, auth=False,
        )
        record(
            "state_with_read_key",
            "GET", "/owner/api/state", None,
            headers={"Authorization": "Bearer " + read_key}, auth=False,
        )
        record(
            "machine_only_endpoint",
            "POST", "/owner/api/machine-keys",
            {"action": "create", "name": "nope", "scope": "read"},
            headers={"Authorization": "Bearer " + write_key}, auth=False,
        )
        record(
            "cookie_and_machine_key",
            "POST", "/owner/api/auth/logout", {},
            headers={"Authorization": "Bearer " + write_key}, auth=True,
        )
        record(
            "flag_flip_with_write_key",
            "POST", "/owner/api/settings",
            {"action": "owner_console_auth", "required": False},
            headers={"Authorization": "Bearer " + write_key}, auth=False,
        )
        return results

    def test_the_flag_does_not_change_machine_key_behaviour(self) -> None:
        results = {}
        for gate_off in (False, True):
            with self.subTest(gate_off=gate_off):
                harness = self._harness(gate_off)
                read_key = harness.app.machine_keys.create("fence-read", "read")["key"]
                write_key = harness.app.machine_keys.create("fence-write", "write")["key"]
                results[gate_off] = self._probes(harness, read_key, write_key)

                # The fence itself, spelled out: an unknown machine key is
                # refused by the machine path even with the gate off (the
                # cookie is never a fallback there), a read-scoped key still
                # cannot write, and a write-scoped key still can.
                self.assertEqual(results[gate_off]["unknown_key_write"][0], 401)
                self.assertEqual(results[gate_off]["unknown_key_write"][1],
                                 "authentication_required")
                self.assertEqual(results[gate_off]["read_scope_write"][0], 403)
                self.assertEqual(results[gate_off]["read_scope_write"][1], "scope_required")
                self.assertEqual(results[gate_off]["write_scope_initialize"][0], 200)
                self.assertEqual(results[gate_off]["state_with_read_key"][0], 200)
                # Human-session-only endpoint: a machine key is refused
                # outright (401 no-cookie shape or 403 explicit), same in
                # both modes - the comparison below is the real assertion.
                self.assertIn(results[gate_off]["machine_only_endpoint"][0], (401, 403))
                self.assertEqual(results[gate_off]["cookie_and_machine_key"][0], 403)
                self.assertEqual(results[gate_off]["cookie_and_machine_key"][1],
                                 "cookie_rejected")

        # With auth on nothing changes: byte-identical outcomes in both modes.
        self.assertEqual(results[False], results[True])

    def test_a_machine_key_cannot_flip_the_flag(self) -> None:
        for gate_off in (False, True):
            with self.subTest(gate_off=gate_off):
                harness = self._harness(gate_off)
                write_key = harness.app.machine_keys.create("flag-flipper", "write")["key"]
                status, payload, _ = request(
                    harness, "POST", "/owner/api/settings",
                    {"action": "owner_console_auth", "required": False},
                    headers={"Authorization": "Bearer " + write_key}, auth=False,
                )
                self.assertEqual(status, 403, payload)
                self.assertEqual(error_type(payload), "machine_key_forbidden")
                self.assertEqual(
                    error_message(payload),
                    "machine keys cannot change the owner console login boundary",
                )
                # ... and the flag did not move: still on, still off.
                self.assertIs(
                    harness.config.snapshot().owner_console_auth, not gate_off
                )


class ClientKeyFenceTests(unittest.TestCase):
    """Critical fence: the model client key is a different boundary."""

    def test_the_model_client_key_is_independent_of_the_flag(self) -> None:
        results = {}
        for gate_off in (False, True):
            with self.subTest(gate_off=gate_off):
                harness = GatewayHarness()
                self.addCleanup(harness.close)
                if gate_off:
                    gate_state(harness, False)
                harness.app.owner_auth.set_client_key(CLIENT_KEY)

                probes = {}
                for name, headers in (
                    ("no_key", None),
                    ("right_key", {"Authorization": "Bearer " + CLIENT_KEY}),
                    ("wrong_key", {"Authorization": "Bearer sk-wrong"}),
                ):
                    status, payload, _ = request(
                        harness, "GET", "/v1/models", headers=headers, auth=False
                    )
                    probes[name] = (status, error_type(payload), error_message(payload))
                results[gate_off] = probes

                self.assertEqual(probes["no_key"][0], 401)
                self.assertEqual(probes["no_key"][1], "invalid_api_key")
                self.assertEqual(probes["right_key"][0], 200)
                self.assertEqual(probes["wrong_key"][0], 401)
                self.assertEqual(probes["wrong_key"][1], "invalid_api_key")

        # The console gate does not answer for /v1, in either mode.
        self.assertEqual(results[False], results[True])


class NoLeakTests(unittest.TestCase):
    """No password hash, setup code, machine-key material or session secret
    in any owner response, in either mode."""

    def _owner_get_paths(self, harness: GatewayHarness):
        paths = {"/owner/", "/owner/app.js"}
        for route in harness.app._routes._routes:
            if route.name.startswith("/owner") and "GET" in route.methods:
                paths.add(route.name)
        return sorted(paths)

    def test_no_secret_material_survives_in_either_mode(self) -> None:
        for gate_off in (False, True):
            with self.subTest(gate_off=gate_off):
                harness = GatewayHarness()
                self.addCleanup(harness.close)
                if gate_off:
                    gate_state(harness, False)
                harness.app.owner_auth.set_client_key(CLIENT_KEY)
                machine_key = harness.app.machine_keys.create("leak-probe", "write")["key"]
                setup_code = harness.app.owner_auth.read_setup_code()

                # Secret VALUES never appear anywhere - not in JSON, not in
                # the console's own HTML/JS.
                value_needles = [
                    harness.owner_cookie,        # the session token
                    machine_key,                 # machine-key plaintext
                    CLIENT_KEY,                  # client-key plaintext
                    FIXTURE_PASSWORD,            # the owner password itself
                ]
                if setup_code:
                    value_needles.append(setup_code)
                # Internal KEY NAMES only have to stay out of JSON: the
                # console's own source legitimately names the setup-code form
                # field it posts.
                key_needles = [
                    "password_hash", "key_hash", "pbkdf2_sha256",
                    "code_hash", "session_secret",
                ]

                for path in self._owner_get_paths(harness):
                    status, payload, _ = request(harness, "GET", path, auth=True)
                    self.assertEqual(status, 200, f"{path}: {status} {payload!r}")
                    blob = payload if isinstance(payload, str) else json.dumps(payload)
                    needles = list(value_needles)
                    if not isinstance(payload, str):
                        needles += key_needles
                    for needle in needles:
                        self.assertNotIn(
                            needle, blob, f"{path} leaked {needle!r} with gate_off={gate_off}"
                        )

                # The one-time setup code is consumed and its host file gone.
                self.assertIsNone(harness.app.owner_auth.read_setup_code())


class ToggleActionTests(unittest.TestCase):
    """Turning the gate off and back on: who may do it, and how."""

    def _harness(self, gate_off: bool = False) -> GatewayHarness:
        harness = GatewayHarness()
        self.addCleanup(harness.close)
        if gate_off:
            gate_state(harness, False)
        return harness

    @staticmethod
    def _toggle(harness: GatewayHarness, required, **kwargs):
        return harness.request(
            "POST", "/owner/api/settings",
            {"action": "owner_console_auth", "required": required}, **kwargs,
        )

    def test_a_signed_in_owner_can_turn_the_gate_off_and_back_on(self) -> None:
        harness = self._harness()
        status, payload, _ = self._toggle(harness, False, auth=True)
        self.assertEqual(status, 200, payload)
        self.assertIs(payload["ok"], True)
        self.assertIs(payload["owner_console_auth"], False)
        self.assertIs(harness.config.snapshot().owner_console_auth, False)

        status, state, _ = request(harness, "GET", "/owner/api/state", auth=False)
        self.assertEqual(status, 200)
        self.assertEqual(state["console_auth"], "disabled")
        self.assertIn("sign-in is OFF", state["access_mode_description"])

        status, payload, _ = self._toggle(harness, True, auth=True)
        self.assertEqual(status, 200, payload)
        self.assertIs(harness.config.snapshot().owner_console_auth, True)
        status, _, _ = request(harness, "GET", "/owner/api/state", auth=False)
        self.assertEqual(status, 401)

    def test_an_anonymous_caller_cannot_turn_the_gate_off(self) -> None:
        harness = self._harness()
        status, payload, _ = self._toggle(harness, False, auth=False)
        self.assertEqual(status, 401)
        self.assertEqual(error_message(payload), "owner sign-in required")
        self.assertIs(harness.config.snapshot().owner_console_auth, True)
        status, _, _ = request(harness, "GET", "/owner/api/state", auth=False)
        self.assertEqual(status, 401)

    def test_the_opt_out_is_fail_closed_against_a_bad_value(self) -> None:
        harness = self._harness()
        for bad in ("false", "no", 0, None, [], {}):
            with self.subTest(required=repr(bad)):
                status, payload, _ = self._toggle(harness, bad, auth=True)
                self.assertEqual(status, 400, payload)
                self.assertEqual(error_type(payload), "invalid_setting")
                self.assertIs(harness.config.snapshot().owner_console_auth, True)
        # ... and none of those attempts opened the console as a side effect.
        status, _, _ = request(harness, "GET", "/owner/api/state", auth=False)
        self.assertEqual(status, 401)

    def test_rearming_the_gate_needs_no_session_but_always_closes_it(self) -> None:
        # With the flag off the settings endpoint is reachable by design -
        # that is the mode. The only value that changes how much protection
        # exists is the re-arm, and it is fail-safe: it always re-arms,
        # because there is nothing left to un-arm.
        harness = self._harness(gate_off=True)
        status, payload, _ = self._toggle(harness, True, auth=False)
        self.assertEqual(status, 200, payload)
        self.assertIs(harness.config.snapshot().owner_console_auth, True)
        status, _, _ = request(harness, "GET", "/owner/api/state", auth=False)
        self.assertEqual(status, 401)


if __name__ == "__main__":
    unittest.main()
