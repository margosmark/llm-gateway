"""Image-input capability: sourced honestly, published, and enforced.

The rules under test:

* Image support is recorded per **exact upstream model ID**, from official
  model-provider documentation, with the source URL kept beside it. A model
  with no such evidence stays false - it is never inferred from a model name
  or a wire dialect.
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
import unittest
from pathlib import Path

from gateway.adapters.base import ProviderRequest, ProviderResponse, TestResult
from gateway.bridge.requests import convert_request, payload_has_image_input
from gateway.config import (
    Alias,
    JevInference,
    ModelEntry,
    PhaseRouting,
    PhaseRoute,
    ProviderState,
    RouteRef,
)
from gateway.core.capabilities import DIALECT_OPENAI_CHAT, DIALECT_OPENAI_RESPONSES
from tests.test_end_to_end import FAKE_PROVIDER, GatewayHarness

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
        status, payload, _ = harness.request("POST", "/v1/chat/completions", body=_image_payload())
        self.assertEqual(status, 503)
        self.assertEqual(payload["error"]["type"], "no_eligible_route")
        self.assertEqual(primary.calls, [])
        # The public refusal is generic: no upstream provider or model
        # identifiers may leak into the response payload.
        serialized = json.dumps(payload)
        for identifier in (FAKE_PROVIDER, "gpt-6-luna", "opencode_go", "mimo-v2.6-flash"):
            self.assertNotIn(identifier, serialized)
        self.assertNotIn("detail", payload["error"])

    def test_a_text_only_request_is_unaffected_by_the_image_rule(self) -> None:
        harness, primary = self._harness(primary_images=False, fallback_images=False)
        self.addCleanup(harness.close)
        status, _, _ = harness.request(
            "POST", "/v1/chat/completions",
            body={"model": "coding", "messages": [{"role": "user", "content": "synthetic"}]},
        )
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

    def test_only_minimax_m3_1_flash_has_official_image_evidence(self) -> None:
        models = json.loads(_DATA.read_text(encoding="utf-8"))["models"]
        self.assertTrue(models["minimax-m3.1-flash"]["supports_image_input"])
        self.assertEqual(models["minimax-m3.1-flash"]["image_input_source"], _MINIMAX_SOURCE)

    def test_models_without_official_evidence_stay_false_and_sourceless(self) -> None:
        models = json.loads(_DATA.read_text(encoding="utf-8"))["models"]
        for model_id in ("mimo-v2.6-flash", "gpt-6-luna", "claude-haiku-5-5",
                         "gemini-4-argon-contributor", "omen-alpha"):
            with self.subTest(model=model_id):
                self.assertFalse(models[model_id]["supports_image_input"])
                self.assertIsNone(models[model_id].get("image_input_source"))

    def test_the_builder_writes_the_evidence_next_to_the_flag(self) -> None:
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "build_go_catalog_snapshot",
            Path(__file__).resolve().parent.parent / "scripts" / "build_go_catalog_snapshot.py",
        )
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        models = module.build()["models"]
        self.assertTrue(models["minimax-m3.1-flash"]["supports_image_input"])
        self.assertEqual(models["minimax-m3.1-flash"]["image_input_source"], _MINIMAX_SOURCE)
        # Every claimed capability has a source; every unclaimed one has none.
        for model_id, entry in models.items():
            with self.subTest(model=model_id):
                self.assertEqual(bool(entry["supports_image_input"]),
                                 entry["image_input_source"] is not None)


if __name__ == "__main__":
    unittest.main()
