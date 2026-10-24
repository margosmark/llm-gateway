"""A rejected request must be attributable.

The 2026-10-24 incident this module exists for: 93 ``malformed`` 502s over
33 minutes, every one of them opaque. The rejection body was discarded, the
502 access-log line named no alias and no route, and the durable ledger
carried no usable row - so nobody could say *which* field the provider had
refused, or even which client was calling.

These tests pin the three fixes:

* the field names a shape rejection names are carried on the error, so
  ``attribution.attempts[].reason`` says what was refused;
* a failed request is recorded and logged like a served one - alias,
  provider, model;
* an owner who has defined a safe policy for ``malformed`` failures gets the
  fallback route instead of a 502, and the failure is still recorded.

They also pin the boundary that makes carrying those names safe: only
identifier-shaped tokens leave an upstream body. Prompt text, quoted values
and arbitrary provider prose must never reach the ledger.

No test contacts a real provider.
"""

from __future__ import annotations

import io
import json
import unittest

from gateway.adapters.base import (
    AdapterError,
    error_from_response,
    rejected_fields_from_payload,
    sanitize_rejected_fields,
)
from gateway.adapters.chatgpt_codex import CODEX_UNSUPPORTED_PAYLOAD_FIELDS
from gateway.config import Alias, ModelEntry, ProviderState, RouteRef
from gateway.logging import RequestLogger
from gateway.observability import RequestLedger
from tests.test_end_to_end import FAKE_PROVIDER, FakeAdapter, GatewayHarness

#: The body the Codex backend returns for a rejected field. Documented in
#: docs/PROVIDER_CONTRACTS.md and reproduced by the live sweep.
UNSUPPORTED_FIELD_BODY = json.dumps(
    {"detail": [{"type": "value_error", "loc": ["body", "temperature"], "msg": "Unsupported parameter: temperature"}]}
).encode("utf-8")


def rejection(payload: dict) -> AdapterError:
    """A malformed 400 exactly as the gateway classifies it upstream."""
    return error_from_response(
        400,
        json.dumps(payload).encode("utf-8"),
        FAKE_PROVIDER,
        payload=payload,
    )


def _alias(primary: RouteRef, **kwargs) -> Alias:
    alias = Alias(name="coding", enabled=True, primary=primary)
    for key, value in kwargs.items():
        setattr(alias, key, value)
    return alias


class RejectedFieldExtractionTests(unittest.TestCase):
    """What may leave an upstream rejection body: names, and nothing else."""

    def test_a_validation_body_names_the_field_it_refused(self) -> None:
        error = rejection({"detail": [{"type": "value_error", "loc": ["body", "temperature"], "msg": "must be <= 2"}]})
        self.assertEqual(error.error_class, "malformed")
        self.assertEqual(error.rejected_fields, ("temperature",))

    def test_a_nested_validation_body_names_the_top_level_field(self) -> None:
        error = rejection({"detail": [{"loc": ["body", "stream_options", "include_usage"], "msg": "unexpected"}]})
        self.assertEqual(error.rejected_fields, ("stream_options",))

    def test_the_documented_unsupported_parameter_message_names_the_field(self) -> None:
        error = rejection({"detail": [{"msg": "Unsupported parameter: max_tokens"}]})
        self.assertEqual(error.rejected_fields, ("max_tokens",))

    def test_a_list_of_entries_keeps_every_name_it_named(self) -> None:
        error = rejection(
            {
                "detail": [
                    {"loc": ["body", "temperature"], "msg": "extra"},
                    {"msg": "Unsupported parameter: max_tokens"},
                ]
            }
        )
        self.assertEqual(error.rejected_fields, ("temperature", "max_tokens"))

    def test_prose_and_values_are_never_retained(self) -> None:
        # Each of these is a body that names no field. Copying any of them
        # would put provider text - or a caller's own prompt - in the ledger.
        cases = [
            {"detail": [{"msg": "Unsupported parameter: the secret is hunter2"}]},
            {"detail": [{"msg": "Unsupported parameter: " + "x" * 200}]},
            {"detail": [{"msg": "Store must be set to false."}]},
            {"detail": "the request body is invalid: messages[0].content is too long"},
            {"detail": [{"msg": "Unsupported parameter: 123"}]},
            {"detail": [{"msg": "Unsupported parameter: "}]},
            "a plain string body",
            {"detail": [{"loc": "body.temperature", "msg": "extra"}]},
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                self.assertEqual(rejected_fields_from_payload(payload), ())

    def test_only_identifier_shaped_names_survive_the_gate(self) -> None:
        self.assertEqual(
            sanitize_rejected_fields(["temperature", "temperature", " max_tokens ", "", "not a name", None, 7]),
            ("temperature", "max_tokens"),
        )
        self.assertEqual(sanitize_rejected_fields("temperature"), ())
        self.assertEqual(sanitize_rejected_fields(["a" * 100]), ())

    def test_a_non_shape_failure_keeps_no_field_names(self) -> None:
        # Classification decides what is extracted. A rate limit that echoes
        # a validation body is still a rate limit, and carries no names.
        from gateway.config import ERROR_RATE_LIMITED

        error = error_from_response(
            429,
            b'{"detail": [{"loc": ["body", "temperature"], "msg": "extra"}]}',
            FAKE_PROVIDER,
            payload={"detail": [{"loc": ["body", "temperature"], "msg": "extra"}]},
        )
        self.assertEqual(error.error_class, ERROR_RATE_LIMITED)
        self.assertEqual(error.rejected_fields, ())

    def test_to_dict_reports_the_names_without_the_body(self) -> None:
        detail = rejection({"detail": [{"loc": ["body", "temperature"], "msg": "Unsupported parameter: temperature"}]}).to_dict()
        self.assertEqual(detail["rejected_fields"], ["temperature"])
        self.assertNotIn("detail", json.dumps(detail))
        # An error that named nothing reports null, not an empty list.
        self.assertIsNone(AdapterError("malformed").to_dict()["rejected_fields"])


class FailedRequestAttributionTests(unittest.TestCase):
    """A 502 must name its alias and the route it died on."""

    def _harness(self, adapter: FakeAdapter, alias: Alias) -> GatewayHarness:
        harness = GatewayHarness(adapter)
        self.addCleanup(harness.close)
        with harness.config.mutate() as config:
            config.aliases["coding"] = alias
        return harness

    def test_the_durable_ledger_records_the_failed_route_and_the_field(self) -> None:
        adapter = FakeAdapter(fail_with=rejection({"detail": [{"msg": "Unsupported parameter: temperature"}]}))
        harness = self._harness(adapter, _alias(RouteRef(FAKE_PROVIDER, "gpt-6-luna")))
        ledger = RequestLedger(persistence_path=harness.data_dir / "request-ledger.jsonl")
        harness.router._ledger = ledger
        harness.app.ledger = ledger

        status, payload, _ = harness.request(
            "POST",
            "/v1/chat/completions",
            body={"model": "coding", "messages": [{"role": "user", "content": "hi"}], "temperature": 0.3},
        )
        self.assertEqual(status, 502, payload)
        self.assertTrue(ledger.flush())
        ledger.close()

        rows = [json.loads(line) for line in (harness.data_dir / "request-ledger.jsonl").read_text().splitlines()]
        self.assertEqual(len(rows), 1, rows)
        row = rows[0]
        self.assertEqual(row["requested_alias"], "coding")
        self.assertEqual(row["status"], 502)
        self.assertEqual(row["error_class"], "malformed")
        # The route that failed, not null, null.
        self.assertEqual(row["serving_provider"], FAKE_PROVIDER)
        self.assertEqual(row["serving_model"], "gpt-6-luna")
        # And the field that made it fail.
        self.assertEqual(row["attempts"][0]["rejected_fields"], ["temperature"])
        self.assertIn("temperature", row["attempts"][0]["reason"])
        # The stored line is still metadata only.
        stored = (harness.data_dir / "request-ledger.jsonl").read_text()
        self.assertNotIn("hi", stored)
        self.assertNotIn("messages", stored)

    def test_the_error_body_carries_the_field_and_not_the_upstream_body(self) -> None:
        adapter = FakeAdapter(fail_with=rejection({"detail": [{"loc": ["body", "temperature"], "msg": "must be <= 2"}]}))
        harness = self._harness(adapter, _alias(RouteRef(FAKE_PROVIDER, "gpt-6-luna")))
        status, payload, _ = harness.request(
            "POST",
            "/v1/chat/completions",
            body={"model": "coding", "messages": [{"role": "user", "content": "hi"}]},
        )
        self.assertEqual(status, 502)
        attempts = payload["error"]["detail"]["attempts"]
        self.assertEqual(attempts[0]["rejected_fields"], ["temperature"])
        self.assertNotIn("must be <= 2", json.dumps(payload))

    def test_the_502_access_log_line_names_the_alias_and_route(self) -> None:
        adapter = FakeAdapter(fail_with=rejection({"detail": [{"msg": "Unsupported parameter: temperature"}]}))
        harness = self._harness(adapter, _alias(RouteRef(FAKE_PROVIDER, "gpt-6-luna")))
        stream = io.StringIO()
        harness.app.logger = RequestLogger(stream=stream, enabled=True)

        harness.request(
            "POST",
            "/v1/chat/completions",
            body={"model": "coding", "messages": [{"role": "user", "content": "hi"}], "stream": True},
        )
        lines = [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]
        self.assertEqual(len(lines), 1, lines)
        line = lines[0]
        self.assertEqual(line["status"], 502)
        self.assertEqual(line["alias"], "coding")
        self.assertEqual(line["provider"], FAKE_PROVIDER)
        self.assertEqual(line["model"], "gpt-6-luna")
        self.assertEqual(line["error_class"], "malformed")
        self.assertTrue(line["streamed"])
        # The body is never logged.
        self.assertNotIn("hi", json.dumps(line))

    def test_the_served_request_still_names_its_route(self) -> None:
        harness = self._harness(FakeAdapter(), _alias(RouteRef(FAKE_PROVIDER, "gpt-6-luna")))
        stream = io.StringIO()
        harness.app.logger = RequestLogger(stream=stream, enabled=True)
        status, _, _ = harness.request(
            "POST", "/v1/chat/completions", body={"model": "coding", "messages": [{"role": "user", "content": "hi"}]}
        )
        self.assertEqual(status, 200)
        line = json.loads(stream.getvalue().splitlines()[0])
        self.assertEqual(line["provider"], FAKE_PROVIDER)
        self.assertEqual(line["model"], "gpt-6-luna")


class OwnerPolicyFallbackTests(unittest.TestCase):
    """The owner's policy, and only the owner's policy, may send
    ``malformed`` to a fallback route."""

    def _harness(self, adapter: FakeAdapter, **alias_kwargs) -> GatewayHarness:
        harness = GatewayHarness(adapter)
        self.addCleanup(harness.close)
        harness.registry.register(FakeAdapter(provider_id="opencode_go", dialect="openai_chat"))
        with harness.config.mutate() as config:
            config.providers["opencode_go"] = ProviderState(
                provider_id="opencode_go",
                kind="opencode_go",
                state="connected",
                models={
                    "mimo-v2.6-flash": ModelEntry(
                        model_id="mimo-v2.6-flash",
                        enabled=True,
                        dialect="openai_chat",
                        source="discovered",
                    )
                },
            )
            alias = _alias(
                RouteRef(FAKE_PROVIDER, "gpt-6-luna"),
                fallbacks=[RouteRef("opencode_go", "mimo-v2.6-flash")],
                fallback_authorized=True,
                fallback_confirmed_providers=["opencode_go"],
                **alias_kwargs,
            )
            config.aliases["coding"] = alias
        return harness

    def test_without_a_policy_the_malformed_failure_is_still_a_502(self) -> None:
        harness = self._harness(
            FakeAdapter(fail_with=rejection({"detail": [{"msg": "Unsupported parameter: temperature"}]}))
        )
        status, payload, _ = harness.request(
            "POST", "/v1/chat/completions", body={"model": "coding", "messages": [{"role": "user", "content": "hi"}]}
        )
        self.assertEqual(status, 502)
        self.assertIn("malformed", payload["error"]["type"])

    def test_with_a_policy_the_request_is_served_by_the_fallback_route(self) -> None:
        fallback = FakeAdapter(provider_id="opencode_go", dialect="openai_chat")
        harness = self._harness(
            FakeAdapter(fail_with=rejection({"detail": [{"msg": "Unsupported parameter: temperature"}]})),
            fallback_error_classes=["malformed"],
        )
        harness.registry.register(fallback)

        status, payload, _ = harness.request(
            "POST", "/v1/chat/completions", body={"model": "coding", "messages": [{"role": "user", "content": "hi"}]}
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(len(fallback.calls), 1)

        row = harness.router._ledger.recent()[0]
        self.assertEqual(row["serving_provider"], "opencode_go")
        self.assertEqual(row["serving_model"], "mimo-v2.6-flash")
        self.assertTrue(row["fallback_used"])
        self.assertEqual(row["attempts"][0]["provider"], FAKE_PROVIDER)
        self.assertEqual(row["attempts"][0]["rejected_fields"], ["temperature"])


class _RealCodexStrip(FakeAdapter):
    """A fake that declares the *shipped* Codex strip list.

    The generic fake declares nothing, because the strip is provider-owned.
    Using the real constant here proves the shipped list covers the payload
    that produced the incident.
    """

    UNSUPPORTED_PAYLOAD_FIELDS = CODEX_UNSUPPORTED_PAYLOAD_FIELDS


class IncidentPayloadTests(unittest.TestCase):
    """The payload that produced the 93 502s is served, not refused."""

    def test_the_decomposers_payload_is_served_with_the_fields_dropped(self) -> None:
        harness = GatewayHarness(adapter=_RealCodexStrip())
        self.addCleanup(harness.close)
        with harness.config.mutate() as config:
            config.aliases["coding"] = _alias(RouteRef(FAKE_PROVIDER, "gpt-6-luna"))

        status, payload, _ = harness.request(
            "POST",
            "/v1/chat/completions",
            body={
                "model": "coding",
                "messages": [{"role": "user", "content": "hi"}],
                # Exactly what the Hermes kanban decomposer sends.
                "temperature": 0.3,
                "max_tokens": 4000,
            },
        )
        self.assertEqual(status, 200, payload)
        sent = harness.adapter.calls[0].payload
        self.assertNotIn("temperature", sent)
        self.assertNotIn("max_tokens", sent)
        self.assertIn("input", sent)
        # The length control crosses in the dialect's own spelling, which
        # the live sweep confirmed the backend accepts (round 2b): the
        # client's value survives the conversion, so nothing is stripped and
        # nothing is recorded for it.
        self.assertEqual(sent["max_output_tokens"], 4000)

        # The substitution that did happen is recorded, never silent.
        row = harness.router._ledger.recent()[0]
        degradation = row["degradations"][0]
        self.assertEqual(degradation["dropped_fields"], ["temperature"])
        self.assertIn("temperature", degradation["reason"])
        self.assertEqual(row["status"], 200)


if __name__ == "__main__":
    unittest.main()
