"""``POST /v1/messages``: the Anthropic Messages handler, contract and flow.

Two things are pinned here.

**The Anthropic contract** (:mod:`gateway.http.messages`): the required
fields Anthropic marks required are required on this surface too, a
wrongly-typed option the bridge would silently drop is refused before
any provider is called, and the error body keeps the gateway's own
envelope (``{"error": {"message": ..., "type": ...}}``) with the same
codes the OpenAI surfaces produce.

**The surface flow**: an Anthropic-shaped caller reaches both kinds of
route - an Anthropic-dialect model answers directly and an
OpenAI-dialect model answers through the bridge - on the buffered and
the streamed path, the alias never reaches the provider, a ``thinking``
budget is honoured by a native Anthropic route and refused (not
downgraded) on a route that would have to convert it, and the Host
policy is the same one the OpenAI surfaces enforce.

The provider is a fake adapter - no test contacts a real provider.
"""

from __future__ import annotations

import json
import unittest

from gateway.adapters.base import ProviderRequest, ProviderResponse
from gateway.config import Alias, ModelEntry, ProviderState, RouteRef
from gateway.core.capabilities import (
    DIALECT_ANTHROPIC_MESSAGES,
    DIALECT_OPENAI_CHAT,
)

from tests.test_anthropic_surface import FakeAnthropicAdapter, _build_harness
from tests.test_end_to_end import FAKE_PROVIDER, FakeAdapter, GatewayHarness

CHAT_MODEL = "mimo-v2.6-flash"

#: A minimal request that this surface accepts.
VALID = {
    "model": "claude-haiku-5-5",
    "max_tokens": 32,
    "messages": [{"role": "user", "content": "hi"}],
}


def _chat_body(text: str = "ok") -> dict:
    return {
        "id": "chatcmpl-x",
        "object": "chat.completion",
        "model": CHAT_MODEL,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2},
    }


def _chat_sse(text: str = "ok") -> list[bytes]:
    def frame(delta, finish=None):
        return (
            "data: "
            + json.dumps(
                {
                    "id": "c1",
                    "object": "chat.completion.chunk",
                    "model": CHAT_MODEL,
                    "choices": [
                        {"index": 0, "delta": delta, "finish_reason": finish}
                    ],
                }
            )
            + "\n\n"
        ).encode()

    return [
        frame({"role": "assistant"}),
        frame({"content": text}),
        frame({}, "stop"),
        b"data: [DONE]\n\n",
    ]


class FakeChatAdapter(FakeAdapter):
    """A provider that answers in the OpenAI Chat dialect."""

    def __init__(self, **kwargs) -> None:
        kwargs.setdefault("provider_id", FAKE_PROVIDER)
        kwargs.setdefault("dialect", DIALECT_OPENAI_CHAT)
        super().__init__(**kwargs)
        if not kwargs.get("stream_frames"):
            self.stream_frames = _chat_sse()
        self.buffered_body: dict = _chat_body()

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
            status=200,
            headers={},
            body=body,
            content_type="application/json",
            dialect=self.dialect,
        )


def _build_chat_harness(adapter: FakeChatAdapter | None = None) -> GatewayHarness:
    harness = GatewayHarness(adapter or FakeChatAdapter())
    with harness.config.mutate() as config:
        config.providers[FAKE_PROVIDER] = ProviderState(
            provider_id=FAKE_PROVIDER,
            kind=FAKE_PROVIDER,
            state="connected",
            models={
                CHAT_MODEL: ModelEntry(
                    model_id=CHAT_MODEL,
                    enabled=True,
                    dialect=DIALECT_OPENAI_CHAT,
                    source="discovered",
                )
            },
        )
        config.aliases[CHAT_MODEL] = Alias(
            name=CHAT_MODEL, enabled=True, primary=RouteRef(FAKE_PROVIDER, CHAT_MODEL)
        )
    return harness


class MessagesContractTests(unittest.TestCase):
    """Anthropic's required fields are required; bad types are refused."""

    def setUp(self) -> None:
        self.harness = _build_harness()
        self.addCleanup(self.harness.close)

    def _post(self, body):
        return self.harness.request("POST", "/v1/messages", body=body)

    def _refused(self, body, needle: str | None = None) -> None:
        status, payload, _ = self._post(body)
        self.assertEqual(status, 400, payload)
        self.assertEqual(set(payload), {"error"})
        self.assertEqual(set(payload["error"]), {"message", "type"})
        self.assertEqual(payload["error"]["type"], "malformed_request")
        if needle is not None:
            self.assertIn(needle, payload["error"]["message"])
        adapter: FakeAnthropicAdapter = self.harness.adapter  # type: ignore[assignment]
        self.assertEqual(adapter.calls, [], "no provider may be called for a 400")

    def test_max_tokens_is_required_and_must_be_a_positive_integer(self) -> None:
        missing = {key: value for key, value in VALID.items() if key != "max_tokens"}
        self._refused(missing, "max_tokens")
        for bad in ("32", 0, -1, True, 3.5):
            with self.subTest(max_tokens=bad):
                self._refused({**VALID, "max_tokens": bad}, "max_tokens")

    def test_messages_must_be_a_non_empty_array(self) -> None:
        for bad in (None, [], "hi", {}):
            with self.subTest(messages=bad):
                self._refused({**VALID, "messages": bad}, "messages")

    def test_each_message_needs_a_role_and_content(self) -> None:
        self._refused(
            {**VALID, "messages": [{"role": "system", "content": "hi"}]}, "role"
        )
        self._refused(
            {**VALID, "messages": [{"role": "user"}]}, "content"
        )
        self._refused({**VALID, "messages": ["hi"]}, "object")

    def test_an_uncarryable_content_block_is_refused_by_name(self) -> None:
        status, payload, _ = self._post(
            {
                **VALID,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "hi"},
                            {"type": "document", "source": {"type": "url"}},
                        ],
                    }
                ],
            }
        )
        self.assertEqual(status, 400, payload)
        self.assertIn("document", payload["error"]["message"])
        self.assertIn("text", payload["error"]["message"])
        adapter: FakeAnthropicAdapter = self.harness.adapter  # type: ignore[assignment]
        self.assertEqual(adapter.calls, [])

    def test_a_tool_use_block_needs_an_object_input(self) -> None:
        """The block *type* being carryable is not enough - its insides are.

        Regression for the T5 silent-drop finding: a non-object ``input``
        used to be replaced by ``{}`` inside the bridge and forwarded as
        ``"arguments":"{}"``, so the caller's data vanished without a record.
        """
        for bad in ("not-an-object", ["a"], 7, None):
            with self.subTest(input=bad):
                self._refused(
                    {
                        **VALID,
                        "messages": [
                            {
                                "role": "user",
                                "content": [
                                    {
                                        "type": "tool_use",
                                        "id": "t1",
                                        "name": "search",
                                        "input": bad,
                                    }
                                ],
                            }
                        ],
                    },
                    "input",
                )

    def test_a_tool_use_block_with_an_object_input_still_serves(self) -> None:
        status, payload, _ = self._post(
            {
                **VALID,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "t1",
                                "name": "search",
                                "input": {"q": "x"},
                            }
                        ],
                    }
                ],
            }
        )
        self.assertEqual(status, 200, payload)
        adapter: FakeAnthropicAdapter = self.harness.adapter  # type: ignore[assignment]
        self.assertEqual(len(adapter.calls), 1)

    def test_mistyped_request_options_are_refused_not_silently_dropped(self) -> None:
        cases = {
            "temperature": "hot",
            "top_p": "0.9",
            "top_k": "40",
            "stop_sequences": ["a", 5],
            "system": 7,
            "stream": "yes",
            "tools": ["web_search"],
            "tool_choice": "auto",
            "metadata": [],
        }
        for field, bad in cases.items():
            with self.subTest(field=field):
                self._refused({**VALID, field: bad}, field)

    def test_an_empty_body_is_a_400_not_a_500(self) -> None:
        status, payload, _ = self.harness.request("POST", "/v1/messages")
        self.assertEqual(status, 400, payload)
        self.assertEqual(payload["error"]["type"], "malformed_request")

    def test_an_unknown_model_keeps_the_openai_surfaces_error(self) -> None:
        status, payload, _ = self._post({**VALID, "model": "not-a-model"})
        self.assertEqual(status, 404, payload)
        self.assertEqual(payload["error"]["type"], "unknown_model")


class MessagesThinkingVocabularyTests(unittest.TestCase):
    """T4's canonical vocabulary governs thinking, with no invented budget."""

    def setUp(self) -> None:
        self.harness = _build_harness()
        self.addCleanup(self.harness.close)
        self.chat = _build_chat_harness()
        self.addCleanup(self.chat.close)

    def test_a_canonical_level_spelling_is_refused_and_both_vocabularies_named(self) -> None:
        status, payload, _ = self.harness.request(
            "POST", "/v1/messages", body={**VALID, "thinking": "high"}
        )
        self.assertEqual(status, 400, payload)
        message = payload["error"]["message"]
        self.assertIn("high", message)
        self.assertIn("budget_tokens", message)
        self.assertIn("none", message)  # the canonical vocabulary, listed
        adapter: FakeAnthropicAdapter = self.harness.adapter  # type: ignore[assignment]
        self.assertEqual(adapter.calls, [])

    def test_enabled_requires_a_positive_budget_and_disabled_forbids_one(self) -> None:
        cases = [
            ({"type": "enabled"}, "budget_tokens"),
            ({"type": "enabled", "budget_tokens": 0}, "budget_tokens"),
            ({"type": "enabled", "budget_tokens": "2048"}, "budget_tokens"),
            ({"type": "disabled", "budget_tokens": 1024}, "budget_tokens"),
            ({"type": "sometimes"}, "thinking.type"),
            ("on", "thinking"),
        ]
        for thinking, needle in cases:
            with self.subTest(thinking=thinking):
                status, payload, _ = self.harness.request(
                    "POST", "/v1/messages", body={**VALID, "thinking": thinking}
                )
                self.assertEqual(status, 400, payload)
                self.assertIn(needle, payload["error"]["message"])
        adapter: FakeAnthropicAdapter = self.harness.adapter  # type: ignore[assignment]
        self.assertEqual(adapter.calls, [])

    def test_a_budget_is_honoured_by_an_anthropic_route(self) -> None:
        status, payload, _ = self.harness.request(
            "POST",
            "/v1/messages",
            body={
                **VALID,
                "thinking": {"type": "enabled", "budget_tokens": 2048},
            },
        )
        self.assertEqual(status, 200, payload)
        adapter: FakeAnthropicAdapter = self.harness.adapter  # type: ignore[assignment]
        sent = adapter.calls[-1]
        self.assertEqual(sent.dialect, DIALECT_ANTHROPIC_MESSAGES)
        self.assertEqual(
            sent.payload["thinking"], {"type": "enabled", "budget_tokens": 2048}
        )

    def test_a_budget_is_refused_on_a_route_that_would_have_to_convert_it(self) -> None:
        status, payload, _ = self.chat.request(
            "POST",
            "/v1/messages",
            body={
                "model": CHAT_MODEL,
                "max_tokens": 32,
                "messages": [{"role": "user", "content": "hi"}],
                "thinking": {"type": "enabled", "budget_tokens": 2048},
            },
        )
        self.assertNotEqual(status, 200, payload)
        self.assertIn("thinking", json.dumps(payload))
        chat: FakeChatAdapter = self.chat.adapter  # type: ignore[assignment]
        self.assertEqual(chat.calls, [], "the refusal happens before any provider call")

    def test_disabled_reaches_an_openai_route_without_inventing_a_budget(self) -> None:
        status, payload, _ = self.chat.request(
            "POST",
            "/v1/messages",
            body={
                "model": CHAT_MODEL,
                "max_tokens": 32,
                "messages": [{"role": "user", "content": "hi"}],
                "thinking": {"type": "disabled"},
            },
        )
        self.assertEqual(status, 200, payload)
        chat: FakeChatAdapter = self.chat.adapter  # type: ignore[assignment]
        sent = chat.calls[-1]
        self.assertNotIn("thinking", sent.payload)
        # A disabled level is the canonical "off"; the Chat dialect has no
        # published spelling for it, so the control is omitted, not guessed.
        self.assertNotIn("reasoning_effort", sent.payload)

    def test_top_k_is_carried_by_an_anthropic_route_and_refused_on_an_openai_one(self) -> None:
        status, payload, _ = self.harness.request(
            "POST", "/v1/messages", body={**VALID, "top_k": 40}
        )
        self.assertEqual(status, 200, payload)
        adapter: FakeAnthropicAdapter = self.harness.adapter  # type: ignore[assignment]
        self.assertEqual(adapter.calls[-1].payload.get("top_k"), 40)

        status, payload, _ = self.chat.request(
            "POST",
            "/v1/messages",
            body={
                "model": CHAT_MODEL,
                "max_tokens": 32,
                "messages": [{"role": "user", "content": "hi"}],
                "top_k": 40,
            },
        )
        self.assertNotEqual(status, 200, payload)
        chat: FakeChatAdapter = self.chat.adapter  # type: ignore[assignment]
        self.assertEqual(chat.calls, [])


class MessagesFlowTests(unittest.TestCase):
    """The surface runs the established flow, in Anthropic shape, both ways."""

    def setUp(self) -> None:
        self.harness = _build_harness()
        self.addCleanup(self.harness.close)
        self.chat = _build_chat_harness()
        self.addCleanup(self.chat.close)

    def test_an_anthropic_route_answers_buffered_in_anthropic_shape(self) -> None:
        status, payload, _ = self.harness.request(
            "POST", "/v1/messages", body=VALID
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["type"], "message")
        self.assertEqual(payload["role"], "assistant")
        self.assertEqual(payload["content"], [{"type": "text", "text": "ok"}])
        self.assertEqual(payload["usage"], {"input_tokens": 3, "output_tokens": 2})

    def test_an_anthropic_route_answers_a_stream_with_anthropic_events(self) -> None:
        status, body, _ = self.harness.request(
            "POST", "/v1/messages", body={**VALID, "stream": True}, raw=True
        )
        self.assertEqual(status, 200)
        text = body.decode()
        self.assertIn("event: message_start", text)
        self.assertIn("event: content_block_delta", text)
        self.assertIn('"type":"text_delta"', text.replace(" ", ""))
        self.assertIn("event: message_stop", text)
        # The OpenAI sentinel has no meaning in the Anthropic dialect.
        self.assertNotIn("[DONE]", text)

    def test_an_openai_route_is_converted_in_both_directions(self) -> None:
        status, payload, _ = self.chat.request(
            "POST",
            "/v1/messages",
            body={
                "model": CHAT_MODEL,
                "max_tokens": 32,
                "system": "Be terse.",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        self.assertEqual(status, 200, payload)
        # The response is an Anthropic Message, not the upstream body.
        self.assertEqual(payload["type"], "message")
        self.assertEqual(payload["content"], [{"type": "text", "text": "ok"}])
        self.assertEqual(payload["usage"], {"input_tokens": 3, "output_tokens": 2})
        # The request reached the provider in the Chat dialect.
        chat: FakeChatAdapter = self.chat.adapter  # type: ignore[assignment]
        sent: ProviderRequest = chat.calls[-1]
        self.assertEqual(sent.dialect, DIALECT_OPENAI_CHAT)
        self.assertEqual(
            sent.payload["messages"][0], {"role": "system", "content": "Be terse."}
        )
        self.assertNotIn("thinking", sent.payload)

    def test_a_stream_from_an_openai_route_is_rendered_as_anthropic_events(self) -> None:
        status, body, _ = self.chat.request(
            "POST",
            "/v1/messages",
            body={
                "model": CHAT_MODEL,
                "max_tokens": 32,
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
            raw=True,
        )
        self.assertEqual(status, 200)
        text = body.decode()
        self.assertIn("event: message_start", text)
        self.assertIn("event: message_stop", text)
        self.assertNotIn("[DONE]", text)

    def test_a_streaming_only_upstream_is_aggregated_for_a_buffered_caller(self) -> None:
        chat = _build_chat_harness(FakeChatAdapter())
        self.addCleanup(chat.close)
        chat.adapter.always_stream = True  # type: ignore[union-attr]
        status, payload, _ = chat.request(
            "POST",
            "/v1/messages",
            body={
                "model": CHAT_MODEL,
                "max_tokens": 32,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["type"], "message")
        self.assertEqual(payload["content"], [{"type": "text", "text": "ok"}])

    def test_the_alias_never_reaches_the_provider(self) -> None:
        with self.harness.config.mutate() as config:
            config.aliases["assistant"] = Alias(
                name="assistant",
                enabled=True,
                primary=RouteRef(FAKE_PROVIDER, "claude-haiku-5-5"),
            )
        status, payload, _ = self.harness.request(
            "POST",
            "/v1/messages",
            body={**VALID, "model": "assistant"},
        )
        self.assertEqual(status, 200, payload)
        adapter: FakeAnthropicAdapter = self.harness.adapter  # type: ignore[assignment]
        sent = adapter.calls[-1]
        self.assertEqual(sent.model, "claude-haiku-5-5")
        self.assertEqual(sent.dialect, DIALECT_ANTHROPIC_MESSAGES)

    def test_get_is_method_not_allowed(self) -> None:
        status, payload, headers = self.harness.request("GET", "/v1/messages")
        self.assertEqual(status, 405, payload)
        self.assertEqual(headers.get("Allow"), "POST")

    def test_the_surface_shares_the_openai_surfaces_host_policy(self) -> None:
        bad_host = {"Host": "evil.example"}
        status_messages, body_messages, _ = self.harness.request(
            "POST", "/v1/messages", body=VALID, headers=bad_host
        )
        status_chat, body_chat, _ = self.harness.request(
            "POST",
            "/v1/chat/completions",
            body={"model": "claude-haiku-5-5", "messages": [{"role": "user", "content": "hi"}]},
            headers=bad_host,
        )
        self.assertEqual(status_messages, 403, body_messages)
        self.assertEqual(
            (status_messages, body_messages), (status_chat, body_chat)
        )


if __name__ == "__main__":
    unittest.main()
