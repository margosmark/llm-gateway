"""Image-input capability: sourced honestly, published, and enforced.

The rules under test:

* Image support is recorded per **exact upstream model ID**, from official
  model-provider documentation, with the source URL kept beside it. A model
  with no such evidence stays false - it is never inferred from a model name
  or a wire dialect. Sibling slugs can disagree, which is the point: the
  verdict is never copied across a family.
* A verdict that exists only in the source-backed catalogue is worth nothing
  until it reaches the stored row the router reads, so each boot reconciles
  the rows that evidence covers - promotion only, exact slug only, no
  provider call, no row added.
* An alias is advertised as image-capable when **any** route that could
  serve it accepts images, because the router enforces the capability per
  request: an image is routed to a capable route and a text-only route is
  excluded for that request with the reason recorded.
* A request carrying an image is routed only to an image-capable model; the
  route is excluded with a recorded reason, exactly like the tools check.
* Detection uses the same content parsers the bridge uses, so routing and
  rendering can never disagree about what counts as an image.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
import urllib.request
from pathlib import Path

from gateway.adapters.base import ProviderRequest, ProviderResponse, TestResult
from gateway.adapters.chatgpt_codex import ChatGPTCodexAdapter
from gateway.adapters.opencode_go import OpenCodeGoAdapter
from gateway.bridge.model import ConversionError
from gateway.bridge.requests import convert_request, from_responses, payload_has_image_input
from gateway.cli import build_application
from gateway.config import (
    Alias,
    ConfigStore,
    JevInference,
    ModelEntry,
    PhaseRouting,
    PhaseRoute,
    ProviderState,
    RouteRef,
)
from gateway.core.capabilities import (
    DIALECT_OPENAI_CHAT,
    DIALECT_OPENAI_RESPONSES,
    ImageEvidence,
    apply_image_evidence,
    image_evidence,
)
from gateway.core.routing import Router
from gateway.http.core import Server
from tests.test_end_to_end import FAKE_PROVIDER, GatewayHarness, free_port

_DATA = Path(__file__).resolve().parent.parent / "data" / "go_catalog.json"
_MINIMAX_SOURCE = "https://platform.minimax.io/docs/api-reference/text-openai-api"


class ImageDetectionTests(unittest.TestCase):
    """Routing and conversion must agree on which payloads carry an image."""

    def test_chat_image_url_part_is_detected(self) -> None:
        payload = {"messages": [{"role": "user", "content": [
            {"type": "text", "text": "look"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}},
        ]}]}
        self.assertTrue(payload_has_image_input(payload, DIALECT_OPENAI_CHAT))

    def test_responses_input_image_part_is_detected(self) -> None:
        payload = {"input": [{"role": "user", "content": [
            {"type": "input_text", "text": "look"},
            {"type": "input_image", "image_url": "data:image/png;base64,AA"},
        ]}]}
        self.assertTrue(payload_has_image_input(payload, DIALECT_OPENAI_RESPONSES))

    def test_text_only_payloads_are_not_images(self) -> None:
        for dialect, payload in (
            (DIALECT_OPENAI_CHAT, {"messages": [{"role": "user", "content": "hello"}]}),
            (DIALECT_OPENAI_RESPONSES, {"input": "hello"}),
            (DIALECT_OPENAI_RESPONSES, {"input": [{"role": "user", "content": "hello"}]}),
            (DIALECT_OPENAI_CHAT, {}),
        ):
            with self.subTest(dialect=dialect, payload=payload):
                self.assertFalse(payload_has_image_input(payload, dialect))

    def test_detection_survives_the_bridge_round_trip(self) -> None:
        """A payload detected as an image still carries one after conversion."""
        chat = {"model": "alias", "messages": [{"role": "user", "content": [
            {"type": "text", "text": "look"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}},
        ]}]}
        self.assertTrue(payload_has_image_input(chat, DIALECT_OPENAI_CHAT))
        converted = convert_request(chat, DIALECT_OPENAI_CHAT, DIALECT_OPENAI_RESPONSES)
        self.assertTrue(payload_has_image_input(converted, DIALECT_OPENAI_RESPONSES))
        rendered = converted["input"][0]["content"]
        self.assertIn({"type": "input_image", "image_url": "data:image/png;base64,AA"}, rendered)

    def test_responses_image_renders_back_to_the_chat_shape(self) -> None:
        """The other direction: Responses input_image -> Chat image_url.

        Both the bare-string and the ``{"url": ...}`` forms the Responses API
        accepts must land back on the Chat ``image_url`` part, so a model that
        speaks Responses can still be reached from a Chat client and a
        forwarded conversation keeps its image.
        """
        for form in ("data:image/png;base64,AA", {"url": "data:image/png;base64,AA"}):
            with self.subTest(form=type(form).__name__):
                responses = {"model": "alias", "input": [{"role": "user", "content": [
                    {"type": "input_text", "text": "look"},
                    {"type": "input_image", "image_url": form},
                ]}]}
                self.assertTrue(payload_has_image_input(responses, DIALECT_OPENAI_RESPONSES))
                converted = convert_request(
                    responses, DIALECT_OPENAI_RESPONSES, DIALECT_OPENAI_CHAT
                )
                self.assertTrue(payload_has_image_input(converted, DIALECT_OPENAI_CHAT))
                content = converted["messages"][0]["content"]
                images = [p for p in content if p.get("type") == "image_url"]
                self.assertEqual(len(images), 1, "exactly one image, never dropped")
                self.assertEqual(
                    images[0]["image_url"], {"url": "data:image/png;base64,AA"}
                )
                texts = [p for p in content if p.get("type") == "text"]
                self.assertEqual(len(texts), 1, "the text survives alongside the image")

    def test_an_image_survives_a_full_round_trip(self) -> None:
        """Chat -> Responses -> Chat keeps the image and the text."""
        original = {"model": "alias", "messages": [{"role": "user", "content": [
            {"type": "text", "text": "look"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}},
        ]}]}
        as_responses = convert_request(
            original, DIALECT_OPENAI_CHAT, DIALECT_OPENAI_RESPONSES
        )
        back = convert_request(
            as_responses, DIALECT_OPENAI_RESPONSES, DIALECT_OPENAI_CHAT
        )
        content = back["messages"][0]["content"]
        self.assertEqual(
            [p for p in content if p.get("type") == "image_url"][0]["image_url"],
            {"url": "data:image/png;base64,AA"},
            "the image URL is byte-identical after the round trip",
        )
        self.assertEqual(
            [p for p in content if p.get("type") == "text"][0]["text"], "look"
        )

    def test_a_top_level_responses_image_item_is_refused_not_served(self) -> None:
        """A root-level ``input_image`` item is malformed for every route.

        The Responses contract carries an image as an ``input_image``
        **content part inside a message**. A top-level item claiming to be
        an image used to bypass capability detection entirely - it has no
        ``content`` to inspect - and same-dialect pass-through forwarded it
        to a text-only adapter that silently ignored it. Every read of the
        root now refuses it instead.
        """
        finding = {"model": "m", "input": [
            {"role": "user", "content": [{"type": "input_text", "text": "look"}]},
            {"type": "input_image", "image_url": "data:image/png;base64,AA"},
        ]}
        with self.assertRaises(ConversionError) as ctx:
            payload_has_image_input(finding, DIALECT_OPENAI_RESPONSES)
        self.assertEqual(ctx.exception.capability, "image_input")
        for source, target in (
            (DIALECT_OPENAI_RESPONSES, DIALECT_OPENAI_RESPONSES),
            (DIALECT_OPENAI_RESPONSES, DIALECT_OPENAI_CHAT),
        ):
            with self.subTest(source=source, target=target), self.assertRaises(ConversionError):
                convert_request(finding, source, target)

    def test_every_malformed_top_level_image_shape_is_refused(self) -> None:
        for item in (
            {"type": "input_image"},  # no URL at all
            {"type": "input_image", "image_url": {"url": "data:image/png;base64,AA"}},
            {"type": "image_url", "image_url": "https://example.com/a.png"},
            {"image_url": "https://example.com/a.png"},  # no type at all
            {"type": "input_image", "image_url": "file:///etc/passwd"},
        ):
            payload = {"model": "m", "input": [item]}
            with self.subTest(item=item), self.assertRaises(ConversionError):
                payload_has_image_input(payload, DIALECT_OPENAI_RESPONSES)

    def test_a_non_object_responses_input_item_is_refused(self) -> None:
        for entry in (42, [], None, True):
            payload = {"model": "m", "input": [entry]}
            with self.subTest(entry=entry), self.assertRaises(ConversionError):
                payload_has_image_input(payload, DIALECT_OPENAI_RESPONSES)

    def test_detection_keeps_non_image_root_items_the_pass_through_owns(self) -> None:
        """A typed item that is not an image is not refused by detection.

        Same-dialect pass-through forwards it (the provider defines what it
        accepts); only a *cross-dialect* conversion refuses what it cannot
        express. Detection must not over-reject a text request that carries
        such history.
        """
        payload = {"model": "m", "input": [
            {"role": "user", "content": "hi"},
            {"type": "function_call", "call_id": "c1", "name": "f", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "c1", "output": "ok"},
            {"type": "reasoning", "summary": []},
        ]}
        self.assertFalse(payload_has_image_input(payload, DIALECT_OPENAI_RESPONSES))
        forwarded = convert_request(
            payload, DIALECT_OPENAI_RESPONSES, DIALECT_OPENAI_RESPONSES
        )
        self.assertEqual(len(forwarded["input"]), 4)
        # A cross-dialect conversion cannot express that item, so it refuses
        # rather than silently dropping it from the conversation.
        with self.assertRaises(ConversionError):
            from_responses(payload)


class ImageCapableAdapter:
    """A provider that answers any request; it records what it was sent."""

    def __init__(self, provider_id: str, dialect: str = DIALECT_OPENAI_CHAT) -> None:
        self.provider_id = provider_id
        self.kind = provider_id
        self.dialect = dialect
        self.calls: list[ProviderRequest] = []

    def list_models(self) -> list:
        return []

    def test_connection(self) -> TestResult:
        return TestResult(ok=True, state="connected", message="synthetic")

    def invoke(self, request: ProviderRequest) -> ProviderResponse:
        self.calls.append(request)
        body = json.dumps({
            "id": "resp_fake", "object": "chat.completion", "model": request.model,
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }).encode("utf-8")
        return ProviderResponse(200, {}, body, content_type="application/json", dialect=self.dialect)


def _alias_with_routes(primary: RouteRef, fallbacks: list[RouteRef], name: str = "coding") -> Alias:
    return Alias(name=name, enabled=True, primary=primary, fallbacks=fallbacks)


def _image_payload(model: str = "coding") -> dict:
    return {"model": model, "messages": [{"role": "user", "content": [
        {"type": "text", "text": "synthetic"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}},
    ]}]}


class RouterImageEnforcementTests(unittest.TestCase):
    """An image request is served only by a route that can see the image."""

    def _harness(self, *, primary_images: bool, fallback_images: bool) -> tuple[GatewayHarness, ImageCapableAdapter]:
        harness = GatewayHarness()
        primary = ImageCapableAdapter(FAKE_PROVIDER, DIALECT_OPENAI_RESPONSES)
        fallback = ImageCapableAdapter("opencode_go", DIALECT_OPENAI_CHAT)
        harness.registry.register(primary)
        harness.registry.register(fallback)
        with harness.config.mutate() as config:
            config.providers[FAKE_PROVIDER] = ProviderState(
                provider_id=FAKE_PROVIDER, kind=FAKE_PROVIDER, state="connected",
                models={"gpt-6-luna": ModelEntry(
                    model_id="gpt-6-luna", enabled=True, dialect=DIALECT_OPENAI_RESPONSES,
                    source="discovered", supports_image_input=primary_images)},
            )
            config.providers["opencode_go"] = ProviderState(
                provider_id="opencode_go", kind="opencode_go", state="connected",
                models={"mimo-v2.6-flash": ModelEntry(
                    model_id="mimo-v2.6-flash", enabled=True, dialect=DIALECT_OPENAI_CHAT,
                    source="discovered", supports_image_input=fallback_images)},
            )
            config.aliases["coding"] = _alias_with_routes(
                RouteRef(FAKE_PROVIDER, "gpt-6-luna"),
                [RouteRef("opencode_go", "mimo-v2.6-flash")],
            )
        return harness, primary

    def test_image_capable_primary_serves_the_image_request(self) -> None:
        harness, primary = self._harness(primary_images=True, fallback_images=False)
        self.addCleanup(harness.close)
        status, _, _ = harness.request("POST", "/v1/chat/completions", body=_image_payload())
        self.assertEqual(status, 200)
        self.assertEqual(len(primary.calls), 1)
        self.assertTrue(payload_has_image_input(primary.calls[0].payload, DIALECT_OPENAI_RESPONSES))

    def test_image_request_excludes_a_text_only_primary_and_uses_the_capable_fallback(self) -> None:
        harness, primary = self._harness(primary_images=False, fallback_images=True)
        self.addCleanup(harness.close)
        status, _, _ = harness.request("POST", "/v1/chat/completions", body=_image_payload())
        self.assertEqual(status, 200)
        # The text-only primary was excluded before any provider was called.
        self.assertEqual(primary.calls, [])
        event = harness.router._ledger.recent()[0]
        self.assertEqual(event["serving_provider"], "opencode_go")
        self.assertIn(
            {"provider": FAKE_PROVIDER, "model": "gpt-6-luna",
             "reason": "this model does not support image input"},
            event["skipped"],
        )

    def test_image_request_with_no_capable_route_fails_closed_at_the_gateway(self) -> None:
        harness, primary = self._harness(primary_images=False, fallback_images=False)
        self.addCleanup(harness.close)
        status, payload, headers = harness.request("POST", "/v1/chat/completions", body=_image_payload())
        self.assertEqual(status, 503)
        self.assertEqual(payload["error"]["type"], "no_eligible_route")
        # The refusal says *what* was refused, so a client can act on it:
        # the capability that no route of this alias meets, and a message
        # that names the image rather than hiding behind a generic failure.
        self.assertEqual(payload["error"]["capability"], "image_input")
        self.assertIn("image", payload["error"]["message"])
        # It must not invite a retry: no route will accept this attachment
        # later either.
        self.assertNotIn("retry_after_seconds", payload["error"])
        self.assertNotIn("Retry-After", dict(headers))
        self.assertEqual(primary.calls, [])
        # The public refusal is generic: no upstream provider or model
        # identifiers may leak into the response payload.
        serialized = json.dumps(payload)
        for identifier in (FAKE_PROVIDER, "gpt-6-luna", "opencode_go", "mimo-v2.6-flash"):
            self.assertNotIn(identifier, serialized)
        self.assertNotIn("detail", payload["error"])

    def test_circuit_expiry_is_returned_as_bounded_retry_after(self) -> None:
        # Both routes accept images, so the alias *does* publish an
        # image-capable route and this is a bench rather than a capability
        # gap - the client is told when to come back. An alias with no
        # image-capable route refuses instead and carries no retry hint,
        # because retrying the same attachment can never succeed; see
        # ``test_image_request_with_no_capable_route_fails_closed_at_the_gateway``.
        harness, primary = self._harness(primary_images=True, fallback_images=True)
        self.addCleanup(harness.close)
        harness.router.circuits.set(FAKE_PROVIDER, 900, model="gpt-6-luna")
        harness.router.circuits.set("opencode_go", 900, model="mimo-v2.6-flash")
        status, payload, headers = harness.request("POST", "/v1/chat/completions", body=_image_payload())
        self.assertEqual(status, 503)
        retry_after = int(dict(headers)["Retry-After"])
        self.assertGreaterEqual(retry_after, 895)
        self.assertLessEqual(retry_after, 900)
        self.assertEqual(payload["error"]["retry_after_seconds"], retry_after)
        # A bench is not a capability gap: the response does not claim the
        # alias lacks an image-capable route, because it has two.
        self.assertNotIn("capability", payload["error"])
        self.assertEqual(primary.calls, [])

    def test_a_text_only_request_is_unaffected_by_the_image_rule(self) -> None:
        harness, primary = self._harness(primary_images=False, fallback_images=False)
        self.addCleanup(harness.close)
        status, _, _ = harness.request(
            "POST", "/v1/chat/completions",
            body={"model": "coding", "messages": [{"role": "user", "content": "synthetic"}]},
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(primary.calls), 1)


class ResponsesImageAdapter(ImageCapableAdapter):
    """An image-capable Responses route answering with a Responses object."""

    def invoke(self, request: ProviderRequest) -> ProviderResponse:
        self.calls.append(request)
        body = json.dumps({
            "id": "resp_fake", "object": "response", "model": request.model,
            "status": "completed",
            "output": [{"type": "message", "role": "assistant",
                        "content": [{"type": "output_text", "text": "ok"}]}],
            "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        }).encode("utf-8")
        return ProviderResponse(200, {}, body, content_type="application/json", dialect=self.dialect)


class TopLevelResponsesImageItemTests(unittest.TestCase):
    """A top-level ``input_image`` item is refused at the API surface.

    Regression for the security finding on candidate ``0696c8c2``: a
    malformed top-level Responses ``{"type": "input_image", ...}`` item
    carried no ``content``, so capability detection missed it and
    same-dialect pass-through forwarded it to a text-only adapter, which
    silently ignored it (200, one adapter call, image lost). The refusal
    now happens before any route is chosen, with zero adapter calls.
    """

    def _harness(self, *, images: bool) -> tuple[GatewayHarness, ResponsesImageAdapter]:
        harness = GatewayHarness()
        primary = ResponsesImageAdapter(FAKE_PROVIDER, DIALECT_OPENAI_RESPONSES)
        harness.registry.register(primary)
        with harness.config.mutate() as config:
            config.providers[FAKE_PROVIDER] = ProviderState(
                provider_id=FAKE_PROVIDER, kind=FAKE_PROVIDER, state="connected",
                models={"gpt-6-luna": ModelEntry(
                    model_id="gpt-6-luna", enabled=True, dialect=DIALECT_OPENAI_RESPONSES,
                    source="discovered", supports_image_input=images)},
            )
            config.aliases["coding"] = _alias_with_routes(
                RouteRef(FAKE_PROVIDER, "gpt-6-luna"), [], name="coding")
        return harness, primary

    @staticmethod
    def _finding_payload() -> dict:
        """The exact probe the security gate reported: text message, then a
        top-level image-like item, against a text-only configured route."""
        return {"model": "coding", "input": [
            {"role": "user", "content": [{"type": "input_text", "text": "look"}]},
            {"type": "input_image", "image_url": "data:image/png;base64,AA"},
        ]}

    def _assert_refused(self, body: dict) -> None:
        self.assertEqual(body["error"]["type"], "conversion_refused")
        self.assertEqual(body["error"]["capability"], "image_input")
        # The refusal is the gateway's own: no upstream identifiers leak.
        serialized = json.dumps(body)
        for identifier in (FAKE_PROVIDER, "gpt-6-luna"):
            self.assertNotIn(identifier, serialized)

    def test_the_reported_bypass_is_refused_with_zero_adapter_calls(self) -> None:
        harness, primary = self._harness(images=False)
        self.addCleanup(harness.close)
        status, body, _ = harness.request(
            "POST", "/v1/responses", body=self._finding_payload())
        self.assertEqual(status, 422)
        self._assert_refused(body)
        self.assertEqual(primary.calls, [], "no adapter may see the malformed item")

    def test_an_image_capable_route_does_not_make_the_item_valid(self) -> None:
        # The item is malformed for every route: an image-capable model
        # would still be handed a root item its upstream does not accept.
        harness, primary = self._harness(images=True)
        self.addCleanup(harness.close)
        status, body, _ = harness.request(
            "POST", "/v1/responses", body=self._finding_payload())
        self.assertEqual(status, 422)
        self._assert_refused(body)
        self.assertEqual(primary.calls, [])

    def test_every_malformed_top_level_shape_is_refused_over_http(self) -> None:
        for item in (
            {"type": "input_image"},  # no URL at all
            {"type": "input_image", "image_url": {"url": "data:image/png;base64,AA"}},
            {"image_url": "https://example.com/a.png"},  # no type at all
            {"type": "input_image", "image_url": "file:///etc/passwd"},
        ):
            with self.subTest(item=item):
                harness, primary = self._harness(images=False)
                self.addCleanup(harness.close)
                status, body, _ = harness.request("POST", "/v1/responses", body={
                    "model": "coding", "input": [{"role": "user", "content": "hi"}, item]})
                self.assertEqual(status, 422)
                self._assert_refused(body)
                self.assertEqual(primary.calls, [])

    def test_a_valid_responses_image_still_reaches_an_image_capable_route(self) -> None:
        """No regression of the merged P0 on the Responses entry point."""
        harness, primary = self._harness(images=True)
        self.addCleanup(harness.close)
        status, _, _ = harness.request("POST", "/v1/responses", body={
            "model": "coding", "input": [{"role": "user", "content": [
                {"type": "input_text", "text": "look"},
                {"type": "input_image", "image_url": "data:image/png;base64,AA"},
            ]}]})
        self.assertEqual(status, 200)
        self.assertEqual(len(primary.calls), 1)
        self.assertTrue(
            payload_has_image_input(primary.calls[0].payload, DIALECT_OPENAI_RESPONSES))
        self.assertEqual(harness.router._ledger.recent()[0]["serving_provider"], FAKE_PROVIDER)

    def test_a_valid_responses_image_fails_closed_with_only_text_only_routes(self) -> None:
        harness, primary = self._harness(images=False)
        self.addCleanup(harness.close)
        status, body, _ = harness.request("POST", "/v1/responses", body={
            "model": "coding", "input": [{"role": "user", "content": [
                {"type": "input_text", "text": "look"},
                {"type": "input_image", "image_url": "data:image/png;base64,AA"},
            ]}]})
        self.assertEqual(status, 503)
        self.assertEqual(body["error"]["type"], "no_eligible_route")
        self.assertEqual(primary.calls, [])

    def test_a_plain_responses_text_request_is_unaffected(self) -> None:
        harness, primary = self._harness(images=False)
        self.addCleanup(harness.close)
        status, _, _ = harness.request(
            "POST", "/v1/responses", body={"model": "coding", "input": "hello"})
        self.assertEqual(status, 200)
        self.assertEqual(len(primary.calls), 1)


class AliasCatalogImagePublicationTests(unittest.TestCase):
    """An alias is advertised as image-capable when any route can accept one."""

    def test_an_alias_is_image_capable_when_any_route_is(self) -> None:
        harness = GatewayHarness()
        self.addCleanup(harness.close)
        with harness.config.mutate() as config:
            config.providers[FAKE_PROVIDER] = ProviderState(
                provider_id=FAKE_PROVIDER, kind=FAKE_PROVIDER, state="connected",
                models={"gpt-6-luna": ModelEntry(
                    model_id="gpt-6-luna", enabled=True, dialect=DIALECT_OPENAI_RESPONSES,
                    source="discovered", supports_image_input=True)},
            )
            config.providers["opencode_go"] = ProviderState(
                provider_id="opencode_go", kind="opencode_go", state="connected",
                models={
                    "vision": ModelEntry(model_id="vision", enabled=True, dialect=DIALECT_OPENAI_CHAT,
                                         source="discovered", supports_image_input=True),
                    "text": ModelEntry(model_id="text", enabled=True, dialect=DIALECT_OPENAI_CHAT,
                                       source="discovered", supports_image_input=False),
                    "text2": ModelEntry(model_id="text2", enabled=True, dialect=DIALECT_OPENAI_CHAT,
                                        source="discovered", supports_image_input=False),
                },
            )
            config.aliases["allvision"] = _alias_with_routes(
                RouteRef(FAKE_PROVIDER, "gpt-6-luna"), [RouteRef("opencode_go", "vision")],
                name="allvision")
            # A text-only fallback does not cost the alias its image input:
            # the router excludes that route from an image request instead.
            config.aliases["mixed"] = _alias_with_routes(
                RouteRef(FAKE_PROVIDER, "gpt-6-luna"), [RouteRef("opencode_go", "text")],
                name="mixed")
            config.aliases["single"] = _alias_with_routes(
                RouteRef("opencode_go", "vision"), [], name="single")
            # No route here accepts images, so the alias stays text-only.
            config.aliases["textonly"] = _alias_with_routes(
                RouteRef("opencode_go", "text"), [RouteRef("opencode_go", "text2")],
                name="textonly")
        _, body, _ = harness.request("GET", "/v1/alias-catalog")
        by_id = {item["id"]: item for item in body["data"]}
        self.assertTrue(by_id["allvision"]["supports_image_input"])
        self.assertTrue(by_id["mixed"]["supports_image_input"])
        self.assertTrue(by_id["single"]["supports_image_input"])
        self.assertFalse(by_id["textonly"]["supports_image_input"])

    def test_a_route_with_no_known_entry_is_not_image_capable(self) -> None:
        """Unknown is not capability. A missing model entry disables the claim."""
        harness = GatewayHarness()
        self.addCleanup(harness.close)
        with harness.config.mutate() as config:
            config.aliases["coding"] = _alias_with_routes(
                RouteRef(FAKE_PROVIDER, "gpt-6-luna"), [])
        _, body, _ = harness.request("GET", "/v1/alias-catalog")
        self.assertFalse(body["data"][0]["supports_image_input"])

    def test_a_codex_primary_with_transcribed_evidence_makes_the_alias_image_capable(self) -> None:
        """The real `pm` shape: a ChatGPT/Codex primary, a text-only fallback.

        ``gpt-5.6-terra`` carries transcribed image evidence, so the alias is
        image-capable even though its ``mimo-v2.6-flash`` fallback is not -
        the router excludes that fallback from an image request instead.
        """
        harness = GatewayHarness()
        self.addCleanup(harness.close)
        with harness.config.mutate() as config:
            config.providers[FAKE_PROVIDER] = ProviderState(
                provider_id=FAKE_PROVIDER, kind=FAKE_PROVIDER, state="connected",
                models={"gpt-5.6-terra": ModelEntry(
                    model_id="gpt-5.6-terra", enabled=True, dialect=DIALECT_OPENAI_RESPONSES,
                    source="discovered", supports_image_input=True)},
            )
            config.providers["opencode_go"] = ProviderState(
                provider_id="opencode_go", kind="opencode_go", state="connected",
                models={"mimo-v2.6-flash": ModelEntry(
                    model_id="mimo-v2.6-flash", enabled=True, dialect=DIALECT_OPENAI_CHAT,
                    source="discovered", supports_image_input=False)},
            )
            config.aliases["pm"] = _alias_with_routes(
                RouteRef(FAKE_PROVIDER, "gpt-5.6-terra"),
                [RouteRef("opencode_go", "mimo-v2.6-flash")], name="pm")
        _, body, _ = harness.request("GET", "/v1/alias-catalog")
        self.assertTrue(body["data"][0]["supports_image_input"])


class PhaseRouteImageEnforcementTests(unittest.TestCase):
    """A phase route is subject to the image rule like any other route.

    The phase policy is an owner-configured *route selection*, not a
    capability override. A phase route that cannot see an image must be
    excluded for an image request, with the reason recorded, rather than
    being served the image anyway.
    """

    def _harness(self, *, primary_images: bool, phase_images: bool):
        harness = GatewayHarness()
        primary = ImageCapableAdapter(FAKE_PROVIDER, DIALECT_OPENAI_RESPONSES)
        phase = ImageCapableAdapter("opencode_go", DIALECT_OPENAI_CHAT)
        harness.registry.register(primary)
        harness.registry.register(phase)
        with harness.config.mutate() as config:
            config.providers[FAKE_PROVIDER] = ProviderState(
                provider_id=FAKE_PROVIDER, kind=FAKE_PROVIDER, state="connected",
                models={"gpt-6-luna": ModelEntry(
                    model_id="gpt-6-luna", enabled=True, dialect=DIALECT_OPENAI_RESPONSES,
                    source="discovered", supports_image_input=primary_images)},
            )
            config.providers["opencode_go"] = ProviderState(
                provider_id="opencode_go", kind="opencode_go", state="connected",
                models={"mimo-v2.6-flash": ModelEntry(
                    model_id="mimo-v2.6-flash", enabled=True, dialect=DIALECT_OPENAI_CHAT,
                    source="discovered", supports_image_input=phase_images)},
            )
            config.aliases["coding"] = Alias(
                name="coding", enabled=True,
                primary=RouteRef(FAKE_PROVIDER, "gpt-6-luna"),
                phase_routing=PhaseRouting(
                    enabled=True,
                    routes={"plan": PhaseRoute(
                        phase="plan", route=RouteRef("opencode_go", "mimo-v2.6-flash"))},
                    inference=JevInference(),
                ),
            )
        return harness, primary, phase

    def _image_request(self, phase: str | None):
        body = _image_payload()
        if phase is not None:
            body["metadata"] = {"personal_gateway": {"phase": phase}}
        return body

    def test_a_text_only_phase_route_is_excluded_for_an_image_request(self) -> None:
        harness, primary, phase = self._harness(primary_images=True, phase_images=False)
        self.addCleanup(harness.close)
        status, _, _ = harness.request(
            "POST", "/v1/chat/completions", body=self._image_request("plan"))
        self.assertEqual(status, 200)
        # The phase route could not see the image, so it was not served one.
        self.assertEqual(phase.calls, [])
        self.assertEqual(len(primary.calls), 1)
        event = harness.router._ledger.recent()[0]
        self.assertIn(
            {"provider": "opencode_go", "model": "mimo-v2.6-flash",
             "reason": "this model does not support image input"},
            event["skipped"],
        )

    def test_an_image_capable_phase_route_serves_the_image_and_is_not_a_fallback(self) -> None:
        harness, primary, phase = self._harness(primary_images=False, phase_images=True)
        self.addCleanup(harness.close)
        status, _, _ = harness.request(
            "POST", "/v1/chat/completions", body=self._image_request("plan"))
        self.assertEqual(status, 200)
        self.assertEqual(primary.calls, [])
        self.assertEqual(len(phase.calls), 1)
        self.assertTrue(payload_has_image_input(phase.calls[0].payload, DIALECT_OPENAI_CHAT))
        event = harness.router._ledger.recent()[0]
        self.assertEqual(event["serving_provider"], "opencode_go")
        self.assertEqual(event["phase"], "plan")
        # The phase route is the primary for its phase, so it is not a fallback.
        self.assertFalse(event["fallback_used"])

    def test_an_image_request_with_no_capable_phase_route_fails_closed(self) -> None:
        harness, primary, phase = self._harness(primary_images=False, phase_images=False)
        self.addCleanup(harness.close)
        status, payload, _ = harness.request(
            "POST", "/v1/chat/completions", body=self._image_request("plan"))
        self.assertEqual(status, 503)
        self.assertEqual(payload["error"]["type"], "no_eligible_route")
        self.assertEqual(primary.calls, [])
        self.assertEqual(phase.calls, [])

    def test_a_text_only_request_still_uses_the_phase_route(self) -> None:
        # The image rule must not change text routing: the phase route is
        # still selected, and still not reported as a fallback.
        harness, primary, phase = self._harness(primary_images=False, phase_images=False)
        self.addCleanup(harness.close)
        status, _, _ = harness.request(
            "POST", "/v1/chat/completions",
            body={"model": "coding", "messages": [{"role": "user", "content": "synthetic"}],
                  "metadata": {"personal_gateway": {"phase": "plan"}}},
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(phase.calls), 1)
        self.assertEqual(primary.calls, [])
        event = harness.router._ledger.recent()[0]
        self.assertEqual(event["phase"], "plan")
        self.assertFalse(event["fallback_used"])


class SnapshotImageEvidenceTests(unittest.TestCase):
    """The dated snapshot carries image evidence only where it was sourced."""

    #: Every slug the snapshot transcribes an image verdict for, with the
    #: verdict and the page it came from. ``mimo-v2.5-pro`` is listed with
    #: ``False`` **and** a source: its page was read, and it says text only.
    EVIDENCED = {
        "minimax-m3.1-flash": (True, _MINIMAX_SOURCE),
        "mimo-v2.6-flash": (True, "https://opencode.ai/data/xiaomi/mimo-v2-6-flash"),
        "mimo-v2.6-pro": (True, "https://opencode.ai/data/xiaomi/mimo-v2-6-pro"),
        "mimo-v2.5": (True, "https://opencode.ai/data/xiaomi/mimo-v2-5"),
        "mimo-v2.5-pro": (False, "https://opencode.ai/data/xiaomi/mimo-v2-5-pro"),
    }

    def test_every_evidenced_slug_carries_its_own_verdict_and_source(self) -> None:
        models = json.loads(_DATA.read_text(encoding="utf-8"))["models"]
        for model_id, (verdict, source) in self.EVIDENCED.items():
            with self.subTest(model=model_id):
                self.assertEqual(models[model_id]["supports_image_input"], verdict)
                self.assertEqual(models[model_id]["image_input_source"], source)
                self.assertEqual(models[model_id]["image_input_observed_at"],
                                 "2026-10-23" if model_id == "minimax-m3.1-flash"
                                 else "2026-10-24")

    def test_models_without_evidence_stay_false_and_sourceless(self) -> None:
        """Unknown stays false, and an unsourced row is never a claim."""
        models = json.loads(_DATA.read_text(encoding="utf-8"))["models"]
        # The rows whose pages were never read: the one data page that
        # carries no model payload, and the three ids with no data page at
        # all. They are the only false-with-no-source rows in the snapshot.
        for model_id in ("gemini-4-argon-contributor", "omen-alpha",
                         "iceman-contributor-free", "longcat-2.5-preview-free"):
            with self.subTest(model=model_id):
                self.assertFalse(models[model_id]["supports_image_input"])
                self.assertIsNone(models[model_id].get("image_input_source"))
                self.assertIsNone(models[model_id].get("image_input_observed_at"))
        # A row whose page WAS read (2026-10-25, except V2.5 Pro which was
        # read on 10-24) and publishes no image input: false with a source,
        # which is a claim someone can go and check. These twelve are the
        # whole false-with-source set.
        for model_id in ("beam-free", "deepseek-v4-pro", "glm-5.1", "glm-5.2",
                         "glm-5.3", "hy3", "hy4-preview", "longcat-2.0",
                         "mimo-v2.5-pro", "minimax-m2.5", "minimax-m2.7",
                         "qwen3.7-max"):
            with self.subTest(model=model_id):
                self.assertFalse(models[model_id]["supports_image_input"])
                self.assertTrue(models[model_id].get("image_input_source"))
                self.assertEqual(
                    models[model_id].get("image_input_observed_at"),
                    "2026-10-24" if model_id == "mimo-v2.5-pro" else "2026-10-25")

    def test_sibling_mimo_slugs_disagree_and_the_verdicts_stay_apart(self) -> None:
        """The regression that family-level inference would have broken.

        ``mimo-v2.5`` publishes image input while its sibling
        ``mimo-v2.5-pro`` does not, so a single "MiMo accepts images" answer
        is wrong for one of them. The verdict lives per exact id and is
        never copied across the family.
        """
        models = json.loads(_DATA.read_text(encoding="utf-8"))["models"]
        self.assertTrue(models["mimo-v2.5"]["supports_image_input"])
        self.assertFalse(models["mimo-v2.5-pro"]["supports_image_input"])
        self.assertNotEqual(models["mimo-v2.5"]["image_input_source"],
                            models["mimo-v2.5-pro"]["image_input_source"])
        self.assertTrue(models["mimo-v2.6-flash"]["supports_image_input"])
        self.assertTrue(models["mimo-v2.6-pro"]["supports_image_input"])

    def test_the_builder_writes_a_source_and_a_read_date_behind_every_verdict(self) -> None:
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "build_go_catalog_snapshot",
            Path(__file__).resolve().parent.parent / "scripts" / "build_go_catalog_snapshot.py",
        )
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        models = module.build()["models"]
        for model_id, entry in models.items():
            with self.subTest(model=model_id):
                source = entry["image_input_source"]
                observed = entry["image_input_observed_at"]
                if entry["supports_image_input"]:
                    # A claim is only ever made from a page that was read.
                    self.assertTrue(source, "an image claim without a source")
                    self.assertTrue(observed, "an image claim without a read date")
                if source:
                    self.assertTrue(observed, "a source without a read date")
                else:
                    self.assertIsNone(observed)
        # The builder's own table and the JSON it wrote agree slug for slug:
        # nothing about image input was hand-edited into the snapshot.
        self.assertEqual(
            {model_id for model_id, entry in models.items() if entry["image_input_source"]},
            set(module.IMAGE_INPUT_EVIDENCE),
        )
        for model_id, evidence in module.IMAGE_INPUT_EVIDENCE.items():
            with self.subTest(model=model_id):
                self.assertEqual(models[model_id]["supports_image_input"],
                                 bool(evidence["supports_image_input"]))
                self.assertEqual(models[model_id]["image_input_source"], evidence["source"])
                self.assertEqual(models[model_id]["image_input_observed_at"],
                                 evidence["observed_at"])

    def test_audio_and_video_inputs_are_recorded_nowhere(self) -> None:
        """Known limitation, asserted so nobody mistakes it for a claim.

        The four OpenCode data pages list Audio and Video inputs alongside
        Image. The gateway capability model has a single boolean,
        ``supports_image_input``: only the image modality is transcribed, and
        no field here may be read as an audio or video guarantee.
        """
        models = json.loads(_DATA.read_text(encoding="utf-8"))["models"]
        for model_id, entry in models.items():
            with self.subTest(model=model_id):
                self.assertNotIn("supports_audio_input", entry)
                self.assertNotIn("supports_video_input", entry)
                self.assertNotIn("modalities", entry)


def _seed_production_shaped_config(path: Path, providers: dict) -> None:
    """Write a config.json shaped like the deployed one.

    The production rows were written before ``supports_image_input`` was
    stored, so ``from_dict`` fills the declared default and the file itself
    says nothing about images - which is the exact state this card starts
    from, and why a verdict that never reaches a row reads as false.
    """
    for provider in providers.values():
        for row in provider["models"].values():
            assert "supports_image_input" not in row, "seed must carry no image key"
    path.write_text(json.dumps({
        "revision": 7,
        "initialized": True,
        "providers": providers,
        "aliases": {},
        "profiles": {},
    }, indent=2) + "\n", encoding="utf-8")


def _seeded_providers() -> dict:
    return {
        "opencode_go": {
            "provider_id": "opencode_go", "kind": "opencode_go", "state": "connected",
            "models": {
                model_id: {"model_id": model_id, "enabled": True, "source": "discovered",
                           "dialect": "openai_chat"}
                for model_id in ("mimo-v2.6-flash", "mimo-v2.6-pro", "mimo-v2.5",
                                 "mimo-v2.5-pro", "glm-5.3-flash")
            },
        },
        "chatgpt_codex": {
            "provider_id": "chatgpt_codex", "kind": "chatgpt_codex", "state": "connected",
            "models": {
                model_id: {"model_id": model_id, "enabled": True, "source": "discovered",
                           "dialect": "openai_responses"}
                for model_id in ("gpt-5.6-terra", "gpt-6-luna")
            },
        },
    }


class ImageEvidenceReconciliationTests(unittest.TestCase):
    """Evidence that never reaches the stored row is indistinguishable from false."""

    def _stored_config(self) -> ConfigStore:
        tmp = tempfile.mkdtemp(prefix="image-evidence-")
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        path = Path(tmp) / "config.json"
        _seed_production_shaped_config(path, _seeded_providers())
        store = ConfigStore(path)
        for provider_id, provider in store.snapshot().providers.items():
            for model_id, entry in provider.models.items():
                with self.subTest(model=f"{provider_id}/{model_id}"):
                    self.assertFalse(entry.supports_image_input)
        return store

    def test_the_go_hook_reports_the_catalogue_verdicts_per_exact_slug(self) -> None:
        evidence = image_evidence(OpenCodeGoAdapter(None))
        self.assertTrue(evidence["mimo-v2.6-flash"].supports_image_input)
        self.assertEqual(evidence["mimo-v2.6-flash"].source,
                         "https://opencode.ai/data/xiaomi/mimo-v2-6-flash")
        # A sourced *absence* is evidence too, and it says false.
        self.assertFalse(evidence["mimo-v2.5-pro"].supports_image_input)
        self.assertEqual(evidence["mimo-v2.5-pro"].source,
                         "https://opencode.ai/data/xiaomi/mimo-v2-5-pro")
        # The 2026-10-25 transcription: verdicts that the data pages carry
        # now appear, each with its own slug's URL.
        self.assertTrue(evidence["glm-5.3-flash"].supports_image_input)
        self.assertEqual(evidence["glm-5.3-flash"].source,
                         "https://opencode.ai/data/zhipuai/glm-5-3-flash")
        self.assertTrue(evidence["gpt-6-luna"].supports_image_input)
        self.assertEqual(evidence["gpt-6-luna"].source,
                         "https://opencode.ai/data/openai/gpt-6-luna")
        # A row with no page read for it is not evidence at all, so it never
        # appears - never an invented URL.
        self.assertNotIn("omen-alpha", evidence)
        self.assertNotIn("iceman-contributor-free", evidence)

    def test_the_codex_hook_reports_gpt_5_6_terra_with_its_read_date(self) -> None:
        evidence = image_evidence(ChatGPTCodexAdapter(None))
        terra = evidence["gpt-5.6-terra"]
        self.assertTrue(terra.supports_image_input)
        self.assertEqual(terra.source,
                         "https://developers.openai.com/api/docs/models/gpt-5.6-terra")
        self.assertEqual(terra.observed_at, "2026-10-23")
        self.assertEqual(set(evidence), {"gpt-5.6-terra"})

    def test_reconciliation_promotes_exactly_the_evidenced_true_rows(self) -> None:
        store = self._stored_config()
        with store.mutate() as config:
            changed = apply_image_evidence(config, {
                "opencode_go": image_evidence(OpenCodeGoAdapter(None)),
                "chatgpt_codex": image_evidence(ChatGPTCodexAdapter(None)),
            })
        self.assertEqual(sorted(changed), [
            ("chatgpt_codex", "gpt-5.6-terra"),
            ("opencode_go", "glm-5.3-flash"),
            ("opencode_go", "mimo-v2.5"),
            ("opencode_go", "mimo-v2.6-flash"),
            ("opencode_go", "mimo-v2.6-pro"),
        ])
        config = store.snapshot()
        go = config.providers["opencode_go"].models
        codex = config.providers["chatgpt_codex"].models
        self.assertTrue(go["mimo-v2.6-flash"].supports_image_input)
        self.assertTrue(go["mimo-v2.6-pro"].supports_image_input)
        self.assertTrue(go["mimo-v2.5"].supports_image_input)
        self.assertTrue(go["glm-5.3-flash"].supports_image_input)
        self.assertTrue(codex["gpt-5.6-terra"].supports_image_input)
        # The sourced absence, the never-transcribed model and the model the
        # provider knows nothing about all stay false.
        self.assertFalse(go["mimo-v2.5-pro"].supports_image_input)
        self.assertFalse(codex["gpt-6-luna"].supports_image_input)

    def test_a_second_reconciliation_changes_nothing(self) -> None:
        store = self._stored_config()
        evidence = {
            "opencode_go": image_evidence(OpenCodeGoAdapter(None)),
            "chatgpt_codex": image_evidence(ChatGPTCodexAdapter(None)),
        }
        with store.mutate() as config:
            self.assertTrue(apply_image_evidence(config, evidence))
        with store.mutate() as config:
            self.assertEqual(apply_image_evidence(config, evidence), [])

    def test_a_restart_never_demotes_a_capability(self) -> None:
        """Promotion only: boot is not where a capability is taken away."""
        store = self._stored_config()
        with store.mutate() as config:
            # A row set true earlier - by an owner, or by a wider
            # transcription - for a slug whose current source says text-only.
            config.providers["opencode_go"].models["mimo-v2.5-pro"].supports_image_input = True
        with store.mutate() as config:
            apply_image_evidence(config, {"opencode_go": image_evidence(OpenCodeGoAdapter(None))})
        self.assertTrue(
            store.snapshot().providers["opencode_go"].models["mimo-v2.5-pro"].supports_image_input)

    def test_reconciliation_never_adds_enables_or_disables_a_row(self) -> None:
        store = self._stored_config()
        before = {provider_id: sorted(provider.models)
                  for provider_id, provider in store.snapshot().providers.items()}
        with store.mutate() as config:
            apply_image_evidence(config, {
                "opencode_go": image_evidence(OpenCodeGoAdapter(None)),
                "chatgpt_codex": image_evidence(ChatGPTCodexAdapter(None)),
            })
            for provider in config.providers.values():
                for entry in provider.models.values():
                    self.assertTrue(entry.enabled, "reconciliation changed exposure")
        after = {provider_id: sorted(provider.models)
                 for provider_id, provider in store.snapshot().providers.items()}
        self.assertEqual(before, after)

    def test_an_adapter_without_the_hook_contributes_nothing(self) -> None:
        class Bare:
            provider_id = "bare"

        self.assertEqual(image_evidence(Bare()), {})
        self.assertEqual(image_evidence(None), {})

    def test_a_broken_hook_fails_closed_instead_of_stopping_boot(self) -> None:
        class Broken:
            def image_input_evidence(self):
                raise RuntimeError("catalogue is unreadable")

        self.assertEqual(image_evidence(Broken()), {})

    def test_evidence_without_a_source_is_not_evidence(self) -> None:
        class Unsourced:
            def image_input_evidence(self):
                return {
                    "model-a": ImageEvidence(True, "", "2026-10-24"),
                    "model-b": ImageEvidence(True, "https://example.test/b", "2026-10-24"),
                    "model-c": "not-evidence",
                }

        self.assertEqual(set(image_evidence(Unsourced())), {"model-b"})


class BootReconciliationTests(unittest.TestCase):
    """A boot brings the stored rows, config.json and the alias catalogue together.

    This is the acceptance path for the sync fix: the production-shaped file
    starts with no image key anywhere, a real ``build_application`` boot runs,
    and the answer is read back from the *endpoints*, not from the file the
    evidence was written into.
    """

    #: An alias whose routes are all evidenced image-capable, and one whose
    #: only route has never had a source read for it.
    TEXT_ONLY = "textonly"

    def setUp(self) -> None:
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="boot-image-")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.data_dir = Path(self.tmp)
        providers = _seeded_providers()
        providers["opencode_go"]["models"]["glm-5.3-flash"] = {
            "model_id": "glm-5.3-flash", "enabled": True, "source": "discovered",
            "dialect": "openai_chat",
        }
        # A discovered row whose page carries no model payload: it has a
        # catalogue row but no image evidence, so boot must not invent one.
        providers["opencode_go"]["models"]["omen-alpha"] = {
            "model_id": "omen-alpha", "enabled": True, "source": "discovered",
            "dialect": None,
        }
        # A documented dialect with no image evidence: a route that exists
        # and still refuses an image, which is what TEXT_ONLY is for.
        providers["opencode_go"]["models"]["longcat-2.5-preview-free"] = {
            "model_id": "longcat-2.5-preview-free", "enabled": True,
            "source": "discovered", "dialect": "openai_chat",
        }
        _seed_production_shaped_config(self.data_dir / "config.json", providers)
        with ConfigStore(self.data_dir / "config.json").mutate() as config:
            config.aliases = {
                "pm": Alias(name="pm", enabled=True, primary=RouteRef(
                    "chatgpt_codex", "gpt-5.6-terra"), fallbacks=[
                        RouteRef("opencode_go", "mimo-v2.6-flash")]),
                self.TEXT_ONLY: Alias(name=self.TEXT_ONLY, enabled=True, primary=RouteRef(
                    "opencode_go", "longcat-2.5-preview-free"), fallbacks=[]),
                # A discovered row the 2026-10-25 evidence promotes at boot:
                # the alias flips with it, at the endpoint.
                "flipped": Alias(name="flipped", enabled=True, primary=RouteRef(
                    "opencode_go", "glm-5.3-flash"), fallbacks=[]),
            }

    def _boot(self) -> tuple[ConfigStore, Router, int]:
        port = free_port("127.0.0.1")
        app, config, _, router = build_application(self.data_dir, "127.0.0.1", port)
        server = Server("127.0.0.1", port, app.handle)
        server.start_background()
        self.addCleanup(server.stop)
        return config, router, port

    def test_a_boot_writes_the_evidence_into_the_stored_rows_and_the_file(self) -> None:
        config, _, _ = self._boot()
        go = config.snapshot().providers["opencode_go"].models
        codex = config.snapshot().providers["chatgpt_codex"].models
        self.assertTrue(go["mimo-v2.6-flash"].supports_image_input)
        self.assertTrue(go["mimo-v2.6-pro"].supports_image_input)
        self.assertTrue(go["mimo-v2.5"].supports_image_input)
        self.assertTrue(codex["gpt-5.6-terra"].supports_image_input)
        self.assertFalse(go["mimo-v2.5-pro"].supports_image_input)
        # The discovered row the evidence covers is promoted at boot, and
        # the row whose page carries no payload stays false.
        self.assertTrue(go["glm-5.3-flash"].supports_image_input)
        self.assertFalse(go["omen-alpha"].supports_image_input)
        # The row in the file carries the key too, so a restart does not
        # start over from an unreadable state.
        on_disk = json.loads((self.data_dir / "config.json").read_text(encoding="utf-8"))
        self.assertTrue(
            on_disk["providers"]["opencode_go"]["models"]["mimo-v2.6-flash"]
            ["supports_image_input"])
        self.assertIs(
            on_disk["providers"]["chatgpt_codex"]["models"]["gpt-5.6-terra"]
            ["supports_image_input"], True)
        self.assertIs(
            on_disk["providers"]["opencode_go"]["models"]["glm-5.3-flash"]
            ["supports_image_input"], True)
        self.assertIs(
            on_disk["providers"]["opencode_go"]["models"]["omen-alpha"]
            ["supports_image_input"], False)

    def test_the_alias_catalogue_reports_what_the_rows_say(self) -> None:
        _, _, port = self._boot()
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/v1/alias-catalog", timeout=10) as response:
            catalog = json.load(response)
        self.assertEqual(catalog["object"], "list")
        by_id = {entry["id"]: entry for entry in catalog["data"]}
        self.assertEqual(sorted(by_id), ["flipped", "pm", self.TEXT_ONLY])
        # The alias that owns an evidenced image-capable route says so.
        self.assertTrue(by_id["pm"]["supports_image_input"])
        # An alias whose route was promoted by the boot's evidence says so.
        self.assertTrue(by_id["flipped"]["supports_image_input"])
        # The alias that owns none says false - the truthful refusal at
        # acceptance, visible before any request is made.
        self.assertFalse(by_id[self.TEXT_ONLY]["supports_image_input"])

    def test_a_second_boot_leaves_the_verdicts_alone(self) -> None:
        self._boot()
        config, _, _ = self._boot()
        rows = config.snapshot().providers["opencode_go"].models
        self.assertTrue(rows["mimo-v2.6-flash"].supports_image_input)
        self.assertTrue(rows["glm-5.3-flash"].supports_image_input)
        self.assertFalse(rows["omen-alpha"].supports_image_input)

    def test_an_image_for_a_capable_alias_reaches_only_capable_routes(self) -> None:
        """The real catalogue's verdict selects the candidates, not a guess.

        This is the live-data counterpart of the synthetic exclusion tests:
        the evidence that came off the catalogue at boot is what the router
        filters on when a message carries an image.
        """
        config, router, _ = self._boot()
        snapshot = config.snapshot()
        routes = router.candidates(snapshot.aliases["pm"], needs_image_input=True)
        self.assertEqual([candidate.route.model for candidate in routes],
                         ["gpt-5.6-terra", "mimo-v2.6-flash"])
        self.assertTrue(all(candidate.eligible for candidate in routes),
                        {candidate.excluded_reason for candidate in routes})
        # The alias the boot promoted now routes its image to that model.
        promoted = router.candidates(snapshot.aliases["flipped"],
                                     needs_image_input=True)
        self.assertEqual([candidate.route.model for candidate in promoted],
                         ["glm-5.3-flash"])
        self.assertTrue(all(candidate.eligible for candidate in promoted),
                        {candidate.excluded_reason for candidate in promoted})
        # The alias whose only route has no evidenced image support keeps no
        # candidate at all, and every one of them says why in the same words
        # the refusal is built from.
        refused = router.candidates(snapshot.aliases[self.TEXT_ONLY],
                                    needs_image_input=True)
        self.assertFalse(any(candidate.eligible for candidate in refused))
        self.assertEqual(
            {candidate.excluded_reason for candidate in refused},
            {"this model does not support image input"})


if __name__ == "__main__":
    unittest.main()
