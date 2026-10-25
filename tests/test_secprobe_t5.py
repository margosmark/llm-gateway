"""Security regressions for T5: the Anthropic Messages surface and the bridge.

Adapted from the security acceptance probes of ``t_9ae7b587`` (F1-F3) and
kept in-tree so the fixes carry their own regression evidence. Every
provider is a fake adapter; no test contacts a network, a credential or a
real provider.

The probes pin:
* the request boundary (forwarded headers, Host, Origin, auth ordering) is
  the same one ``/v1/chat/completions`` runs, on the refusal paths;
* every surface-level refusal happens **before** any adapter is invoked;
* the alias never reaches an adapter, on either surface, buffered or streamed;
* the wave-1.6r top-level Responses ``input_image`` refusal and the Retry-After
  header are still in force on the candidate;
* image-capability gating and the bridge's "nothing is silently dropped"
  contract hold for Anthropic-shaped content blocks - an image URL the
  wave-1.6r control rejects, an ``input_image`` block, an unreadable image
  source, a non-object ``tool_use.input`` and an image inside a
  ``tool_result`` are each refused or recorded, never served 200 with the
  attachment silently gone.
"""

from __future__ import annotations

import json
import unittest

from gateway.adapters.base import ProviderRequest, ProviderResponse
from gateway.config import Alias, ModelEntry, ProviderState, RouteRef
from gateway.core.capabilities import (
    DIALECT_ANTHROPIC_MESSAGES,
    DIALECT_OPENAI_CHAT,
    DIALECT_OPENAI_RESPONSES,
)
from gateway.bridge.anthropic import convert_request, payload_has_image_input

from tests.test_anthropic_surface import FakeAnthropicAdapter
from tests.test_end_to_end import FAKE_PROVIDER, FakeAdapter, GatewayHarness
from tests.test_messages_handler import FakeChatAdapter

ALIAS = "sec-alias"
UPSTREAM_MODEL = "upstream-x"


def _harness(adapter, dialect: str, *, image: bool = False, alias_name: str = ALIAS):
    """One alias whose single route is ``upstream_model`` behind ``adapter``."""
    harness = GatewayHarness(adapter)
    with harness.config.mutate() as config:
        config.providers[FAKE_PROVIDER] = ProviderState(
            provider_id=FAKE_PROVIDER,
            kind=FAKE_PROVIDER,
            state="connected",
            models={
                UPSTREAM_MODEL: ModelEntry(
                    model_id=UPSTREAM_MODEL,
                    enabled=True,
                    dialect=dialect,
                    source="discovered",
                    supports_image_input=image,
                )
            },
        )
        if alias_name is not None:
            config.aliases[alias_name] = Alias(
                name=alias_name,
                enabled=True,
                primary=RouteRef(FAKE_PROVIDER, UPSTREAM_MODEL),
            )
    return harness


def _messages_body(content, **extra) -> dict:
    body = {
        "model": ALIAS,
        "max_tokens": 32,
        "messages": [{"role": "user", "content": content}],
    }
    body.update(extra)
    return body


def _strict_anthropic_frames(body: bytes) -> list[dict]:
    """Parse an Anthropic SSE body the strict way: frame = data + terminator.

    Fails (raises AssertionError) on a frame that is not terminated, that
    lacks its ``event:`` line, or whose data is not JSON.
    """
    text = body.decode("utf-8")
    assert text.endswith("\n\n"), "stream does not end on a frame terminator"
    raw_frames = text.split("\n\n")[:-1]
    events: list[dict] = []
    for raw in raw_frames:
        assert raw, "empty frame between terminators"
        lines = raw.split("\n")
        assert lines[0].startswith("event: "), f"frame has no event line: {raw[:60]!r}"
        data_lines = [line for line in lines if line.startswith("data: ")]
        assert data_lines, f"frame has no data line: {raw[:60]!r}"
        events.append(json.loads("\n".join(d[len("data: ") :] for d in data_lines)))
    return events


def _chat_frames(text: str = "ok") -> list[bytes]:
    """A Chat Completions stream that reports its usage, as OpenAI does."""
    def frame(choices, usage=None) -> bytes:
        obj: dict = {
            "id": "c1",
            "object": "chat.completion.chunk",
            "model": UPSTREAM_MODEL,
            "choices": choices,
        }
        if usage is not None:
            obj["usage"] = usage
        return ("data: " + json.dumps(obj) + "\n\n").encode()

    return [
        frame([{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]),
        frame([{"index": 0, "delta": {"content": text}, "finish_reason": None}]),
        frame([{"index": 0, "delta": {}, "finish_reason": "stop"}]),
        frame([], usage={"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}),
        b"data: [DONE]\n\n",
    ]


class _ResponsesAdapter(FakeAdapter):
    """A Responses-dialect provider that reports usage on both paths."""

    def __init__(self, **kwargs) -> None:
        kwargs.setdefault("dialect", DIALECT_OPENAI_RESPONSES)
        super().__init__(**kwargs)
        self.stream_frames = [
            b'data: {"type":"response.output_text.delta","delta":"ok"}\n\n',
            b'data: {"type":"response.completed","response":{"id":"resp_1",'
            b'"usage":{"input_tokens":3,"output_tokens":2}}}\n\n',
        ]
        self.buffered_body = {
            "id": "resp_1",
            "object": "response",
            "status": "completed",
            "model": UPSTREAM_MODEL,
            "output": [{
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": "ok"}],
            }],
            "usage": {"input_tokens": 3, "output_tokens": 2},
        }

    def invoke(self, request: ProviderRequest) -> ProviderResponse:
        self.calls.append(request)
        if request.stream:
            return ProviderResponse(
                status=200, headers={}, body=None, stream=iter(self.stream_frames),
                content_type="text/event-stream", dialect=self.dialect,
            )
        return ProviderResponse(
            status=200, headers={}, body=json.dumps(self.buffered_body).encode("utf-8"),
            content_type="application/json", dialect=self.dialect,
        )


class BoundaryParityTests(unittest.TestCase):
    """The new surface carries the same LAN boundary as the OpenAI ones."""

    def setUp(self) -> None:
        self.adapter = FakeChatAdapter(provider_id=FAKE_PROVIDER, dialect=DIALECT_OPENAI_CHAT)
        self.harness = _harness(self.adapter, DIALECT_OPENAI_CHAT)
        self.addCleanup(self.harness.close)

    def test_forwarded_header_is_refused_on_both_surfaces_before_any_provider_call(self) -> None:
        for path in ("/v1/messages", "/v1/chat/completions"):
            with self.subTest(path=path):
                self.adapter.calls.clear()
                status, payload, _ = self.harness.request(
                    "POST",
                    path,
                    body=_messages_body("hi") if path.endswith("messages")
                    else {"model": ALIAS, "messages": [{"role": "user", "content": "hi"}]},
                    headers={"X-Forwarded-For": "1.2.3.4"},
                )
                self.assertEqual(status, 400, payload)
                self.assertEqual(payload["error"]["type"], "forwarded_header_rejected")
                self.assertEqual(self.adapter.calls, [])

    def test_host_mismatch_is_refused_on_both_surfaces_before_any_provider_call(self) -> None:
        for path in ("/v1/messages", "/v1/chat/completions"):
            with self.subTest(path=path):
                self.adapter.calls.clear()
                status, payload, _ = self.harness.request(
                    "POST",
                    path,
                    body=_messages_body("hi") if path.endswith("messages")
                    else {"model": ALIAS, "messages": [{"role": "user", "content": "hi"}]},
                    headers={"Host": "evil.example"},
                )
                self.assertEqual(status, 403, payload)
                self.assertEqual(payload["error"]["type"], "host_mismatch")
                self.assertEqual(self.adapter.calls, [])

    def test_origin_policy_matches_the_openai_surfaces_and_the_console_still_refuses(self) -> None:
        # API surfaces authenticate with a bearer token, so a cross-origin
        # POST is not refused there - /v1/messages must behave exactly like
        # /v1/chat/completions, not more and not less.
        cross = {"Origin": "https://evil.example"}
        anthropic = self.harness.request(
            "POST", "/v1/messages", body=_messages_body("hi"), headers=cross
        )
        self.adapter.calls.clear()
        openai = self.harness.request(
            "POST",
            "/v1/chat/completions",
            body={"model": ALIAS, "messages": [{"role": "user", "content": "hi"}]},
            headers=cross,
        )
        self.assertEqual(anthropic[0], openai[0], (anthropic[1], openai[1]))
        self.assertEqual(anthropic[0], 200)
        # The browser surface still refuses the same cross-origin mutation:
        # the control is live on this candidate, it just is not API-relevant.
        console = self.harness.request(
            "POST", "/owner/api/settings", body={}, headers=cross
        )
        self.assertEqual(console[0], 403, console[1])
        self.assertEqual(console[1]["error"]["type"], "origin_mismatch")

    def test_a_valid_request_is_served_and_the_alias_never_reaches_the_adapter(self) -> None:
        status, payload, _ = self.harness.request(
            "POST", "/v1/messages", body=_messages_body("hi")
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["type"], "message")
        self.assertEqual(payload["content"][0]["text"], "ok")
        sent: ProviderRequest = self.adapter.calls[-1]
        self.assertEqual(sent.model, UPSTREAM_MODEL)
        serialized = json.dumps(sent.payload)
        self.assertNotIn(ALIAS, serialized)


class RefusalBeforeProviderCallTests(unittest.TestCase):
    """Every surface-level refusal happens before an adapter is invoked."""

    #: (label, body) - each must be refused with 400 malformed_request.
    MALFORMED = [
        ("missing max_tokens", {"model": ALIAS, "messages": [{"role": "user", "content": "hi"}]}),
        ("messages not a list", {"model": ALIAS, "max_tokens": 8, "messages": "hi"}),
        ("messages empty", {"model": ALIAS, "max_tokens": 8, "messages": []}),
        ("message not an object", {"model": ALIAS, "max_tokens": 8, "messages": ["hi"]}),
        ("unknown role", {"model": ALIAS, "max_tokens": 8,
                          "messages": [{"role": "system", "content": "hi"}]}),
        ("content neither str nor list", {"model": ALIAS, "max_tokens": 8,
                                          "messages": [{"role": "user", "content": 7}]}),
        ("block not an object", {"model": ALIAS, "max_tokens": 8,
                                 "messages": [{"role": "user", "content": ["hi"]}]}),
        ("uncarryable block type", {"model": ALIAS, "max_tokens": 8,
                                    "messages": [{"role": "user", "content": [
                                        {"type": "document", "source": "x"}]}]}),
        ("temperature is a string", _messages_body("hi", temperature="hot")),
        ("stop_sequences not strings", _messages_body("hi", stop_sequences=[1])),
        ("top_k is a string", _messages_body("hi", top_k="many")),
        ("stream is a string", _messages_body("hi", stream="yes")),
        ("thinking as a level", _messages_body("hi", thinking="high")),
        ("thinking budget missing", _messages_body("hi", thinking={"type": "enabled"})),
        ("thinking disabled with budget", _messages_body(
            "hi", thinking={"type": "disabled", "budget_tokens": 10})),
        ("metadata not an object", _messages_body("hi", metadata="me")),
        ("tool_choice not an object", _messages_body("hi", tool_choice="auto")),
        ("tools not a list", _messages_body("hi", tools="[]")),
        ("system not text blocks", _messages_body("hi", system=[{"type": "image"}])),
    ]

    def setUp(self) -> None:
        self.adapter = FakeChatAdapter(provider_id=FAKE_PROVIDER, dialect=DIALECT_OPENAI_CHAT)
        self.harness = _harness(self.adapter, DIALECT_OPENAI_CHAT)
        self.addCleanup(self.harness.close)

    def test_each_malformed_body_is_400_with_zero_provider_calls(self) -> None:
        for label, body in self.MALFORMED:
            with self.subTest(case=label):
                self.adapter.calls.clear()
                status, payload, _ = self.harness.request("POST", "/v1/messages", body=body)
                self.assertEqual(status, 400, payload)
                self.assertEqual(payload["error"]["type"], "malformed_request")
                self.assertEqual(self.adapter.calls, [], f"{label}: a provider was called")

    def test_unsupported_fields_are_refused_or_served_by_a_route_that_can_carry_them(self) -> None:
        # top_k / a thinking budget are Anthropic-only on a converted path.
        # On an OpenAI-only alias the conversion refuses before any adapter
        # call; on an Anthropic-dialect route the same request is served
        # untouched, because that route speaks the dialect natively.
        for label, body in (
            ("top_k", _messages_body("hi", top_k=40)),
            ("thinking budget", _messages_body(
                "hi", thinking={"type": "enabled", "budget_tokens": 1024})),
        ):
            with self.subTest(field=label, route="openai_only"):
                self.adapter.calls.clear()
                status, payload, _ = self.harness.request("POST", "/v1/messages", body=body)
                self.assertNotEqual(status, 200, payload)
                self.assertEqual(self.adapter.calls, [], f"{label}: provider called after refusal")

        anthropic_adapter = FakeAnthropicAdapter(provider_id=FAKE_PROVIDER)
        harness = _harness(anthropic_adapter, DIALECT_ANTHROPIC_MESSAGES)
        self.addCleanup(harness.close)
        for label, body in (
            ("top_k", _messages_body("hi", top_k=40)),
            ("thinking budget", _messages_body(
                "hi", thinking={"type": "enabled", "budget_tokens": 1024})),
        ):
            with self.subTest(field=label, route="anthropic_native"):
                anthropic_adapter.calls.clear()
                status, payload, _ = harness.request("POST", "/v1/messages", body=body)
                self.assertEqual(status, 200, payload)
                self.assertEqual(len(anthropic_adapter.calls), 1)
                forwarded = anthropic_adapter.calls[0].payload
                if label == "top_k":
                    self.assertEqual(forwarded.get("top_k"), 40)
                else:
                    self.assertEqual(
                        forwarded.get("thinking"),
                        {"type": "enabled", "budget_tokens": 1024},
                    )


class RouteAndAliasGateTests(unittest.TestCase):
    """The new dialect cannot sidestep the alias, route or capability gates."""

    def setUp(self) -> None:
        self.adapter = FakeChatAdapter(provider_id=FAKE_PROVIDER, dialect=DIALECT_OPENAI_CHAT)
        self.harness = _harness(self.adapter, DIALECT_OPENAI_CHAT)
        self.addCleanup(self.harness.close)

    def test_a_raw_upstream_id_is_not_reachable_unless_published_as_an_alias(self) -> None:
        for path, body in (
            ("/v1/messages", _messages_body("hi", model=UPSTREAM_MODEL)),
            ("/v1/chat/completions",
             {"model": UPSTREAM_MODEL, "messages": [{"role": "user", "content": "hi"}]}),
        ):
            with self.subTest(path=path):
                self.adapter.calls.clear()
                status, payload, _ = self.harness.request("POST", path, body=body)
                self.assertEqual(status, 404, payload)
                self.assertEqual(payload["error"]["type"], "unknown_model")
                self.assertEqual(self.adapter.calls, [])

    def test_a_disabled_alias_is_refused_on_the_messages_surface(self) -> None:
        with self.harness.config.mutate() as config:
            config.aliases[ALIAS].enabled = False
        status, payload, _ = self.harness.request("POST", "/v1/messages", body=_messages_body("hi"))
        self.assertEqual(status, 404, payload)
        self.assertEqual(payload["error"]["type"], "unknown_model")
        self.assertEqual(self.adapter.calls, [])

    def test_a_route_whose_model_is_disabled_is_refused_without_a_provider_call(self) -> None:
        with self.harness.config.mutate() as config:
            config.providers[FAKE_PROVIDER].models[UPSTREAM_MODEL].enabled = False
        status, payload, _ = self.harness.request("POST", "/v1/messages", body=_messages_body("hi"))
        self.assertEqual(status, 503, payload)
        self.assertEqual(self.adapter.calls, [])

    def test_a_non_convertible_dialect_cannot_be_reached_from_the_messages_surface(self) -> None:
        """The Google dialect is still untranslated: excluded, recorded, uncalled."""
        from gateway.core.capabilities import DIALECT_GOOGLE_V1BETA

        adapter = FakeChatAdapter(provider_id=FAKE_PROVIDER, dialect=DIALECT_OPENAI_CHAT)
        harness = GatewayHarness(adapter)
        self.addCleanup(harness.close)
        with harness.config.mutate() as config:
            config.providers[FAKE_PROVIDER] = ProviderState(
                provider_id=FAKE_PROVIDER, kind=FAKE_PROVIDER, state="connected",
                models={"google-model": ModelEntry(
                    model_id="google-model", enabled=True,
                    dialect=DIALECT_GOOGLE_V1BETA, source="discovered")},
            )
            config.aliases[ALIAS] = Alias(
                name=ALIAS, enabled=True,
                primary=RouteRef(FAKE_PROVIDER, "google-model"),
            )
        status, payload, _ = harness.request("POST", "/v1/messages", body=_messages_body("hi"))
        self.assertEqual(status, 503, payload)
        self.assertEqual(adapter.calls, [])

    def test_alias_never_reaches_the_adapter_on_buffered_or_streamed_requests(self) -> None:
        for stream in (False, True):
            with self.subTest(stream=stream):
                self.adapter.calls.clear()
                body = _messages_body("hi")
                body["stream"] = stream
                status, payload, _ = self.harness.request(
                    "POST", "/v1/messages", body=body, raw=stream
                )
                self.assertEqual(status, 200, payload)
                sent: ProviderRequest = self.adapter.calls[-1]
                self.assertEqual(sent.model, UPSTREAM_MODEL)
                self.assertEqual(sent.payload["model"], UPSTREAM_MODEL)
                self.assertNotIn(ALIAS, json.dumps(sent.payload))


class ImageCapabilityGateTests(unittest.TestCase):
    """Image gating must see every block type the bridge carries."""

    def _alias(self, *, image_capable: bool):
        adapter = FakeChatAdapter(provider_id=FAKE_PROVIDER, dialect=DIALECT_OPENAI_CHAT)
        harness = _harness(adapter, DIALECT_OPENAI_CHAT, image=image_capable)
        self.addCleanup(harness.close)
        return adapter, harness

    def test_an_image_block_is_refused_when_no_route_accepts_images(self) -> None:
        adapter, harness = self._alias(image_capable=False)
        body = _messages_body([
            {"type": "text", "text": "look"},
            {"type": "image", "source": {"type": "url", "url": "https://x/y.png"}},
        ])
        status, payload, _ = harness.request("POST", "/v1/messages", body=body)
        self.assertEqual(status, 503, payload)
        self.assertEqual(payload["error"]["capability"], "image_input")
        self.assertEqual(adapter.calls, [])

    def test_an_input_image_block_is_gated_exactly_like_an_image_block(self) -> None:
        """F2 regression: the OpenAI spelling is gated like the Anthropic one.

        ``input_image`` is an accepted content-block type on this surface
        (gateway/http/messages.py CARRYABLE_BLOCK_TYPES) and the bridge
        renders it as a real image part, so routing must learn the request
        carries an attachment. Detection now parses it with the same
        parser conversion uses, so both spellings fail closed the same way
        on a text-only alias: 503, capability ``image_input``, zero
        adapter calls, and nothing served behind an empty ledger.
        """
        adapter, harness = self._alias(image_capable=False)
        body = _messages_body([
            {"type": "text", "text": "look"},
            {"type": "input_image", "image_url": "https://x/y.png"},
        ])
        status, payload, _ = harness.request("POST", "/v1/messages", body=body)
        self.assertEqual(status, 503, payload)
        self.assertEqual(payload["error"]["type"], "no_eligible_route")
        self.assertEqual(payload["error"]["capability"], "image_input")
        self.assertEqual(adapter.calls, [])
        self.assertEqual(harness.router._ledger.recent(), [])

    def test_both_block_spellings_reach_an_image_capable_route_intact(self) -> None:
        """The gate must not over-refuse: a capable route still serves both."""
        for label, block in (
            ("image", {"type": "image",
                       "source": {"type": "url", "url": "https://x/y.png"}}),
            ("input_image", {"type": "input_image", "image_url": "https://x/y.png"}),
        ):
            with self.subTest(block=label):
                adapter, harness = self._alias(image_capable=True)
                status, payload, _ = harness.request(
                    "POST", "/v1/messages",
                    body=_messages_body([{"type": "text", "text": "look"}, block]),
                )
                self.assertEqual(status, 200, payload)
                self.assertEqual(len(adapter.calls), 1)
                self.assertIn("y.png", json.dumps(adapter.calls[0].payload))

    def test_detection_and_conversion_agree_on_both_block_spellings(self) -> None:
        image_block = [{"role": "user", "content": [
            {"type": "image", "source": {"type": "url", "url": "https://x/y.png"}}]}]
        input_image_block = [{"role": "user", "content": [
            {"type": "input_image", "image_url": "https://x/y.png"}]}]
        self.assertTrue(payload_has_image_input(
            {"messages": image_block}, DIALECT_ANTHROPIC_MESSAGES))
        self.assertTrue(payload_has_image_input(
            {"messages": input_image_block}, DIALECT_ANTHROPIC_MESSAGES))
        # Conversion turns that very block into an image the serving model
        # is asked to look at - so detection cannot miss it (F2).
        converted = convert_request(
            {"model": ALIAS, "max_tokens": 8, "messages": input_image_block},
            DIALECT_ANTHROPIC_MESSAGES, DIALECT_OPENAI_CHAT,
        )
        parts = converted["messages"][0]["content"]
        self.assertIn({"type": "image_url", "image_url": {"url": "https://x/y.png"}}, parts)

    def test_deep_tool_result_nesting_is_never_answered_with_a_500(self) -> None:
        """F6 regression: capability detection walks the chain iteratively.

        A ~1200-deep nested ``tool_result`` chain used to blow Python's
        recursion limit *inside* ``payload_has_image_input``, before any
        route was chosen, and the dispatcher's defensive layer turned
        that into a generic 500 ``gateway_error``. The walk no longer
        recurses, so depth answers like depth always did: the same clean,
        recorded refusal a 900-deep chain gets, with zero adapter calls -
        and a native Anthropic route, which speaks the dialect, passes
        the whole chain through and serves it.
        """
        def body_of(depth: int) -> dict:
            block: dict = {"type": "text", "text": "bottom"}
            for _ in range(depth):
                block = {"type": "tool_result", "tool_use_id": "t1", "content": [block]}
            return _messages_body([block])

        adapter, harness = self._alias(image_capable=True)
        status, payload, _ = harness.request(
            "POST", "/v1/messages", body=body_of(1200))
        self.assertNotEqual(status, 500, payload)
        self.assertEqual(status, 502, payload)
        self.assertEqual(payload["error"]["type"], "policy")
        attempts = payload["error"]["detail"]["attempts"]
        self.assertEqual([entry["outcome"] for entry in attempts],
                         ["conversion_refused"])
        self.assertEqual(adapter.calls, [])
        self.assertEqual(harness.router._ledger.recent()[0]["attempts"][0]["outcome"],
                         "conversion_refused")

        native = FakeAnthropicAdapter(provider_id=FAKE_PROVIDER)
        native_harness = _harness(native, DIALECT_ANTHROPIC_MESSAGES, image=True)
        self.addCleanup(native_harness.close)
        status, payload, _ = native_harness.request(
            "POST", "/v1/messages", body=body_of(1200))
        self.assertEqual(status, 200, payload)
        self.assertEqual(len(native.calls), 1)
        self.assertIn("bottom", json.dumps(native.calls[0].payload))


class SilentDropTests(unittest.TestCase):
    """Blocks the surface accepts must not vanish inside the bridge."""

    def setUp(self) -> None:
        self.adapter = FakeChatAdapter(provider_id=FAKE_PROVIDER, dialect=DIALECT_OPENAI_CHAT)
        self.harness = _harness(self.adapter, DIALECT_OPENAI_CHAT)
        self.addCleanup(self.harness.close)

    def _send(self, content):
        self.adapter.calls.clear()
        status, payload, _ = self.harness.request(
            "POST", "/v1/messages", body=_messages_body(content)
        )
        return status, payload

    def test_an_image_block_without_a_usable_source_is_refused(self) -> None:
        """F3 regression: the block's insides are validated, not skipped.

        ``{"type": "image"}`` with an unrecognised ``source`` passes
        CARRYABLE_BLOCK_TYPES (a type check), so the *parser* has to catch
        it. It now raises instead of returning ``None``, so the block can
        never be skipped at the conversion boundary: the refusal comes from
        capability detection, before any route is chosen, on a text-only
        alias as well as an image-capable one.
        """
        for image_capable in (False, True):
            with self.subTest(image_capable=image_capable):
                adapter = FakeChatAdapter(provider_id=FAKE_PROVIDER,
                                          dialect=DIALECT_OPENAI_CHAT)
                harness = _harness(adapter, DIALECT_OPENAI_CHAT, image=image_capable)
                self.addCleanup(harness.close)
                status, payload, _ = harness.request(
                    "POST", "/v1/messages",
                    body=_messages_body([
                        {"type": "text", "text": "look"},
                        {"type": "image", "source": {"type": "unknown_format"}},
                    ]),
                )
                self.assertEqual(status, 422, payload)
                self.assertEqual(payload["error"]["type"], "conversion_refused")
                self.assertEqual(payload["error"]["capability"], "image_input")
                self.assertEqual(adapter.calls, [])
        # The refusal names what was wrong, so the caller can fix it.
        status, payload, _ = self.harness.request(
            "POST", "/v1/messages",
            body=_messages_body([
                {"type": "image", "source": {"type": "unknown_format"}},
            ]),
        )
        self.assertEqual(status, 422, payload)
        self.assertIn("source", payload["error"]["message"])
        self.assertEqual(self.adapter.calls, [])

    def test_a_tool_use_block_with_a_non_object_input_is_refused(self) -> None:
        """F3 regression: the caller's arguments are never replaced by {}."""
        status, payload = self._send([
            {"type": "tool_use", "id": "t1", "name": "search", "input": "not-an-object"},
        ])
        self.assertEqual(status, 400, payload)
        self.assertEqual(payload["error"]["type"], "malformed_request")
        self.assertIn("input", payload["error"]["message"])
        self.assertEqual(self.adapter.calls, [], "no provider may be called")

    def test_an_image_inside_a_tool_result_is_refused_not_dropped(self) -> None:
        """F3 regression: a vision tool result survives or is refused, never lost.

        Three paths, three correct answers:

        * a text-only alias cannot see the image - 503 ``image_input``,
          zero calls (detection counts an image inside a ``tool_result``);
        * an image-capable **converted** route cannot express an image as a
          string function output - the bridge refuses, and the refusal is
          *recorded* (``attempts[].outcome = conversion_refused``), never a
          bare 200 with the attachment gone;
        * an image-capable **native Anthropic** route speaks the dialect
          natively, so the block is passed through whole and served.
        """
        content = [
            {"type": "tool_result", "tool_use_id": "t1", "content": [
                {"type": "image", "source": {"type": "url", "url": "https://x/y.png"}},
                {"type": "text", "text": "caption"},
            ]},
        ]

        status, payload = self._send(content)
        self.assertEqual(status, 503, payload)
        self.assertEqual(payload["error"]["capability"], "image_input")
        self.assertEqual(self.adapter.calls, [])

        adapter = FakeChatAdapter(provider_id=FAKE_PROVIDER, dialect=DIALECT_OPENAI_CHAT)
        harness = _harness(adapter, DIALECT_OPENAI_CHAT, image=True)
        self.addCleanup(harness.close)
        status, payload, _ = harness.request(
            "POST", "/v1/messages", body=_messages_body(content))
        self.assertNotEqual(status, 200, payload)
        self.assertEqual(adapter.calls, [])
        self.assertEqual(payload["error"]["type"], "policy")
        attempts = payload["error"]["detail"]["attempts"]
        self.assertEqual([entry["outcome"] for entry in attempts],
                         ["conversion_refused"])
        ledger = harness.router._ledger.recent()[0]
        self.assertEqual(ledger["attempts"][0]["outcome"], "conversion_refused")

        native = FakeAnthropicAdapter(provider_id=FAKE_PROVIDER)
        native_harness = _harness(native, DIALECT_ANTHROPIC_MESSAGES, image=True)
        self.addCleanup(native_harness.close)
        status, payload, _ = native_harness.request(
            "POST", "/v1/messages", body=_messages_body(content))
        self.assertEqual(status, 200, payload)
        self.assertEqual(len(native.calls), 1)
        self.assertIn("y.png", json.dumps(native.calls[0].payload))
        self.assertIn("caption", json.dumps(native.calls[0].payload))

    def test_a_hybrid_image_text_tool_result_block_is_never_served_as_its_caption(self) -> None:
        """F5 regression: a ``text`` field on an image block is not a text block.

        The ``tool_result`` reader counts blocks by **type**, so an
        ``image`` block that also carries a caption is still an image.
        On a converted route it is refused and the refusal is *recorded*
        (``attempts[].outcome = conversion_refused``) - never a bare 200
        whose provider payload holds the caption and nothing else - while
        a native Anthropic route passes the block through whole. On a
        text-only alias the gate refuses it first, exactly like the same
        image without a caption.
        """
        content = [
            {"type": "tool_result", "tool_use_id": "t1", "content": [
                {"type": "image", "text": "caption field",
                 "source": {"type": "url", "url": "https://cdn.example/vision.png"}},
            ]},
        ]

        # text-only alias: the image gate answers first (parity with the
        # same block stripped of its caption).
        status, payload = self._send(content)
        self.assertEqual(status, 503, payload)
        self.assertEqual(payload["error"]["capability"], "image_input")
        self.assertEqual(self.adapter.calls, [])

        # image-capable converted route: refused and recorded, never
        # coerced to the caption string.
        adapter = FakeChatAdapter(provider_id=FAKE_PROVIDER, dialect=DIALECT_OPENAI_CHAT)
        harness = _harness(adapter, DIALECT_OPENAI_CHAT, image=True)
        self.addCleanup(harness.close)
        status, payload, _ = harness.request(
            "POST", "/v1/messages", body=_messages_body(content))
        self.assertNotEqual(status, 200, payload)
        self.assertEqual(adapter.calls, [])
        self.assertEqual(payload["error"]["type"], "policy")
        attempts = payload["error"]["detail"]["attempts"]
        self.assertEqual([entry["outcome"] for entry in attempts],
                         ["conversion_refused"])
        ledger = harness.router._ledger.recent()[0]
        self.assertEqual(ledger["attempts"][0]["outcome"], "conversion_refused")

        # native Anthropic route: the block is handed over whole - the
        # provider payload carries the URL and the caption together.
        native = FakeAnthropicAdapter(provider_id=FAKE_PROVIDER)
        native_harness = _harness(native, DIALECT_ANTHROPIC_MESSAGES, image=True)
        self.addCleanup(native_harness.close)
        status, payload, _ = native_harness.request(
            "POST", "/v1/messages", body=_messages_body(content))
        self.assertEqual(status, 200, payload)
        self.assertEqual(len(native.calls), 1)
        rendered = json.dumps(native.calls[0].payload)
        self.assertIn("vision.png", rendered)
        self.assertIn("caption field", rendered)

    def test_a_non_dict_block_is_refused_rather_than_dropped(self) -> None:
        status, payload = self._send(["just a string"])
        self.assertEqual(status, 400, payload)
        self.assertEqual(self.adapter.calls, [])


class BufferedStreamSymmetryTests(unittest.TestCase):
    """One answer, two shapes: buffered and strict-SSE agree."""

    def _run(self, adapter, dialect: str):
        harness = _harness(adapter, dialect)
        self.addCleanup(harness.close)
        status, buffered, _ = harness.request("POST", "/v1/messages", body=_messages_body("hello"))
        self.assertEqual(status, 200, buffered)
        streamed_body = _messages_body("hello", stream=True)
        status, raw, headers = harness.request(
            "POST", "/v1/messages", body=streamed_body, raw=True
        )
        self.assertEqual(status, 200, raw)
        content_type = next(
            (v for k, v in headers.items() if k.lower() == "content-type"), ""
        )
        self.assertIn("text/event-stream", content_type)
        return buffered, _strict_anthropic_frames(raw)

    def _assert_symmetric(self, buffered: dict, events: list[dict]) -> None:
        kinds = [event.get("type") for event in events]
        for expected in (
            "message_start", "content_block_start", "content_block_delta",
            "content_block_stop", "message_delta", "message_stop",
        ):
            self.assertIn(expected, kinds)
        self.assertEqual(kinds[0], "message_start")
        self.assertEqual(kinds[-1], "message_stop")

        text = "".join(
            event["delta"]["text"]
            for event in events
            if event.get("type") == "content_block_delta"
            and event.get("delta", {}).get("type") == "text_delta"
        )
        buffered_text = "".join(
            block.get("text", "")
            for block in buffered["content"]
            if block.get("type") == "text"
        )
        self.assertEqual(text, buffered_text)
        self.assertEqual(buffered_text, "ok")

        # Usage: the buffered object carries prompt and completion; the
        # stream must carry the same totals. On a converted route the
        # upstream only learns its prompt count at the end of the chat
        # stream, so the gateway reports both at ``message_delta``; a native
        # Anthropic upstream reports the prompt at ``message_start``. Merge
        # the two events and the totals must equal the buffered ones.
        start = next(e for e in events if e["type"] == "message_start")
        delta = next(e for e in events if e["type"] == "message_delta")
        self.assertIn("input_tokens", start["message"]["usage"])
        merged = dict(start["message"]["usage"])
        merged.update(delta["usage"])
        self.assertEqual(merged.get("input_tokens"), buffered["usage"]["input_tokens"])
        self.assertEqual(merged.get("output_tokens"), buffered["usage"]["output_tokens"])
        self.assertEqual(buffered["usage"]["input_tokens"], 3)
        self.assertEqual(buffered["usage"]["output_tokens"], 2)
        self.assertEqual(buffered["stop_reason"], "end_turn")

    def test_converted_route_chat_upstream_serves_anthropic_shape_on_both_paths(self) -> None:
        adapter = FakeChatAdapter(
            provider_id=FAKE_PROVIDER, dialect=DIALECT_OPENAI_CHAT,
            stream_frames=_chat_frames(),
        )
        buffered, events = self._run(adapter, DIALECT_OPENAI_CHAT)
        self.assertEqual(buffered["type"], "message")
        self._assert_symmetric(buffered, events)

    def test_native_anthropic_route_serves_both_paths(self) -> None:
        adapter = FakeAnthropicAdapter(provider_id=FAKE_PROVIDER)
        buffered, events = self._run(adapter, DIALECT_ANTHROPIC_MESSAGES)
        self.assertEqual(buffered["type"], "message")
        self._assert_symmetric(buffered, events)

    def test_responses_route_serves_anthropic_shape_on_both_paths(self) -> None:
        adapter = _ResponsesAdapter(provider_id=FAKE_PROVIDER)
        buffered, events = self._run(adapter, DIALECT_OPENAI_RESPONSES)
        self.assertEqual(buffered["type"], "message")
        self._assert_symmetric(buffered, events)

    def test_the_openai_surfaces_still_render_anthropic_upstreams(self) -> None:
        # The other direction of the same contract: an Anthropic upstream
        # answering an OpenAI caller on both paths.
        adapter = FakeAnthropicAdapter(provider_id=FAKE_PROVIDER)
        harness = _harness(adapter, DIALECT_ANTHROPIC_MESSAGES)
        self.addCleanup(harness.close)
        status, payload, _ = harness.request(
            "POST", "/v1/chat/completions",
            body={"model": ALIAS, "messages": [{"role": "user", "content": "hi"}]},
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["object"], "chat.completion")
        self.assertEqual(payload["choices"][0]["message"]["content"], "ok")

        status, raw, _ = harness.request(
            "POST", "/v1/chat/completions",
            body={"model": ALIAS, "messages": [{"role": "user", "content": "hi"}],
                  "stream": True},
            raw=True,
        )
        self.assertEqual(status, 200)
        text = raw.decode()
        self.assertIn("chat.completion.chunk", text)
        self.assertTrue(text.endswith("data: [DONE]\n\n"), text[-40:])
        self.assertIn('"content":"ok"', text)


class WaveRetainedTests(unittest.TestCase):
    """The candidate keeps the already-deployed wave-1.6r refusals."""

    def setUp(self) -> None:
        self.adapter = FakeChatAdapter(provider_id=FAKE_PROVIDER, dialect=DIALECT_OPENAI_CHAT)
        self.harness = _harness(self.adapter, DIALECT_OPENAI_CHAT)
        self.addCleanup(self.harness.close)

    def test_top_level_responses_input_image_is_refused_before_route_selection(self) -> None:
        body = {
            "model": ALIAS,
            "input": [
                {"role": "user", "content": [{"type": "input_text", "text": "hi"}]},
                {"type": "input_image", "image_url": "data:image/png;base64,AA"},
            ],
        }
        status, payload, _ = self.harness.request("POST", "/v1/responses", body=body)
        self.assertEqual(status, 422, payload)
        self.assertEqual(payload["error"]["type"], "conversion_refused")
        self.assertEqual(payload["error"]["capability"], "image_input")
        self.assertEqual(self.adapter.calls, [])

    def test_top_level_input_image_is_refused_on_the_detections_path_too(self) -> None:
        body = {
            "model": ALIAS,
            "input": [{"type": "input_image", "image_url": "data:image/png;base64,AA"}],
        }
        status, payload, _ = self.harness.request("POST", "/v1/responses", body=body)
        self.assertEqual(status, 422, payload)
        self.assertEqual(self.adapter.calls, [])

    def test_retry_after_still_reaches_the_client_from_the_messages_surface(self) -> None:
        self.harness.router.circuits.set(FAKE_PROVIDER, 900, model=UPSTREAM_MODEL)
        status, payload, headers = self.harness.request(
            "POST", "/v1/messages", body=_messages_body("hi")
        )
        self.assertEqual(status, 503, payload)
        retry_after = int(dict(headers)["Retry-After"])
        self.assertGreaterEqual(retry_after, 895)
        self.assertLessEqual(retry_after, 900)
        self.assertEqual(self.adapter.calls, [])


class ImageUrlValidationTests(unittest.TestCase):
    """The wave-1.6r image-URL validation applies on every surface.

    ``gateway.bridge.requests._validated_image_url`` refuses non-HTTP(S)
    schemes, credentialed URLs, malformed data URLs and oversized payloads.
    The Chat and Responses parsers run it; the Anthropic parser added by T5
    now runs it too, on every image URL entering a content part - the
    control case (the OpenAI surface) must keep its 422, and the
    ``/v1/messages`` cases must match it instead of forwarding.
    """

    CONTROL_BODY = {"model": ALIAS, "messages": [{"role": "user", "content": [
        {"type": "text", "text": "look"},
        {"type": "image_url", "image_url": {"url": "file:///etc/passwd"}},
    ]}]}

    def _harness(self, *, image: bool):
        adapter = FakeChatAdapter(provider_id=FAKE_PROVIDER, dialect=DIALECT_OPENAI_CHAT)
        harness = _harness(adapter, DIALECT_OPENAI_CHAT, image=image)
        self.addCleanup(harness.close)
        return adapter, harness

    @staticmethod
    def _refusal(status: int, payload: dict, adapter) -> None:
        assert status == 422, payload
        assert payload["error"]["type"] == "conversion_refused", payload
        assert payload["error"]["capability"] == "image_input", payload
        assert adapter.calls == [], "no provider may be called after a refusal"

    def test_the_openai_surface_refuses_a_non_http_image_url(self) -> None:
        adapter, harness = self._harness(image=True)
        status, payload, _ = harness.request("POST", "/v1/chat/completions", body=self.CONTROL_BODY)
        self.assertEqual(status, 422, payload)
        self.assertEqual(payload["error"]["capability"], "image_input")
        self.assertEqual(adapter.calls, [])

    def test_the_messages_surface_refuses_a_non_http_image_url(self) -> None:
        """F1: ``file:///etc/passwd`` is refused here exactly as on Chat."""
        adapter, harness = self._harness(image=True)
        body = _messages_body([
            {"type": "text", "text": "look"},
            {"type": "image", "source": {"type": "url", "url": "file:///etc/passwd"}},
        ])
        status, payload, _ = harness.request("POST", "/v1/messages", body=body)
        self._refusal(status, payload, adapter)

    def test_the_messages_surface_refuses_an_unvalidated_input_image_url(self) -> None:
        # Text-only alias: the OpenAI spelling is gated *and* validated, so
        # the scheme check cannot be dodged by skipping the image gate.
        adapter, harness = self._harness(image=False)
        body = _messages_body([
            {"type": "text", "text": "look"},
            {"type": "input_image", "image_url": "ftp://internal/secret.png"},
        ])
        status, payload, _ = harness.request("POST", "/v1/messages", body=body)
        self._refusal(status, payload, adapter)

    def test_the_messages_surface_refuses_an_unvalidated_data_url(self) -> None:
        adapter, harness = self._harness(image=True)
        body = _messages_body([
            {"type": "image", "source": {
                "type": "base64",
                "media_type": "text/html",
                "data": "not-valid-base64!!!",
            }},
        ])
        status, payload, _ = harness.request("POST", "/v1/messages", body=body)
        self._refusal(status, payload, adapter)
        serialized = json.dumps(payload)
        self.assertNotIn("text/html", serialized)
        self.assertNotIn("not-valid-base64!!!", serialized)

    def test_every_unreadable_image_shape_is_refused_over_http(self) -> None:
        """The refusal list the security evidence asked for, end to end."""
        cases = {
            "missing input_image url": {"type": "input_image", "image_url": None},
            "credentialed url": {"type": "image", "source": {
                "type": "url", "url": "https://user:pass@internal/secret.png"}},
            "control characters": {"type": "image", "source": {
                "type": "url", "url": "https://x/y.png\nX-Injected: 1"}},
            "non-http scheme": {"type": "image", "source": {
                "type": "url", "url": "ftp://internal/secret.png"}},
            "no source at all": {"type": "image"},
            "base64 without data": {"type": "image", "source": {
                "type": "base64", "media_type": "image/png"}},
        }
        for label, block in cases.items():
            with self.subTest(case=label):
                adapter, harness = self._harness(image=True)
                status, payload, _ = harness.request(
                    "POST", "/v1/messages", body=_messages_body([block]))
                self._refusal(status, payload, adapter)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
