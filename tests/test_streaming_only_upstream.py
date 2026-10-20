"""A streaming-only upstream, served to a client that asked for one object.

The ChatGPT / Codex backend rejects a Responses request that does not set
``stream: true``, and it expects the same request fields OpenAI's own Codex
client sends. So the adapter always streams upstream, and a client that wants
a single non-streamed completion is served by consuming that stream and
presenting the result as one object.

The properties under test:

* the adapter always asks the upstream to stream, and always sends the
  fields the Codex client sends;
* a Responses stream aggregates into the final Responses object;
* a Chat Completions stream aggregates into one Chat Completions object;
* end to end, a non-streaming client against a streaming-only upstream gets
  one object, in the client's own dialect.
"""

from __future__ import annotations

import json
import unittest

from gateway.adapters.base import ProviderRequest
from gateway.adapters.chatgpt_codex import ChatGPTCodexAdapter
from gateway.bridge.aggregate import aggregate_chat, aggregate_responses
from gateway.core.capabilities import DIALECT_OPENAI_CHAT, DIALECT_OPENAI_RESPONSES
from tests.test_end_to_end import FAKE_PROVIDER, GatewayHarness


class ScriptedUpstream:
    """Records the request and returns a scripted response."""

    def __init__(self, body: bytes = b"{}", status: int = 200, frames: list[bytes] | None = None) -> None:
        self.body = body
        self.status = status
        self.frames = frames
        self.requests: list[dict] = []

    def request(self, method, url, headers, body=None, stream=False, provider_id=""):
        self.requests.append({"method": method, "url": url, "headers": headers, "body": body})
        from gateway.adapters.http_client import UpstreamResponse

        if self.frames is not None:
            return UpstreamResponse(self.status, {}, b"", iter(self.frames), "text/event-stream")
        return UpstreamResponse(self.status, {}, self.body, None, "application/json")


def responses_stream(text: str = "Hello") -> list[bytes]:
    """A minimal Responses stream, in the shape the ChatGPT backend sends."""
    events = [
        {"type": "response.created", "response": {"id": "resp_1", "model": "gpt-6-luna"}},
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {"id": "msg_1", "type": "message", "role": "assistant", "status": "in_progress"},
        },
        {"type": "response.output_text.delta", "output_index": 0, "item_id": "msg_1", "delta": text},
        {
            "type": "response.output_item.done",
            "output_index": 0,
            "item": {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": text}],
            },
        },
        {
            "type": "response.completed",
            "response": {
                "id": "resp_1",
                "object": "response",
                "status": "completed",
                "model": "gpt-6-luna",
                "output": [
                    {
                        "id": "msg_1",
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [{"type": "output_text", "text": text}],
                    }
                ],
                "usage": {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10},
            },
        },
    ]
    return [("data: " + json.dumps(event) + "\n\n").encode("utf-8") for event in events]


def chat_stream(text: str = "Hello") -> list[bytes]:
    events = [
        {"id": "c1", "created": 1, "model": "m", "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]},
        {"id": "c1", "created": 1, "model": "m", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        {"id": "c1", "created": 1, "model": "m", "choices": [], "usage": {"prompt_tokens": 2, "completion_tokens": 1}},
    ]
    return [("data: " + json.dumps(event) + "\n\n").encode("utf-8") for event in events]


class CodexRequestShapeTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile
        from pathlib import Path

        from gateway.credentials import CredentialStore

        self._tmp = tempfile.TemporaryDirectory()
        self.store = CredentialStore(Path(self._tmp.name))
        self.store.provider_handle("chatgpt_codex").write({"access_token": "synthetic"})

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_the_upstream_is_always_asked_to_stream(self) -> None:
        upstream = ScriptedUpstream(frames=responses_stream())
        adapter = ChatGPTCodexAdapter(
            self.store.provider_handle("chatgpt_codex"), client=upstream
        )
        # Even for a client that asked for a single object.
        adapter.invoke(
            ProviderRequest(model="gpt-6-luna", payload={"model": "gpt-6-luna", "input": []}, stream=False)
        )
        payload = json.loads(upstream.requests[0]["body"])
        self.assertIs(payload["stream"], True)

    def test_the_codex_request_fields_are_sent(self) -> None:
        upstream = ScriptedUpstream(frames=responses_stream())
        adapter = ChatGPTCodexAdapter(
            self.store.provider_handle("chatgpt_codex"), client=upstream
        )
        adapter.invoke(
            ProviderRequest(model="gpt-6-luna", payload={"model": "gpt-6-luna", "input": []}, stream=False)
        )
        payload = json.loads(upstream.requests[0]["body"])
        self.assertEqual(payload["tool_choice"], "auto")
        self.assertIs(payload["parallel_tool_calls"], True)
        # The provider is never asked to store the response.
        self.assertIs(payload["store"], False)
        self.assertIn("reasoning.encrypted_content", payload["include"])

    def test_a_caller_may_override_the_defaults(self) -> None:
        upstream = ScriptedUpstream(frames=responses_stream())
        adapter = ChatGPTCodexAdapter(
            self.store.provider_handle("chatgpt_codex"), client=upstream
        )
        adapter.invoke(
            ProviderRequest(
                model="gpt-6-luna",
                payload={
                    "model": "gpt-6-luna",
                    "input": [],
                    "tool_choice": "none",
                    "parallel_tool_calls": False,
                },
                stream=False,
            )
        )
        payload = json.loads(upstream.requests[0]["body"])
        self.assertEqual(payload["tool_choice"], "none")
        self.assertIs(payload["parallel_tool_calls"], False)


class AggregationTests(unittest.TestCase):
    def test_a_responses_stream_aggregates_into_the_final_object(self) -> None:
        final = aggregate_responses(responses_stream("Hello"))
        self.assertEqual(final["id"], "resp_1")
        self.assertEqual(final["status"], "completed")
        self.assertEqual(final["output"][0]["content"][0]["text"], "Hello")
        self.assertEqual(final["usage"]["total_tokens"], 10)

    def test_a_chat_stream_aggregates_into_one_object(self) -> None:
        final = aggregate_chat(chat_stream("Hello"))
        self.assertEqual(final["object"], "chat.completion")
        self.assertEqual(final["choices"][0]["message"]["content"], "Hello")
        self.assertEqual(final["choices"][0]["finish_reason"], "stop")
        self.assertEqual(final["usage"]["prompt_tokens"], 2)

    def test_a_stream_without_a_terminal_event_is_refused(self) -> None:
        from gateway.bridge.model import ConversionError

        frames = [b'data: {"type":"response.output_text.delta","delta":"x"}\n\n']
        with self.assertRaises(ConversionError):
            aggregate_responses(frames)

    def test_a_failed_stream_is_reported_as_a_failure(self) -> None:
        frames = [
            b'data: {"type":"response.failed","response":{"id":"r","status":"failed",'
            b'"error":{"message":"nope"},"output":[]}}\n\n'
        ]
        final = aggregate_responses(frames)
        self.assertEqual(final["status"], "failed")


class StreamingOnlyUpstreamTests(unittest.TestCase):
    """End to end, against an upstream that only speaks streams."""

    def setUp(self) -> None:
        self.harness = GatewayHarness()
        self.addCleanup(self.harness.close)
        with self.harness.config.mutate() as config:
            config.aliases["gpt"] = _alias()
        # The fake provider answers with a stream, whatever the client asked for.
        self.harness.adapter.stream_frames = responses_stream("ok")
        self.harness.adapter.always_stream = True

    def test_a_non_streaming_client_gets_one_object(self) -> None:
        status, payload, _ = self.harness.request(
            "POST",
            "/v1/chat/completions",
            body={"model": "gpt", "messages": [{"role": "user", "content": "hi"}]},
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["object"], "chat.completion")
        self.assertEqual(payload["choices"][0]["message"]["content"], "ok")
        self.assertEqual(payload["usage"]["prompt_tokens"], 7)

    def test_a_responses_client_gets_a_response_object(self) -> None:
        status, payload, _ = self.harness.request(
            "POST",
            "/v1/responses",
            body={"model": "gpt", "input": [{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}]},
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["object"], "response")
        self.assertEqual(payload["status"], "completed")


def _alias():
    from gateway.config import Alias, RouteRef

    return Alias(
        name="gpt",
        enabled=True,
        primary=RouteRef(FAKE_PROVIDER, "gpt-6-luna"),
    )


if __name__ == "__main__":
    unittest.main()
