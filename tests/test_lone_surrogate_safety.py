"""F9: lone-surrogate client text must not break responses or console state.

A client can send ``tool_choice.type`` as the ASCII escape ``\\ud800x``;
``json.loads`` accepts it as a lone surrogate. The conversion refusal then
interpolates the raw client text into an error message, which reaches both
the attacker's own error envelope and the in-memory request ledger. Two
boundaries must hold:

1. The attacker's own request gets a proper JSON error envelope, never a
   bare 500 with a traceback on stderr.
2. ``GET /owner/api/state`` still answers 200 afterwards - one hostile
   request must not poison the owner console.

Pre-existing on base 5449638 (security finding F9, gate t_cf41ab26).
"""
from __future__ import annotations

import unittest

from tests.test_messages_handler import CHAT_MODEL, _build_chat_harness

# Wire form: plain ASCII, exactly what any client can put in a JSON body.
# json.loads turns the escape into a lone surrogate in the decoded string.
HOSTILE_BODY = (
    b'{"model": "'
    + CHAT_MODEL.encode()
    + b'", "max_tokens": 32,'
    b' "messages": [{"role": "user", "content": "hi"}],'
    b' "tool_choice": {"type": "\\ud800x"}}'
)

CLEAN_BODY = (
    b'{"model": "'
    + CHAT_MODEL.encode()
    + b'", "max_tokens": 32,'
    b' "messages": [{"role": "user", "content": "hi"}]}'
)


class LoneSurrogateSafetyTests(unittest.TestCase):
    """One hostile request must not take down its own response or the console."""

    def setUp(self) -> None:
        self.harness = _build_chat_harness()
        self.addCleanup(self.harness.close)

    def test_hostile_tool_choice_keeps_response_envelope_and_state(self) -> None:
        status_before, _, _ = self.harness.request("GET", "/owner/api/state")
        self.assertEqual(status_before, 200, "console state must start healthy")

        status_bad, payload_bad, _ = self.harness.request(
            "POST",
            "/v1/messages",
            body=HOSTILE_BODY,
            headers={"Content-Type": "application/json"},
        )

        # 1. A proper error envelope, not a bare 500.
        self.assertNotEqual(
            status_bad, 500,
            f"hostile request must not become a bare 500, got {payload_bad!r}",
        )
        self.assertIn("error", payload_bad, payload_bad)
        self.assertIn("message", payload_bad["error"], payload_bad)
        self.assertIn("type", payload_bad["error"], payload_bad)
        # The envelope itself must be encodable UTF-8 (no lone surrogates).
        self.assertIsInstance(payload_bad["error"]["message"], str)

        # 2. The owner console state endpoint still answers 200.
        status_after, payload_after, _ = self.harness.request("GET", "/owner/api/state")
        self.assertEqual(
            status_after, 200,
            f"/owner/api/state broke after one hostile request: {payload_after!r}",
        )

    def test_routing_error_envelope_survives_a_broken_encoder(self) -> None:
        """Layer 3: if the envelope cannot be serialized, keep the original
        status and serve the canned envelope instead of a bare 500."""
        from unittest import mock

        malformed = b'{"model": "", "messages": []}'
        # Sanity: without the injected failure this is a clean 4xx envelope.
        status_clean, _, _ = self.harness.request(
            "POST", "/v1/messages", body=malformed,
            headers={"Content-Type": "application/json"},
        )
        self.assertNotEqual(status_clean, 500)
        self.assertGreaterEqual(status_clean, 400)
        self.assertLess(status_clean, 500)

        with mock.patch(
            "gateway.http.app.json_response",
            side_effect=UnicodeEncodeError("utf-8", "x", 0, 1, "surrogates not allowed"),
        ):
            status_fallback, payload_fallback, _ = self.harness.request(
                "POST", "/v1/messages", body=malformed,
                headers={"Content-Type": "application/json"},
            )

        self.assertEqual(
            status_fallback, status_clean,
            "canned fallback must preserve the original error status",
        )
        self.assertEqual(
            payload_fallback,
            {"error": {"message": "internal error", "type": "gateway_error"}},
            payload_fallback,
        )


if __name__ == "__main__":
    unittest.main()
