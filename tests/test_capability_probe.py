"""Bounded, opt-in capability probing — synthetic and offline.

Nothing in this module ever contacts a provider. Every upstream byte these
tests observe comes from a loopback fake or a scripted transport callable.

The product contract under test, in the order the gates fire:

* a probe run exists only for an exact ``(provider_id, model_id)`` pair that
  carries a reviewed protocol, a published price and an explicit owner
  selection — unknown or stale pricing fails closed;
* the owner must authorize the billing route and accept an *estimate*
  (there is no provider-enforced dollar cap, so a hard-cap policy is
  refused rather than pretended);
* at most two one-shot, non-streamed, non-retried requests, bounded in
  time, bytes and output tokens, concurrency 1, cancelable;
* evidence is classified per capability: an error, a mismatch, a missing
  tool call or absent cost accounting is `inconclusive`/`not_tested` and is
  never converted into `false`;
* a returned tool call is inspected as data and never executed;
* a run writes evidence only — no config, routing, cooldown or ledger
  change, and no provider request happens without an explicit run.
"""

from __future__ import annotations

import dataclasses
import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar
from unittest.mock import patch

from gateway.adapters.base import AdapterError, ProviderResponse

from gateway.capability_probe import (
    PROBE_PROTOCOLS,
    BLUE_CIRCLE_COUNT,
    RED_SQUARE_PRESENT,
    ProbeRefused,
    build_preview,
    build_text_request,
    build_visual_request,
    catalog_price_for,
    classify_text,
    classify_visual,
    image_text_matches,
    plan_requests,
    price_status,
    probe_png,
    run_suite,
)

PROTOCOL = PROBE_PROTOCOLS[("opencode_go", "mimo-v2.6-flash")]
NOW = datetime(2026, 10, 29, 12, 0, 0, tzinfo=timezone.utc)

FRESH_PRICE = {
    "input_per_m": 0.14,
    "output_per_m": 0.28,
    "source": "https://opencode.example/data/xiaomi/mimo-v2-6-flash",
    "observed_at": "2026-10-28",
}

STALE_PRICE = dict(FRESH_PRICE, observed_at="2026-01-01")

ALL_PROBES = ["text_generation", "image_input", "function_tools"]


def owner(max_spend: float = 0.05, *, policy: str = "estimate_accepted", authorize: bool = True) -> dict:
    return {
        "authorize_billing_route": authorize,
        "spend_policy": policy,
        "max_spend_estimate_usd": max_spend,
    }


def preview(owner_input: dict | None = None, *, probes=None, price=FRESH_PRICE, revision: int = 11):
    """Build a preview the way the owner API does: price always injected."""
    return build_preview(
        "opencode_go",
        "mimo-v2.6-flash",
        probes if probes is not None else ALL_PROBES,
        revision,
        owner=owner_input,
        now=NOW,
        price_lookup=lambda _provider, _model: price,
    )


class PreviewGateTests(unittest.TestCase):
    def test_unregistered_target_stays_blocked(self):
        result = build_preview("provider-a", "model-x", ["text_generation"], 7, now=NOW)
        self.assertFalse(result["available"])
        self.assertEqual(result["code"], "probe_unavailable")
        self.assertTrue(result["reason"])

    def test_registered_target_without_owner_input_is_blocked_with_a_reason(self):
        result = preview(None)
        self.assertFalse(result["available"])
        self.assertEqual(result["code"], "route_authorization_required")
        self.assertIn("billing route", result["reason"])
        # The full preview shape survives so the console can render it.
        for key in ("provider_id", "model_id", "probes", "suite_version", "reason", "price"):
            self.assertIn(key, result)

    def test_unknown_price_fails_closed_before_authorization_is_considered(self):
        result = preview(owner(), price=None)
        self.assertFalse(result["available"])
        self.assertEqual(result["code"], "price_unavailable")
        self.assertEqual(result["price"]["status"], "unknown")
        self.assertIsNone(result["estimate"]["total_usd"])

    def test_stale_price_fails_closed(self):
        result = preview(owner(), price=STALE_PRICE)
        self.assertFalse(result["available"])
        self.assertEqual(result["code"], "price_unavailable")
        self.assertIn("stale", result["reason"])

    def test_price_for_another_provider_is_never_inherited(self):
        """Pricing evidence is per provider+slug, never deduped by model."""
        with patch("gateway.capability_probe.catalog_model_price", return_value=FRESH_PRICE):
            self.assertIsNone(catalog_price_for("anthropic", "mimo-v2.6-flash"))
            self.assertIsNotNone(catalog_price_for("opencode_go", "mimo-v2.6-flash"))

    def test_hard_cap_policy_is_refused_because_no_provider_cap_is_verified(self):
        result = preview(owner(policy="hard_cap_required"))
        self.assertFalse(result["available"])
        self.assertEqual(result["code"], "hard_cap_unavailable")
        self.assertIn("no provider-enforced", result["reason"])
        self.assertFalse(result["estimate"]["hard_ceiling_available"])

    def test_missing_spend_ceiling_is_refused(self):
        for ceiling in (None, "1", 0):
            with self.subTest(ceiling=ceiling):
                fields = owner()
                if ceiling is None:
                    fields.pop("max_spend_estimate_usd")
                else:
                    fields["max_spend_estimate_usd"] = ceiling
                result = preview(fields)
                self.assertFalse(result["available"])
                self.assertEqual(result["code"], "spend_ceiling_required")

    def test_ceiling_below_the_estimate_is_refused(self):
        result = preview(owner(max_spend=0.0000001))
        self.assertFalse(result["available"])
        self.assertEqual(result["code"], "spend_ceiling_exceeded")

    def test_ceiling_above_the_estimate_is_available(self):
        result = preview(owner())
        self.assertTrue(result["available"], result.get("reason"))
        self.assertEqual(result["code"], "ready")
        self.assertTrue(result["estimate_is_not_a_bill_guarantee"])
        self.assertFalse(result["estimate"]["hard_ceiling_available"])

    def test_invalid_owner_ceilings_fail_closed(self):
        """SEC-1: NaN and ±inf used to pass the gate and open the run.

        Every unusable ceiling — non-finite, non-positive, non-numeric or
        absent — is refused before a confirmation exists, and none of them may
        move the estimate itself: the tariff stays the only authority.
        """
        baseline = preview(owner())["estimate"]["total_usd"]
        invalid = (
            float("nan"),
            float("inf"),
            float("-inf"),
            0,
            -0.01,
            True,
            False,
            "0.5",
            None,
            10**400,  # not representable as a float: behaves like +inf
        )
        for ceiling in invalid:
            with self.subTest(ceiling=ceiling):
                fields = owner()
                if ceiling is None:
                    fields.pop("max_spend_estimate_usd")
                else:
                    fields["max_spend_estimate_usd"] = ceiling
                result = preview(fields)
                self.assertFalse(result["available"])
                self.assertEqual(result["code"], "spend_ceiling_required")
                self.assertNotIn("confirmation", result)
                self.assertFalse(result["estimate"]["hard_ceiling_available"])
                self.assertEqual(result["estimate"]["total_usd"], baseline)

    def test_build_preview_itself_treats_an_unusable_ceiling_as_exceeded(self):
        """Defence in depth: with the owner gate neutralised, a non-finite
        ceiling still cannot read as 'open-ended' (SEC-1)."""
        for ceiling in (float("nan"), float("inf"), 10**400):
            with self.subTest(ceiling=ceiling), patch(
                "gateway.capability_probe._owner_gate", return_value=None
            ):
                result = preview(owner(max_spend=ceiling))
                self.assertFalse(result["available"])
                self.assertEqual(result["code"], "spend_ceiling_exceeded")
                self.assertNotIn("confirmation", result)

    def test_estimate_is_arithmetically_derived_from_the_published_tariff(self):
        result = preview(owner())
        estimate = result["estimate"]
        # 32 + 64 capped completion tokens at the published $0.28/1M.
        self.assertAlmostEqual(estimate["output_component_usd"], 96 * 0.28 / 1_000_000, places=12)
        self.assertEqual(estimate["currency"], "USD")
        self.assertEqual(estimate["price_as_of"], "2026-10-28")
        self.assertEqual(estimate["price_source"], FRESH_PRICE["source"])
        self.assertGreater(estimate["input_component_usd"], 0.0)
        # The input component is a request-bytes upper bound, labelled as
        # non-authoritative because image tokenisation is not documented.
        self.assertEqual(estimate["input_basis"], "request_bytes_upper_bound")
        self.assertFalse(estimate["input_authoritative"])
        self.assertAlmostEqual(
            estimate["total_usd"],
            estimate["output_component_usd"] + estimate["input_component_usd"],
            places=12,
        )
        self.assertEqual(result["price"]["status"], "known")

    def test_response_body_byte_limit_marks_result_inconclusive(self):
        protocol = dataclasses.replace(PROTOCOL, max_response_bytes=32)
        planned = plan_requests(protocol, ["text_generation"])
        outcome = run_suite(
            protocol, ["text_generation"],
            lambda *_a, **_kw: ProviderResponse(status=200, body=b"{" + b" " * 64 + b"}"),
            price=FRESH_PRICE, plan=planned,
        )
        self.assertEqual(outcome.results[0]["status"], "inconclusive")
        self.assertEqual(outcome.results[0]["error_class"], "response_too_large")

    def test_invalid_prices_fail_closed(self):
        for invalid in (float("nan"), float("inf"), float("-inf"), -0.01):
            with self.subTest(invalid=invalid):
                price = dict(FRESH_PRICE, input_per_m=invalid)
                self.assertEqual(price_status(price, NOW), "unknown")
                result = preview(owner(), price=price)
                self.assertFalse(result["available"])
                self.assertEqual(result["code"], "price_unavailable")

    def test_response_for_different_or_missing_model_is_inconclusive(self):
        protocol = PROBE_PROTOCOLS[("opencode_go", "mimo-v2.6-flash")]
        planned = plan_requests(protocol, ["text_generation"])
        nonce = planned[0].nonce
        for reported in ("DIFFERENT-MODEL", None):
            payload = {
                "model": reported,
                "choices": [{"message": {"content": nonce}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "cost": 0.0},
            }
            outcome = run_suite(protocol, ["text_generation"], lambda *_a, **_kw: ProviderResponse(
                status=200, body=json.dumps(payload).encode(),
            ), price=FRESH_PRICE, plan=planned)
            self.assertEqual(outcome.results[0]["status"], "inconclusive")

    def test_price_status_rules(self):
        self.assertEqual(price_status(FRESH_PRICE, NOW), "known")
        self.assertEqual(price_status(STALE_PRICE, NOW), "stale")
        self.assertEqual(price_status({"source": "x"}, NOW), "unknown")
        self.assertEqual(price_status(dict(FRESH_PRICE, observed_at="not-a-date"), NOW), "unknown")
        # An observation date in the future is not evidence either.
        self.assertEqual(price_status(dict(FRESH_PRICE, observed_at="2030-01-01"), NOW), "unknown")

    def test_preview_states_the_execution_bounds(self):
        result = preview(owner())
        self.assertEqual(result["suite_version"], "mvp-1")
        self.assertEqual(result["maximum_requests"], 2)
        self.assertEqual(result["max_output_tokens"], 96)
        self.assertEqual(result["timeout_seconds"], PROTOCOL.request_timeout_seconds)
        self.assertEqual(result["run_deadline_seconds"], PROTOCOL.run_deadline_seconds)
        self.assertEqual((result["concurrency"], result["retries"], result["streaming"]), (1, 0, False))

    def test_single_probe_selection_costs_one_request(self):
        result = preview(owner(), probes=["text_generation"])
        self.assertTrue(result["available"], result.get("reason"))
        self.assertEqual(result["maximum_requests"], 1)

    def test_invalid_selection_is_a_client_error(self):
        for probes in ([], ["magic"], ["text_generation", "text_generation"]):
            with self.subTest(probes=probes), self.assertRaises(ValueError):
                preview(owner(), probes=probes)


class RequestBuilderTests(unittest.TestCase):
    def test_text_request_is_fixed_bounded_and_non_streamed(self):
        payload = build_text_request(PROTOCOL, "a" * 32)
        self.assertEqual(payload["model"], "mimo-v2.6-flash")
        self.assertIs(payload["stream"], False)
        self.assertEqual(payload["max_completion_tokens"], 32)
        self.assertEqual(len(payload["messages"]), 1)
        self.assertEqual(payload["messages"][0]["role"], "user")
        self.assertIn("a" * 32, payload["messages"][0]["content"])
        self.assertNotIn("tools", payload)

    def test_visual_png_is_generated_in_memory_and_small(self):
        png = probe_png()
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
        width = int.from_bytes(png[16:20], "big")
        height = int.from_bytes(png[20:24], "big")
        self.assertEqual((width, height), (160, 96))
        self.assertLessEqual(len(png), PROTOCOL.max_image_bytes)
        # Deterministic: the same recipe always yields the same bytes.
        self.assertEqual(png, probe_png())

    def test_visual_request_carries_an_inline_data_url_and_a_closed_tool(self):
        payload = build_visual_request(PROTOCOL, include_image=True, include_tools=True)
        self.assertEqual(payload["model"], "mimo-v2.6-flash")
        self.assertIs(payload["stream"], False)
        self.assertEqual(payload["max_completion_tokens"], 64)
        content = payload["messages"][0]["content"]
        image_parts = [part for part in content if part.get("type") == "image_url"]
        self.assertEqual(len(image_parts), 1)
        self.assertTrue(image_parts[0]["image_url"]["url"].startswith("data:image/png;base64,"))
        self.assertNotIn("http", image_parts[0]["image_url"]["url"].split("base64,")[0][5:])
        tool = payload["tools"][0]
        schema = tool["function"]["parameters"]
        self.assertEqual(tool["function"]["name"], "probe_visual_answer")
        self.assertEqual(sorted(schema["required"]), ["blue_circle_count", "red_square_present"])
        self.assertIs(schema["additionalProperties"], False)
        self.assertEqual(set(schema["properties"]), {"blue_circle_count", "red_square_present"})

    def test_probe_selection_shapes_the_visual_request(self):
        image_only = build_visual_request(PROTOCOL, include_image=True, include_tools=False)
        self.assertNotIn("tools", image_only)
        tool_only = build_visual_request(PROTOCOL, include_image=False, include_tools=True)
        text = tool_only["messages"][0]["content"]
        self.assertFalse([part for part in text if part.get("type") == "image_url"])


class PlanAndBoundTests(unittest.TestCase):
    def test_plan_groups_three_selected_probes_into_two_requests(self):
        plan = plan_requests(PROTOCOL, ALL_PROBES)
        self.assertEqual(len(plan), 2)
        self.assertEqual(plan[0].capabilities, ("text_generation",))
        self.assertEqual(set(plan[1].capabilities), {"image_input", "function_tools"})
        for item in plan:
            self.assertEqual(item.byte_length, len(_body(item)))

    def test_request_byte_ceiling_is_enforced_before_any_send(self):
        protocol = dataclasses.replace(PROTOCOL, max_request_bytes=64)
        with self.assertRaises(ProbeRefused) as caught:
            plan_requests(protocol, ALL_PROBES)
        self.assertEqual(caught.exception.code, "request_too_large")
        self.assertEqual(caught.exception.status, 422)

    def test_image_byte_ceiling_is_enforced_before_any_send(self):
        protocol = dataclasses.replace(PROTOCOL, max_image_bytes=8)
        with self.assertRaises(ProbeRefused) as caught:
            plan_requests(protocol, ["image_input"])
        self.assertEqual(caught.exception.code, "image_too_large")


def _body(item) -> bytes:
    return json.dumps(item.payload, ensure_ascii=False).encode("utf-8")


def chat_response(*, text=None, tool_calls=None, finish_reason: str | None = "stop", usage=None, cost=0.000001):
    """A Chat Completions answer shaped the way the Go dialect returns one."""
    message: dict = {}
    if text is not None:
        message["content"] = text
    if tool_calls is not None:
        message["content"] = message.get("content")
        message["tool_calls"] = tool_calls
    usage_payload = (
        {"prompt_tokens": 120, "completion_tokens": 40, "cost": cost,
         "opencode": {"billing_source": "plan"}}
        if usage is None else usage
    )
    return {
        "id": "chatcmpl-probe",
        "object": "chat.completion",
        "model": "mimo-v2.6-flash",
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": usage_payload,
    }


def tool_call(name="probe_visual_answer", arguments=None, raw=None):
    return {
        "id": "call_probe",
        "type": "function",
        "function": {
            "name": name,
            "arguments": raw if raw is not None else json.dumps(arguments or {}),
        },
    }


class ClassificationTests(unittest.TestCase):
    def test_text_success_requires_the_exact_nonce_and_a_clean_finish(self):
        nonce = "b" * 32
        self.assertEqual(
            classify_text(chat_response(text=nonce), nonce),
            ("observed_supported", "exact nonce match on a completed response"),
        )

    def test_text_mismatch_is_inconclusive_not_unsupported(self):
        status, summary = classify_text(chat_response(text="here you go: " + "c" * 32), "b" * 32)
        self.assertEqual(status, "inconclusive")
        self.assertNotIn("c" * 32, summary)

    def test_truncated_or_missing_finish_reason_is_inconclusive(self):
        nonce = "d" * 32
        self.assertEqual(classify_text(chat_response(text=nonce, finish_reason="length"), nonce)[0], "inconclusive")
        self.assertEqual(classify_text(chat_response(text=nonce, finish_reason=None), nonce)[0], "inconclusive")

    def test_malformed_response_is_inconclusive(self):
        for body in (None, {}, {"choices": []}, {"choices": [{"message": {}}]}):
            with self.subTest(body=body):
                self.assertEqual(classify_text(body, "e" * 32)[0], "inconclusive")

    def test_a_correct_tool_call_proves_both_tool_transport_and_image_understanding(self):
        payload = chat_response(
            tool_calls=[tool_call(arguments={
                "blue_circle_count": BLUE_CIRCLE_COUNT,
                "red_square_present": RED_SQUARE_PRESENT,
            })],
            finish_reason="tool_calls",
        )
        result = classify_visual(payload, image_probe=True, tool_probe=True)
        self.assertEqual(result["image_input"], "observed_supported")
        self.assertEqual(result["function_tools"], "observed_supported")

    def test_a_schema_valid_call_with_wrong_visual_facts_proves_transport_only(self):
        payload = chat_response(
            tool_calls=[tool_call(arguments={"blue_circle_count": 7, "red_square_present": False})],
            finish_reason="tool_calls",
        )
        result = classify_visual(payload, image_probe=True, tool_probe=True)
        self.assertEqual(result["function_tools"], "observed_supported")
        self.assertEqual(result["image_input"], "inconclusive")

    def test_a_correct_text_answer_proves_image_understanding_only(self):
        payload = chat_response(text="The image has 2 blue circles and 1 red square.")
        result = classify_visual(payload, image_probe=True, tool_probe=True)
        self.assertEqual(result["image_input"], "observed_supported")
        self.assertEqual(result["function_tools"], "inconclusive")

    def test_a_wrong_text_answer_is_inconclusive(self):
        for text in ("3 blue circles and a red square.", "one blue circle and no red square", "I cannot see it."):
            with self.subTest(text=text):
                self.assertFalse(image_text_matches(text))
                result = classify_visual(chat_response(text=text), image_probe=True, tool_probe=True)
                self.assertEqual(result["image_input"], "inconclusive")

    def test_missing_malformed_or_foreign_tool_calls_are_inconclusive(self):
        cases = [
            chat_response(text="2 blue circles, red square present."),
            chat_response(tool_calls=[tool_call(raw="not json")], finish_reason="tool_calls"),
            chat_response(tool_calls=[tool_call(raw='{"blue_circle_count": 2}')], finish_reason="tool_calls"),
            chat_response(tool_calls=[tool_call(arguments={
                "blue_circle_count": "two", "red_square_present": True})], finish_reason="tool_calls"),
            chat_response(tool_calls=[tool_call(arguments={
                "blue_circle_count": 2, "red_square_present": True, "extra": 1})], finish_reason="tool_calls"),
            chat_response(tool_calls=[tool_call(name="some_other_tool", arguments={
                "blue_circle_count": 2, "red_square_present": True})], finish_reason="tool_calls"),
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                result = classify_visual(payload, image_probe=True, tool_probe=True)
                self.assertEqual(result["function_tools"], "inconclusive")

    def test_error_responses_never_become_unsupported(self):
        """The Go error contract for this model is not documented, so every
        failure — including an explicit feature rejection — is inconclusive."""
        from gateway.adapters.base import AdapterError
        from gateway.config import ERROR_MALFORMED, ERROR_POLICY

        for error_class in (ERROR_MALFORMED, ERROR_POLICY):
            with self.subTest(error_class=error_class):
                error = AdapterError(error_class, "provider text is never retained")
                outcome = run_suite(PROTOCOL, ALL_PROBES, _failing_transport(error))
                self.assertEqual(outcome.results[0]["status"], "inconclusive")
                # Nothing ran after the first failure: no retry, no failover.
                self.assertEqual(outcome.request_count, 1)
                self.assertEqual(outcome.blocked_reason, "prior_failure")
                for later in outcome.results[1:]:
                    self.assertEqual(later["status"], "not_tested")

    def test_unselected_probes_run_exactly_one_request(self):
        outcome = run_suite(PROTOCOL, ["text_generation"], _scripted([chat_response(text="x" * 32)]))
        self.assertEqual(len(outcome.results), 1)
        self.assertEqual(outcome.results[0]["capability"], "text_generation")
        self.assertEqual(outcome.request_count, 1)
        self.assertIsNone(outcome.blocked_reason)


class _ScriptedTransport:
    """A scripted transport: answers in order, records every payload."""

    def __init__(self, answers):
        self._answers = list(answers)
        self.payloads: list[dict] = []
        self.session_ids: list[str | None] = []

    def __call__(self, payload, *, timeout, connect_timeout, session_id=None) -> Any:
        from gateway.adapters.base import ProviderResponse

        self.payloads.append(payload)
        self.session_ids.append(session_id)
        if not self._answers:
            raise AssertionError("the runner sent more requests than the script has answers")
        answer = self._answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        body = json.dumps(answer).encode("utf-8")
        return ProviderResponse(status=200, headers={}, body=body, content_type="application/json",
                                dialect="openai_chat")


def _scripted(answers):
    return _ScriptedTransport(answers)


class _EchoTransport(_ScriptedTransport):
    """Answers the text probe with the nonce the runner actually sent.

    The nonce is read out of the prompt the runner built, which keeps the
    echo honest: a real upstream cannot know a nonce it was not given.
    """

    def __call__(self, payload, **kwargs) -> Any:
        messages = payload.get("messages")
        content = messages[0].get("content") if isinstance(messages, list) else None
        if isinstance(content, str):
            self._answers.insert(0, chat_response(text=content.rsplit(": ", 1)[-1]))
        return super().__call__(payload, **kwargs)


def _failing_transport(error, calls=None):
    def transport(payload, *, timeout, connect_timeout, session_id=None):
        if calls is not None:
            calls.append(payload)
        raise error

    return transport


class _RecordingHandler(BaseHTTPRequestHandler):
    """Loopback stand-in for the Go chat endpoint. Records every request."""

    protocol_version = "HTTP/1.1"
    requests: ClassVar[list[dict]] = []
    status: ClassVar[int] = 200
    body: ClassVar[bytes | None] = None
    location: ClassVar[str] = "/elsewhere"

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            parsed = json.loads(raw.decode("utf-8")) if raw else None
        except ValueError:
            parsed = None
        type(self).requests.append(
            {
                "method": self.command,
                "path": self.path,
                "headers": {key.lower(): value for key, value in self.headers.items()},
                "body": parsed,
                "raw": raw,
            }
        )
        if 300 <= type(self).status < 400:
            payload = b""
            self.send_response(type(self).status)
            self.send_header("Location", type(self).location)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        scripted = type(self).body
        payload = scripted if scripted is not None else json.dumps(
            {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
             "usage": {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.000001}}
        ).encode("utf-8")
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args, **kwargs):
        pass


def _go_adapter(server, *, api_key="synthetic-probe-key-0123456789"):
    """The real OpenCode Go adapter, pointed at a loopback fake."""
    from gateway.adapters.http_client import UpstreamClient
    from gateway.adapters.opencode_go import OpenCodeGoAdapter
    from gateway.credentials import CredentialStore

    holder = tempfile.TemporaryDirectory()
    credentials = CredentialStore(Path(holder.name))
    if api_key is not None:
        credentials.provider_handle("opencode_go").write({"api_key": api_key})
    adapter = OpenCodeGoAdapter(
        credentials=credentials.provider_handle("opencode_go"),
        client=UpstreamClient(),
        base_url=f"http://127.0.0.1:{server.server_address[1]}",
    )
    return adapter, holder


class ProbeTransportContractTests(unittest.TestCase):
    """The reviewed transport: one bounded POST, no retry, no redirect."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _RecordingHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self) -> None:
        _RecordingHandler.requests = []
        _RecordingHandler.status = 200
        _RecordingHandler.body = None

    def tearDown(self) -> None:
        _RecordingHandler.requests = []
        if hasattr(self, "_holder"):
            self._holder.cleanup()

    def _adapter(self, **kwargs):
        adapter, holder = _go_adapter(self.server, **kwargs)
        self._holder = holder
        self.addCleanup(adapter._client.close)
        return adapter

    def test_request_targets_the_reviewed_endpoint_with_the_required_headers(self):
        adapter = self._adapter()
        nonce = "f" * 32
        response = adapter.capability_probe_request(
            build_text_request(PROTOCOL, nonce),
            timeout=5.0,
            connect_timeout=2.0,
            session_id="a" * 32,
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(len(_RecordingHandler.requests), 1)
        sent = _RecordingHandler.requests[0]
        self.assertEqual(sent["method"], "POST")
        self.assertEqual(sent["path"], "/openai/v1/chat/completions")
        self.assertEqual(sent["headers"]["user-agent"], "personal-llm-gateway/2.0")
        self.assertEqual(sent["headers"]["x-opencode-session"], "a" * 32)
        self.assertEqual(sent["headers"]["authorization"], "Bearer synthetic-probe-key-0123456789")
        self.assertEqual(sent["headers"]["accept"], "application/json")
        self.assertNotIn("event-stream", sent["headers"]["accept"])
        self.assertEqual(sent["body"]["model"], "mimo-v2.6-flash")
        self.assertEqual(sent["body"]["stream"], False)
        self.assertNotIn("tools", sent["body"])

    def test_a_server_error_is_classified_and_never_retried(self):
        adapter = self._adapter()
        for status in (500, 503, 429, 401):
            with self.subTest(status=status):
                _RecordingHandler.requests = []
                _RecordingHandler.status = status
                with self.assertRaises(AdapterError) as caught:
                    adapter.capability_probe_request(
                        build_text_request(PROTOCOL, "b" * 32),
                        timeout=5.0,
                        connect_timeout=2.0,
                        session_id="c" * 32,
                    )
                self.assertEqual(caught.exception.provider_id, "opencode_go")
                self.assertEqual(len(_RecordingHandler.requests), 1, "one shot: no retry, no failover")

    def test_a_redirect_is_refused_not_followed(self):
        adapter = self._adapter()
        _RecordingHandler.status = 302
        with self.assertRaises(AdapterError):
            adapter.capability_probe_request(
                build_text_request(PROTOCOL, "d" * 32),
                timeout=5.0,
                connect_timeout=2.0,
                session_id="e" * 32,
            )
        self.assertEqual(len(_RecordingHandler.requests), 1, "a redirect must not replay the credential")

    def test_readiness_is_false_without_a_stored_key(self):
        without_key = self._adapter(api_key=None)
        ready, reason = without_key.probe_readiness()
        self.assertFalse(ready)
        self.assertEqual(reason, "provider_not_configured")
        with_key = self._adapter()
        self.assertEqual(with_key.probe_readiness(), (True, ""))

    def test_readiness_never_exposes_the_key(self):
        adapter = self._adapter(api_key="synthetic-probe-key-0123456789")
        ready, reason = adapter.probe_readiness()
        self.assertTrue(ready)
        self.assertNotIn("synthetic-probe-key", reason)


class RunnerBoundsTests(unittest.TestCase):
    """The runner stops early, and every early stop leaves honest evidence."""

    def test_missing_cost_accounting_stops_before_the_second_request(self):
        no_cost = {"prompt_tokens": 10, "completion_tokens": 5}
        transport = _scripted([chat_response(text="a" * 32, usage=no_cost)])
        outcome = run_suite(PROTOCOL, ALL_PROBES, transport, price=FRESH_PRICE)
        self.assertEqual(len(transport.payloads), 1, "the second request must never be sent")
        self.assertEqual(outcome.request_count, 1)
        self.assertEqual(outcome.blocked_reason, "cost_accounting")
        by_capability = {row["capability"]: row for row in outcome.results}
        self.assertEqual(by_capability["text_generation"]["status"], "inconclusive")
        self.assertEqual(by_capability["image_input"]["status"], "not_tested")
        self.assertEqual(by_capability["function_tools"]["status"], "not_tested")

    def test_cancellation_stops_before_the_second_request(self):
        cancel = threading.Event()

        class CancelAfterFirst(_ScriptedTransport):
            def __call__(self, payload, **kwargs):
                result = super().__call__(payload, **kwargs)
                cancel.set()
                return result

        transport = CancelAfterFirst([chat_response(text="a" * 32)])
        outcome = run_suite(PROTOCOL, ALL_PROBES, transport, cancel_event=cancel)
        self.assertEqual(len(transport.payloads), 1)
        self.assertTrue(outcome.cancelled)
        self.assertEqual(outcome.blocked_reason, "cancelled")
        statuses = {row["capability"]: row["status"] for row in outcome.results}
        self.assertEqual(statuses["image_input"], "not_tested")
        self.assertEqual(statuses["function_tools"], "not_tested")

    def test_the_run_deadline_bounds_the_whole_run(self):
        ticks = iter([0.0, 10_000.0])
        transport = _scripted([])
        outcome = run_suite(PROTOCOL, ALL_PROBES, transport, monotonic=lambda: next(ticks))
        self.assertEqual(transport.payloads, [], "a run past its deadline sends nothing")
        self.assertEqual(outcome.request_count, 0)
        self.assertEqual(outcome.blocked_reason, "deadline")
        self.assertTrue(all(row["status"] == "not_tested" for row in outcome.results))

    def test_usage_and_reported_cost_are_kept_as_metadata(self):
        transport = _scripted([
            chat_response(text="a" * 32),
            chat_response(text="2 blue circles and a red square.", finish_reason="stop"),
        ])
        outcome = run_suite(PROTOCOL, ALL_PROBES, transport, price=FRESH_PRICE)
        self.assertEqual(outcome.request_count, 2)
        for row in outcome.results:
            self.assertEqual(row["reported_usage"], {"input_tokens": 120, "output_tokens": 40})
            self.assertEqual(row["spend_estimate"]["currency"], "USD")
            self.assertEqual(row["spend_estimate"]["source"], FRESH_PRICE["source"])
        self.assertAlmostEqual(outcome.reported_cost_usd or 0.0, 0.000002, places=12)

    def test_one_stable_session_id_covers_the_whole_run(self):
        transport = _scripted([
            chat_response(text="a" * 32),
            chat_response(text="2 blue circles and a red square."),
        ])
        outcome = run_suite(PROTOCOL, ALL_PROBES, transport, session_id="9" * 32)
        self.assertEqual(outcome.session_id, "9" * 32)
        self.assertEqual(transport.session_ids, ["9" * 32, "9" * 32])

    def test_evidence_carries_no_response_text(self):
        marker = "LEAKY-RESPONSE-TEXT-MUST-NOT-PERSIST"
        transport = _scripted([
            chat_response(text="a" * 32),
            chat_response(text=marker),
        ])
        outcome = run_suite(PROTOCOL, ALL_PROBES, transport, price=FRESH_PRICE)
        self.assertNotIn(marker, json.dumps(outcome.results))
        self.assertNotIn("a" * 32, json.dumps(outcome.results))

    def test_full_success_records_two_requests_and_three_independent_results(self):
        transport = _EchoTransport([
            chat_response(
                tool_calls=[tool_call(arguments={
                    "blue_circle_count": BLUE_CIRCLE_COUNT,
                    "red_square_present": RED_SQUARE_PRESENT})],
                finish_reason="tool_calls",
            ),
        ])
        outcome = run_suite(PROTOCOL, ALL_PROBES, transport, price=FRESH_PRICE)
        statuses = {row["capability"]: row["status"] for row in outcome.results}
        self.assertEqual(statuses, {
            "text_generation": "observed_supported",
            "image_input": "observed_supported",
            "function_tools": "observed_supported",
        })
        self.assertEqual(outcome.request_count, 2)
        self.assertIsNone(outcome.blocked_reason)

    def test_an_unexpected_transport_exception_fails_closed_with_evidence(self):
        """Whatever escapes the transport is inconclusive evidence, not a 500.

        A cancellation or deadline landing inside an active read can
        surface from inside the HTTP stack as an error no clause names -
        QA re-gate F3 caught an ``AttributeError`` escaping the runner to
        the owner API as an unclassified 500, with the confirmation
        consumed and no run recorded. The runner fails closed instead: the
        probe is inconclusive, the stop reason is honest, and there is
        evidence to record.
        """
        transport = _failing_transport(
            AttributeError("'NoneType' object has no attribute 'close'")
        )
        outcome = run_suite(PROTOCOL, ALL_PROBES, transport)
        self.assertEqual(outcome.request_count, 1)
        self.assertEqual(outcome.blocked_reason, "prior_failure")
        by_capability = {row["capability"]: row for row in outcome.results}
        self.assertEqual(by_capability["text_generation"]["status"], "inconclusive")
        self.assertEqual(by_capability["text_generation"]["error_class"], "transport")
        self.assertEqual(by_capability["image_input"]["status"], "not_tested")
        self.assertEqual(by_capability["function_tools"]["status"], "not_tested")

    def test_an_unexpected_exception_during_a_cancellation_still_reports_it(self):
        """An escape after a cancel is reported as the cancellation it was."""
        cancel = threading.Event()

        class CancelThenEscape(_ScriptedTransport):
            def __call__(self, payload, **kwargs):
                cancel.set()
                raise AttributeError("response raced the cancellation")

        outcome = run_suite(PROTOCOL, ALL_PROBES, CancelThenEscape([]), cancel_event=cancel)
        self.assertTrue(outcome.cancelled)
        self.assertEqual(outcome.blocked_reason, "cancelled")
        by_capability = {row["capability"]: row for row in outcome.results}
        self.assertEqual(by_capability["text_generation"]["status"], "inconclusive")


if __name__ == "__main__":
    unittest.main()
