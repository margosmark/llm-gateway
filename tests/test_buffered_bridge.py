"""A converted request must receive a converted response.

Regression cover for **R9**: the request was converted between dialects
but the **non-streaming** response body was returned verbatim. A Chat
Completions client routed to a Responses-dialect model therefore received a
Responses object, which it cannot parse.

The streaming path already converted. This covers the buffered path, in
both directions and end to end.
"""

from __future__ import annotations

import json
import unittest

from gateway.bridge.buffered import chat_body_to_responses, responses_body_to_chat
from tests.test_end_to_end import FAKE_PROVIDER, GatewayHarness


def responses_body() -> dict:
    return {
        "id": "resp_abc123",
        "object": "response",
        "created_at": 1700000000,
        "status": "completed",
        "model": "gpt-6-luna",
        "output": [
            {
                "type": "message",
                "id": "msg_1",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Hello there"}],
            }
        ],
        "usage": {"input_tokens": 11, "output_tokens": 5, "total_tokens": 16},
    }


def chat_body() -> dict:
    return {
        "id": "chatcmpl-abc123",
        "object": "chat.completion",
        "created": 1700000000,
        "model": "mimo-v2.6-flash",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "Hello there"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 11, "completion_tokens": 5, "total_tokens": 16},
    }


class ResponsesToChatTests(unittest.TestCase):
    def test_the_text_becomes_the_assistant_message(self) -> None:
        chat = responses_body_to_chat(responses_body())
        self.assertEqual(chat["object"], "chat.completion")
        self.assertEqual(chat["choices"][0]["message"]["content"], "Hello there")
        self.assertEqual(chat["choices"][0]["message"]["role"], "assistant")
        self.assertEqual(chat["choices"][0]["finish_reason"], "stop")

    def test_the_usage_is_carried_across(self) -> None:
        chat = responses_body_to_chat(responses_body())
        self.assertEqual(chat["usage"]["prompt_tokens"], 11)
        self.assertEqual(chat["usage"]["completion_tokens"], 5)
        self.assertEqual(chat["usage"]["total_tokens"], 16)

    def test_a_function_call_becomes_a_tool_call(self) -> None:
        body = responses_body()
        body["output"] = [
            {
                "type": "function_call",
                "id": "fc_1",
                "call_id": "call_9",
                "name": "lookup",
                "arguments": '{"q":"x"}',
            }
        ]
        chat = responses_body_to_chat(body)
        calls = chat["choices"][0]["message"]["tool_calls"]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["id"], "call_9")
        self.assertEqual(calls[0]["type"], "function")
        self.assertEqual(calls[0]["function"]["name"], "lookup")
        self.assertEqual(calls[0]["function"]["arguments"], '{"q":"x"}')
        self.assertEqual(chat["choices"][0]["finish_reason"], "tool_calls")

    def test_a_reasoning_item_is_dropped_rather_than_invented_as_content(self) -> None:
        body = responses_body()
        body["output"] = [
            {"type": "reasoning", "id": "rs_1", "summary": []},
            {"type": "message", "content": [{"type": "output_text", "text": "Hi"}]},
        ]
        chat = responses_body_to_chat(body)
        self.assertEqual(chat["choices"][0]["message"]["content"], "Hi")

    def test_an_incomplete_response_becomes_a_length_finish(self) -> None:
        body = responses_body()
        body["status"] = "incomplete"
        self.assertEqual(responses_body_to_chat(body)["choices"][0]["finish_reason"], "length")


class ChatToResponsesTests(unittest.TestCase):
    def test_the_text_becomes_a_message_item(self) -> None:
        response = chat_body_to_responses(chat_body())
        self.assertEqual(response["object"], "response")
        self.assertEqual(response["status"], "completed")
        self.assertEqual(response["output"][0]["type"], "message")
        self.assertEqual(response["output"][0]["content"][0]["text"], "Hello there")
        self.assertEqual(response["output"][0]["content"][0]["type"], "output_text")

    def test_the_usage_is_carried_across(self) -> None:
        response = chat_body_to_responses(chat_body())
        self.assertEqual(response["usage"]["input_tokens"], 11)
        self.assertEqual(response["usage"]["output_tokens"], 5)
        self.assertEqual(response["usage"]["total_tokens"], 16)

    def test_a_tool_call_becomes_a_function_call_item(self) -> None:
        body = chat_body()
        body["choices"][0]["message"] = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "call_9", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}
            ],
        }
        body["choices"][0]["finish_reason"] = "tool_calls"
        response = chat_body_to_responses(body)
        item = response["output"][0]
        self.assertEqual(item["type"], "function_call")
        self.assertEqual(item["call_id"], "call_9")
        self.assertEqual(item["name"], "lookup")
        self.assertEqual(item["arguments"], "{}")

    def test_a_length_finish_becomes_an_incomplete_status(self) -> None:
        body = chat_body()
        body["choices"][0]["finish_reason"] = "length"
        self.assertEqual(chat_body_to_responses(body)["status"], "incomplete")


class EndToEndDialectTests(unittest.TestCase):
    """The two endpoint/dialect permutations, over real HTTP."""

    def setUp(self) -> None:
        self.harness = GatewayHarness()
        self.addCleanup(self.harness.close)
        with self.harness.config.mutate() as config:
            config.aliases["coding"] = _alias()

    def test_a_chat_client_gets_a_chat_object_from_a_responses_upstream(self) -> None:
        status, payload, _ = self.harness.request(
            "POST",
            "/v1/chat/completions",
            body={"model": "coding", "messages": [{"role": "user", "content": "hi"}]},
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["object"], "chat.completion")
        self.assertIn("choices", payload)
        self.assertNotIn("output", payload)
        self.assertEqual(payload["choices"][0]["message"]["content"], "ok")

    def test_a_responses_client_gets_a_responses_object(self) -> None:
        status, payload, _ = self.harness.request(
            "POST",
            "/v1/responses",
            body={"model": "coding", "input": [{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}]},
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["object"], "response")
        self.assertIn("output", payload)
        self.assertNotIn("choices", payload)

    def test_the_converted_response_keeps_the_usage(self) -> None:
        _, payload, _ = self.harness.request(
            "POST",
            "/v1/chat/completions",
            body={"model": "coding", "messages": [{"role": "user", "content": "hi"}]},
        )
        # The fake upstream reports no usage, and the absence of counters
        # stays absent - it is never invented as a zero.
        self.assertNotIn("usage", payload)

    def test_a_usage_reporting_upstream_keeps_its_counters(self) -> None:
        from gateway.adapters.base import ProviderResponse

        body = json.dumps(responses_body()).encode("utf-8")
        self.harness.adapter.invoke = lambda request: ProviderResponse(  # type: ignore[method-assign]
            status=200, headers={}, body=body, content_type="application/json", dialect="openai_responses"
        )
        _, payload, _ = self.harness.request(
            "POST",
            "/v1/chat/completions",
            body={"model": "coding", "messages": [{"role": "user", "content": "hi"}]},
        )
        self.assertEqual(payload["usage"]["prompt_tokens"], 11)
        self.assertEqual(payload["usage"]["completion_tokens"], 5)


def _alias():
    from gateway.config import Alias, RouteRef

    return Alias(
        name="coding",
        enabled=True,
        primary=RouteRef(FAKE_PROVIDER, "gpt-6-luna"),
    )


if __name__ == "__main__":
    unittest.main()
