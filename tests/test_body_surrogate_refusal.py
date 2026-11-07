"""F9 rework: a lone surrogate in a request body is refused at one choke.

The security gate on 18a6010 (t_765ecbf0) found the *class* still open:
six client-controlled ingress points raised ``UnicodeEncodeError`` and
surfaced as a bare 500 - ``/v1`` bodies on any real adapter, the owner
model/profile/password forms, and machine-key create, which also wedged
``MachineKeyStore`` in memory because the record was mutated before the
atomic write failed.

The fix is a single choke:

1. ``Request.json`` refuses a body whose strings cannot be UTF-8 encoded
   (:class:`~gateway.http.core.InvalidBodyText`, deliberately **not** a
   ``ValueError``, so no handler's ``except ValueError: payload = {}``
   can swallow the refusal into an empty payload);
2. the dispatch boundary turns it into ``400 invalid_request`` naming the
   field, records the terminal ledger row (``error_class=policy``, with
   the offending text as visible escape text) and writes the access line;
3. ``MachineKeyStore`` rolls back an in-memory mutation whose save failed,
   so a failed write can never wedge later create/rotate/revoke;
4. ``RequestLogger`` encodes with ``backslashreplace`` instead of
   silently dropping the whole line, and a generic 500 records its cause
   as an exception class name.

Regression: every test here is red on 18a6010 (bare 500s, wedged store,
dropped log line, no ledger row) and green on the rework.
"""
from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from gateway.http.core import InvalidBodyText, Request
from gateway.logging import RequestLogger
from gateway.owner_auth import MachineKeyStore, OwnerAuthError
from tests.test_messages_handler import CHAT_MODEL, _build_chat_harness

#: Wire form: a plain ASCII body, exactly what any client can send.
#: ``json.loads`` turns the ``\\ud800`` escape into a lone surrogate.
HOSTILE_CONTENT = (
    b'{"model": "' + CHAT_MODEL.encode() + b'", "max_tokens": 32, '
    b'"messages": [{"role": "user", "content": "msg\\ud800here"}]}'
)
CLEAN_CONTENT = (
    '{"model": "' + CHAT_MODEL + '", "max_tokens": 32, '
    '"messages": [{"role": "user", "content": "héllo 中 🎉"}]}'
).encode("utf-8")


def _request(body: bytes) -> Request:
    return Request("POST", "/probe", "", None, body, ("127.0.0.1", 1), 8765)


class BodyParseChokeTests(unittest.TestCase):
    """The choke itself: refuse unencodable text, never rewrite it."""

    def test_a_lone_surrogate_anywhere_in_the_body_is_refused(self) -> None:
        cases = {
            "value": b'{"content": "msg\\ud800here"}',
            "nested": b'{"messages": [{"content": "a\\ud800b"}]}',
            "list": b'{"stop": ["\\ud800end"]}',
            "key": b'{"weird\\ud800key": 1}',
            "model": b'{"model": "no-such\\ud800alias"}',
            "top_level_string": b'"raw\\ud800text"',
        }
        for name, body in cases.items():
            with self.subTest(case=name):
                with self.assertRaises(InvalidBodyText) as caught:
                    _request(body).json()
                # Not a ValueError: every handler that falls back to an
                # empty payload on ValueError must not swallow this one.
                self.assertNotIsInstance(caught.exception, ValueError)
                self.assertTrue(caught.exception.excerpt)
                self.assertNotIn("\ud800", caught.exception.path)

    def test_the_refusal_carries_escape_text_never_a_raw_surrogate(self) -> None:
        with self.assertRaises(InvalidBodyText) as caught:
            _request(b'{"content": "msg\\ud800here"}').json()
        excerpt = caught.exception.excerpt
        self.assertIn("\\ud800", excerpt)
        self.assertEqual(excerpt.encode("utf-8"), excerpt.encode("utf-8", "backslashreplace"))

    def test_clean_bodies_still_parse_and_bad_json_still_raises_value_error(self) -> None:
        self.assertEqual(
            _request(CLEAN_CONTENT).json()["messages"][0]["content"],
            "héllo 中 🎉",
        )
        with self.assertRaises(ValueError) as caught:
            _request(b"{").json()
        self.assertNotIsInstance(caught.exception, InvalidBodyText)


class HostileRequestBodyTests(unittest.TestCase):
    """The six ingress points: 400 at the choke, no 500, no side effects."""

    def setUp(self) -> None:
        self.harness = _build_chat_harness()
        self.addCleanup(self.harness.close)
        self.log = io.StringIO()
        self._real_logger = self.harness.app.logger
        self.harness.app.logger = RequestLogger(stream=self.log)
        self.addCleanup(setattr, self.harness.app, "logger", self._real_logger)

    def _post(self, path: str, body: bytes) -> tuple[int, Any, Any]:
        return self.harness.request(
            "POST", path, body=body, headers={"Content-Type": "application/json"}
        )

    def _refused(self, path: str, body: bytes) -> dict:
        status, payload, _ = self._post(path, body)
        self.assertEqual(status, 400, payload)
        self.assertEqual(payload["error"]["type"], "invalid_request", payload)
        self.assertIn("field", payload["error"])
        return payload

    def test_hostile_v1_body_is_400_recorded_logged_and_never_forwarded(self) -> None:
        status_before, _, _ = self.harness.request("GET", "/owner/api/state")
        self.assertEqual(status_before, 200)
        rows_before = len(self.harness.app.ledger.recent(limit=50))

        self._refused("/v1/messages", HOSTILE_CONTENT)

        # Nothing downstream may run: no provider call, no route, no 500.
        self.assertEqual(self.harness.adapter.calls, [], "no provider may see the body")
        status_after, _, _ = self.harness.request("GET", "/owner/api/state")
        self.assertEqual(status_after, 200, "one hostile request must not poison the console")

        # Terminal row: the routing layer never ran, so the dispatch
        # boundary records it - with the offending text as escape text.
        rows = self.harness.app.ledger.recent(limit=50)
        self.assertEqual(len(rows), rows_before + 1, "a refusal must leave a ledger row")
        row = rows[0]
        self.assertEqual(row["status"], 400)
        self.assertEqual(row["error_class"], "policy")
        reasons = [str(a.get("reason", "")) for a in row.get("attempts") or []]
        self.assertTrue(any("\\ud800" in reason for reason in reasons), reasons)

        # And the access line exists, naming the error class.
        line = self.log.getvalue()
        self.assertIn('"status":400', line.replace(" ", ""))
        self.assertIn('"error_class":"invalid_request"', line.replace(" ", ""))

    def test_a_deep_but_legal_body_is_refused_not_crashed(self) -> None:
        """Depth is bounded by MAX_JSON_NESTING (3000), not by recursion.

        The parser accepts far more levels than Python's recursion limit,
        so a recursive walk over the parsed payload raised ``RecursionError``
        at ~1000 levels and a deep-but-legal body with a surrogate came back
        as the bare 500 this choke exists to remove (the F6 deep-tool-nesting
        probe caught it mid-rework).
        """
        depth = 2500  # under MAX_JSON_NESTING, past any recursion limit
        body = b"[" * depth + b'"msg\\ud800here"' + b"]" * depth
        status, payload, _ = self._post("/v1/messages", body)
        self.assertEqual(status, 400, payload)
        self.assertEqual(payload["error"]["type"], "invalid_request", payload)
        status, _, _ = self.harness.request("GET", "/owner/api/state")
        self.assertEqual(status, 200)

    def test_clean_unicode_body_still_serves(self) -> None:
        status, payload, _ = self.harness.request(
            "POST", "/v1/messages", body=CLEAN_CONTENT,
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(len(self.harness.adapter.calls), 1)

    def test_owner_forms_are_refused_and_the_console_stays_healthy(self) -> None:
        cases = {
            "model_id": ("/owner/api/model",
                         b'{"action": "manual", "provider_id": "anthropic", "model_id": "m\\ud800id"}'),
            "model_notes": ("/owner/api/model",
                            (b'{"action": "manual", "provider_id": "anthropic", '
                             b'"model_id": "claude-probe", "notes": "n\\ud800tes"}')),
            "profile_name": ("/owner/api/profile",
                             b'{"action": "save", "profile_id": "p1", "name": "p\\ud800name"}'),
            "password": ("/owner/api/auth/password",
                         (b'{"current_password": "fixture-owner-password", '
                          b'"new_password": "np\\ud800ss"}')),
            "machine_key_name": ("/owner/api/machine-keys",
                                 b'{"action": "create", "name": "k\\ud800label", "scope": "read"}'),
            "alias_tag": ("/owner/api/alias",
                          b'{"action": "save", "name": "probe-alias", "tag": "t\\ud800g"}'),
        }
        for name, (path, body) in cases.items():
            with self.subTest(case=name):
                self._refused(path, body)
                status, _, _ = self.harness.request("GET", "/owner/api/state")
                self.assertEqual(status, 200)

        # The wedge proof: a benign machine-key create right after the
        # hostile one must still work.
        status, payload, _ = self._post(
            "/owner/api/machine-keys",
            b'{"action": "create", "name": "benign-key", "scope": "read"}',
        )
        self.assertEqual(status, 200, payload)

        # Config writes and login are unaffected too.
        status, payload, _ = self._post("/owner/api/settings", b'{"action": "initialize"}')
        self.assertEqual(status, 200, payload)
        status, payload, _ = self.harness.request(
            "POST", "/owner/api/auth/login",
            json.dumps({"username": "owner", "password": "fixture-owner-password"}).encode(),
            headers={"Content-Type": "application/json"},
            auth=False,
        )
        self.assertEqual(status, 200, payload)


class MachineKeyStoreRollbackTests(unittest.TestCase):
    """A save that fails must not leave its in-memory mutation behind."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = MachineKeyStore(Path(self._tmp.name))
        self.created = self.store.create("benign", "read")
        self.before = self.store.list()

    def _fail_write(self):
        return mock.patch(
            "gateway.owner_auth.atomic_write_json",
            side_effect=UnicodeEncodeError("utf-8", "x", 0, 1, "surrogates not allowed"),
        )

    def test_failed_create_leaves_no_phantom_record(self) -> None:
        with self._fail_write(), self.assertRaises(UnicodeEncodeError):
            self.store.create("hostile", "read")
        self.assertEqual(self.store.list(), self.before, "the store must not hold a record that was never written")
        # The store still works afterwards - no wedge until restart.
        created = self.store.create("benign-2", "read")
        self.assertEqual(created["name"], "benign-2")
        self.assertEqual(len(self.store.list()), 2)

    def test_failed_rotate_keeps_the_previous_secret(self) -> None:
        display_id = self.created["display_id"]
        with self._fail_write(), self.assertRaises(UnicodeEncodeError):
            self.store.rotate(display_id)
        record = self.store.authenticate(self.created["key"], touch=False)
        self.assertIsNotNone(record, "the old key must still authenticate after a failed rotate")
        self.assertEqual(record["display_id"], display_id)

    def test_failed_revoke_keeps_the_key(self) -> None:
        display_id = self.created["display_id"]
        with self._fail_write(), self.assertRaises(UnicodeEncodeError):
            self.store.revoke(display_id)
        self.assertEqual([row["display_id"] for row in self.store.list()], [display_id])
        self.assertIsNotNone(self.store.authenticate(self.created["key"], touch=False))

    def test_a_surrogate_name_is_refused_by_the_store_itself(self) -> None:
        with self.assertRaises(OwnerAuthError) as caught:
            self.store.create("k\ud800label", "read")
        self.assertEqual(caught.exception.status, 400)
        self.assertEqual(self.store.list(), self.before)


class AccessLogSurvivesSurrogatesTests(unittest.TestCase):
    """F9c: a hostile field must not drop the whole access line."""

    def test_a_lone_surrogate_in_a_field_is_written_as_escape_text(self) -> None:
        buf = io.BytesIO()
        logger = RequestLogger(stream=io.TextIOWrapper(buf, encoding="utf-8", errors="strict"))
        logger.log(method="POST", path="/v1/x", status=500, duration_ms=1, model="m\ud800")
        written = buf.getvalue().decode("utf-8")
        self.assertIn('"model"', written)
        self.assertIn("\\ud800", written)


class GenericFailureBoundaryTests(unittest.TestCase):
    """A bare 500 names its cause in the log and leaves a ledger row."""

    def setUp(self) -> None:
        self.harness = _build_chat_harness()
        self.addCleanup(self.harness.close)
        self.log = io.StringIO()
        self._real_logger = self.harness.app.logger
        self.harness.app.logger = RequestLogger(stream=self.log)
        self.addCleanup(setattr, self.harness.app, "logger", self._real_logger)

    def test_an_unhandled_exception_is_logged_by_class_and_recorded(self) -> None:
        target = None
        for route in self.harness.app._routes._routes:
            if getattr(route.handler, "__name__", "") == "handle_health":
                target = route
                break
        self.assertIsNotNone(target)
        original = target.handler

        def boom(request):
            raise RuntimeError("provider exploded")

        target.handler = boom
        try:
            status, payload, _ = self.harness.request("GET", "/health")
        finally:
            target.handler = original

        self.assertEqual(status, 500)
        self.assertEqual(payload, {"error": {"message": "internal error", "type": "gateway_error"}})

        line = self.log.getvalue()
        self.assertIn('"exception":"RuntimeError"', line.replace(" ", ""))
        # The class name only - never the exception text, which could
        # carry client-controlled content.
        self.assertNotIn("provider exploded", line)

        row = self.harness.app.ledger.recent(limit=5)[0]
        self.assertEqual(row["status"], 500)
        self.assertEqual(row["error_class"], "internal")
        self.assertIn("RuntimeError", str(row.get("attempts")))


if __name__ == "__main__":
    unittest.main()
