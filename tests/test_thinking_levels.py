"""T4: one thinking-level vocabulary, every inbound spelling, per dialect.

A thinking level is a *preference*, never a capability (R19): a route is
never excluded over one. What this module pins down is the other half of
the rule - that a control a client actually sent survives to the upstream
in the dialect's own spelling, that it is refused when it cannot, and
that two code paths never disagree about the same field.

No credentials, no provider account, no network: the converted paths are
pure functions, and the routing cases run through the test adapter seam.
"""
import unittest

from gateway.bridge.model import ConversionError
from gateway.bridge.requests import convert_request
from gateway.config import Alias, ModelEntry, RouteRef
from gateway.core.capabilities import (
    DIALECT_OPENAI_CHAT,
    DIALECT_OPENAI_RESPONSES,
    REASONING_EFFORTS,
)
from gateway.thinking import CANONICAL_LEVELS, DISABLED_LEVEL, LEVEL_SOURCES
from tests.test_end_to_end import FAKE_PROVIDER, GatewayHarness

CHAT = DIALECT_OPENAI_CHAT
RESPONSES = DIALECT_OPENAI_RESPONSES
GO_PROVIDER = "opencode_go"
MINIMAX = "minimax-m3.1-flash"

#: The published levels of the two dialects the gateway serves, from the
#: dated catalogue (data/go_catalog.json, observed 2026-10-22).
LUNA_LEVELS = ["none", "low", "medium", "high", "xhigh", "max"]
MINIMAX_LEVELS = ["low", "medium", "high", "xhigh", "max"]


def chat_body(**thinking: object) -> dict:
    return {"model": "alias", "messages": [{"role": "user", "content": "synthetic"}]} | thinking


def responses_body(**thinking: object) -> dict:
    return {"model": "alias", "input": "synthetic"} | thinking


class VocabularyTests(unittest.TestCase):
    """The vocabulary is published by a provider, or it is not claimed."""

    def test_the_gateway_vocabulary_is_the_published_one(self):
        self.assertEqual(REASONING_EFFORTS, CANONICAL_LEVELS)
        self.assertEqual(DISABLED_LEVEL, "none")
        # OpenAI publishes these; the gateway claims exactly them.
        for level in ("none", "minimal", "low", "medium", "high", "xhigh", "max"):
            with self.subTest(level=level):
                self.assertIn(level, CANONICAL_LEVELS)
                self.assertTrue(LEVEL_SOURCES.get(level), f"{level} has no cited source")

    def test_no_invented_level_is_claimed(self):
        # `ultra` is published by no provider this gateway serves or fronts,
        # and is absent from OpenAI's published set, so it is not in the
        # vocabulary - but a client asking for it is still forwarded, and
        # the router decides per route under the R19 rule.
        self.assertNotIn("ultra", CANONICAL_LEVELS)
        self.assertNotIn("ultra", REASONING_EFFORTS)
        rendered = convert_request(chat_body(reasoning_effort="ultra"), CHAT, RESPONSES)
        self.assertEqual(rendered["reasoning"], {"effort": "ultra"})


class InboundSpellingTests(unittest.TestCase):
    """Every spelling parses to one canonical level, on both source dialects."""

    def test_every_chat_completions_spelling_normalises(self):
        cases = [
            ({"reasoning_effort": "high"}, {"effort": "high"}),
            ({"reasoning": {"effort": "high"}}, {"effort": "high"}),
            ({"reasoning_effort": "high", "reasoning": {"effort": "high"}}, {"effort": "high"}),
            # The disabled forms are one state, not three.
            ({"reasoning_effort": "none"}, {"effort": "none"}),
            ({"reasoning": {"effort": "none"}}, {"effort": "none"}),
            ({"reasoning": {"enabled": False}}, {"effort": "none"}),
            ({"reasoning": {"enabled": False, "effort": "none"}}, {"effort": "none"}),
        ]
        for thinking, expected in cases:
            with self.subTest(thinking=thinking):
                rendered = convert_request(chat_body(**thinking), CHAT, RESPONSES)
                self.assertEqual(rendered["reasoning"], expected)

    def test_every_responses_spelling_normalises(self):
        cases = [
            ({"reasoning": {"effort": "high"}}, "high"),
            # The flat spelling used to be dropped on this path.
            ({"reasoning_effort": "high"}, "high"),
            ({"reasoning": {"effort": "high"}, "reasoning_effort": "high"}, "high"),
            # A disabled control has no spelling on a Chat Completions target.
            ({"reasoning": {"effort": "none"}}, None),
            ({"reasoning": {"enabled": False}}, None),
            ({"reasoning_effort": "none"}, None),
        ]
        for thinking, expected in cases:
            with self.subTest(thinking=thinking):
                rendered = convert_request(responses_body(**thinking), RESPONSES, CHAT)
                self.assertEqual(rendered.get("reasoning_effort"), expected)
                self.assertNotIn("reasoning", rendered)

    def test_a_disabled_control_is_never_rendered_as_a_chat_level(self):
        # Regression: `none` returns HTTP 400 from a model that does not
        # publish it, so it must not reach a Chat Completions upstream.
        for thinking in (
            {"reasoning_effort": "none"},
            {"reasoning": {"effort": "none"}},
            {"reasoning": {"enabled": False}},
        ):
            with self.subTest(thinking=thinking):
                rendered = convert_request(responses_body(**thinking), RESPONSES, CHAT)
                self.assertNotIn("reasoning_effort", rendered)
                self.assertNotIn("reasoning", rendered)


class RefusalTests(unittest.TestCase):
    """A control the target cannot express is refused, never discarded."""

    def test_a_budget_control_is_refused_on_both_source_dialects(self):
        for body in (
            chat_body(thinking={"type": "enabled", "budget_tokens": 32000}),
            responses_body(thinking={"type": "enabled", "budget_tokens": 32000}),
            chat_body(thinkingConfig={"thinkingLevel": "high"}),
            responses_body(thinkingConfig={"thinkingLevel": "high"}),
        ):
            source = CHAT if "messages" in body else RESPONSES
            target = RESPONSES if source == CHAT else CHAT
            with self.subTest(body=body):
                with self.assertRaises(ConversionError) as ctx:
                    convert_request(body, source, target)
                self.assertIn("reasoning effort", ctx.exception.message)

    def test_a_reasoning_summary_is_refused_on_a_chat_target(self):
        with self.assertRaises(ConversionError) as ctx:
            convert_request(
                responses_body(reasoning={"effort": "high", "summary": "auto"}),
                RESPONSES,
                CHAT,
            )
        self.assertIn("reasoning summary", ctx.exception.message)
        self.assertIn("Responses-dialect route", ctx.exception.message)

    def test_a_reasoning_summary_survives_to_a_responses_target(self):
        forwarded = convert_request(
            responses_body(reasoning={"effort": "high", "summary": "auto"}),
            RESPONSES,
            RESPONSES,
        )
        self.assertEqual(forwarded["reasoning"], {"effort": "high", "summary": "auto"})

    def test_unmodelled_reasoning_fields_are_refused_on_both_source_dialects(self):
        for body in (
            chat_body(reasoning={"budget_tokens": 32000}),
            responses_body(reasoning={"budget_tokens": 32000}),
            chat_body(reasoning={"effort": "high", "summary": "auto"}),
        ):
            source = CHAT if "messages" in body else RESPONSES
            target = RESPONSES if source == CHAT else CHAT
            with self.subTest(body=body):
                with self.assertRaises(ConversionError) as ctx:
                    convert_request(body, source, target)
                self.assertIn("reasoning", ctx.exception.message)

    def test_conflicting_controls_are_refused_on_both_source_dialects(self):
        for source, target, thinking in (
            (CHAT, RESPONSES, {"reasoning": {"effort": "high"}, "reasoning_effort": "low"}),
            (RESPONSES, CHAT, {"reasoning": {"effort": "high"}, "reasoning_effort": "low"}),
            (CHAT, RESPONSES, {"reasoning": {"enabled": False, "effort": "high"}}),
            (RESPONSES, CHAT, {"reasoning": {"enabled": False, "effort": "high"}}),
        ):
            with self.subTest(source=source, thinking=thinking):
                with self.assertRaises(ConversionError):
                    convert_request(chat_body(**thinking) if source == CHAT else responses_body(**thinking), source, target)

    def test_a_value_of_the_wrong_type_is_refused_on_both_source_dialects(self):
        for source, thinking in (
            (CHAT, {"reasoning_effort": 5}),
            (RESPONSES, {"reasoning_effort": 5}),
            (CHAT, {"reasoning": {"effort": 5}}),
            (RESPONSES, {"reasoning": {"effort": 5}}),
            (CHAT, {"reasoning": "high"}),
            (RESPONSES, {"reasoning": "high"}),
            (CHAT, {"reasoning": {"enabled": "no"}}),
            (RESPONSES, {"reasoning": {"summary": 1}}),
        ):
            body = chat_body(**thinking) if source == CHAT else responses_body(**thinking)
            target = RESPONSES if source == CHAT else CHAT
            with self.subTest(source=source, thinking=thinking):
                with self.assertRaises(ConversionError):
                    convert_request(body, source, target)


class RenderingTests(unittest.TestCase):
    """The rendered outbound form is dialect-correct."""

    def test_a_level_renders_in_the_target_dialect(self):
        as_responses = convert_request(chat_body(reasoning_effort="high"), CHAT, RESPONSES)
        self.assertEqual(as_responses["reasoning"], {"effort": "high"})
        self.assertNotIn("reasoning_effort", as_responses)

        as_chat = convert_request(responses_body(reasoning={"effort": "high"}), RESPONSES, CHAT)
        self.assertEqual(as_chat["reasoning_effort"], "high")
        self.assertNotIn("reasoning", as_chat)

    def test_a_same_dialect_payload_the_gateway_does_not_model_is_forwarded(self):
        # The provider owns its own dialect; forwarding is not a drop.
        forwarded = convert_request(
            chat_body(thinking={"type": "enabled"}), CHAT, CHAT
        )
        self.assertEqual(forwarded["thinking"], {"type": "enabled"})
        forwarded = convert_request(
            responses_body(reasoning={"effort": "high", "summary": "auto"}),
            RESPONSES,
            RESPONSES,
        )
        self.assertEqual(forwarded["reasoning"], {"effort": "high", "summary": "auto"})

    def test_the_flat_and_the_nested_spelling_never_disagree(self):
        # The defect R25 named: the same-dialect path and the converted
        # path disagreed about `reasoning.effort`. Both are now a function
        # of the *target* dialect, so a disabled control renders once.
        disabled_spellings = (
            {"reasoning_effort": "none"},
            {"reasoning": {"effort": "none"}},
            {"reasoning": {"enabled": False}},
        )
        for thinking in disabled_spellings:
            with self.subTest(spelling=thinking, target="chat"):
                for source, body in ((CHAT, chat_body(**thinking)), (RESPONSES, responses_body(**thinking))):
                    rendered = convert_request(body, source, CHAT)
                    self.assertNotIn("reasoning_effort", rendered)
                    self.assertNotIn("reasoning", rendered)
            with self.subTest(spelling=thinking, target="responses"):
                for source, body in ((CHAT, chat_body(**thinking)), (RESPONSES, responses_body(**thinking))):
                    rendered = convert_request(body, source, RESPONSES)
                    self.assertEqual(rendered["reasoning"], {"effort": "none"})


class RoutingTests(unittest.TestCase):
    """A level the model does not publish keeps the route, and is recorded."""

    def _responses_route(self) -> GatewayHarness:
        harness = GatewayHarness()
        self.addCleanup(harness.close)
        with harness.config.mutate() as config:
            config.providers[FAKE_PROVIDER].models["gpt-6-luna"].reasoning_efforts = list(LUNA_LEVELS)
            config.aliases["think"] = Alias(
                name="think", enabled=True, primary=RouteRef(FAKE_PROVIDER, "gpt-6-luna")
            )
        return harness

    def _chat_route(self) -> GatewayHarness:
        harness = GatewayHarness()
        self.addCleanup(harness.close)
        with harness.config.mutate() as config:
            provider = config.providers[GO_PROVIDER]
            provider.state = "connected"
            provider.models[MINIMAX] = ModelEntry(
                model_id=MINIMAX,
                enabled=True,
                dialect=CHAT,
                source="discovered",
                reasoning_efforts=list(MINIMAX_LEVELS),
            )
            config.aliases["mini"] = Alias(
                name="mini", enabled=True, primary=RouteRef(GO_PROVIDER, MINIMAX)
            )
        return harness

    @staticmethod
    def _upstream_calls(harness: GatewayHarness, provider: str) -> list:
        adapter = harness.registry.get(provider)
        if adapter is None:  # pragma: no cover - the harness registers it
            raise AssertionError(f"no adapter registered for {provider}")
        return adapter.calls

    def test_an_unpublished_level_is_dropped_and_recorded_for_the_serving_route(self):
        harness = self._responses_route()
        # `minimal` is published by OpenAI but not by this model, so the
        # route still serves and the substitution is recorded.
        status, _, _ = harness.request(
            "POST",
            "/v1/chat/completions",
            body=chat_body(model="think", reasoning_effort="minimal"),
        )
        self.assertEqual(status, 200)
        self.assertNotIn("reasoning", harness.adapter.calls[0].payload)

        row = harness.router._ledger.recent()[0]
        self.assertEqual(row["serving_provider"], FAKE_PROVIDER)
        self.assertEqual(row["serving_model"], "gpt-6-luna")
        self.assertTrue(row["degradations"], "the substitution was not recorded")
        self.assertIn("minimal", str(row["degradations"]))

    def test_a_published_level_reaches_the_upstream(self):
        harness = self._responses_route()
        status, _, _ = harness.request(
            "POST",
            "/v1/chat/completions",
            body=chat_body(model="think", reasoning_effort="xhigh"),
        )
        self.assertEqual(status, 200)
        self.assertEqual(harness.adapter.calls[0].payload["reasoning"], {"effort": "xhigh"})
        self.assertFalse(harness.router._ledger.recent()[0]["degradations"])

    def test_the_rule_holds_for_a_chat_dialect_route_too(self):
        harness = self._chat_route()
        status, _, _ = harness.request(
            "POST",
            "/v1/chat/completions",
            body=chat_body(model="mini", reasoning_effort="minimal"),
        )
        self.assertEqual(status, 200)
        payload = self._upstream_calls(harness, GO_PROVIDER)[0].payload
        self.assertNotIn("reasoning_effort", payload)
        self.assertNotIn("reasoning", payload)

        row = harness.router._ledger.recent()[0]
        self.assertEqual(row["serving_model"], MINIMAX)
        self.assertTrue(row["degradations"], "the substitution was not recorded")

    def test_an_unknown_level_is_dropped_not_refused(self):
        harness = self._chat_route()
        status, _, _ = harness.request(
            "POST",
            "/v1/chat/completions",
            body=chat_body(model="mini", reasoning_effort="ultra"),
        )
        self.assertEqual(status, 200)
        self.assertNotIn("reasoning_effort", self._upstream_calls(harness, GO_PROVIDER)[0].payload)
        self.assertTrue(harness.router._ledger.recent()[0]["degradations"])

    def test_a_disabled_request_is_served_without_a_capability_requirement(self):
        harness = self._chat_route()
        status, _, _ = harness.request(
            "POST",
            "/v1/chat/completions",
            body=chat_body(model="mini", reasoning={"enabled": False}),
        )
        self.assertEqual(status, 200)
        payload = self._upstream_calls(harness, GO_PROVIDER)[0].payload
        self.assertNotIn("reasoning", payload)
        self.assertNotIn("reasoning_effort", payload)
        # Disabled normalizes to `none`; an unpublished level is dropped and
        # recorded just like any other reasoning preference (R19).
        degradations = harness.router._ledger.recent()[0]["degradations"]
        self.assertTrue(degradations)
        self.assertIn("none", str(degradations))


if __name__ == "__main__":
    unittest.main()
