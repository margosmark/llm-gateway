"""The five catalogued Anthropic-dialect models served over the OpenAI surfaces.

Before this change the router refused any route whose dialect was not an
OpenAI one, so ``claude-haiku-5-5``, ``minimax-m3``, ``qwen3.7-plus``,
``qwen3.8-flash`` and ``qwen3.8-max`` (catalogued as ``anthropic_messages``)
were unreachable. These tests pin the new behaviour end to end over real
HTTP: each model answers a Chat Completions and a Responses request with a
properly converted OpenAI-shaped body, on the buffered and the streamed
path, and a response the target cannot express is refused rather than
silently dropped.

The provider is a fake adapter - no test contacts a real provider.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from gateway.adapters.base import ProviderRequest, ProviderResponse
from gateway.config import Alias, ModelEntry, ProviderState, RouteRef
from gateway.core.capabilities import (
    DIALECT_ANTHROPIC_MESSAGES,
)
from tests.test_end_to_end import FAKE_PROVIDER, FakeAdapter, GatewayHarness

#: Exactly the rows the go catalogue carries with dialect == anthropic_messages.
ANTHROPIC_MODELS = [
    "claude-haiku-5-5",
    "minimax-m3",
    "qwen3.7-plus",
    "qwen3.8-flash",
    "qwen3.8-max",
]

#: The retention class each row publishes. The console's cross-provider
#: fallback warning follows the route actually selected, so the class is a
#: per-model fact: ``claude-haiku-5-5`` sits in a 30-day regime while the
#: other four are 0-day, all under one provider.
ANTHROPIC_RETENTION = {
    "claude-haiku-5-5": "30_days",
    "minimax-m3": "0_days",
    "qwen3.7-plus": "0_days",
    "qwen3.8-flash": "0_days",
    "qwen3.8-max": "0_days",
}


def _catalog_rows() -> dict:
    """The five rows straight from the dated catalogue snapshot."""
    path = Path(__file__).resolve().parents[1] / "data" / "go_catalog.json"
    rows = json.loads(path.read_text(encoding="utf-8"))["models"]
    return {model: rows[model] for model in ANTHROPIC_MODELS}


def _anthropic_body(text: str = "ok", content=None, usage=None) -> dict:
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "claude-haiku-5-5",
        "content": content if content is not None else [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": usage if usage is not None else {"input_tokens": 3, "output_tokens": 2},
    }


def _anthropic_sse(text: str = "ok") -> list[bytes]:
    def frame(obj, event):
        return (f"event: {event}\ndata: {json.dumps(obj)}\n\n").encode()

    return [
        frame(
            {
                "type": "message_start",
                "message": {"id": "msg_test", "model": "claude-haiku-5-5", "usage": {"input_tokens": 3}},
            },
            "message_start",
        ),
        frame({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}, "content_block_start"),
        frame({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}}, "content_block_delta"),
        frame({"type": "content_block_stop", "index": 0}, "content_block_stop"),
        frame(
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 2}},
            "message_delta",
        ),
        frame({"type": "message_stop"}, "message_stop"),
    ]


class FakeAnthropicAdapter(FakeAdapter):
    """A provider that answers in the Anthropic Messages dialect."""

    def __init__(self, **kwargs) -> None:
        kwargs.setdefault("dialect", DIALECT_ANTHROPIC_MESSAGES)
        super().__init__(**kwargs)
        if not kwargs.get("stream_frames"):
            self.stream_frames = _anthropic_sse()
        self.buffered_body: dict = _anthropic_body()

    def invoke(self, request: ProviderRequest) -> ProviderResponse:
        self.calls.append(request)
        if self.fail_with is not None:
            raise self.fail_with
        if request.stream or self.always_stream:
            return ProviderResponse(
                status=200,
                headers={},
                body=None,
                stream=iter(self.stream_frames),
                content_type="text/event-stream",
                dialect=self.dialect,
            )
        body = json.dumps(self.buffered_body).encode("utf-8")
        return ProviderResponse(
            status=200, headers={}, body=body, content_type="application/json", dialect=self.dialect
        )


def _build_harness(adapter: FakeAnthropicAdapter | None = None) -> GatewayHarness:
    harness = GatewayHarness(adapter or FakeAnthropicAdapter())
    with harness.config.mutate() as config:
        config.providers[FAKE_PROVIDER] = ProviderState(
            provider_id=FAKE_PROVIDER,
            kind=FAKE_PROVIDER,
            state="connected",
            models={
                model: ModelEntry(
                    model_id=model,
                    enabled=True,
                    dialect=DIALECT_ANTHROPIC_MESSAGES,
                    source="discovered",
                    # The refresh path copies the published class onto the
                    # entry, so the fixture carries the catalogue's value
                    # rather than a default.
                    retention_class=ANTHROPIC_RETENTION[model],
                )
                for model in ANTHROPIC_MODELS
            },
        )
        # One published alias per catalogue row, so a caller reaches each model
        # through the normal alias gate rather than by raw identifier.
        for model in ANTHROPIC_MODELS:
            config.aliases[model] = Alias(
                name=model, enabled=True, primary=RouteRef(FAKE_PROVIDER, model)
            )
    return harness


class AnthropicModelServabilityTests(unittest.TestCase):
    """The five catalogue rows answer both OpenAI surfaces, not refused."""

    def setUp(self) -> None:
        self.harness = _build_harness()
        self.addCleanup(self.harness.close)

    def test_each_catalogued_model_is_servable_over_chat_completions(self) -> None:
        for model in ANTHROPIC_MODELS:
            with self.subTest(model=model, surface="chat"):
                status, payload, _ = self.harness.request(
                    "POST",
                    "/v1/chat/completions",
                    body={"model": model, "messages": [{"role": "user", "content": "hi"}]},
                )
                self.assertEqual(status, 200, payload)
                self.assertEqual(payload["object"], "chat.completion")
                self.assertEqual(payload["choices"][0]["message"]["content"], "ok")
                self.assertEqual(payload["choices"][0]["finish_reason"], "stop")
                self.assertEqual(payload["usage"]["total_tokens"], 5)

    def test_each_catalogued_model_is_servable_over_responses(self) -> None:
        for model in ANTHROPIC_MODELS:
            with self.subTest(model=model, surface="responses"):
                status, payload, _ = self.harness.request(
                    "POST",
                    "/v1/responses",
                    body={"model": model, "input": "hi"},
                )
                self.assertEqual(status, 200, payload)
                self.assertEqual(payload["object"], "response")
                self.assertEqual(payload["status"], "completed")
                self.assertEqual(payload["output"][0]["content"][0]["text"], "ok")
                self.assertEqual(payload["usage"]["input_tokens"], 3)

    def test_the_request_reaches_the_provider_in_anthropic_dialect_and_the_alias_never_leaks(self) -> None:
        adapter: FakeAnthropicAdapter = self.harness.adapter  # type: ignore[assignment]
        status, _, _ = self.harness.request(
            "POST",
            "/v1/chat/completions",
            body={"model": "claude-haiku-5-5", "messages": [{"role": "user", "content": "hi"}]},
        )
        self.assertEqual(status, 200)
        sent: ProviderRequest = adapter.calls[-1]
        self.assertEqual(sent.dialect, DIALECT_ANTHROPIC_MESSAGES)
        # The alias is replaced by the upstream model id (T4), and the payload
        # is in the Anthropic wire shape rather than the caller's OpenAI one.
        self.assertEqual(sent.model, "claude-haiku-5-5")
        self.assertIn("max_tokens", sent.payload)
        self.assertIn("messages", sent.payload)
        self.assertNotIn("object", sent.payload)


class AnthropicStreamedSurfaceTests(unittest.TestCase):
    """The streamed path converts the Anthropic upstream into an OpenAI stream."""

    def setUp(self) -> None:
        self.harness = _build_harness()
        self.addCleanup(self.harness.close)

    def test_chat_stream_is_converted_from_anthropic(self) -> None:
        status, payload, headers = self.harness.request(
            "POST",
            "/v1/chat/completions",
            body={"model": "claude-haiku-5-5", "messages": [{"role": "user", "content": "hi"}], "stream": True},
            raw=True,
        )
        self.assertEqual(status, 200)
        content_type = next((v for k, v in headers.items() if k.lower() == "content-type"), "")
        self.assertIn("text/event-stream", content_type)
        text = payload.decode()
        self.assertIn("chat.completion.chunk", text)
        self.assertIn('"content":"ok"', text)
        self.assertIn('"finish_reason":"stop"', text)
        self.assertIn('"prompt_tokens":3', text)
        self.assertIn('"completion_tokens":2', text)

    def test_responses_stream_is_converted_from_anthropic(self) -> None:
        status, payload, _ = self.harness.request(
            "POST",
            "/v1/responses",
            body={"model": "minimax-m3", "input": "hi", "stream": True},
            raw=True,
        )
        self.assertEqual(status, 200)
        text = payload.decode()
        self.assertIn("response.output_text.delta", text)
        self.assertIn('"delta":"ok"', text)
        # Terminal response event is always present.
        self.assertIn("response.completed", text)


class AnthropicReasoningSurfaceTests(unittest.TestCase):
    """Anthropic ``thinking`` is carried into the OpenAI shape, not dropped."""

    def setUp(self) -> None:
        self.adapter = FakeAnthropicAdapter()
        self.adapter.buffered_body = _anthropic_body(
            content=[
                {"type": "thinking", "thinking": "private reasoning", "signature": "sig"},
                {"type": "text", "text": "answer"},
            ]
        )
        self.harness = _build_harness(self.adapter)
        self.addCleanup(self.harness.close)

    def test_a_thinking_block_is_carried_as_reasoning_content_on_chat(self) -> None:
        status, payload, _ = self.harness.request(
            "POST",
            "/v1/chat/completions",
            body={"model": "claude-haiku-5-5", "messages": [{"role": "user", "content": "hi"}]},
        )
        self.assertEqual(status, 200, payload)
        message = payload["choices"][0]["message"]
        self.assertEqual(message["content"], "answer")
        # The reasoning is carried, matching the streamed path - not silently
        # dropped, and not refused away.
        self.assertEqual(message["reasoning_content"], "private reasoning")

    def test_a_thinking_block_is_carried_as_a_reasoning_summary_on_responses(self) -> None:
        status, payload, _ = self.harness.request(
            "POST",
            "/v1/responses",
            body={"model": "claude-haiku-5-5", "input": "hi"},
        )
        self.assertEqual(status, 200, payload)
        reasoning = next(item for item in payload["output"] if item["type"] == "reasoning")
        self.assertEqual(reasoning["summary"][0]["text"], "private reasoning")

    def test_a_plain_text_only_response_is_unchanged(self) -> None:
        self.adapter.buffered_body = _anthropic_body()
        status, payload, _ = self.harness.request(
            "POST",
            "/v1/chat/completions",
            body={"model": "qwen3.8-max", "messages": [{"role": "user", "content": "hi"}]},
        )
        self.assertEqual(status, 200, payload)
        message = payload["choices"][0]["message"]
        self.assertEqual(message["content"], "ok")
        self.assertNotIn("reasoning_content", message)


class AnthropicRetentionTests(unittest.TestCase):
    """The retention data follows the route actually selected, per model.

    The console's cross-provider fallback warning is only honest if the class
    it shows belongs to the route that will serve (or fall back to) the
    request. Within this one provider the five Anthropic rows do not agree:
    ``claude-haiku-5-5`` is 30-day, the other four are 0-day.
    """

    def test_the_catalog_publishes_the_documented_retention_classes(self) -> None:
        rows = _catalog_rows()
        for model, expected in ANTHROPIC_RETENTION.items():
            with self.subTest(model=model):
                self.assertEqual(rows[model]["dialect"], DIALECT_ANTHROPIC_MESSAGES)
                self.assertEqual(rows[model]["retention_class"], expected)

    def test_the_state_reports_retention_per_model_not_per_provider(self) -> None:
        harness = _build_harness()
        self.addCleanup(harness.close)
        status, state, _ = harness.request("GET", "/owner/api/state")
        self.assertEqual(status, 200, state)
        rows = {
            model["model_id"]: model["retention_class"]
            for provider in state["providers"]
            for model in provider["models"]
            if model["model_id"] in ANTHROPIC_MODELS
        }
        self.assertEqual(rows, ANTHROPIC_RETENTION)

    def test_the_selected_route_carries_its_own_retention_class(self) -> None:
        harness = _build_harness()
        self.addCleanup(harness.close)
        snapshot = harness.config.snapshot()
        for model, expected in ANTHROPIC_RETENTION.items():
            with self.subTest(model=model):
                candidates = harness.router.candidates(snapshot.aliases[model])
                selected = candidates[0]
                self.assertTrue(selected.eligible, selected.excluded_reason)
                self.assertEqual(selected.route.model, model)
                self.assertIsNotNone(selected.capability)
                self.assertEqual(selected.capability.retention_class, expected)

    def test_a_fallback_route_reports_its_own_retention_not_the_primary_s(self) -> None:
        harness = _build_harness()
        self.addCleanup(harness.close)
        with harness.config.mutate() as config:
            config.aliases["mixed"] = Alias(
                name="mixed",
                enabled=True,
                primary=RouteRef(FAKE_PROVIDER, "claude-haiku-5-5"),
                fallbacks=[RouteRef(FAKE_PROVIDER, "qwen3.8-max")],
            )
        snapshot = harness.config.snapshot()
        candidates = harness.router.candidates(snapshot.aliases["mixed"])
        self.assertEqual(
            [candidate.route.model for candidate in candidates],
            ["claude-haiku-5-5", "qwen3.8-max"],
        )
        self.assertTrue(all(candidate.eligible for candidate in candidates),
                        {candidate.excluded_reason for candidate in candidates})
        # Each route carries its own class, so whatever the router selects
        # (primary today, fallback after an error) is what the warning shows.
        self.assertEqual(candidates[0].capability.retention_class, "30_days")
        self.assertEqual(candidates[1].capability.retention_class, "0_days")


if __name__ == "__main__":
    unittest.main()
