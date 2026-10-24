"""T21: the owner's request policy per alias.

The policy is applied in one place - ``gateway.policy.apply_alias_policy``,
called by the router on every path that reaches a provider - in the order
``docs/TODO.md`` T21 fixes: the client's value, the owner's default if the
client gave none, the owner's cap if it clamps the value, then the deny
rules. What it changed is recorded against the route that served the
request; what it denies is refused with the rule named.

No credentials, no provider account, no network: the pure cases call the
applier directly, the routed cases go through the test adapter seam.
"""
import json
import unittest

from gateway.config import (
    Alias,
    AliasPolicy,
    MAX_SYSTEM_DEFAULT_LENGTH,
    RouteRef,
)
from gateway.core.capabilities import DIALECT_OPENAI_CHAT, DIALECT_OPENAI_RESPONSES
from gateway.policy import (
    KIND_CLAMPED,
    KIND_DEFAULTED,
    KIND_FORCED,
    PolicyViolation,
    apply_alias_policy,
)
from tests.test_end_to_end import FAKE_PROVIDER, GatewayHarness

CHAT = DIALECT_OPENAI_CHAT
RESPONSES = DIALECT_OPENAI_RESPONSES

#: The full policy block from the TODO's own example.
TODO_POLICY = {
    "defaults": {
        "temperature": 0.1,
        "max_output_tokens": 8000,
        "thinking_level": "high",
        "system": "You are a careful senior engineer.",
    },
    "caps": {"max_output_tokens": 32000, "thinking_level": "medium"},
    "deny": {"tools": True, "store": False},
}


def policied_alias(**policy: object) -> Alias:
    return Alias(name="coding", policy=AliasPolicy.from_dict(policy))


def chat_body(**fields: object) -> dict:
    return {"model": "coding", "messages": [{"role": "user", "content": "synthetic"}]} | fields


class ConfigTests(unittest.TestCase):
    """The policy block parses from the vocabulary the product publishes."""

    def test_the_todos_own_example_parses(self) -> None:
        policy = AliasPolicy.from_dict(TODO_POLICY)
        assert policy is not None
        self.assertEqual(policy.defaults.temperature, 0.1)  # type: ignore[union-attr]
        self.assertEqual(policy.defaults.max_output_tokens, 8000)  # type: ignore[union-attr]
        self.assertEqual(policy.defaults.thinking_level, "high")  # type: ignore[union-attr]
        self.assertEqual(policy.caps.max_output_tokens, 32000)  # type: ignore[union-attr]
        self.assertEqual(policy.caps.thinking_level, "medium")  # type: ignore[union-attr]
        self.assertTrue(policy.deny.tools)  # type: ignore[union-attr]
        self.assertIs(policy.deny.store, False)  # type: ignore[union-attr]

    def test_the_policy_survives_an_alias_round_trip(self) -> None:
        alias = policied_alias(**TODO_POLICY)
        reparsed = Alias.from_dict(alias.to_dict())
        assert reparsed is not None and reparsed.policy is not None
        self.assertEqual(reparsed.policy, alias.policy)

    def test_an_out_of_vocabulary_value_is_dropped_not_invented(self) -> None:
        policy = AliasPolicy.from_dict(
            {
                "defaults": {
                    "temperature": 3.0,  # outside the published range
                    "max_output_tokens": 0,  # not a budget
                    "thinking_level": "ultra",  # not in the vocabulary
                    "system": "   ",  # empty
                },
                "caps": {"thinking_level": "sideways"},
            }
        )
        self.assertIsNone(policy)

    def test_an_over_long_system_default_is_dropped(self) -> None:
        policy = AliasPolicy.from_dict({"defaults": {"system": "x" * (MAX_SYSTEM_DEFAULT_LENGTH + 1)}})
        self.assertIsNone(policy)

    def test_a_block_that_rules_nothing_is_no_policy(self) -> None:
        alias = Alias.from_dict({"name": "x", "policy": {"defaults": {}, "caps": {}, "deny": {"tools": False}}})
        assert alias is not None
        self.assertIsNone(alias.policy)

    def test_an_alias_without_a_policy_block_is_unchanged(self) -> None:
        alias = Alias.from_dict({"name": "x"})
        assert alias is not None
        self.assertIsNone(alias.policy)
        self.assertIsNone(alias.to_dict()["policy"])


class DefaultTests(unittest.TestCase):
    """A default fills a silence; it never overrides a client that asked."""

    def setUp(self) -> None:
        self.alias = policied_alias(defaults=TODO_POLICY["defaults"])

    def test_every_silent_control_receives_the_owners_default(self) -> None:
        out, changes = apply_alias_policy(self.alias, chat_body(), CHAT)
        self.assertEqual(out["temperature"], 0.1)
        self.assertEqual(out["max_completion_tokens"], 8000)
        self.assertEqual(out["reasoning_effort"], "high")
        self.assertEqual(out["messages"][0], {"role": "system", "content": "You are a careful senior engineer."})
        self.assertEqual(
            [(change.kind, change.field) for change in changes],
            [
                (KIND_DEFAULTED, "temperature"),
                (KIND_DEFAULTED, "max_output_tokens"),
                (KIND_DEFAULTED, "thinking_level"),
                (KIND_DEFAULTED, "system"),
            ],
        )

    def test_a_client_value_survives_untouched_and_unrecorded(self) -> None:
        out, changes = apply_alias_policy(
            self.alias,
            chat_body(temperature=0.7, max_tokens=4000, reasoning_effort="low"),
            CHAT,
        )
        self.assertEqual(out["temperature"], 0.7)
        self.assertEqual(out["max_tokens"], 4000)
        self.assertEqual(out["reasoning_effort"], "low")
        # The system default still applies - the client gave no system -
        # but no numeric or level default does.
        self.assertEqual([change.field for change in changes], ["system"])

    def test_the_system_default_never_replaces_a_client_system(self) -> None:
        out, changes = apply_alias_policy(
            self.alias,
            chat_body(messages=[{"role": "system", "content": "mine"}, {"role": "user", "content": "hi"}]),
            CHAT,
        )
        self.assertEqual(out["messages"][0]["content"], "mine")
        self.assertNotIn("system", [change.field for change in changes])

    def test_the_responses_dialect_places_the_default_in_instructions(self) -> None:
        out, changes = apply_alias_policy(self.alias, {"model": "coding", "input": "hi"}, RESPONSES)
        self.assertEqual(out["instructions"], "You are a careful senior engineer.")
        self.assertEqual(out["max_output_tokens"], 8000)
        self.assertEqual(out["reasoning"], {"effort": "high"})
        self.assertNotIn("messages", out)

    def test_the_system_record_is_a_length_not_the_text(self) -> None:
        _, changes = apply_alias_policy(self.alias, chat_body(), CHAT)
        system_change = next(change for change in changes if change.field == "system")
        self.assertEqual(system_change.value, len("You are a careful senior engineer."))
        self.assertNotIn("careful", json.dumps([change.reason for change in changes]))


class CapTests(unittest.TestCase):
    """A cap clamps what stands after the default, and says so."""

    def setUp(self) -> None:
        self.alias = policied_alias(caps=TODO_POLICY["caps"])

    def test_a_value_under_the_cap_is_untouched(self) -> None:
        out, changes = apply_alias_policy(
            self.alias, chat_body(max_tokens=8000, reasoning_effort="low"), CHAT
        )
        self.assertEqual(out["max_tokens"], 8000)
        self.assertEqual(out["reasoning_effort"], "low")
        self.assertEqual(changes, [])

    def test_a_numeric_cap_clamps_in_the_clients_own_spelling(self) -> None:
        for key in ("max_tokens", "max_completion_tokens"):
            with self.subTest(key=key):
                out, changes = apply_alias_policy(self.alias, chat_body(**{key: 50000}), CHAT)
                self.assertEqual(out[key], 32000)
                self.assertEqual(
                    [(change.kind, change.field, change.previous, change.value) for change in changes],
                    [(KIND_CLAMPED, "max_output_tokens", 50000, 32000)],
                )

    def test_the_responses_dialect_caps_its_own_spelling(self) -> None:
        out, changes = apply_alias_policy(
            self.alias, {"model": "coding", "input": "hi", "max_output_tokens": 50000}, RESPONSES
        )
        self.assertEqual(out["max_output_tokens"], 32000)
        self.assertEqual(changes[0].kind, KIND_CLAMPED)

    def test_the_thinking_cap_is_an_ordinal_ceiling(self) -> None:
        cases = {"xhigh": "medium", "max": "medium", "high": "medium", "medium": None, "low": None, "none": None}
        for level, expected in cases.items():
            with self.subTest(level=level):
                out, changes = apply_alias_policy(self.alias, chat_body(reasoning_effort=level), CHAT)
                if expected is None:
                    self.assertEqual(out["reasoning_effort"], level)
                    self.assertEqual(changes, [])
                else:
                    self.assertEqual(out["reasoning_effort"], expected)
                    self.assertEqual(changes[0].kind, KIND_CLAMPED)
                    self.assertEqual(changes[0].previous, level)

    def test_a_level_the_gateway_cannot_order_does_not_pass_the_ceiling(self) -> None:
        out, changes = apply_alias_policy(self.alias, chat_body(reasoning_effort="ultra"), CHAT)
        self.assertEqual(out["reasoning_effort"], "medium")
        self.assertEqual(changes[0].kind, KIND_CLAMPED)
        self.assertEqual(changes[0].previous, "ultra")

    def test_an_unknown_level_is_forwarded_when_no_cap_applies(self) -> None:
        alias = policied_alias()
        out, changes = apply_alias_policy(alias, chat_body(reasoning_effort="ultra"), CHAT)
        self.assertEqual(out["reasoning_effort"], "ultra")
        self.assertEqual(changes, [])

    def test_a_default_is_also_subject_to_the_cap(self) -> None:
        alias = policied_alias(
            defaults={"max_output_tokens": 8000, "thinking_level": "high"},
            caps={"max_output_tokens": 4000, "thinking_level": "medium"},
        )
        out, changes = apply_alias_policy(alias, chat_body(), CHAT)
        self.assertEqual(out["max_completion_tokens"], 4000)
        self.assertEqual(out["reasoning_effort"], "medium")
        self.assertEqual(
            [(change.kind, change.field) for change in changes],
            [
                (KIND_DEFAULTED, "max_output_tokens"),
                (KIND_CLAMPED, "max_output_tokens"),
                (KIND_DEFAULTED, "thinking_level"),
                (KIND_CLAMPED, "thinking_level"),
            ],
        )

    def test_the_level_change_leaves_exactly_one_spelling(self) -> None:
        nested, _ = apply_alias_policy(
            self.alias, chat_body(reasoning={"effort": "xhigh", "summary": "auto"}), CHAT
        )
        self.assertEqual(nested["reasoning"], {"effort": "medium", "summary": "auto"})
        self.assertNotIn("reasoning_effort", nested)

        both, _ = apply_alias_policy(
            self.alias, chat_body(reasoning_effort="xhigh", reasoning={"effort": "xhigh"}), CHAT
        )
        self.assertEqual(both["reasoning_effort"], "medium")
        self.assertEqual(both["reasoning"], {"effort": "medium"})

    def test_a_control_the_policy_cannot_read_is_left_for_the_bridge(self) -> None:
        # Two spellings that contradict each other: the policy neither
        # defaults nor clamps it, and the bridge refuses it downstream.
        out, changes = apply_alias_policy(
            self.alias, chat_body(reasoning_effort="low", reasoning={"effort": "high"}), CHAT
        )
        self.assertEqual(out["reasoning_effort"], "low")
        self.assertEqual(out["reasoning"], {"effort": "high"})
        self.assertEqual(changes, [])


class DenyTests(unittest.TestCase):
    """A denial refuses with the rule named; a force is applied and recorded."""

    def setUp(self) -> None:
        self.alias = policied_alias(deny=TODO_POLICY["deny"])

    def test_a_request_with_tools_is_refused_and_the_rule_is_named(self) -> None:
        with self.assertRaises(PolicyViolation) as caught:
            apply_alias_policy(self.alias, chat_body(tools=[{"type": "function"}]), CHAT)
        self.assertEqual(caught.exception.status, 403)
        self.assertEqual(caught.exception.rule, "deny.tools")
        self.assertIn("deny.tools", caught.exception.message)

    def test_an_empty_tool_list_is_not_tools(self) -> None:
        out, _ = apply_alias_policy(self.alias, chat_body(tools=[]), CHAT)
        self.assertEqual(out["tools"], [])

    def test_a_request_without_tools_is_served(self) -> None:
        out, _ = apply_alias_policy(self.alias, chat_body(), CHAT)
        self.assertNotIn("tools", out)

    def test_store_is_forced_and_recorded(self) -> None:
        out, changes = apply_alias_policy(self.alias, chat_body(store=True), CHAT)
        self.assertIs(out["store"], False)
        self.assertEqual(
            [(change.kind, change.field, change.previous, change.value) for change in changes],
            [(KIND_FORCED, "store", True, False)],
        )

    def test_an_absent_store_is_still_forced(self) -> None:
        out, changes = apply_alias_policy(self.alias, chat_body(), CHAT)
        self.assertIs(out["store"], False)
        self.assertEqual(changes[-1].kind, KIND_FORCED)
        self.assertIsNone(changes[-1].previous)

    def test_a_store_that_already_conforms_records_nothing(self) -> None:
        out, changes = apply_alias_policy(self.alias, chat_body(store=False), CHAT)
        self.assertIs(out["store"], False)
        self.assertEqual(changes, [])

    def test_the_client_payload_is_never_mutated(self) -> None:
        payload = chat_body(store=True, max_tokens=50000)
        snapshot = json.loads(json.dumps(payload))
        apply_alias_policy(self.alias, payload, CHAT)
        self.assertEqual(payload, snapshot)


class RoutingTests(unittest.TestCase):
    """The policy holds on the path that reaches a provider, not just in isolation."""

    def _harness_with_policy(self, **policy: object) -> GatewayHarness:
        harness = GatewayHarness()
        self.addCleanup(harness.close)
        with harness.config.mutate() as config:
            config.providers[FAKE_PROVIDER].models["gpt-6-luna"].reasoning_efforts = [
                "none", "low", "medium", "high", "xhigh", "max"
            ]
            config.aliases["coding"] = Alias(
                name="coding",
                enabled=True,
                primary=RouteRef(FAKE_PROVIDER, "gpt-6-luna"),
                policy=AliasPolicy.from_dict(policy),
            )
        return harness

    @staticmethod
    def _ledger_row(harness: GatewayHarness) -> dict:
        return harness.router._ledger.recent()[0]

    def test_owner_defaults_reach_the_provider_on_a_converted_route(self) -> None:
        harness = self._harness_with_policy(defaults=TODO_POLICY["defaults"])
        status, _, _ = harness.request("POST", "/v1/chat/completions", body=chat_body())
        self.assertEqual(status, 200)
        upstream = harness.adapter.calls[0].payload
        self.assertEqual(upstream["temperature"], 0.1)
        self.assertEqual(upstream["max_output_tokens"], 8000)
        self.assertEqual(upstream["reasoning"], {"effort": "high"})
        self.assertEqual(upstream["instructions"], "You are a careful senior engineer.")

    def test_a_clamp_is_recorded_for_the_serving_route_only(self) -> None:
        harness = self._harness_with_policy(caps=TODO_POLICY["caps"])
        status, _, _ = harness.request(
            "POST", "/v1/chat/completions", body=chat_body(max_tokens=50000, reasoning_effort="xhigh")
        )
        self.assertEqual(status, 200)
        upstream = harness.adapter.calls[0].payload
        self.assertEqual(upstream["max_output_tokens"], 32000)
        self.assertEqual(upstream["reasoning"], {"effort": "medium"})

        row = self._ledger_row(harness)
        self.assertEqual(row["serving_provider"], FAKE_PROVIDER)
        self.assertEqual(row["serving_model"], "gpt-6-luna")
        fields = {(entry["kind"], entry["field"]): entry for entry in row["degradations"]}
        self.assertEqual(fields[(KIND_CLAMPED, "max_output_tokens")]["from"], 50000)
        self.assertEqual(fields[(KIND_CLAMPED, "max_output_tokens")]["to"], 32000)
        self.assertEqual(fields[(KIND_CLAMPED, "max_output_tokens")]["provider"], FAKE_PROVIDER)
        self.assertIn("cap", fields[(KIND_CLAMPED, "max_output_tokens")]["reason"])
        self.assertEqual(fields[(KIND_CLAMPED, "thinking_level")]["from"], "xhigh")

    def test_a_forced_store_reaches_the_provider_even_across_conversion(self) -> None:
        harness = self._harness_with_policy(deny=TODO_POLICY["deny"])
        status, _, _ = harness.request("POST", "/v1/chat/completions", body=chat_body())
        self.assertEqual(status, 200)
        # Chat Completions in, Responses out: the bridge does not model
        # `store`, so the router's re-assertion is what carries it.
        self.assertIs(harness.adapter.calls[0].payload["store"], False)
        row = self._ledger_row(harness)
        forced = [entry for entry in row["degradations"] if entry["kind"] == KIND_FORCED]
        self.assertEqual([(entry["field"], entry["to"]) for entry in forced], [("store", False)])

    def test_denied_tools_are_refused_before_any_provider_work(self) -> None:
        harness = self._harness_with_policy(deny=TODO_POLICY["deny"])
        status, body, _ = harness.request(
            "POST",
            "/v1/chat/completions",
            body=chat_body(tools=[{"type": "function", "function": {"name": "f"}}]),
        )
        self.assertEqual(status, 403)
        assert isinstance(body, dict)
        self.assertEqual(body["error"]["type"], "policy_denied")
        self.assertIn("deny.tools", body["error"]["message"])
        self.assertEqual(body["error"]["detail"]["rule"], "deny.tools")
        self.assertEqual(harness.adapter.calls, [], "a denied request must reach no provider")

    def test_a_client_value_within_the_caps_is_never_recorded(self) -> None:
        harness = self._harness_with_policy(defaults=TODO_POLICY["defaults"], caps=TODO_POLICY["caps"])
        status, _, _ = harness.request(
            "POST", "/v1/chat/completions", body=chat_body(temperature=0.7, max_tokens=4000, reasoning_effort="low")
        )
        self.assertEqual(status, 200)
        # The client's own values stand untouched and unrecorded; the one
        # record is the silence the default filled - the client sent no
        # system instruction.
        row = self._ledger_row(harness)
        self.assertEqual(
            [(entry["kind"], entry["field"]) for entry in row["degradations"]],
            [(KIND_DEFAULTED, "system")],
        )

    def test_the_alias_test_path_applies_the_policy_too(self) -> None:
        harness = self._harness_with_policy(defaults=TODO_POLICY["defaults"], deny=TODO_POLICY["deny"])
        status, body, _ = harness.request(
            "POST", "/owner/api/alias", body={"action": "test", "name": "coding"}
        )
        self.assertEqual(status, 200, body)
        upstream = harness.adapter.calls[0].payload
        self.assertEqual(upstream["temperature"], 0.1)
        self.assertEqual(upstream["reasoning"], {"effort": "high"})
        self.assertIs(upstream["store"], False)

    def test_an_alias_without_a_policy_is_untouched(self) -> None:
        harness = self._harness_with_policy()
        status, _, _ = harness.request(
            "POST", "/v1/chat/completions", body=chat_body(temperature=0.7, max_tokens=4000)
        )
        self.assertEqual(status, 200)
        upstream = harness.adapter.calls[0].payload
        self.assertEqual(upstream["temperature"], 0.7)
        self.assertEqual(upstream["max_output_tokens"], 4000)
        self.assertNotIn("store", upstream)
        self.assertFalse(self._ledger_row(harness)["degradations"])


class ConsoleSaveTests(unittest.TestCase):
    """A console alias save must never silently drop or change a policy (R40)."""

    def _harness(self, **policy: object) -> GatewayHarness:
        harness = GatewayHarness()
        self.addCleanup(harness.close)
        with harness.config.mutate() as config:
            config.providers[FAKE_PROVIDER].models["gpt-6-luna"].reasoning_efforts = [
                "none", "low", "medium", "high", "xhigh", "max"
            ]
            config.aliases["coding"] = Alias(
                name="coding",
                enabled=True,
                primary=RouteRef(FAKE_PROVIDER, "gpt-6-luna"),
                policy=AliasPolicy.from_dict(policy),
            )
        return harness

    @staticmethod
    def _save(harness: GatewayHarness, **extra: object):
        payload = {
            "action": "save",
            "name": "coding",
            "primary": {"provider": FAKE_PROVIDER, "model": "gpt-6-luna"},
            "enabled": True,
        }
        payload.update(extra)
        return harness.request("POST", "/owner/api/alias", body=payload)

    def test_a_save_without_a_policy_block_preserves_the_existing_one(self) -> None:
        # The console form never submits `policy`, so this is the path an
        # owner takes when editing any other field of a policied alias.
        harness = self._harness(**TODO_POLICY)
        status, body, _ = self._save(harness)
        self.assertEqual(status, 200, body)
        assert isinstance(body, dict)
        self.assertEqual(body["policy"]["state"], "preserved")
        self.assertEqual(harness.config.snapshot().aliases["coding"].policy, AliasPolicy.from_dict(TODO_POLICY))
        # The preserved policy still governs the next request.
        status, _, _ = harness.request("POST", "/v1/chat/completions", body=chat_body())
        self.assertEqual(status, 200)
        upstream = harness.adapter.calls[0].payload
        self.assertEqual(upstream["temperature"], 0.1)
        # The cap clamps the default: defaults *and* caps survived the save.
        self.assertEqual(upstream["reasoning"], {"effort": "medium"})
        self.assertIs(upstream["store"], False)

    def test_an_explicit_policy_replaces_the_old_one_and_reports_it(self) -> None:
        harness = self._harness(defaults=TODO_POLICY["defaults"])
        replacement = {"caps": TODO_POLICY["caps"]}
        status, body, _ = self._save(harness, policy=replacement)
        self.assertEqual(status, 200, body)
        assert isinstance(body, dict)
        before = AliasPolicy.from_dict({"defaults": TODO_POLICY["defaults"]})
        after = AliasPolicy.from_dict(replacement)
        assert before is not None and after is not None
        self.assertEqual(body["policy"]["state"], "changed")
        self.assertEqual(body["policy"]["before"], before.to_dict())
        self.assertEqual(body["policy"]["after"], after.to_dict())
        self.assertEqual(harness.config.snapshot().aliases["coding"].policy, after)
        # The new policy is the one in force: the old default is gone.
        status, _, _ = harness.request("POST", "/v1/chat/completions", body=chat_body(temperature=0.7))
        self.assertEqual(status, 200)
        self.assertEqual(harness.adapter.calls[0].payload["temperature"], 0.7)

    def test_a_resubmitted_identical_policy_reports_unchanged(self) -> None:
        harness = self._harness(caps=TODO_POLICY["caps"])
        status, body, _ = self._save(harness, policy={"caps": TODO_POLICY["caps"]})
        self.assertEqual(status, 200, body)
        assert isinstance(body, dict)
        self.assertEqual(body["policy"]["state"], "unchanged")

    def test_an_explicit_null_clears_the_policy_and_says_so(self) -> None:
        harness = self._harness(deny=TODO_POLICY["deny"])
        status, body, _ = self._save(harness, policy=None)
        self.assertEqual(status, 200, body)
        assert isinstance(body, dict)
        self.assertEqual(body["policy"]["state"], "cleared")
        self.assertIsNone(harness.config.snapshot().aliases["coding"].policy)
        # With the policy cleared, the deny rule no longer forces `store`.
        status, _, _ = harness.request("POST", "/v1/chat/completions", body=chat_body())
        self.assertEqual(status, 200)
        self.assertNotIn("store", harness.adapter.calls[0].payload)

    def test_an_invalid_explicit_policy_is_refused_and_the_policy_survives(self) -> None:
        harness = self._harness(defaults=TODO_POLICY["defaults"])
        status, body, _ = self._save(harness, policy={"defaults": {"temperature": 99}})
        self.assertEqual(status, 400, body)
        assert isinstance(body, dict)
        self.assertEqual(body["error"]["field"], "policy")
        self.assertEqual(
            harness.config.snapshot().aliases["coding"].policy,
            AliasPolicy.from_dict({"defaults": TODO_POLICY["defaults"]}),
        )

    def test_a_save_of_an_alias_without_a_policy_reports_none(self) -> None:
        harness = self._harness()
        status, body, _ = self._save(harness)
        self.assertEqual(status, 200, body)
        assert isinstance(body, dict)
        self.assertEqual(body["policy"]["state"], "none")
        self.assertIsNone(harness.config.snapshot().aliases["coding"].policy)


if __name__ == "__main__":
    unittest.main()
