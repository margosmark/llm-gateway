"""Tests for the Anthropic Messages <-> OpenAI-family bridge.

The properties under test are the ones the bridge contract names, applied
to the Anthropic dialect:

* **The conversion is symmetric** in both directions, and works on the
  buffered and the streamed path.
* **Content is preserved exactly** -- text, images, tool calls (with their
  identifiers) and tool results survive a round trip.
* **Nothing is silently dropped**: a field the target dialect cannot
  express is refused with an actionable message.
* **The terminal event is always emitted** and usage counters are carried
  across, with the absence of a counter staying absent.
"""

from __future__ import annotations

import json
import unittest

from gateway.bridge.anthropic import (
    DEFAULT_MAX_TOKENS,
    aggregate_anthropic,
    anthropic_body_to_chat,
    anthropic_body_to_responses,
    convert_buffered_body,
    convert_request,
    convert_stream_bytes,
    from_anthropic_messages,
    payload_has_image_input,
    to_anthropic_messages,
)
from gateway.bridge.model import ConversionError
from gateway.core.capabilities import (
    DIALECT_ANTHROPIC_MESSAGES,
    DIALECT_OPENAI_CHAT,
    DIALECT_OPENAI_RESPONSES,
)


def _sse(obj, event=None):
    prefix = f"event: {event}\n" if event else ""
    return (prefix + "data: " + json.dumps(obj) + "\n\n").encode()


def _frames(blob: bytes):
    """Split an SSE blob into a list of frame bytes, as the upstream yields them."""
    out = []
    for chunk in blob.split(b"\n\n"):
        if chunk.strip():
            out.append(chunk + b"\n\n")
    return out


class RequestConversionTests(unittest.TestCase):
    def test_chat_request_round_trips_through_anthropic(self) -> None:
        original = {
            "model": "coding",
            "messages": [
                {"role": "system", "content": "Be terse."},
                {"role": "user", "content": "hello"},
            ],
            "tools": [
                {"type": "function", "function": {"name": "lookup", "description": "d", "parameters": {"type": "object"}}}
            ],
            "max_tokens": 128,
        }
        as_anthropic = convert_request(original, DIALECT_OPENAI_CHAT, DIALECT_ANTHROPIC_MESSAGES)
        self.assertEqual(as_anthropic["system"], "Be terse.")
        self.assertEqual(as_anthropic["max_tokens"], 128)
        self.assertEqual(as_anthropic["tools"][0]["input_schema"], {"type": "object"})
        self.assertNotIn("reasoning", as_anthropic)

        back = convert_request(as_anthropic, DIALECT_ANTHROPIC_MESSAGES, DIALECT_OPENAI_CHAT)
        self.assertEqual(back["messages"][0], {"role": "system", "content": "Be terse."})
        self.assertEqual(back["messages"][1]["content"], "hello")
        self.assertEqual(back["tools"][0]["function"]["name"], "lookup")

    def test_responses_request_round_trips_through_anthropic(self) -> None:
        original = {
            "model": "coding",
            "instructions": "Be terse.",
            "input": [
                {"role": "user", "content": [{"type": "input_text", "text": "hi"}]},
                {"type": "function_call", "call_id": "call_1", "name": "lookup", "arguments": '{"q":"x"}'},
                {"type": "function_call_output", "call_id": "call_1", "output": "result"},
            ],
            "tools": [{"type": "function", "name": "lookup", "description": "d", "parameters": {"type": "object"}}],
        }
        as_anthropic = convert_request(original, DIALECT_OPENAI_RESPONSES, DIALECT_ANTHROPIC_MESSAGES)
        self.assertEqual(as_anthropic["system"], "Be terse.")
        # The function call becomes a tool_use block on an assistant message;
        # the output becomes a tool_result block on the following user message.
        roles = [m["role"] for m in as_anthropic["messages"]]
        self.assertIn("assistant", roles)
        tool_use = next(
            b for m in as_anthropic["messages"] for b in m["content"] if b.get("type") == "tool_use"
        )
        self.assertEqual(tool_use["id"], "call_1")
        self.assertEqual(tool_use["name"], "lookup")
        self.assertEqual(tool_use["input"], {"q": "x"})
        tool_result = next(
            b for m in as_anthropic["messages"] for b in m["content"] if b.get("type") == "tool_result"
        )
        self.assertEqual(tool_result["tool_use_id"], "call_1")
        self.assertEqual(tool_result["content"], "result")

        back = convert_request(as_anthropic, DIALECT_ANTHROPIC_MESSAGES, DIALECT_OPENAI_RESPONSES)
        kinds = [item.get("type") for item in back["input"]]
        self.assertIn("function_call", kinds)
        self.assertIn("function_call_output", kinds)
        call = next(item for item in back["input"] if item.get("type") == "function_call")
        self.assertEqual(call["call_id"], "call_1")
        self.assertEqual(call["arguments"], '{"q": "x"}')

    def test_a_tool_call_and_its_result_survive_the_round_trip(self) -> None:
        chat = {
            "model": "coding",
            "messages": [
                {"role": "user", "content": "weather?"},
                {"role": "assistant", "content": None, "tool_calls": [
                    {"id": "call_9", "type": "function", "function": {"name": "get", "arguments": '{"city":"TO"}'}}
                ]},
                {"role": "tool", "tool_call_id": "call_9", "content": "sunny"},
            ],
        }
        as_anthropic = convert_request(chat, DIALECT_OPENAI_CHAT, DIALECT_ANTHROPIC_MESSAGES)
        back = convert_request(as_anthropic, DIALECT_ANTHROPIC_MESSAGES, DIALECT_OPENAI_CHAT)
        assistant = back["messages"][1]
        self.assertEqual(assistant["tool_calls"][0]["id"], "call_9")
        self.assertEqual(assistant["tool_calls"][0]["function"]["name"], "get")
        self.assertEqual(assistant["tool_calls"][0]["function"]["arguments"], '{"city": "TO"}')
        self.assertEqual(back["messages"][2]["tool_call_id"], "call_9")
        self.assertEqual(back["messages"][2]["content"], "sunny")

    def test_an_image_survives_into_the_anthropic_shape(self) -> None:
        chat = {
            "model": "coding",
            "messages": [
                {"role": "user", "content": [
                    {"type": "text", "text": "what is this"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
                ]}
            ],
        }
        as_anthropic = convert_request(chat, DIALECT_OPENAI_CHAT, DIALECT_ANTHROPIC_MESSAGES)
        image = as_anthropic["messages"][0]["content"][1]
        self.assertEqual(image["type"], "image")
        self.assertEqual(image["source"]["type"], "base64")
        self.assertEqual(image["source"]["data"], "AAAA")

    def test_payload_has_image_input_reads_the_anthropic_shape(self) -> None:
        with_image = {
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": "see"},
                {"type": "image", "source": {"type": "url", "url": "http://x/y.png"}},
            ]}]
        }
        self.assertTrue(payload_has_image_input(with_image, DIALECT_ANTHROPIC_MESSAGES))
        self.assertFalse(payload_has_image_input({"messages": [{"role": "user", "content": "hi"}]}, DIALECT_ANTHROPIC_MESSAGES))

    def test_max_tokens_defaults_when_the_caller_expressed_no_bound(self) -> None:
        as_anthropic = convert_request(
            {"model": "coding", "messages": [{"role": "user", "content": "hi"}]},
            DIALECT_OPENAI_CHAT,
            DIALECT_ANTHROPIC_MESSAGES,
        )
        self.assertEqual(as_anthropic["max_tokens"], DEFAULT_MAX_TOKENS)

    def test_a_reasoning_level_is_not_rendered_into_a_thinking_budget(self) -> None:
        # There is no published level->budget mapping, so the control is not
        # invented. The router drops the level for the route and records the
        # substitution (R19); the conversion must not smuggle a budget in.
        as_anthropic = convert_request(
            {"model": "coding", "messages": [{"role": "user", "content": "hi"}], "reasoning_effort": "high"},
            DIALECT_OPENAI_CHAT,
            DIALECT_ANTHROPIC_MESSAGES,
        )
        self.assertNotIn("thinking", as_anthropic)


class RequestRefusalTests(unittest.TestCase):
    def test_an_anthropic_thinking_budget_is_refused_on_an_openai_target(self) -> None:
        payload = {
            "model": "claude-haiku-5-5",
            "max_tokens": 10,
            "messages": [{"role": "user", "content": "hi"}],
            "thinking": {"type": "enabled", "budget_tokens": 1024},
        }
        with self.assertRaises(ConversionError) as ctx:
            convert_request(payload, DIALECT_ANTHROPIC_MESSAGES, DIALECT_OPENAI_CHAT)
        self.assertEqual(ctx.exception.capability, "thinking")
        self.assertIn("budget", ctx.exception.message)

    def test_top_k_is_refused_on_an_openai_target(self) -> None:
        payload = {"model": "m", "max_tokens": 5, "messages": [], "top_k": 40}
        with self.assertRaises(ConversionError) as ctx:
            from_anthropic_messages(payload)
        self.assertEqual(ctx.exception.capability, "top_k")

    def test_a_forced_tool_choice_is_refused_on_an_openai_target(self) -> None:
        payload = {"model": "m", "max_tokens": 5, "messages": [], "tool_choice": {"type": "tool", "name": "x"}}
        with self.assertRaises(ConversionError) as ctx:
            from_anthropic_messages(payload)
        self.assertEqual(ctx.exception.capability, "tool_choice")

    def test_a_thinking_block_in_messages_is_refused(self) -> None:
        payload = {
            "model": "m",
            "max_tokens": 5,
            "messages": [{"role": "assistant", "content": [{"type": "thinking", "thinking": "hmm", "signature": "s"}]}],
        }
        with self.assertRaises(ConversionError) as ctx:
            from_anthropic_messages(payload)
        self.assertEqual(ctx.exception.capability, "thinking")

    def test_hosted_tools_are_refused_on_an_anthropic_target(self) -> None:
        request = from_anthropic_messages({"model": "m", "messages": [], "tools": [{"type": "web_search_20250305"}]})
        self.assertEqual(len(request.hosted_tools), 1)
        with self.assertRaises(ConversionError) as ctx:
            to_anthropic_messages(request)
        self.assertEqual(ctx.exception.capability, "hosted_tools")

    def test_previous_response_id_is_refused_on_an_anthropic_target(self) -> None:
        request = from_anthropic_messages({"model": "m", "messages": []})
        request.previous_response_id = "resp_123"
        with self.assertRaises(ConversionError) as ctx:
            to_anthropic_messages(request)
        self.assertEqual(ctx.exception.capability, "previous_response_id")

    def test_a_reasoning_summary_is_refused_on_an_anthropic_target(self) -> None:
        request = from_anthropic_messages({"model": "m", "messages": []})
        request.reasoning_summary = "detailed"
        with self.assertRaises(ConversionError) as ctx:
            to_anthropic_messages(request)
        self.assertEqual(ctx.exception.capability, "reasoning_summary")


class ImageAndToolBlockRefusalTests(unittest.TestCase):
    """F1/F2/F3 at the bridge level: no image is read or lost by accident.

    ``payload_has_image_input`` (routing) and ``convert_request`` (rendering)
    must agree on what counts as an image, every image URL must pass the
    wave-1.6r validation before it reaches a content part, and a block the
    bridge cannot render must raise instead of disappearing.
    """

    MODEL = {"model": "claude-haiku-5-5", "max_tokens": 8}

    def _request(self, *blocks: dict, dialect: str = DIALECT_OPENAI_CHAT):
        return convert_request(
            {**self.MODEL, "messages": [{"role": "user", "content": list(blocks)}]},
            DIALECT_ANTHROPIC_MESSAGES, dialect)

    @staticmethod
    def _payload(*blocks: dict) -> dict:
        return {"messages": [{"role": "user", "content": list(blocks)}]}

    def test_both_image_spellings_are_detected(self) -> None:
        image = {"type": "image", "source": {"type": "url", "url": "https://x/y.png"}}
        input_image = {"type": "input_image", "image_url": "https://x/y.png"}
        nested = {"type": "tool_result", "tool_use_id": "t1", "content": [image]}
        self.assertTrue(payload_has_image_input(self._payload(image),
                                                DIALECT_ANTHROPIC_MESSAGES))
        self.assertTrue(payload_has_image_input(self._payload(input_image),
                                                DIALECT_ANTHROPIC_MESSAGES))
        self.assertTrue(payload_has_image_input(self._payload(nested),
                                                DIALECT_ANTHROPIC_MESSAGES))
        self.assertFalse(payload_has_image_input(
            self._payload({"type": "text", "text": "hi"}),
            DIALECT_ANTHROPIC_MESSAGES))

    def test_detection_and_conversion_never_disagree(self) -> None:
        """Whatever detection calls an image, conversion must render."""
        cases = [
            ({"type": "image", "source": {"type": "url", "url": "https://x/y.png"}},
             "y.png"),
            ({"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                          "data": "aGVsbG8="}},
             "aGVsbG8="),
            ({"type": "input_image", "image_url": "https://x/y.png"}, "y.png"),
        ]
        for block, needle in cases:
            with self.subTest(block=block["type"]):
                self.assertTrue(payload_has_image_input(self._payload(block),
                                                        DIALECT_ANTHROPIC_MESSAGES))
                rendered = json.dumps(self._request(block))
                self.assertIn(needle, rendered)

    def test_an_image_url_the_control_rejects_is_refused_by_the_anthropic_parser(self) -> None:
        for label, source in {
            "file scheme": {"type": "url", "url": "file:///etc/passwd"},
            "ftp scheme": {"type": "url", "url": "ftp://internal/x.png"},
            "credentialed": {"type": "url", "url": "https://u:p@internal/x.png"},
            "control chars": {"type": "url", "url": "https://x/y.png\nX: 1"},
            "unknown format": {"type": "unknown_format"},
            "missing": None,
        }.items():
            with self.subTest(case=label):
                with self.assertRaises(ConversionError) as ctx:
                    self._request({"type": "image", "source": source})
                self.assertEqual(ctx.exception.capability, "image_input")

    def test_an_input_image_url_is_validated_too(self) -> None:
        with self.assertRaises(ConversionError) as ctx:
            self._request({"type": "input_image", "image_url": "ftp://x/y.png"})
        self.assertEqual(ctx.exception.capability, "image_input")
        with self.assertRaises(ConversionError) as ctx:
            self._request({"type": "input_image", "image_url": None})
        self.assertEqual(ctx.exception.capability, "image_input")

    def test_a_bogus_base64_media_type_is_refused(self) -> None:
        with self.assertRaises(ConversionError) as ctx:
            self._request({"type": "image", "source": {
                "type": "base64", "media_type": "text/html",
                "data": "not-valid-base64!!!"}})
        self.assertEqual(ctx.exception.capability, "image_input")

    def test_detection_refuses_what_conversion_would_refuse(self) -> None:
        """Routing must fail closed *before* a route is chosen (F2/F3)."""
        for block in (
            {"type": "image", "source": {"type": "url", "url": "file:///etc/passwd"}},
            {"type": "input_image", "image_url": "ftp://x/y.png"},
            {"type": "image", "source": {"type": "unknown_format"}},
            {"type": "input_image", "image_url": None},
        ):
            with self.subTest(block=block["type"]):
                with self.assertRaises(ConversionError) as ctx:
                    payload_has_image_input(self._payload(block),
                                            DIALECT_ANTHROPIC_MESSAGES)
                self.assertEqual(ctx.exception.capability, "image_input")

    def test_a_tool_result_cannot_carry_an_image_as_function_output(self) -> None:
        """The OpenAI function-output string cannot hold an image: refuse.

        Nothing may be dropped, so the choice is between converting the
        image (impossible in this dialect) and refusing with an actionable
        message - never serving 200 with the attachment gone.
        """
        with self.assertRaises(ConversionError) as ctx:
            self._request({"type": "tool_result", "tool_use_id": "t1", "content": [
                {"type": "image", "source": {"type": "url", "url": "https://x/y.png"}},
                {"type": "text", "text": "caption"},
            ]})
        self.assertEqual(ctx.exception.capability, "tool_result")

    def test_a_text_only_tool_result_still_converts(self) -> None:
        request = self._request(
            {"type": "tool_result", "tool_use_id": "t1", "content": "done"})
        rendered = json.dumps(request)
        self.assertIn("done", rendered)

    def test_a_non_object_tool_use_input_is_refused_not_replaced(self) -> None:
        for bad in ("not-an-object", ["a"], 7, None):
            with self.subTest(input=bad):
                with self.assertRaises(ConversionError) as ctx:
                    self._request({"type": "tool_use", "id": "t1", "name": "search",
                                   "input": bad})
                self.assertEqual(ctx.exception.capability, "tool_use")

    def test_an_object_tool_use_input_is_still_serialised(self) -> None:
        request = self._request({"type": "tool_use", "id": "t1", "name": "search",
                                 "input": {"q": "x"}})
        arguments = json.loads(
            request["messages"][0]["tool_calls"][0]["function"]["arguments"])
        self.assertEqual(arguments, {"q": "x"})

    def test_a_native_anthropic_target_keeps_the_tool_result_whole(self) -> None:
        """No re-encoding: the dialect that can express it is handed it as-is."""
        block = {"type": "tool_result", "tool_use_id": "t1", "content": [
            {"type": "image", "source": {"type": "url", "url": "https://x/y.png"}}]}
        request = self._request(block, dialect=DIALECT_ANTHROPIC_MESSAGES)
        rendered = json.dumps(request)
        self.assertIn("y.png", rendered)

    def test_input_image_also_converts_on_the_responses_target(self) -> None:
        request = self._request({"type": "input_image", "image_url": "https://x/y.png"},
                                dialect=DIALECT_OPENAI_RESPONSES)
        self.assertIn("y.png", json.dumps(request))


class BufferedResponseTests(unittest.TestCase):
    def _anthropic_body(self):
        return {
            "id": "msg_abc123",
            "type": "message",
            "role": "assistant",
            "model": "claude-haiku-5-5",
            "content": [{"type": "text", "text": "hello"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 5, "output_tokens": 2},
        }

    def test_anthropic_body_to_chat(self) -> None:
        chat = anthropic_body_to_chat(self._anthropic_body())
        self.assertEqual(chat["object"], "chat.completion")
        self.assertEqual(chat["choices"][0]["message"]["content"], "hello")
        self.assertEqual(chat["choices"][0]["finish_reason"], "stop")
        self.assertEqual(chat["usage"]["prompt_tokens"], 5)
        self.assertEqual(chat["usage"]["completion_tokens"], 2)
        self.assertEqual(chat["usage"]["total_tokens"], 7)
        self.assertEqual(chat["id"], "chatcmpl-abc123")

    def test_anthropic_body_to_responses(self) -> None:
        responses = anthropic_body_to_responses(self._anthropic_body())
        self.assertEqual(responses["object"], "response")
        self.assertEqual(responses["status"], "completed")
        self.assertEqual(responses["output"][0]["content"][0]["text"], "hello")
        self.assertEqual(responses["usage"]["input_tokens"], 5)
        self.assertEqual(responses["id"], "resp_abc123")

    def test_tool_use_maps_to_tool_calls(self) -> None:
        body = self._anthropic_body()
        body["content"] = [
            {"type": "text", "text": "let me look"},
            {"type": "tool_use", "id": "toolu_1", "name": "get", "input": {"city": "TO"}},
        ]
        body["stop_reason"] = "tool_use"
        chat = anthropic_body_to_chat(body)
        message = chat["choices"][0]["message"]
        self.assertEqual(chat["choices"][0]["finish_reason"], "tool_calls")
        self.assertEqual(message["tool_calls"][0]["id"], "toolu_1")
        self.assertEqual(message["tool_calls"][0]["function"]["arguments"], '{"city": "TO"}')

    def test_max_tokens_stop_reason_maps_to_length(self) -> None:
        body = self._anthropic_body()
        body["stop_reason"] = "max_tokens"
        self.assertEqual(anthropic_body_to_chat(body)["choices"][0]["finish_reason"], "length")
        self.assertEqual(anthropic_body_to_responses(body)["status"], "incomplete")

    def test_chat_body_to_anthropic(self) -> None:
        chat = {
            "id": "chatcmpl-xyz",
            "object": "chat.completion",
            "model": "coding",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 1},
        }
        body = convert_buffered_body(chat, DIALECT_OPENAI_CHAT, DIALECT_ANTHROPIC_MESSAGES)
        self.assertEqual(body["type"], "message")
        self.assertEqual(body["role"], "assistant")
        self.assertEqual(body["content"], [{"type": "text", "text": "hi"}])
        self.assertEqual(body["stop_reason"], "end_turn")
        self.assertEqual(body["usage"]["input_tokens"], 3)
        self.assertEqual(body["usage"]["output_tokens"], 1)

    def test_responses_body_to_anthropic(self) -> None:
        responses = {
            "id": "resp_xyz",
            "object": "response",
            "status": "completed",
            "model": "coding",
            "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "yo"}]}],
            "usage": {"input_tokens": 4, "output_tokens": 2},
        }
        body = convert_buffered_body(responses, DIALECT_OPENAI_RESPONSES, DIALECT_ANTHROPIC_MESSAGES)
        self.assertEqual(body["content"], [{"type": "text", "text": "yo"}])
        self.assertEqual(body["stop_reason"], "end_turn")
        self.assertEqual(body["usage"]["input_tokens"], 4)


class StreamConversionTests(unittest.TestCase):
    def test_anthropic_stream_to_chat(self) -> None:
        frames = [
            _sse({"type": "message_start", "message": {"id": "msg_1", "model": "m", "usage": {"input_tokens": 3}}}, "message_start"),
            _sse({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}, "content_block_start"),
            _sse({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hel"}}, "content_block_delta"),
            _sse({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "lo"}}, "content_block_delta"),
            _sse({"type": "content_block_stop", "index": 0}, "content_block_stop"),
            _sse({"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 2}}, "message_delta"),
            _sse({"type": "message_stop"}, "message_stop"),
        ]
        out = b"".join(convert_stream_bytes(frames, DIALECT_ANTHROPIC_MESSAGES, DIALECT_OPENAI_CHAT, "m"))
        self.assertIn(b"chat.completion.chunk", out)
        self.assertIn(b'"content":"Hel"', out)
        self.assertIn(b'"content":"lo"', out)
        # The terminal chunk carries finish_reason, and usage is merged.
        self.assertIn(b'"finish_reason":"stop"', out)
        self.assertIn(b'"prompt_tokens":3', out)
        self.assertIn(b'"completion_tokens":2', out)
        self.assertIn(b'"total_tokens":5', out)

    def test_chat_stream_to_anthropic(self) -> None:
        def chunk(delta, finish=None):
            return _sse({"id": "c1", "object": "chat.completion.chunk", "model": "m", "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]})

        frames = [
            chunk({"role": "assistant"}),
            chunk({"content": "Hi "}),
            chunk({"content": "there"}),
            chunk({}, "stop"),
            _sse({"id": "c1", "choices": [], "usage": {"prompt_tokens": 4, "completion_tokens": 2}}),
        ]
        out = b"".join(convert_stream_bytes(frames, DIALECT_OPENAI_CHAT, DIALECT_ANTHROPIC_MESSAGES, "m"))
        self.assertIn(b"event: message_start", out)
        self.assertIn(b'"type":"message_start"', out)
        self.assertIn(b'"type":"text_delta","text":"Hi "', out)
        self.assertIn(b'"stop_reason":"end_turn"', out)
        self.assertIn(b'"type":"message_stop"', out)
        self.assertIn(b'"input_tokens":4', out)
        self.assertIn(b'"output_tokens":2', out)

    def test_anthropic_tool_stream_to_chat(self) -> None:
        frames = [
            _sse({"type": "message_start", "message": {"id": "msg_1", "model": "m", "usage": {"input_tokens": 1}}}, "message_start"),
            _sse({"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "toolu_1", "name": "get", "input": {}}}, "content_block_start"),
            _sse({"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '{"city":'}}, "content_block_delta"),
            _sse({"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '"TO"}'}}, "content_block_delta"),
            _sse({"type": "content_block_stop", "index": 0}, "content_block_stop"),
            _sse({"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 5}}, "message_delta"),
            _sse({"type": "message_stop"}, "message_stop"),
        ]
        out = b"".join(convert_stream_bytes(frames, DIALECT_ANTHROPIC_MESSAGES, DIALECT_OPENAI_CHAT, "m"))
        self.assertIn(b'"id":"toolu_1"', out)
        self.assertIn(b'"name":"get"', out)
        self.assertIn(b'"arguments":"{\\"city\\":', out)
        self.assertIn(b'"finish_reason":"tool_calls"', out)

    def test_the_terminal_event_is_emitted_even_if_upstream_truncates(self) -> None:
        frames = [
            _sse({"type": "message_start", "message": {"id": "msg_1", "model": "m"}}, "message_start"),
            _sse({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}, "content_block_start"),
            _sse({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "partial"}}, "content_block_delta"),
        ]
        out = b"".join(convert_stream_bytes(frames, DIALECT_ANTHROPIC_MESSAGES, DIALECT_OPENAI_CHAT, "m"))
        # No message_delta/message_stop arrived, but a finish_reason chunk is
        # still emitted (length), so the client is not left hanging.
        self.assertIn(b'"finish_reason":"length"', out)


class AggregateTests(unittest.TestCase):
    def test_aggregate_anthropic_builds_one_message(self) -> None:
        frames = [
            _sse({"type": "message_start", "message": {"id": "msg_1", "model": "claude-haiku-5-5", "usage": {"input_tokens": 3}}}, "message_start"),
            _sse({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}, "content_block_start"),
            _sse({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hi"}}, "content_block_delta"),
            _sse({"type": "content_block_stop", "index": 0}, "content_block_stop"),
            _sse({"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}}, "message_delta"),
            _sse({"type": "message_stop"}, "message_stop"),
        ]
        message = aggregate_anthropic(frames)
        self.assertEqual(message["type"], "message")
        self.assertEqual(message["content"], [{"type": "text", "text": "Hi"}])
        self.assertEqual(message["stop_reason"], "end_turn")
        self.assertEqual(message["usage"]["input_tokens"], 3)
        self.assertEqual(message["usage"]["output_tokens"], 1)

    def test_aggregate_anthropic_materialises_tool_input(self) -> None:
        frames = [
            _sse({"type": "message_start", "message": {"id": "msg_1", "model": "m"}}, "message_start"),
            _sse({"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "t1", "name": "get", "input": {}}}, "content_block_start"),
            _sse({"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '{"a":1}'}}, "content_block_delta"),
            _sse({"type": "message_delta", "delta": {"stop_reason": "tool_use"}}, "message_delta"),
            _sse({"type": "message_stop"}, "message_stop"),
        ]
        message = aggregate_anthropic(frames)
        self.assertEqual(message["content"][0]["input"], {"a": 1})

    def test_aggregate_anthropic_refuses_a_stream_with_no_message(self) -> None:
        with self.assertRaises(ConversionError):
            aggregate_anthropic([_sse({"type": "ping"}, "ping")])


class RoundTripInvariantTests(unittest.TestCase):
    def test_chat_to_anthropic_to_chat_preserves_semantics(self) -> None:
        original = {
            "model": "coding",
            "messages": [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "q"},
                {"role": "assistant", "content": "a"},
            ],
        }
        once = convert_request(original, DIALECT_OPENAI_CHAT, DIALECT_ANTHROPIC_MESSAGES)
        twice = convert_request(once, DIALECT_ANTHROPIC_MESSAGES, DIALECT_OPENAI_CHAT)
        self.assertEqual(twice["messages"][0], {"role": "system", "content": "sys"})
        self.assertEqual(twice["messages"][1]["content"], "q")
        self.assertEqual(twice["messages"][2]["content"], "a")


if __name__ == "__main__":
    unittest.main()
