"""F1 regression: ``capabilities/decision`` must not swallow ``InvalidBodyText``.

The security gate on 443366d (t_cfd4d19f) passed the F1 fix but recorded one
residual: ``handle_capability_decision``'s blanket ``except Exception`` sat
after no ``InvalidBodyText`` clause, so a lone-surrogate body on
``POST /owner/api/capabilities/decision`` was converted *inside* the handler
into a bare ``400 invalid_request`` - no ``field``, no terminal ledger row,
no ``gateway_errors_total{error_class="policy"}`` counter - while every other
JSON ingress recorded all three at the dispatch boundary. The fix (t_a93d59e3)
is an ``except InvalidBodyText: raise`` clause before the blanket one
(``src/gateway/http/app.py``), so the refusal reaches the boundary's F9 choke.

That fix shipped with **no in-repo test**: the full suite was green on the
broken base (e7a851f) and would stay green with the ``raise`` line deleted;
coverage lived only in the security card's ephemeral workspace probes. This
module is the repo-doctrine regression test (``AGENTS.md`` /
``docs/REVIEW.md``: a finding is not closed without one):

1. the choke - a lone-surrogate decision body is answered ``400
   invalid_request`` with ``field=$.expected_fingerprint``, +1 policy ledger
   row whose reason carries ``lone surrogate refused at body parse`` and the
   escaped ``\\ud800``, metrics policy +1 / internal +0, exactly one access
   line;
2. the companion - a genuine bug raised inside the handler still converts to
   the same ``400``, but the one access line carries ``exception=OSError``
   (class name only), the injected client text reaches no access line, and
   the request still writes exactly one line.

Mutation-verified: green on 443366d; red when the ``raise`` clause is
removed (checks 1's field/ledger/metrics go the e7a851f way: no field,
rows +0, policy +0) and when
``request.context["handler_exception"] = type(exc).__name__`` is removed
(check 2's ``exception=`` disappears).
"""
from __future__ import annotations

import io
import json
import unittest
from typing import Any
from unittest import mock

from gateway.logging import RequestLogger
from tests.test_messages_handler import _build_chat_harness

#: Wire form: plain ASCII, exactly what any client can send. ``json.loads``
#: turns the ``\\ud800`` escape into a lone surrogate *inside* the field the
#: handler validates, so the body dies at the parse choke - the field is
#: named ``$.expected_fingerprint`` by the boundary.
_DECISION_BODY = (
    b'{"proposal_id": "p1", "decision_id": "d1", "decisions": {}, '
    b'"expected_revision": 1, "expected_fingerprint": "f\\ud800f"}'
)

#: A well-formed decision body: parses cleanly, reaches the handler's
#: ``decision_replay`` call, which the companion test makes raise.
_CLEAN_DECISION_BODY = json.dumps(
    {
        "proposal_id": "p1",
        "decision_id": "d1",
        "decisions": {},
        "expected_revision": 1,
        "expected_fingerprint": "x",
    }
).encode("utf-8")

#: Client-controlled text that must never appear in an access line.
_INJECTED_TEXT = "BOOM_CLIENT_TEXT"

_POLICY_SERIES = 'gateway_errors_total{error_class="policy"}'
_INTERNAL_SERIES = 'gateway_errors_total{error_class="internal"}'


class _DecisionHarnessCase(unittest.TestCase):
    """A live loopback harness whose access lines land in a buffer."""

    def setUp(self) -> None:
        self.harness = _build_chat_harness()
        self.addCleanup(self.harness.close)
        self.log = io.StringIO()
        self._real_logger = self.harness.app.logger
        self.harness.app.logger = RequestLogger(stream=self.log)
        self.addCleanup(setattr, self.harness.app, "logger", self._real_logger)

    def _lines(self) -> list[str]:
        return [line for line in self.log.getvalue().splitlines() if line.strip()]

    def _metric(self, series: str) -> int:
        status, text, _ = self.harness.request("GET", "/metrics", raw=True)
        self.assertEqual(status, 200)
        assert text is not None
        if isinstance(text, bytes):
            text = text.decode("utf-8", "replace")
        for line in text.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0] == series:
                return int(float(parts[1]))
        return 0

    def _rows(self) -> list[dict[str, Any]]:
        ledger = self.harness.app.ledger
        assert ledger is not None
        return ledger.recent(limit=10000)

    def _post_decision(self, body: bytes) -> tuple[int, Any]:
        status, payload, _ = self.harness.request(
            "POST",
            "/owner/api/capabilities/decision",
            body=body,
            headers={"Content-Type": "application/json"},
        )
        return status, payload

    @staticmethod
    def _compact(line: str) -> str:
        return line.replace(" ", "")


class CapabilityDecisionChokeTests(_DecisionHarnessCase):
    """Check 1: the refusal reaches the dispatch boundary, fully recorded."""

    def test_the_refusal_is_400_invalid_request_naming_the_field(self) -> None:
        status, payload = self._post_decision(_DECISION_BODY)
        self.assertEqual(status, 400, payload)
        error = payload["error"]
        self.assertEqual(error["type"], "invalid_request", payload)
        # The e7a851f (swallowed) answer was the same 400 with no field at
        # all - the named field is what proves the boundary answered.
        self.assertEqual(error.get("field"), "$.expected_fingerprint", payload)

        # The refusal must not poison the console either.
        state_status, _, _ = self.harness.request("GET", "/owner/api/state")
        self.assertEqual(state_status, 200)

    def test_the_refusal_records_one_policy_row_with_the_escaped_excerpt(self) -> None:
        rows_before = len(self._rows())
        status, _ = self._post_decision(_DECISION_BODY)
        self.assertEqual(status, 400)
        rows = self._rows()
        self.assertEqual(len(rows), rows_before + 1, "a body refusal must leave a ledger row")
        row = rows[0]
        self.assertEqual(row["status"], 400)
        self.assertEqual(row["error_class"], "policy")
        reasons = [str(a.get("reason", "")) for a in row.get("attempts") or []]
        self.assertTrue(
            any("lone surrogate refused at body parse" in reason for reason in reasons),
            reasons,
        )
        # The offending text appears only as escape text, never as a raw
        # surrogate that would break the JSONL journal.
        self.assertTrue(any("\\ud800" in reason for reason in reasons), reasons)

    def test_the_refusal_bumps_the_policy_counter_and_not_internal(self) -> None:
        policy_before = self._metric(_POLICY_SERIES)
        internal_before = self._metric(_INTERNAL_SERIES)
        status, _ = self._post_decision(_DECISION_BODY)
        self.assertEqual(status, 400)
        self.assertEqual(self._metric(_POLICY_SERIES) - policy_before, 1)
        self.assertEqual(self._metric(_INTERNAL_SERIES) - internal_before, 0)

    def test_the_refusal_writes_exactly_one_access_line(self) -> None:
        lines_before = len(self._lines())
        status, _ = self._post_decision(_DECISION_BODY)
        self.assertEqual(status, 400)
        new_lines = self._lines()[lines_before:]
        self.assertEqual(len(new_lines), 1, new_lines)
        self.assertIn('"status":400', self._compact(new_lines[0]))
        self.assertIn('"path":"/owner/api/capabilities/decision"', self._compact(new_lines[0]))


class CapabilityDecisionCompanionTests(_DecisionHarnessCase):
    """Check 2: a genuine handler bug converts to 400 but stays attributable."""

    def _raise_inside_handler(self) -> tuple[int, Any]:
        with mock.patch.object(
            self.harness.app.discovery,
            "decision_replay",
            side_effect=OSError(_INJECTED_TEXT),
        ):
            return self._post_decision(_CLEAN_DECISION_BODY)

    def test_a_genuine_handler_bug_still_answers_invalid_request(self) -> None:
        status, payload = self._raise_inside_handler()
        # Response conversion is unchanged by the F1 fix: the blanket clause
        # still turns a real failure into the same client-facing 400.
        self.assertEqual(status, 400, payload)
        self.assertEqual(payload["error"]["type"], "invalid_request", payload)
        self.assertNotIn(_INJECTED_TEXT, json.dumps(payload, ensure_ascii=False))

    def test_the_access_line_names_the_class_and_never_the_text(self) -> None:
        lines_before = len(self._lines())
        status, _ = self._raise_inside_handler()
        self.assertEqual(status, 400)
        new_lines = self._lines()[lines_before:]

        # Exactly one access line for the one request - no double log from
        # the handler and the boundary both recording it.
        self.assertEqual(len(new_lines), 1, new_lines)
        # Class name only: the boundary's own ``exception=`` wins only on
        # the 500 path, so this 400 must come from the handler's note.
        self.assertIn('"exception":"OSError"', self._compact(new_lines[0]))
        # The injected client text reaches no access line - not this one,
        # not any other line the harness has written.
        self.assertTrue(
            all(_INJECTED_TEXT not in line for line in self._lines()),
            [line for line in self._lines() if _INJECTED_TEXT in line],
        )


if __name__ == "__main__":
    unittest.main()
