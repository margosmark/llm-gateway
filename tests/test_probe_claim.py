"""The half-open probe is claimed at invocation, and only by its owner.

The incident these tests pin: the probe was claimed while the candidate
list was being assembled, so a route that was never invoked - a primary
that could not see the caller's image, a primary that phase routing did
not choose, a candidate whose conversion was refused - held the recovery
probe for ``PROBE_TIMEOUT_SECONDS``. One image request pinned the primary
to its fallback for five minutes after the provider had recovered, while
every text request in that window was a fallback.

The properties under test:

* eligibility checks and candidate preview **never** claim a probe;
* the claim happens **at the invocation boundary**, atomically, and only
  the claim's owner may release it;
* a candidate that fails before invocation (conversion refused) claims
  nothing;
* while one request holds a live probe, a second concurrent request never
  reaches that route, and the ledger says so;
* a failed probe releases the claim and reopens the circuit, and an
  unexpected failure releases the claim instead of wedging the route;
* the release belongs to the claim, not to whichever handler was
  running: an exception raised **while** the AdapterError handler is
  opening the circuit, or in the recording that follows a success,
  releases the claim exactly the same way.
"""
from __future__ import annotations

import threading
import unittest
from typing import Any
from unittest import mock

from gateway.adapters.base import AdapterError, ProviderResponse
from gateway.bridge.model import ConversionError
from gateway.circuits import CircuitStore
from gateway.config import (
    Alias,
    JevInference,
    ModelEntry,
    PhaseRoute,
    PhaseRouting,
    ProviderState,
    RouteRef,
)
from gateway.core import routing as routing_module
from tests.test_end_to_end import FAKE_PROVIDER, FakeAdapter, GatewayHarness

FALLBACK_PROVIDER = "opencode_go"
FALLBACK_MODEL = "mimo-v2.6-flash"


class _Clock:
    """A clock a test can move."""

    def __init__(self, now: float = 1_700_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class BlockingFakeAdapter(FakeAdapter):
    """A provider that reports when it is inside ``invoke`` and waits.

    It gives a test a deterministic window in which a recovery probe is
    genuinely running, without sleeping.
    """

    def __init__(self, *args: Any, entered: threading.Event, release: threading.Event, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._entered = entered
        self._release = release

    def invoke(self, request: Any) -> ProviderResponse:
        self._entered.set()
        if not self._release.wait(timeout=10):
            raise RuntimeError("the blocking provider was never released")
        return super().invoke(request)


def _chat(**extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": "coding",
        "messages": [{"role": "user", "content": "synthetic request content"}],
    }
    body.update(extra)
    return body


def _image_chat() -> dict[str, Any]:
    return {
        "model": "coding",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "describe this"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}},
                ],
            }
        ],
    }


def _harness(
    *,
    phase_routes: dict[str, RouteRef] | None = None,
    fallback_error_classes: list[str] | None = None,
) -> tuple[GatewayHarness, _Clock]:
    """A harness whose ``coding`` alias has a primary and one fallback.

    The circuit store runs on a clock the test controls, so a circuit can
    be opened and its window allowed to expire without waiting.
    """
    harness = GatewayHarness()
    clock = _Clock()
    harness.router._circuits = CircuitStore(clock=clock)
    with harness.config.mutate() as config:
        config.providers[FALLBACK_PROVIDER] = ProviderState(
            provider_id=FALLBACK_PROVIDER,
            kind=FALLBACK_PROVIDER,
            state="connected",
            models={
                FALLBACK_MODEL: ModelEntry(
                    model_id=FALLBACK_MODEL,
                    enabled=True,
                    dialect="openai_chat",
                    source="discovered",
                    supports_image_input=True,
                )
            },
        )
        alias = Alias(
            name="coding",
            enabled=True,
            primary=RouteRef(FAKE_PROVIDER, "gpt-6-luna"),
            fallbacks=[RouteRef(FALLBACK_PROVIDER, FALLBACK_MODEL)],
            fallback_triggers=["exhausted", "rate_limited", "upstream_unavailable"],
            fallback_authorized=True,
            fallback_confirmed_providers=[FALLBACK_PROVIDER],
        )
        if fallback_error_classes is not None:
            alias.fallback_error_classes = list(fallback_error_classes)
        if phase_routes is not None:
            alias.phase_routing = PhaseRouting(
                enabled=True,
                routes={
                    phase: PhaseRoute(phase=phase, route=ref)
                    for phase, ref in phase_routes.items()
                },
                inference=JevInference(enabled=False),
            )
        config.aliases["coding"] = alias
    harness.router.resolve_alias("coding")
    return harness, clock


def _open_and_expire(harness: GatewayHarness, clock: _Clock) -> None:
    """Put the primary's circuit into the half-open state."""
    harness.router.circuits.record_failure(
        FAKE_PROVIDER, "gpt-6-luna", "rate_limited", base_seconds=300
    )
    clock.advance(400)


def _probe_held(harness: GatewayHarness) -> bool:
    """Is any circuit currently holding a recovery probe?"""
    entries = harness.router.circuits._entries  # type: ignore[attr-defined]
    return any(entry.probe_in_flight for entry in entries.values())


def _latest(harness: GatewayHarness) -> dict[str, Any]:
    ledger = harness.router._ledger
    assert ledger is not None
    return ledger.recent()[0]


class NoClaimBeforeInvocationTests(unittest.TestCase):
    """Enumerating a candidate must never spend the recovery probe."""

    def test_a_capability_ineligible_image_request_leaves_the_probe_claimable(self) -> None:
        # gpt-6-luna does not publish image input, so an image request
        # cannot be served by the primary at all. It must not claim the
        # primary's probe on the way past.
        harness, clock = _harness()
        self.addCleanup(harness.close)
        _open_and_expire(harness, clock)

        status, _, _ = harness.request("POST", "/v1/chat/completions", body=_image_chat())
        self.assertEqual(status, 200)
        event = _latest(harness)
        self.assertEqual(event["serving_provider"], FALLBACK_PROVIDER)
        self.assertIn("image", event["skipped"][0]["reason"])
        self.assertFalse(_probe_held(harness), "a candidate that was never invoked held the probe")

        # The very next compatible text request probes the primary and
        # closes its circuit: nothing waited out the five-minute window.
        harness.registry.register(FakeAdapter())
        status, _, _ = harness.request("POST", "/v1/chat/completions", body=_chat())
        self.assertEqual(status, 200)
        event = _latest(harness)
        self.assertEqual(event["serving_provider"], FAKE_PROVIDER)
        self.assertFalse(event["fallback_used"])
        self.assertEqual(harness.router.circuits.remaining(FAKE_PROVIDER, "gpt-6-luna"), 0)

    def test_a_phase_selected_route_leaves_the_unselected_primaries_probe_claimable(self) -> None:
        harness, clock = _harness(
            phase_routes={"plan": RouteRef(FALLBACK_PROVIDER, FALLBACK_MODEL)}
        )
        self.addCleanup(harness.close)
        _open_and_expire(harness, clock)

        status, _, _ = harness.request(
            "POST",
            "/v1/chat/completions",
            body=_chat(metadata={"personal_gateway": {"phase": "plan"}}),
        )
        self.assertEqual(status, 200)
        event = _latest(harness)
        self.assertEqual(event["serving_provider"], FALLBACK_PROVIDER)
        self.assertEqual(event["phase"], "plan")
        # A phase route is the primary for that phase, not a fallback.
        self.assertFalse(event["fallback_used"])
        self.assertFalse(_probe_held(harness), "the unselected primary held the probe")

        # The primary is still probeable when an ordinary request needs it.
        harness.registry.register(FakeAdapter())
        status, _, _ = harness.request("POST", "/v1/chat/completions", body=_chat())
        self.assertEqual(status, 200)
        event = _latest(harness)
        self.assertEqual(event["serving_provider"], FAKE_PROVIDER)
        self.assertEqual(harness.router.circuits.remaining(FAKE_PROVIDER, "gpt-6-luna"), 0)

    def test_a_refused_conversion_never_claims_the_probe(self) -> None:
        harness, clock = _harness(fallback_error_classes=["policy"])
        self.addCleanup(harness.close)
        _open_and_expire(harness, clock)

        real_convert = routing_module.convert_request

        def refusing_convert(
            payload: dict[str, Any], source: str, target: str
        ) -> dict[str, Any]:
            if target == "openai_responses":
                raise ConversionError("synthetic conversion refusal under test")
            return real_convert(payload, source, target)

        with mock.patch.object(
            routing_module, "convert_request", side_effect=refusing_convert
        ):
            status, _, _ = harness.request("POST", "/v1/chat/completions", body=_chat())
        self.assertEqual(status, 200)
        event = _latest(harness)
        self.assertEqual(event["serving_provider"], FALLBACK_PROVIDER)
        self.assertTrue(event["fallback_used"])
        self.assertEqual(event["attempts"][0]["outcome"], "conversion_refused")
        self.assertFalse(_probe_held(harness), "a candidate refused before invoke held the probe")

        # The probe survived the refusal: a healthy primary is used at once.
        harness.registry.register(FakeAdapter())
        status, _, _ = harness.request("POST", "/v1/chat/completions", body=_chat())
        self.assertEqual(status, 200)
        self.assertEqual(_latest(harness)["serving_provider"], FAKE_PROVIDER)


class ProbeOwnershipTests(unittest.TestCase):
    """Only the request that claimed the probe may release it."""

    def setUp(self) -> None:
        self.clock = _Clock()
        self.store = CircuitStore(clock=self.clock)
        self.store.record_failure(
            FAKE_PROVIDER, "gpt-6-luna", "rate_limited", base_seconds=300
        )
        self.clock.advance(400)

    def test_a_second_claim_is_refused_while_the_first_is_live(self) -> None:
        allowed, _reason, owner = self.store.claim(FAKE_PROVIDER, "gpt-6-luna")
        self.assertTrue(allowed)
        self.assertTrue(owner)
        allowed, reason, _ = self.store.claim(FAKE_PROVIDER, "gpt-6-luna")
        self.assertFalse(allowed)
        self.assertIn("probe", reason)

    def test_a_foreign_owner_cannot_release_the_claim(self) -> None:
        self.store.claim(FAKE_PROVIDER, "gpt-6-luna")
        # Another request - a skipped candidate, an abandoned client - must
        # not clear a probe that is still being served.
        self.store.release_probe(FAKE_PROVIDER, "gpt-6-luna", owner="not-the-owner")
        self.assertFalse(
            self.store.claim(FAKE_PROVIDER, "gpt-6-luna")[0],
            "a foreign owner released a live probe",
        )

    def test_the_owner_releases_its_own_claim(self) -> None:
        _allowed, _reason, owner = self.store.claim(FAKE_PROVIDER, "gpt-6-luna")
        self.store.release_probe(FAKE_PROVIDER, "gpt-6-luna", owner)
        self.assertTrue(self.store.claim(FAKE_PROVIDER, "gpt-6-luna")[0])


class LiveProbeExcludesOtherRequestsTests(unittest.TestCase):
    """One invocation per half-open route, and the ledger says why."""

    def test_a_second_request_during_a_live_probe_never_reaches_the_primary(self) -> None:
        harness, clock = _harness()
        self.addCleanup(harness.close)
        _open_and_expire(harness, clock)

        entered, release = threading.Event(), threading.Event()
        blocking = BlockingFakeAdapter(FAKE_PROVIDER, entered=entered, release=release)
        harness.registry.register(blocking)

        first: dict[str, Any] = {}

        def probe_request() -> None:
            first["result"] = harness.request("POST", "/v1/chat/completions", body=_chat())

        worker = threading.Thread(target=probe_request, daemon=True)
        worker.start()
        self.assertTrue(entered.wait(timeout=10), "the probe never reached the provider")

        # While the recovery probe is running, the second request is served
        # by the fallback and never touches the primary.
        status, _, _ = harness.request("POST", "/v1/chat/completions", body=_chat())
        self.assertEqual(status, 200)
        event = _latest(harness)
        self.assertEqual(event["serving_provider"], FALLBACK_PROVIDER)
        self.assertTrue(event["fallback_used"])
        self.assertIn("probe", event["skipped"][0]["reason"])

        release.set()
        worker.join(timeout=10)
        self.assertFalse(worker.is_alive(), "the probing request never finished")
        self.assertEqual(first["result"][0], 200)
        # Exactly one invocation reached the primary across both requests:
        # the probe. The second request never entered the provider.
        self.assertEqual(len(blocking.calls), 1)
        self.assertEqual(_latest(harness)["serving_provider"], FAKE_PROVIDER)

        # The probe succeeded, so the circuit is closed and the primary
        # serves the next request normally - no lingering cooldown.
        self.assertEqual(harness.router.circuits.remaining(FAKE_PROVIDER, "gpt-6-luna"), 0)
        harness.registry.register(FakeAdapter())
        status, _, _ = harness.request("POST", "/v1/chat/completions", body=_chat())
        self.assertEqual(status, 200)
        event = _latest(harness)
        self.assertEqual(event["serving_provider"], FAKE_PROVIDER)
        self.assertFalse(event["fallback_used"])


class ProbeReleaseOnFailureTests(unittest.TestCase):
    """A probe that ends must not outlive the invocation it belongs to."""

    def test_a_failed_probe_reopens_the_circuit_and_releases_the_claim(self) -> None:
        harness, clock = _harness()
        self.addCleanup(harness.close)
        _open_and_expire(harness, clock)

        harness.registry.register(
            FakeAdapter(fail_with=AdapterError("rate_limited", provider_id=FAKE_PROVIDER))
        )
        status, _, _ = harness.request("POST", "/v1/chat/completions", body=_chat())
        self.assertEqual(status, 200)
        event = _latest(harness)
        self.assertEqual(event["serving_provider"], FALLBACK_PROVIDER)
        self.assertTrue(event["fallback_used"])
        # The failed probe is evidence the provider is still down: the
        # circuit is armed again, and nothing is left claiming it.
        self.assertGreater(harness.router.circuits.remaining(FAKE_PROVIDER, "gpt-6-luna"), 0)
        self.assertFalse(_probe_held(harness))

        # When the new window ends, the primary is probeable again. The
        # window escalates with the second failure, so read it rather than
        # guessing a duration.
        window = harness.router.circuits.remaining(FAKE_PROVIDER, "gpt-6-luna")
        clock.advance(window + 10)
        harness.registry.register(FakeAdapter())
        status, _, _ = harness.request("POST", "/v1/chat/completions", body=_chat())
        self.assertEqual(status, 200)
        self.assertEqual(_latest(harness)["serving_provider"], FAKE_PROVIDER)
        self.assertEqual(harness.router.circuits.remaining(FAKE_PROVIDER, "gpt-6-luna"), 0)

    def test_an_unexpected_failure_releases_the_claim(self) -> None:
        harness, clock = _harness()
        self.addCleanup(harness.close)
        _open_and_expire(harness, clock)

        class ExplodingAdapter(FakeAdapter):
            def invoke(self, request: Any) -> ProviderResponse:
                self.calls.append(request)
                raise RuntimeError("synthetic unexpected provider failure")

        harness.registry.register(ExplodingAdapter(FAKE_PROVIDER))
        status, _, _ = harness.request("POST", "/v1/chat/completions", body=_chat())
        self.assertEqual(status, 500)
        self.assertFalse(
            _probe_held(harness),
            "an unexpected failure left the probe claimed, wedging the route",
        )

        # The route is usable again immediately: the next compatible
        # request claims the probe and a healthy provider closes it.
        harness.registry.register(FakeAdapter())
        status, _, _ = harness.request("POST", "/v1/chat/completions", body=_chat())
        self.assertEqual(status, 200)
        event = _latest(harness)
        self.assertEqual(event["serving_provider"], FAKE_PROVIDER)
        self.assertEqual(harness.router.circuits.remaining(FAKE_PROVIDER, "gpt-6-luna"), 0)


    def test_an_exception_while_the_failure_is_processed_releases_the_claim(self) -> None:
        # The defect this pins: ``_maybe_open_circuit`` runs *inside* the
        # ``except AdapterError`` handler, and an exception raised there is
        # not caught by a sibling ``except``. It escaped as an HTTP 500
        # with the primary's claim still held, so the route refused every
        # probe for the rest of the window while nothing probed it.
        harness, clock = _harness()
        self.addCleanup(harness.close)
        _open_and_expire(harness, clock)

        harness.registry.register(
            FakeAdapter(fail_with=AdapterError("rate_limited", provider_id=FAKE_PROVIDER))
        )
        with mock.patch.object(
            harness.router,
            "_maybe_open_circuit",
            side_effect=RuntimeError("synthetic failure while opening the circuit"),
        ):
            status, _, _ = harness.request("POST", "/v1/chat/completions", body=_chat())
        self.assertEqual(status, 500)
        self.assertFalse(
            _probe_held(harness),
            "an exception inside the AdapterError handler left the probe claimed",
        )

        # The circuit is still half-open, so the very next compatible
        # request claims the probe at once and a healthy provider closes
        # it: the route did not sit on its fallback until the timeout.
        harness.registry.register(FakeAdapter())
        status, _, _ = harness.request("POST", "/v1/chat/completions", body=_chat())
        self.assertEqual(status, 200)
        event = _latest(harness)
        self.assertEqual(event["serving_provider"], FAKE_PROVIDER)
        self.assertFalse(event["fallback_used"])
        self.assertEqual(harness.router.circuits.remaining(FAKE_PROVIDER, "gpt-6-luna"), 0)

    def test_an_exception_after_a_successful_probe_releases_the_claim(self) -> None:
        # Post-claim processing - closing the circuit, writing the ledger -
        # runs after the invocation's own handlers. An exception there used
        # to escape with the half-open claim still held, for the same
        # reason: nobody released it on that path.
        harness, clock = _harness()
        self.addCleanup(harness.close)
        _open_and_expire(harness, clock)
        harness.registry.register(FakeAdapter())

        with mock.patch.object(
            harness.router.circuits,
            "record_success",
            side_effect=RuntimeError("synthetic failure while recording the success"),
        ):
            status, _, _ = harness.request("POST", "/v1/chat/completions", body=_chat())
        self.assertEqual(status, 500)
        self.assertFalse(
            _probe_held(harness),
            "an exception during post-claim processing left the probe claimed",
        )

        # The claim is gone, so the next request is the probe and it
        # succeeds - the half-open route is usable immediately.
        harness.registry.register(FakeAdapter())
        status, _, _ = harness.request("POST", "/v1/chat/completions", body=_chat())
        self.assertEqual(status, 200)
        event = _latest(harness)
        self.assertEqual(event["serving_provider"], FAKE_PROVIDER)
        self.assertEqual(harness.router.circuits.remaining(FAKE_PROVIDER, "gpt-6-luna"), 0)


class NoRetryLeakTests(unittest.TestCase):
    """The claim is released exactly once, by the path that took it."""

    def test_a_skipped_candidate_releases_nothing(self) -> None:
        # A request that never gets past preview leaves the store alone, so
        # a probe claimed by a concurrent request stays claimed.
        harness, clock = _harness()
        self.addCleanup(harness.close)
        _open_and_expire(harness, clock)
        _allowed, _reason, owner = harness.router.circuits.claim(FAKE_PROVIDER, "gpt-6-luna")

        status, _, _ = harness.request("POST", "/v1/chat/completions", body=_chat())
        self.assertEqual(status, 200)
        self.assertEqual(_latest(harness)["serving_provider"], FALLBACK_PROVIDER)
        self.assertTrue(_probe_held(harness), "a skipped candidate released another request's probe")

        harness.router.circuits.release_probe(FAKE_PROVIDER, "gpt-6-luna", owner)
        self.assertFalse(_probe_held(harness))


if __name__ == "__main__":
    unittest.main()
