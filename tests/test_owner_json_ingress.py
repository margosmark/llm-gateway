"""The owner JSON ingress: one choke closes the two pre-existing QA Lows.

Origin: QA's informational note on ``e0cb9b1`` (re-gate ``t_467c85f5``,
2026-11-04) - ``POST /owner/api/provider`` answered **500** for a truthy
non-object JSON body on the candidate *and* on ``origin/main 2f32436``,
i.e. pre-existing rather than introduced by that delta. Reproduced again
on ``main 5db94dc7`` and fixed here (card ``t_43f339ee``):

* **L2** - ``payload = request.json() or {}`` only catches ``ValueError``
  and only replaces *falsy* values, so a string, array, number or ``true``
  body survived into the handler and died at ``payload.get(...)``; the
  dispatch boundary answered the canned 500 ``internal error`` and logged
  ``unhandled AttributeError at the dispatch boundary``. Four routes were
  reachable: ``/owner/api/{provider,model,profile,settings}``.
* **L1** - the request ``Content-Type`` was never validated: a
  ``text/plain``, form-encoded or entirely absent Content-Type body was
  decoded and dispatched exactly like ``application/json``.

Both stop at ``GatewayApp._owner_json_media_type`` /
``GatewayApp._owner_json_object`` with ``400 invalid_request``, after the
security and access-control gates (an unauthenticated caller still gets
its 401) and before any mutation code, so a refused body never writes
``config.json``.

Every case here is red on ``5db94dc7``: the L2 cases as a 500
``gateway_error``, the L1 cases as a processed 404 (or a 500 for the
non-object under ``text/plain``).
"""
from __future__ import annotations

import http.client
import json
import unittest
from typing import Any

from gateway.owner_auth import COOKIE_NAME
from tests.test_end_to_end import FakeAdapter, GatewayHarness

#: The four routes QA's sweep showed answering 500 for a non-object body.
FIVE_HUNDRED_ROUTES = (
    "/owner/api/provider",
    "/owner/api/model",
    "/owner/api/profile",
    "/owner/api/settings",
)

#: Every owner route that can carry a JSON body - the L1 sweep, including
#: ``logout`` (it never reads the body, but the media-type gate still
#: covers it) and the routes whose own guards already answered 400.
OWNER_JSON_ROUTES = (
    "/owner/api/provider",
    "/owner/api/model",
    "/owner/api/alias",
    "/owner/api/alias-group",
    "/owner/api/profile",
    "/owner/api/settings",
    "/owner/api/backup/import",
    "/owner/api/preview",
    "/owner/api/capabilities/preview",
    "/owner/api/capabilities/run",
    "/owner/api/capabilities/decision",
    "/owner/api/capabilities/probe/cancel",
    "/owner/api/auth/setup",
    "/owner/api/auth/login",
    "/owner/api/auth/logout",
    "/owner/api/auth/password",
    "/owner/api/auth/client-key",
    "/owner/api/machine-keys",
)

#: Truthy non-objects: these passed the old ``or {}`` fallback and reached
#: ``payload.get(...)``.
TRUTHY_NON_OBJECTS: dict[str, bytes] = {
    "string": b'"qa-string-payload"',
    "array": b"[1, 2]",
    "number": b"5",
    "true": b"true",
}

#: Falsy non-objects: these used to fall through the ``or {}`` fallback to
#: the handler by accident and answered 404/400 - the evidence QA used to
#: show the guard was only that fallback. The choke refuses them
#: explicitly now.
FALSY_NON_OBJECTS: dict[str, bytes] = {
    "null": b"null",
    "false": b"false",
    "empty array": b"[]",
    "malformed": b"not json at all",
}


class OwnerJsonIngressTests(unittest.TestCase):
    """L2: a non-object body is a 400, never a 500, and never a write."""

    def setUp(self) -> None:
        self.harness = GatewayHarness(FakeAdapter())
        self.addCleanup(self.harness.close)

    def _post(self, path: str, body: Any, **kwargs: Any) -> tuple[int, Any, Any]:
        status, payload, headers = self.harness.request("POST", path, body=body, **kwargs)
        # The harness's return type is a union over ``raw``; every case
        # here wants the parsed JSON payload.
        assert isinstance(payload, dict), payload
        return status, payload, headers

    def _config_bytes(self) -> bytes:
        return (self.harness.data_dir / "config.json").read_bytes()

    def test_truthy_non_object_bodies_are_400_invalid_request(self) -> None:
        config_before = self._config_bytes()
        revision_before = self.harness.config.snapshot().revision
        for path in FIVE_HUNDRED_ROUTES:
            for name, body in TRUTHY_NON_OBJECTS.items():
                with self.subTest(path=path, body=name):
                    status, payload, _ = self._post(path, body)
                    self.assertEqual(status, 400, payload)
                    error = payload["error"]
                    self.assertEqual(error["type"], "invalid_request", payload)
                    self.assertIn("JSON object", error["message"], payload)
        self.assertEqual(self._config_bytes(), config_before, "a refused body must not write config.json")
        self.assertEqual(self.harness.config.snapshot().revision, revision_before)

    def test_the_routes_that_were_already_guarded_keep_their_400s(self) -> None:
        already_guarded = (
            "/owner/api/alias",
            "/owner/api/alias-group",
            "/owner/api/backup/import",
            "/owner/api/capabilities/preview",
            "/owner/api/auth/setup",
            "/owner/api/auth/client-key",
            "/owner/api/machine-keys",
        )
        for path in already_guarded:
            for name, body in TRUTHY_NON_OBJECTS.items():
                with self.subTest(path=path, body=name):
                    status, payload, _ = self._post(path, body)
                    self.assertEqual(status, 400, payload)
                    self.assertNotEqual(payload["error"]["type"], "gateway_error", payload)

    def test_an_object_body_keeps_the_answer_it_had_before(self) -> None:
        status, payload, _ = self._post("/owner/api/provider", {"action": "bogus", "provider_id": "nope"})
        self.assertEqual(status, 404, payload)
        self.assertEqual(payload["error"]["type"], "unknown_provider", payload)
        for path in ("/owner/api/model", "/owner/api/profile", "/owner/api/settings"):
            with self.subTest(path=path):
                status, payload, _ = self._post(path, {"action": "bogus"})
                self.assertEqual(status, 400, payload)
                self.assertNotEqual(payload["error"]["type"], "gateway_error", payload)

    def test_falsy_and_malformed_bodies_are_400_too(self) -> None:
        for name, body in FALSY_NON_OBJECTS.items():
            with self.subTest(body=name):
                status, payload, _ = self._post("/owner/api/provider", body)
                self.assertEqual(status, 400, payload)
                self.assertEqual(payload["error"]["type"], "invalid_request", payload)

    def test_an_empty_body_still_means_the_empty_payload(self) -> None:
        # Nothing to decode and no media type to validate: the handler
        # sees the empty payload it has always seen (404 unknown_provider
        # for an empty action), so this behaviour is pinned rather than
        # silently tightened.
        status, payload, _ = self._post("/owner/api/provider", None)
        self.assertEqual(status, 404, payload)
        self.assertEqual(payload["error"]["type"], "unknown_provider", payload)

    def test_unauthenticated_still_gets_401_before_the_body_is_read(self) -> None:
        for path in ("/owner/api/provider", "/owner/api/model"):
            with self.subTest(path=path):
                status, payload, _ = self._post(path, TRUTHY_NON_OBJECTS["string"], auth=False)
                self.assertEqual(status, 401, payload)
                self.assertEqual(payload["error"]["type"], "authentication_required", payload)


class OwnerContentTypeTests(unittest.TestCase):
    """L1: the request Content-Type is validated on the owner surface."""

    def setUp(self) -> None:
        self.harness = GatewayHarness(FakeAdapter())
        self.addCleanup(self.harness.close)

    def _post_raw(
        self,
        path: str,
        body: bytes,
        *,
        content_type: str | None,
        cookie: bool = True,
    ) -> tuple[int, dict[str, Any]]:
        """POST over a real socket with exactly the headers given.

        ``content_type=None`` sends no ``Content-Type`` header at all: the
        harness (and ``urllib``) always set one, so the missing-header
        case has to be built by hand. ``http.client`` adds ``Content-Length``
        for us but no default media type.
        """
        connection = http.client.HTTPConnection(self.harness.authority, self.harness.port, timeout=10)
        try:
            headers: dict[str, str] = {
                "Origin": f"http://{self.harness.authority}:{self.harness.port}",
            }
            if cookie:
                headers["Cookie"] = f"{COOKIE_NAME}={self.harness.owner_cookie}"
            if content_type is not None:
                headers["Content-Type"] = content_type
            connection.request("POST", path, body=body, headers=headers)
            response = connection.getresponse()
            raw = response.read()
            status = response.status
        finally:
            connection.close()
        return status, json.loads(raw.decode("utf-8") or "{}")

    def _assert_media_type_refusal(self, status: int, payload: dict[str, Any], *, path: str) -> None:
        self.assertEqual(status, 400, f"{path}: {payload}")
        error = payload["error"]
        self.assertEqual(error["type"], "invalid_request", payload)
        self.assertIn("Content-Type", error["message"], payload)

    def test_a_non_json_content_type_is_refused(self) -> None:
        body = b'{"action": "bogus", "provider_id": "nope"}'
        for content_type in (
            "text/plain",
            "application/x-www-form-urlencoded",
            "multipart/form-data; boundary=boundary",
        ):
            with self.subTest(content_type=content_type):
                status, payload = self._post_raw("/owner/api/provider", body, content_type=content_type)
                self._assert_media_type_refusal(status, payload, path="/owner/api/provider")

    def test_a_missing_content_type_is_refused(self) -> None:
        body = b'{"action": "bogus", "provider_id": "nope"}'
        status, payload = self._post_raw("/owner/api/provider", body, content_type=None)
        self._assert_media_type_refusal(status, payload, path="/owner/api/provider")

    def test_the_charset_parameter_is_accepted(self) -> None:
        body = b'{"action": "bogus", "provider_id": "nope"}'
        status, payload = self._post_raw(
            "/owner/api/provider", body, content_type="application/json; charset=utf-8"
        )
        self.assertEqual(status, 404, payload)
        self.assertEqual(payload["error"]["type"], "unknown_provider", payload)

    def test_a_non_object_body_under_text_plain_is_a_400_not_a_500(self) -> None:
        # QA case L1d: the media-type gate now refuses it before the body
        # is even decoded.
        status, payload = self._post_raw("/owner/api/provider", b'"qa-string-payload"', content_type="text/plain")
        self._assert_media_type_refusal(status, payload, path="/owner/api/provider")

    def test_the_gate_covers_every_owner_route_that_can_carry_a_body(self) -> None:
        body = b'{"action": "bogus"}'
        for path in OWNER_JSON_ROUTES:
            with self.subTest(path=path):
                status, payload = self._post_raw(path, body, content_type="text/plain")
                self._assert_media_type_refusal(status, payload, path=path)

    def test_the_gate_runs_after_authentication(self) -> None:
        status, payload = self._post_raw(
            "/owner/api/provider", b'{"action": "bogus"}', content_type="text/plain", cookie=False
        )
        self.assertEqual(status, 401, payload)
        self.assertEqual(payload["error"]["type"], "authentication_required", payload)

    def test_a_json_object_with_the_right_content_type_still_flows(self) -> None:
        body = b'{"action": "bogus", "provider_id": "nope"}'
        status, payload = self._post_raw("/owner/api/provider", body, content_type="application/json")
        self.assertEqual(status, 404, payload)
        self.assertEqual(payload["error"]["type"], "unknown_provider", payload)
