"""Tests for the Responses <-> Chat Completions bridge.

The properties under test are the ones the bridge contract names:

* **Tool-call identity is preserved** in both directions, across the
  streaming boundary.
* **SSE ordering and termination are preserved** - the terminal event is
  always emitted.
* **Usage accounting is preserved**, and the absence of counters stays the
  absence of counters.
* **Nothing is silently dropped** - an inexpressible capability is refused.
* **Round-trip invariants hold** - Chat -> Responses -> Chat is
  semantically identical.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from gateway.bridge import (
    ConversionError,
    chat_chunk_to_events,
    convert_request,
    convert_stream_events,
    events_to_chat_chunks,
    events_to_responses_events,
    from_chat_completions,
    from_responses,
    to_chat_completions,
    to_responses,
)
from gateway.bridge.streams import StreamState, normalise_usage
from gateway.core.capabilities import DIALECT_OPENAI_CHAT, DIALECT_OPENAI_RESPONSES

CORPUS_PATH = Path(__file__).parent / "fixtures" / "bridge_corpus.json"


class RequestConversionTests(unittest.TestCase):
    def test_chat_request_round_trips_through_responses(self) -> None:
        original = {
            "model": "coding",
            "messages": [
                {"role": "system", "content": "You are terse."},
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": None, "tool_calls": [
                    {"id": "call_1", "type": "function", "function": {"name": "lookup", "arguments": '{"q":"x"}'}}
                ]},
                {"role": "tool", "tool_call_id": "call_1", "content": "result text"},
            ],
            "tools": [
                {"type": "function", "function": {"name": "lookup", "description": "look up", "parameters": {"type": "object"}}}
            ],
        }
        as_responses = convert_request(original, DIALECT_OPENAI_CHAT, DIALECT_OPENAI_RESPONSES)
        back = convert_request(as_responses, DIALECT_OPENAI_RESPONSES, DIALECT_OPENAI_CHAT)

        # The system message becomes instructions and comes back as a system
        # message.
        self.assertEqual(back["messages"][0], {"role": "system", "content": "You are terse."})
        self.assertEqual(back["messages"][1], {"role": "user", "content": "hello"})

        assistant = back["messages"][2]
        self.assertEqual(assistant["role"], "assistant")
        self.assertEqual(assistant["tool_calls"][0]["id"], "call_1")
        self.assertEqual(assistant["tool_calls"][0]["function"]["name"], "lookup")
        self.assertEqual(assistant["tool_calls"][0]["function"]["arguments"], '{"q":"x"}')

        tool_message = back["messages"][3]
        self.assertEqual(tool_message["role"], "tool")
        self.assertEqual(tool_message["tool_call_id"], "call_1")
        self.assertEqual(tool_message["content"], "result text")

        self.assertEqual(back["tools"][0]["function"]["name"], "lookup")
        self.assertEqual(back["tools"][0]["function"]["description"], "look up")

    def test_responses_request_round_trips_through_chat(self) -> None:
        original = {
            "model": "coding",
            "instructions": "Be brief.",
            "input": [
                {"role": "user", "content": [{"type": "input_text", "text": "hello"}]},
                {"type": "function_call", "call_id": "call_9", "name": "lookup", "arguments": "{}"},
                {"type": "function_call_output", "call_id": "call_9", "output": "done"},
            ],
            "tools": [{"type": "function", "name": "lookup", "parameters": {"type": "object"}}],
        }
        as_chat = convert_request(original, DIALECT_OPENAI_RESPONSES, DIALECT_OPENAI_CHAT)
        back = convert_request(as_chat, DIALECT_OPENAI_CHAT, DIALECT_OPENAI_RESPONSES)

        self.assertEqual(back["instructions"], "Be brief.")
        self.assertEqual(back["input"][0]["role"], "user")
        self.assertEqual(back["input"][0]["content"][0]["text"], "hello")
        self.assertEqual(back["input"][1]["type"], "function_call")
        self.assertEqual(back["input"][1]["call_id"], "call_9")
        self.assertEqual(back["input"][2]["type"], "function_call_output")
        self.assertEqual(back["input"][2]["call_id"], "call_9")
        self.assertEqual(back["tools"][0]["name"], "lookup")

    def test_text_content_survives_both_directions(self) -> None:
        original = {"model": "m", "messages": [{"role": "user", "content": "a b c"}]}
        as_responses = convert_request(original, DIALECT_OPENAI_CHAT, DIALECT_OPENAI_RESPONSES)
        self.assertEqual(as_responses["input"][0]["content"][0]["text"], "a b c")

    def test_streaming_flag_is_preserved(self) -> None:
        as_responses = convert_request(
            {"model": "m", "messages": [{"role": "user", "content": "x"}], "stream": True},
            DIALECT_OPENAI_CHAT,
            DIALECT_OPENAI_RESPONSES,
        )
        self.assertTrue(as_responses["stream"])

    def test_max_tokens_becomes_max_output_tokens(self) -> None:
        as_responses = convert_request(
            {"model": "m", "messages": [], "max_completion_tokens": 256},
            DIALECT_OPENAI_CHAT,
            DIALECT_OPENAI_RESPONSES,
        )
        self.assertEqual(as_responses["max_output_tokens"], 256)

    def test_reasoning_effort_becomes_reasoning_object(self) -> None:
        as_responses = convert_request(
            {"model": "m", "messages": [], "reasoning_effort": "high"},
            DIALECT_OPENAI_CHAT,
            DIALECT_OPENAI_RESPONSES,
        )
        self.assertEqual(as_responses["reasoning"], {"effort": "high"})

    def test_disabled_reasoning_is_not_forwarded_to_same_dialect(self) -> None:
        forwarded = convert_request(
            {
                "model": "m",
                "messages": [{"role": "user", "content": "hi"}],
                "reasoning": {"enabled": False, "effort": "none"},
            },
            DIALECT_OPENAI_CHAT,
            DIALECT_OPENAI_CHAT,
        )
        self.assertNotIn("reasoning", forwarded)
        self.assertNotIn("reasoning_effort", forwarded)

    def test_stateful_continuation_is_refused_by_chat_dialect(self) -> None:
        with self.assertRaises(ConversionError) as ctx:
            convert_request(
                {"model": "m", "input": [], "previous_response_id": "resp_1"},
                DIALECT_OPENAI_RESPONSES,
                DIALECT_OPENAI_CHAT,
            )
        self.assertIn("previous_response_id", ctx.exception.message)

    def test_hosted_tools_are_refused_rather_than_dropped(self) -> None:
        with self.assertRaises(ConversionError) as ctx:
            convert_request(
                {"model": "m", "input": [], "tools": [{"type": "web_search"}]},
                DIALECT_OPENAI_RESPONSES,
                DIALECT_OPENAI_CHAT,
            )
        self.assertIn("hosted tools", ctx.exception.message)

    def test_missing_model_is_refused(self) -> None:
        with self.assertRaises(ConversionError):
            from_chat_completions({"messages": []})

    def test_unsupported_conversion_pair_is_refused(self) -> None:
        with self.assertRaises(ConversionError):
            convert_request({}, DIALECT_OPENAI_CHAT, "anthropic_messages")


class StreamConversionTests(unittest.TestCase):
    def test_text_stream_converts_and_keeps_the_terminal_event(self) -> None:
        chunks = [
            {"id": "chatcmpl-1", "object": "chat.completion.chunk", "created": 1, "model": "m",
             "choices": [{"index": 0, "delta": {"role": "assistant", "content": "Hel"}, "finish_reason": None}]},
            {"id": "chatcmpl-1", "object": "chat.completion.chunk", "created": 1, "model": "m",
             "choices": [{"index": 0, "delta": {"content": "lo"}, "finish_reason": None}]},
            {"id": "chatcmpl-1", "object": "chat.completion.chunk", "created": 1, "model": "m",
             "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        ]
        events = list(convert_stream_events(chunks, DIALECT_OPENAI_CHAT, DIALECT_OPENAI_RESPONSES, "m"))
        types = [e["type"] for e in events]
        self.assertIn("response.output_text.delta", types)
        self.assertEqual(types[-1], "response.completed")
        text = "".join(e["delta"] for e in events if e["type"] == "response.output_text.delta")
        self.assertEqual(text, "Hello")

    def test_tool_call_identity_survives_conversion(self) -> None:
        chunks = [
            {"id": "c", "model": "m", "choices": [{"index": 0, "delta": {"tool_calls": [
                {"index": 0, "id": "call_abc", "type": "function", "function": {"name": "lookup", "arguments": ""}}
            ]}, "finish_reason": None}]},
            {"id": "c", "model": "m", "choices": [{"index": 0, "delta": {"tool_calls": [
                {"index": 0, "function": {"arguments": '{"q":'}}
            ]}, "finish_reason": None}]},
            {"id": "c", "model": "m", "choices": [{"index": 0, "delta": {"tool_calls": [
                {"index": 0, "function": {"arguments": '"x"}'}}
            ]}, "finish_reason": None}]},
            {"id": "c", "model": "m", "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
        ]
        events = list(convert_stream_events(chunks, DIALECT_OPENAI_CHAT, DIALECT_OPENAI_RESPONSES, "m"))

        added = [e for e in events if e["type"] == "response.output_item.added"]
        self.assertEqual(len(added), 1)
        self.assertEqual(added[0]["item"]["call_id"], "call_abc")
        self.assertEqual(added[0]["item"]["name"], "lookup")

        deltas = [e for e in events if e["type"] == "response.function_call_arguments.delta"]
        arguments = "".join(e["delta"] for e in deltas)
        self.assertEqual(arguments, '{"q":"x"}')

        # The arguments must be attached to the same item as the call id.
        item_ids = {e["item_id"] for e in deltas}
        self.assertEqual(item_ids, {added[0]["item"]["id"]})

    def test_converting_back_preserves_tool_call_identity(self) -> None:
        responses_events = [
            {"type": "response.output_item.added", "output_index": 0, "item": {
                "type": "function_call", "id": "fc_1", "call_id": "call_xyz", "name": "lookup", "arguments": ""}},
            {"type": "response.function_call_arguments.delta", "output_index": 0, "item_id": "fc_1", "delta": '{"a":1}'},
            {"type": "response.output_item.done", "output_index": 0, "item": {
                "type": "function_call", "id": "fc_1", "call_id": "call_xyz", "name": "lookup", "arguments": '{"a":1}'}},
            {"type": "response.completed", "response": {"id": "resp_1", "usage": {"input_tokens": 5, "output_tokens": 7}}},
        ]
        chunks = list(convert_stream_events(responses_events, DIALECT_OPENAI_RESPONSES, DIALECT_OPENAI_CHAT, "m"))

        starts = [c for c in chunks if c["choices"] and c["choices"][0]["delta"].get("tool_calls")]
        self.assertTrue(starts)
        self.assertEqual(starts[0]["choices"][0]["delta"]["tool_calls"][0]["id"], "call_xyz")

        arguments = ""
        for chunk in chunks:
            for choice in chunk["choices"]:
                for call in choice["delta"].get("tool_calls") or []:
                    arguments += call.get("function", {}).get("arguments") or ""
        self.assertEqual(arguments, '{"a":1}')

        final = [c for c in chunks if c.get("choices") and c["choices"][0].get("finish_reason")]
        self.assertEqual(final[-1]["choices"][0]["finish_reason"], "stop")
        usage_chunks = [c for c in chunks if c.get("usage")]
        self.assertEqual(len(usage_chunks), 1)
        self.assertEqual(usage_chunks[0]["usage"]["prompt_tokens"], 5)
        self.assertEqual(usage_chunks[0]["usage"]["completion_tokens"], 7)

    def test_truncated_stream_is_reported_as_incomplete(self) -> None:
        # A stream that ends without a finish_reason was truncated. The
        # terminal event must still be emitted, and it must say so.
        chunks = [{"id": "c", "model": "m", "choices": [{"index": 0, "delta": {"content": "x"}, "finish_reason": None}]}]
        events = list(convert_stream_events(chunks, DIALECT_OPENAI_CHAT, DIALECT_OPENAI_RESPONSES, "m"))
        self.assertEqual(events[-1]["type"], "response.incomplete")

    def test_terminal_event_is_always_emitted(self) -> None:
        for chunks in (
            [{"id": "c", "model": "m", "choices": [{"index": 0, "delta": {"content": "x"}, "finish_reason": "stop"}]}],
            [{"id": "c", "model": "m", "choices": [{"index": 0, "delta": {"content": "x"}, "finish_reason": None}]}],
        ):
            with self.subTest(chunks=chunks):
                events = list(convert_stream_events(chunks, DIALECT_OPENAI_CHAT, DIALECT_OPENAI_RESPONSES, "m"))
                self.assertTrue(
                    any(e["type"] in ("response.completed", "response.incomplete", "response.failed") for e in events),
                    "the stream must always end with a terminal event",
                )

    def test_usage_is_carried_across(self) -> None:
        # OpenAI's shape: the finish chunk arrives first, the usage chunk
        # follows it. The Responses terminal event must still carry usage.
        chunks = [
            {"id": "c", "model": "m", "choices": [{"index": 0, "delta": {"content": "x"}, "finish_reason": "stop"}]},
            {"id": "c", "model": "m", "choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7}},
        ]
        events = list(convert_stream_events(chunks, DIALECT_OPENAI_CHAT, DIALECT_OPENAI_RESPONSES, "m"))
        completed = [e for e in events if e["type"] == "response.completed"]
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0]["response"]["usage"]["input_tokens"], 3)
        self.assertEqual(completed[0]["response"]["usage"]["output_tokens"], 4)
        # The terminal event must be last.
        self.assertEqual(events[-1]["type"], "response.completed")

    def test_missing_usage_stays_missing(self) -> None:
        chunks = [{"id": "c", "model": "m", "choices": [{"index": 0, "delta": {"content": "x"}, "finish_reason": "stop"}]}]
        events = list(convert_stream_events(chunks, DIALECT_OPENAI_CHAT, DIALECT_OPENAI_RESPONSES, "m"))
        completed = [e for e in events if e["type"] == "response.completed"]
        self.assertNotIn("usage", completed[0]["response"])

    def test_normalised_usage_does_not_invent_zeroes(self) -> None:
        self.assertEqual(normalise_usage({"input_tokens": 5}), {"prompt_tokens": 5})
        self.assertEqual(normalise_usage({}), {})

    def test_incomplete_finish_becomes_incomplete_response(self) -> None:
        chunks = [{"id": "c", "model": "m", "choices": [{"index": 0, "delta": {}, "finish_reason": "length"}]}]
        events = list(convert_stream_events(chunks, DIALECT_OPENAI_CHAT, DIALECT_OPENAI_RESPONSES, "m"))
        self.assertEqual(events[-1]["type"], "response.incomplete")

    def test_error_event_is_forwarded(self) -> None:
        events = list(
            convert_stream_events(
                [{"type": "error", "message": "upstream exploded"}],
                DIALECT_OPENAI_RESPONSES,
                DIALECT_OPENAI_CHAT,
                "m",
            )
        )
        self.assertEqual(events[0]["choices"][0]["error"]["message"], "upstream exploded")

    def test_round_trip_preserves_text_and_finish_reason(self) -> None:
        chunks = [
            {"id": "c", "model": "m", "choices": [{"index": 0, "delta": {"content": "abc"}, "finish_reason": None}]},
            {"id": "c", "model": "m", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        ]
        to_responses = list(convert_stream_events(chunks, DIALECT_OPENAI_CHAT, DIALECT_OPENAI_RESPONSES, "m"))
        back = list(convert_stream_events(to_responses, DIALECT_OPENAI_RESPONSES, DIALECT_OPENAI_CHAT, "m"))
        text = "".join(
            c["choices"][0]["delta"].get("content", "")
            for c in back
            if c.get("choices") and c["choices"][0]["delta"].get("content")
        )
        self.assertEqual(text, "abc")
        self.assertEqual(back[-1]["choices"][0]["finish_reason"], "stop")


class GoldenCorpusTests(unittest.TestCase):
    """Run the recorded corpus through the bridge and compare."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.corpus = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))

    def test_corpus_is_present_and_not_empty(self) -> None:
        self.assertTrue(self.corpus["cases"], "the golden corpus must not be empty")

    def test_every_case_round_trips(self) -> None:
        for case in self.corpus["cases"]:
            with self.subTest(case=case["name"]):
                original = case["chat_request"]
                as_responses = convert_request(original, DIALECT_OPENAI_CHAT, DIALECT_OPENAI_RESPONSES)
                back = convert_request(as_responses, DIALECT_OPENAI_RESPONSES, DIALECT_OPENAI_CHAT)
                expected = case["chat_request_round_trip"]
                self.assertEqual(back["messages"], expected["messages"])
                self.assertEqual(back.get("tools"), expected.get("tools"))

    def test_every_stream_case_round_trips(self) -> None:
        for case in self.corpus["cases"]:
            with self.subTest(case=case["name"]):
                chunks = case["chat_stream"]
                to_responses = list(convert_stream_events(chunks, DIALECT_OPENAI_CHAT, DIALECT_OPENAI_RESPONSES, "m"))
                back = list(convert_stream_events(to_responses, DIALECT_OPENAI_RESPONSES, DIALECT_OPENAI_CHAT, "m"))
                text = "".join(
                    c["choices"][0]["delta"].get("content", "")
                    for c in back
                    if c.get("choices") and c["choices"][0]["delta"].get("content")
                )
                self.assertEqual(text, case["stream_text"])


if __name__ == "__main__":
    unittest.main()
