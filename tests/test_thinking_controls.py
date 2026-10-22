"""No silently lost thinking controls on an OpenAI-family conversion."""
import unittest

from gateway.bridge.model import ConversionError
from gateway.bridge.requests import convert_request
from gateway.core.capabilities import DIALECT_OPENAI_CHAT, DIALECT_OPENAI_RESPONSES
from tests.test_end_to_end import GatewayHarness


class ThinkingControlsTests(unittest.TestCase):
    def test_nested_chat_effort_is_rendered_for_responses(self):
        request = {"model": "alias", "messages": [{"role": "user", "content": "synthetic"}],
                   "reasoning": {"effort": "high"}}
        result = convert_request(request, DIALECT_OPENAI_CHAT, DIALECT_OPENAI_RESPONSES)
        self.assertEqual(result["reasoning"], {"effort": "high"})
        self.assertEqual(convert_request(request, DIALECT_OPENAI_CHAT, DIALECT_OPENAI_CHAT)["reasoning_effort"], "high")

    def test_conflicting_and_untranslatable_controls_are_refused(self):
        base = {"model": "alias", "messages": []}
        # On a converted path an Anthropic-style thinking budget cannot be
        # expressed in the OpenAI dialects, so it is refused rather than lost.
        for control in ({"thinking": {"type": "enabled", "budget_tokens": 32000}},
                        {"reasoning": {"effort": "high"}, "reasoning_effort": "low"},
                        {"reasoning": {"budget_tokens": 32000}}):
            with self.subTest(control=control):
                with self.assertRaises(ConversionError):
                    convert_request(base | control, DIALECT_OPENAI_CHAT, DIALECT_OPENAI_RESPONSES)
        # Same dialect: a control the gateway does not model is forwarded to
        # the same-dialect upstream, which is the party that defines what it
        # accepts. Forwarding is not a drop.
        forwarded = convert_request(base | {"thinking": {"type": "enabled"}}, DIALECT_OPENAI_CHAT, DIALECT_OPENAI_CHAT)
        self.assertEqual(forwarded["thinking"], {"type": "enabled"})

    def test_unsupported_effort_is_dropped_and_recorded_on_serving_route(self):
        harness = GatewayHarness()
        self.addCleanup(harness.close)
        from gateway.config import Alias, RouteRef
        with harness.config.mutate() as config:
            config.aliases["think"] = Alias(
                name="think", enabled=True, primary=RouteRef("chatgpt_codex", "gpt-6-luna")
            )
        status, _, _ = harness.request("POST", "/v1/chat/completions", body={
            "model": "think", "messages": [{"role": "user", "content": "synthetic"}],
            "reasoning": {"effort": "high"},
        })
        self.assertEqual(status, 200)
        self.assertNotIn("reasoning", harness.adapter.calls[0].payload)
        self.assertTrue(harness.router._ledger.recent()[0]["degradations"])


if __name__ == "__main__":
    unittest.main()
