"""Regression coverage for Messages routing and streaming.

This suite extends ``test_anthropic_surface``/``test_messages_handler`` with
the properties that are only observable end to end over real HTTP:

* **Every one of the five Anthropic-dialect catalogue rows** answers both
  OpenAI surfaces on the *streamed* path too (the existing suite pinned
  streaming for two of them), with the request reaching the provider in the
  Anthropic dialect under the model's own id.
* **Symmetric conversion over the wire**: a request the caller shaped in one
  dialect arrives at the provider shaped in the other, and the response comes
  back in the caller's dialect - text, tools, stop and usage metadata, and
  thinking, on the buffered and the streamed path.
* **``POST /v1/messages`` route selection and shape**: which route served the
  request, the Anthropic Message body, and a *strictly parsed* SSE stream
  (each frame is its ``event:`` line plus its ``data:`` line, the JSON
  ``type`` equals the event name, the sequence terminates with
  ``message_stop``, and no OpenAI ``[DONE]`` sentinel leaks in).
* **Retention and fallback metadata follow the route actually selected**: a
  primary that fails and a cross-provider fallback that serves are reported
  as such in the owner probe and the request ledger, and the retention class
  reported for the serving route is the serving route's own - ``0_days``
  for the fallback model while the ``30_days`` primary sits benched.

The provider is a fake adapter - no test contacts a real provider.
"""

from __future__ import annotations

import json
import unittest

from gateway.adapters.base import AdapterError, ProviderRequest, ProviderResponse
from gateway.config import Alias, ModelEntry, ProviderState, RouteRef
from gateway.core.capabilities import (
    DIALECT_ANTHROPIC_MESSAGES,
    DIALECT_OPENAI_CHAT,
)

from tests.test_anthropic_surface import (
    ANTHROPIC_MODELS,
    ANTHROPIC_RETENTION,
    FakeAnthropicAdapter,
    _anthropic_body,
    _build_harness,
)
from tests.test_end_to_end import FAKE_PROVIDER, GatewayHarness
from tests.test_messages_handler import FakeChatAdapter, _build_chat_harness

#: The alias under test: a 30-day primary with a 0-day cross-provider
#: fallback. Both retention classes exist in the fixture, so a report that
#: follows the wrong route is detectable.
PRIMARY_MODEL = "claude-haiku-5-5"
FALLBACK_PROVIDER = "opencode_go"
FALLBACK_MODEL = "qwen3.8-max"
MIXED_ALIAS = "mixed"


def _sse_frame(event: str, payload: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(payload)}\n\n".encode()


def _anthropic_tool_frames() -> list[bytes]:
    return [
        _sse_frame(
            "message_start",
            {"type": "message_start", "message": {"id": "msg_1", "model": PRIMARY_MODEL,
                                                   "usage": {"input_tokens": 3}}},
        ),
        _sse_frame(
            "content_block_start",
            {"type": "content_block_start", "index": 0,
             "content_block": {"type": "tool_use", "id": "toolu_9", "name": "get", "input": {}}},
        ),
        _sse_frame(
            "content_block_delta",
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "input_json_delta", "partial_json": '{"city":"TO"}'}},
        ),
        _sse_frame("content_block_stop", {"type": "content_block_stop", "index": 0}),
        _sse_frame(
            "message_delta",
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"},
             "usage": {"output_tokens": 6}},
        ),
        _sse_frame("message_stop", {"type": "message_stop"}),
    ]


def _anthropic_thinking_frames() -> list[bytes]:
    return [
        _sse_frame(
            "message_start",
            {"type": "message_start", "message": {"id": "msg_1", "model": PRIMARY_MODEL,
                                                   "usage": {"input_tokens": 3}}},
        ),
        _sse_frame(
            "content_block_start",
            {"type": "content_block_start", "index": 0,
             "content_block": {"type": "thinking", "thinking": ""}},
        ),
        _sse_frame(
            "content_block_delta",
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "thinking_delta", "thinking": "pondering"}},
        ),
        _sse_frame("content_block_stop", {"type": "content_block_stop", "index": 0}),
        _sse_frame(
            "content_block_start",
            {"type": "content_block_start", "index": 1,
             "content_block": {"type": "text", "text": ""}},
        ),
        _sse_frame(
            "content_block_delta",
            {"type": "content_block_delta", "index": 1,
             "delta": {"type": "text_delta", "text": "answer"}},
        ),
        _sse_frame("content_block_stop", {"type": "content_block_stop", "index": 1}),
        _sse_frame(
            "message_delta",
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
             "usage": {"output_tokens": 5}},
        ),
        _sse_frame("message_stop", {"type": "message_stop"}),
    ]


def _chat_chunk(delta: dict, finish: str | None = None, usage: dict | None = None) -> bytes:
    payload: dict = {
        "id": "c1",
        "object": "chat.completion.chunk",
        "model": "m",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    if usage is not None:
        payload["usage"] = usage
    return ("data: " + json.dumps(payload) + "\n\n").encode()


class _StrictSSEMixin(unittest.TestCase):
    """Parse a gateway SSE body the way an Anthropic client must.

    An SSE frame is its data lines plus the blank line that ends it. The
    gateway renders exactly one ``event:`` line and one ``data:`` line per
    frame, so anything else - a bare ``data: [DONE]`` sentinel, a frame
    whose JSON ``type`` disagrees with its event name, a stray line - is a
    framing defect and fails the test here rather than being waved through
    by a substring search.
    """

    def _parse_frames(self, body: bytes) -> list[tuple[str, dict]]:
        self.assertNotIn("[DONE]", body.decode("utf-8"),
                         "the OpenAI stream sentinel has no meaning in the Anthropic dialect")
        frames: list[tuple[str, dict]] = []
        for raw in body.decode("utf-8").split("\n\n"):
            if not raw.strip():
                continue
            lines = raw.split("\n")
            self.assertEqual(len(lines), 2, f"a frame is its event line plus its data line: {raw!r}")
            event_line, data_line = lines
            self.assertTrue(event_line.startswith("event: "), f"missing event line: {raw!r}")
            self.assertTrue(data_line.startswith("data: "), f"missing data line: {raw!r}")
            event = event_line[len("event: "):]
            payload = json.loads(data_line[len("data: "):])
            self.assertIsInstance(payload, dict, raw)
            self.assertEqual(payload.get("type"), event,
                             f"the frame's type must equal its event name: {raw!r}")
            frames.append((event, payload))
        self.assertTrue(frames, "the stream carried no frames")
        return frames


class StreamedFiveModelRoutingTests(unittest.TestCase):
    """All five catalogue rows answer both OpenAI surfaces when streamed."""

    def setUp(self) -> None:
        self.harness = _build_harness()
        self.addCleanup(self.harness.close)

    def _assert_upstream_request(self, model: str) -> None:
        adapter: FakeAnthropicAdapter = self.harness.adapter  # type: ignore[assignment]
        sent: ProviderRequest = adapter.calls[-1]
        self.assertEqual(sent.dialect, DIALECT_ANTHROPIC_MESSAGES, model)
        self.assertEqual(sent.model, model)

    def test_every_catalogued_model_streams_over_chat_completions(self) -> None:
        for model in ANTHROPIC_MODELS:
            with self.subTest(model=model, surface="chat"):
                status, payload, headers = self.harness.request(
                    "POST",
                    "/v1/chat/completions",
                    body={"model": model, "messages": [{"role": "user", "content": "hi"}],
                          "stream": True},
                    raw=True,
                )
                self.assertEqual(status, 200)
                content_type = next(
                    (v for k, v in headers.items() if k.lower() == "content-type"), ""
                )
                self.assertIn("text/event-stream", content_type)
                text = payload.decode()
                self.assertIn("chat.completion.chunk", text)
                self.assertIn('"content":"ok"', text)
                self.assertIn('"finish_reason":"stop"', text)
                self.assertIn('"prompt_tokens":3', text)
                self.assertIn('"completion_tokens":2', text)
                self.assertIn('"total_tokens":5', text)
                self._assert_upstream_request(model)

    def test_every_catalogued_model_streams_over_responses(self) -> None:
        for model in ANTHROPIC_MODELS:
            with self.subTest(model=model, surface="responses"):
                status, payload, headers = self.harness.request(
                    "POST",
                    "/v1/responses",
                    body={"model": model, "input": "hi", "stream": True},
                    raw=True,
                )
                self.assertEqual(status, 200)
                content_type = next(
                    (v for k, v in headers.items() if k.lower() == "content-type"), ""
                )
                self.assertIn("text/event-stream", content_type)
                text = payload.decode()
                self.assertIn("response.output_text.delta", text)
                self.assertIn('"delta":"ok"', text)
                self.assertIn("response.completed", text)
                self.assertNotIn("[DONE]", text)
                self._assert_upstream_request(model)


class SymmetricConversionOverHttpTests(unittest.TestCase):
    """Both directions, both paths: request out, response back, over HTTP."""

    def setUp(self) -> None:
        self.harness = _build_harness()
        self.addCleanup(self.harness.close)

    def test_a_chat_request_converts_tools_stop_and_bound_into_anthropic(self) -> None:
        status, payload, _ = self.harness.request(
            "POST",
            "/v1/chat/completions",
            body={
                "model": "claude-haiku-5-5",
                "messages": [{"role": "user", "content": "weather?"}],
                "tools": [{"type": "function",
                           "function": {"name": "get", "description": "d",
                                        "parameters": {"type": "object"}}}],
                "stop": ["END"],
                "max_tokens": 128,
            },
        )
        self.assertEqual(status, 200, payload)
        adapter: FakeAnthropicAdapter = self.harness.adapter  # type: ignore[assignment]
        sent: ProviderRequest = adapter.calls[-1]
        self.assertEqual(sent.dialect, DIALECT_ANTHROPIC_MESSAGES)
        tool = sent.payload["tools"][0]
        self.assertEqual(tool["name"], "get")
        self.assertEqual(tool["input_schema"], {"type": "object"})
        self.assertNotIn("function", tool, "the Chat wrapper must not reach an Anthropic route")
        self.assertEqual(sent.payload["stop_sequences"], ["END"])
        self.assertEqual(sent.payload["max_tokens"], 128)

    def test_an_anthropic_tool_response_becomes_chat_tool_calls_with_usage(self) -> None:
        self.harness.adapter.buffered_body = _anthropic_body(
            content=[
                {"type": "text", "text": "let me look"},
                {"type": "tool_use", "id": "toolu_1", "name": "get", "input": {"city": "TO"}},
            ],
            usage={"input_tokens": 11, "output_tokens": 7},
        )
        self.harness.adapter.buffered_body["stop_reason"] = "tool_use"
        status, payload, _ = self.harness.request(
            "POST",
            "/v1/chat/completions",
            body={"model": "claude-haiku-5-5",
                  "messages": [{"role": "user", "content": "weather?"}]},
        )
        self.assertEqual(status, 200, payload)
        choice = payload["choices"][0]
        self.assertEqual(choice["finish_reason"], "tool_calls")
        call = choice["message"]["tool_calls"][0]
        self.assertEqual(call["id"], "toolu_1")
        self.assertEqual(call["function"]["name"], "get")
        self.assertEqual(json.loads(call["function"]["arguments"]), {"city": "TO"})
        self.assertEqual(payload["usage"],
                         {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18})

    def test_an_anthropic_tool_stream_becomes_chat_chunks(self) -> None:
        self.harness.adapter.stream_frames = _anthropic_tool_frames()
        status, payload, _ = self.harness.request(
            "POST",
            "/v1/chat/completions",
            body={"model": "claude-haiku-5-5",
                  "messages": [{"role": "user", "content": "weather?"}], "stream": True},
            raw=True,
        )
        self.assertEqual(status, 200)
        text = payload.decode()
        self.assertIn("chat.completion.chunk", text)
        self.assertIn('"id":"toolu_9"', text)
        self.assertIn('"name":"get"', text)
        self.assertIn('arguments', text)
        self.assertIn('"finish_reason":"tool_calls"', text)
        self.assertIn('"prompt_tokens":3', text)
        self.assertIn('"completion_tokens":6', text)

    def test_an_anthropic_thinking_stream_carries_reasoning_content(self) -> None:
        self.harness.adapter.stream_frames = _anthropic_thinking_frames()
        status, payload, _ = self.harness.request(
            "POST",
            "/v1/chat/completions",
            body={"model": "claude-haiku-5-5",
                  "messages": [{"role": "user", "content": "hi"}], "stream": True},
            raw=True,
        )
        self.assertEqual(status, 200)
        text = payload.decode()
        self.assertIn('"reasoning_content":"pondering"', text)
        self.assertIn('"content":"answer"', text)
        self.assertIn('"finish_reason":"stop"', text)
        self.assertIn('"total_tokens":8', text)
        self.assertIn("data: [DONE]", text)

    def test_a_max_tokens_stop_reason_becomes_length_not_stop(self) -> None:
        self.harness.adapter.buffered_body["stop_reason"] = "max_tokens"
        status, payload, _ = self.harness.request(
            "POST",
            "/v1/chat/completions",
            body={"model": "minimax-m3", "messages": [{"role": "user", "content": "hi"}]},
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["choices"][0]["finish_reason"], "length")


class MessagesRouteSelectionTests(unittest.TestCase):
    """``POST /v1/messages`` picks the right route and keeps Anthropic shape."""

    def setUp(self) -> None:
        self.harness = _build_harness()
        self.addCleanup(self.harness.close)
        self.chat = _build_chat_harness()
        self.addCleanup(self.chat.close)

    def test_each_catalogued_model_selects_its_own_anthropic_route(self) -> None:
        adapter: FakeAnthropicAdapter = self.harness.adapter  # type: ignore[assignment]
        for model in ANTHROPIC_MODELS:
            with self.subTest(model=model):
                status, payload, _ = self.harness.request(
                    "POST",
                    "/v1/messages",
                    body={"model": model, "max_tokens": 32,
                          "messages": [{"role": "user", "content": "hi"}]},
                )
                self.assertEqual(status, 200, payload)
                self.assertEqual(payload["type"], "message")
                sent: ProviderRequest = adapter.calls[-1]
                self.assertEqual(sent.dialect, DIALECT_ANTHROPIC_MESSAGES)
                self.assertEqual(sent.model, model, "the alias is replaced before the provider is called")
                self.assertEqual(sent.payload["max_tokens"], 32)

    def test_the_buffered_message_carries_stop_and_usage_metadata(self) -> None:
        self.harness.adapter.buffered_body["stop_reason"] = "max_tokens"
        self.harness.adapter.buffered_body["usage"] = {"input_tokens": 9, "output_tokens": 32}
        status, payload, _ = self.harness.request(
            "POST",
            "/v1/messages",
            body={"model": "minimax-m3", "max_tokens": 32,
                  "messages": [{"role": "user", "content": "hi"}]},
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["type"], "message")
        self.assertEqual(payload["role"], "assistant")
        self.assertEqual(payload["content"], [{"type": "text", "text": "ok"}])
        self.assertEqual(payload["stop_reason"], "max_tokens")
        self.assertIsNone(payload["stop_sequence"])
        self.assertEqual(payload["usage"], {"input_tokens": 9, "output_tokens": 32})

    def test_tools_round_trip_over_the_messages_surface_on_a_native_route(self) -> None:
        self.harness.adapter.buffered_body = _anthropic_body(
            content=[{"type": "tool_use", "id": "toolu_1", "name": "get",
                      "input": {"city": "TO"}}],
            usage={"input_tokens": 5, "output_tokens": 4},
        )
        self.harness.adapter.buffered_body["stop_reason"] = "tool_use"
        status, payload, _ = self.harness.request(
            "POST",
            "/v1/messages",
            body={
                "model": PRIMARY_MODEL,
                "max_tokens": 64,
                "messages": [{"role": "user", "content": "weather?"}],
                "tools": [{"name": "get", "description": "d",
                           "input_schema": {"type": "object"}}],
            },
        )
        self.assertEqual(status, 200, payload)
        # The request reached the provider in the Anthropic shape it was
        # sent in, tools included.
        adapter: FakeAnthropicAdapter = self.harness.adapter  # type: ignore[assignment]
        sent: ProviderRequest = adapter.calls[-1]
        self.assertEqual(sent.payload["tools"][0]["input_schema"], {"type": "object"})
        # The response came back as an Anthropic Message with the tool_use
        # block and the stop/usage metadata intact.
        self.assertEqual(payload["stop_reason"], "tool_use")
        self.assertEqual(payload["usage"], {"input_tokens": 5, "output_tokens": 4})
        block = next(b for b in payload["content"] if b["type"] == "tool_use")
        self.assertEqual(block["id"], "toolu_1")
        self.assertEqual(block["input"], {"city": "TO"})

    def test_an_openai_route_gets_the_converted_request_and_answers_in_anthropic_shape(self) -> None:
        status, payload, _ = self.chat.request(
            "POST",
            "/v1/messages",
            body={
                "model": "mimo-v2.6-flash",
                "max_tokens": 64,
                "system": "Be terse.",
                "messages": [{"role": "user", "content": "hi"}],
                "stop_sequences": ["END"],
                "tools": [{"name": "get", "input_schema": {"type": "object"}}],
            },
        )
        self.assertEqual(status, 200, payload)
        chat: FakeChatAdapter = self.chat.adapter  # type: ignore[assignment]
        sent: ProviderRequest = chat.calls[-1]
        self.assertEqual(sent.dialect, DIALECT_OPENAI_CHAT)
        self.assertEqual(sent.payload["messages"][0], {"role": "system", "content": "Be terse."})
        self.assertEqual(sent.payload["max_completion_tokens"], 64)
        self.assertEqual(sent.payload["stop"], ["END"])
        self.assertEqual(sent.payload["tools"][0]["function"]["name"], "get")
        # ...and the answer is an Anthropic Message, not the Chat body.
        self.assertEqual(payload["type"], "message")
        self.assertEqual(payload["content"], [{"type": "text", "text": "ok"}])
        self.assertEqual(payload["stop_reason"], "end_turn")
        self.assertEqual(payload["usage"], {"input_tokens": 3, "output_tokens": 2})


class MessagesStreamShapeTests(_StrictSSEMixin, unittest.TestCase):
    """The streamed Messages answer is a strict Anthropic event sequence."""

    def setUp(self) -> None:
        self.harness = _build_harness()
        self.addCleanup(self.harness.close)
        self.chat = _build_chat_harness()
        self.addCleanup(self.chat.close)

    def _stream(self, harness: GatewayHarness, body: dict) -> list[tuple[str, dict]]:
        status, payload, headers = harness.request(
            "POST", "/v1/messages", body={**body, "stream": True}, raw=True
        )
        self.assertEqual(status, 200)
        content_type = next((v for k, v in headers.items() if k.lower() == "content-type"), "")
        self.assertIn("text/event-stream", content_type)
        return self._parse_frames(payload)

    def test_a_native_route_emits_the_canonical_event_sequence(self) -> None:
        frames = self._stream(
            self.harness,
            {"model": PRIMARY_MODEL, "max_tokens": 32,
             "messages": [{"role": "user", "content": "hi"}]},
        )
        self.assertEqual(
            [event for event, _ in frames],
            ["message_start", "content_block_start", "content_block_delta",
             "content_block_stop", "message_delta", "message_stop"],
        )
        start = frames[0][1]
        self.assertEqual(start["message"]["model"], PRIMARY_MODEL)
        self.assertEqual(start["message"]["usage"]["input_tokens"], 3)
        delta = next(payload for event, payload in frames if event == "content_block_delta")
        self.assertEqual(delta["delta"], {"type": "text_delta", "text": "ok"})
        stop = next(payload for event, payload in frames if event == "message_delta")
        self.assertEqual(stop["delta"]["stop_reason"], "end_turn")
        self.assertEqual(stop["usage"], {"output_tokens": 2})

    def test_a_tool_stream_keeps_the_tool_use_blocks_on_both_route_kinds(self) -> None:
        self.harness.adapter.stream_frames = _anthropic_tool_frames()
        cases = [
            ("native", self.harness, {**self.harness_config_payload()}),
            ("converting", self.chat, {
                "model": "mimo-v2.6-flash", "max_tokens": 64,
                "messages": [{"role": "user", "content": "weather?"}],
                "tools": [{"name": "get", "input_schema": {"type": "object"}}],
            }),
        ]
        # Script the Chat-dialect upstream for the converting case.
        self.chat.adapter.stream_frames = [
            _chat_chunk({"role": "assistant"}),
            _chat_chunk({"tool_calls": [{"index": 0, "id": "call_7", "type": "function",
                                         "function": {"name": "get",
                                                      "arguments": '{"city":"TO"}'}}]}),
            _chat_chunk({}, "tool_calls", usage={"prompt_tokens": 4, "completion_tokens": 2}),
            b"data: [DONE]\n\n",
        ]
        for kind, harness, body in cases:
            with self.subTest(route=kind):
                frames = self._stream(harness, body)
                events = [event for event, _ in frames]
                self.assertEqual(events[0], "message_start")
                self.assertEqual(events[-1], "message_stop")
                self.assertIn("content_block_start", events)
                start = next(
                    payload for event, payload in frames
                    if event == "content_block_start"
                    and payload["content_block"]["type"] == "tool_use"
                )
                self.assertIn(start["content_block"]["id"], ("toolu_9", "call_7"))
                self.assertEqual(start["content_block"]["name"], "get")
                args = next(
                    payload for event, payload in frames
                    if event == "content_block_delta"
                    and payload["delta"]["type"] == "input_json_delta"
                )
                self.assertTrue(args["delta"]["partial_json"].startswith("{"))
                stop = next(payload for event, payload in frames if event == "message_delta")
                self.assertEqual(stop["delta"]["stop_reason"], "tool_use")
                self.assertIn("output_tokens", stop["usage"])

    def test_thinking_reaches_an_anthropic_client_as_thinking_delta(self) -> None:
        self.chat.adapter.stream_frames = [
            _chat_chunk({"role": "assistant"}),
            _chat_chunk({"reasoning_content": "hmm "}),
            _chat_chunk({"content": "hi"}),
            _chat_chunk({}, "stop", usage={"prompt_tokens": 4, "completion_tokens": 2}),
            b"data: [DONE]\n\n",
        ]
        frames = self._stream(
            self.chat,
            {"model": "mimo-v2.6-flash", "max_tokens": 32,
             "messages": [{"role": "user", "content": "hi"}]},
        )
        events = [event for event, _ in frames]
        self.assertEqual(events[0], "message_start")
        self.assertEqual(events[-1], "message_stop")
        thinking_start = next(
            payload for event, payload in frames
            if event == "content_block_start" and payload["content_block"]["type"] == "thinking"
        )
        self.assertEqual(thinking_start["content_block"]["thinking"], "")
        thinking = next(
            payload for event, payload in frames
            if event == "content_block_delta" and payload["delta"]["type"] == "thinking_delta"
        )
        self.assertEqual(thinking["delta"]["thinking"], "hmm ")
        stop = next(payload for event, payload in frames if event == "message_delta")
        self.assertEqual(stop["delta"]["stop_reason"], "end_turn")
        self.assertEqual(stop["usage"], {"input_tokens": 4, "output_tokens": 2})

    @staticmethod
    def harness_config_payload() -> dict:
        return {"model": PRIMARY_MODEL, "max_tokens": 64,
                "messages": [{"role": "user", "content": "weather?"}],
                "tools": [{"name": "get", "input_schema": {"type": "object"}}]}


class _FailPrimaryAdapter(FakeAnthropicAdapter):
    """Answers normally except for the named models, which fail hard."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.fail_models: set[str] = set()

    def invoke(self, request: ProviderRequest) -> ProviderResponse:
        if request.model in self.fail_models:
            self.calls.append(request)
            raise AdapterError("exhausted", provider_id=self.provider_id)
        return super().invoke(request)


def _build_fallback_harness(
    fail_primary: bool,
) -> tuple[GatewayHarness, _FailPrimaryAdapter, FakeAnthropicAdapter]:
    """A 30-day primary with a 0-day cross-provider fallback.

    The two routes sit under different providers, because the gateway's
    fallback policy is cross-provider by name and an ``exhausted`` primary
    is benched provider-wide.
    """
    primary = _FailPrimaryAdapter()
    if fail_primary:
        primary.fail_models = {PRIMARY_MODEL}
    harness = _build_harness(primary)
    second = FakeAnthropicAdapter(provider_id=FALLBACK_PROVIDER)
    second.buffered_body = _anthropic_body()
    second.buffered_body["model"] = FALLBACK_MODEL
    harness.registry.register(second)
    with harness.config.mutate() as config:
        config.providers[FALLBACK_PROVIDER] = ProviderState(
            provider_id=FALLBACK_PROVIDER,
            kind=FALLBACK_PROVIDER,
            state="connected",
            models={
                FALLBACK_MODEL: ModelEntry(
                    model_id=FALLBACK_MODEL,
                    enabled=True,
                    dialect=DIALECT_ANTHROPIC_MESSAGES,
                    source="discovered",
                    retention_class=ANTHROPIC_RETENTION[FALLBACK_MODEL],
                )
            },
        )
        config.aliases[MIXED_ALIAS] = Alias(
            name=MIXED_ALIAS,
            enabled=True,
            primary=RouteRef(FAKE_PROVIDER, PRIMARY_MODEL),
            fallbacks=[RouteRef(FALLBACK_PROVIDER, FALLBACK_MODEL)],
            fallback_authorized=True,
            fallback_triggers=["exhausted"],
            fallback_confirmed_providers=[FALLBACK_PROVIDER],
        )
    return harness, primary, second


class SelectedRouteRetentionTests(unittest.TestCase):
    """Retention and fallback metadata follow the route that actually served.

    The console's cross-provider fallback warning is only honest if the
    class it shows belongs to the route that served the request. Under this
    alias the two routes disagree: the primary is ``30_days`` and the
    fallback is ``0_days``.
    """

    def _retention_rows(self, state: dict) -> dict[str, str]:
        return {
            model["model_id"]: model["retention_class"]
            for provider in state["providers"]
            for model in provider["models"]
            if model["model_id"] in (PRIMARY_MODEL, FALLBACK_MODEL)
        }

    def _serving_candidate(self, harness: GatewayHarness, provider: str):
        snapshot = harness.config.snapshot()
        candidates = harness.router.candidates(snapshot.aliases[MIXED_ALIAS])
        return next(c for c in candidates if c.route.provider == provider)

    def test_a_served_fallback_is_reported_with_the_fallback_routes_retention(self) -> None:
        harness, primary, second = _build_fallback_harness(fail_primary=True)
        self.addCleanup(harness.close)
        status, payload, _ = harness.request(
            "POST", "/v1/chat/completions",
            body={"model": MIXED_ALIAS, "messages": [{"role": "user", "content": "hi"}]},
        )
        self.assertEqual(status, 200, payload)
        # The fallback provider answered; the benched primary did not.
        self.assertEqual([call.model for call in primary.calls], [PRIMARY_MODEL])
        self.assertEqual([call.model for call in second.calls], [FALLBACK_MODEL])

        status, state, _ = harness.request("GET", "/owner/api/state")
        self.assertEqual(status, 200)
        event = state["ledger"][0]
        self.assertEqual(event["requested_alias"], MIXED_ALIAS)
        self.assertEqual(event["serving_provider"], FALLBACK_PROVIDER)
        self.assertEqual(event["serving_model"], FALLBACK_MODEL)
        self.assertTrue(event["fallback_used"])
        self.assertTrue(event["fallback_reason"])
        self.assertEqual(event["attempts"][0]["model"], PRIMARY_MODEL)
        self.assertEqual(event["attempts"][0]["error_class"], "exhausted")

        # The route that actually served reports its own class - 0_days -
        # while the benched 30_days primary keeps its own.
        rows = self._retention_rows(state)
        self.assertEqual(rows[FALLBACK_MODEL], "0_days")
        self.assertEqual(rows[PRIMARY_MODEL], "30_days")
        served = self._serving_candidate(harness, FALLBACK_PROVIDER)
        self.assertTrue(served.eligible, served.excluded_reason)
        self.assertEqual(served.capability.retention_class, "0_days")

        # The explicit owner probe reports the same serving route.
        status, probe, _ = harness.request(
            "POST", "/owner/api/alias", body={"action": "test", "name": MIXED_ALIAS}
        )
        self.assertEqual(status, 200, probe)
        self.assertEqual(probe["serving_provider"], FALLBACK_PROVIDER)
        self.assertEqual(probe["serving_model"], FALLBACK_MODEL)
        self.assertTrue(probe["fallback_used"])
        self.assertTrue(probe["fallback_reason"])

    def test_a_healthy_primary_reports_itself_and_its_own_retention(self) -> None:
        harness, primary, second = _build_fallback_harness(fail_primary=False)
        self.addCleanup(harness.close)
        status, payload, _ = harness.request(
            "POST", "/v1/chat/completions",
            body={"model": MIXED_ALIAS, "messages": [{"role": "user", "content": "hi"}]},
        )
        self.assertEqual(status, 200, payload)
        status, state, _ = harness.request("GET", "/owner/api/state")
        self.assertEqual(status, 200)
        event = state["ledger"][0]
        self.assertEqual(event["serving_provider"], FAKE_PROVIDER)
        self.assertEqual(event["serving_model"], PRIMARY_MODEL)
        self.assertFalse(event["fallback_used"])
        self.assertIsNone(event["fallback_reason"])

        # Both routes stay eligible and each carries its own class, so the
        # warning follows whichever one is selected after a failure.
        primary_candidate = self._serving_candidate(harness, FAKE_PROVIDER)
        fallback_candidate = self._serving_candidate(harness, FALLBACK_PROVIDER)
        self.assertTrue(primary_candidate.eligible, primary_candidate.excluded_reason)
        self.assertTrue(fallback_candidate.eligible, fallback_candidate.excluded_reason)
        self.assertEqual(primary_candidate.capability.retention_class, "30_days")
        self.assertEqual(fallback_candidate.capability.retention_class, "0_days")

        status, probe, _ = harness.request(
            "POST", "/owner/api/alias", body={"action": "test", "name": MIXED_ALIAS}
        )
        self.assertEqual(status, 200, probe)
        self.assertEqual(probe["serving_model"], PRIMARY_MODEL)
        self.assertFalse(probe["fallback_used"])
        self.assertIsNone(probe["fallback_reason"])

    def test_the_messages_surface_follows_the_same_selected_route(self) -> None:
        harness, primary, second = _build_fallback_harness(fail_primary=True)
        self.addCleanup(harness.close)
        status, payload, _ = harness.request(
            "POST", "/v1/messages",
            body={"model": MIXED_ALIAS, "max_tokens": 32,
                  "messages": [{"role": "user", "content": "hi"}]},
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["type"], "message")
        self.assertEqual(payload["content"], [{"type": "text", "text": "ok"}])
        # The primary was tried and failed; the fallback route served.
        self.assertEqual([call.model for call in primary.calls], [PRIMARY_MODEL])
        self.assertEqual([call.model for call in second.calls], [FALLBACK_MODEL])
        self.assertEqual(second.calls[-1].dialect, DIALECT_ANTHROPIC_MESSAGES)

        status, state, _ = harness.request("GET", "/owner/api/state")
        self.assertEqual(status, 200)
        event = state["ledger"][0]
        self.assertEqual(event["requested_alias"], MIXED_ALIAS)
        self.assertEqual(event["serving_provider"], FALLBACK_PROVIDER)
        self.assertEqual(event["serving_model"], FALLBACK_MODEL)
        self.assertTrue(event["fallback_used"])
        self.assertEqual(self._retention_rows(state)[FALLBACK_MODEL], "0_days")


if __name__ == "__main__":
    unittest.main()
