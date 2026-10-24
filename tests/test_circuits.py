"""The durable circuit breaker.

A route that fails in a way the owner has enabled a trigger for is put
into a local cooldown. The earlier version was a plain in-memory timer,
and it failed at exactly the moment a cooldown matters: a gateway that
restarted during an outage immediately called the exhausted provider
again.

The properties under test:

* the state **survives a restart**, because it is written to disk;
* a confirmed **exhaustion** opens for a long time and covers the whole
  provider, because the account's allowance is gone;
* a **transient** failure is a blip: it needs a pattern before it opens
  anything;
* the window **escalates** with consecutive failures, and a success
  resets it;
* when a window ends the circuit is **half-open**: exactly one request is
  let through to find out whether the provider recovered;
* the stored state is **metadata only** - no prompt, no completion.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from gateway.circuits import (
    EXHAUSTED_BASE_SECONDS,
    EXHAUSTED_MAX_SECONDS,
    MAX_BACKOFF_SECONDS,
    MAX_ENTRIES,
    MAX_ENTRY_AGE_SECONDS,
    TRANSIENT_FAILURE_THRESHOLD,
    CircuitStore,
)


class _Clock:
    """A clock a test can move."""

    def __init__(self, now: float = 1_700_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class CircuitStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "circuits.json"
        self.clock = _Clock()
        self.store = CircuitStore(self.path, clock=self.clock)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # --- persistence -----------------------------------------------------

    def test_a_circuit_survives_a_restart(self) -> None:
        self.store.record_failure("opencode_go", "mimo-v2.6-flash", "exhausted", base_seconds=300)
        self.assertGreater(self.store.remaining("opencode_go", "mimo-v2.6-flash"), 0)

        # A new process, the same file.
        restarted = CircuitStore(self.path, clock=self.clock)
        self.assertGreater(
            restarted.remaining("opencode_go", "mimo-v2.6-flash"),
            0,
            "a restart during an outage must not defeat the circuit",
        )
        allowed, reason = restarted.check("opencode_go", "mimo-v2.6-flash")
        self.assertFalse(allowed)
        self.assertIn("cooldown", reason)

    def test_the_stored_state_is_metadata_only(self) -> None:
        self.store.record_failure("opencode_go", "mimo-v2.6-flash", "exhausted", base_seconds=300)
        raw = self.path.read_text(encoding="utf-8")
        payload = json.loads(raw)
        entry = next(iter(payload["circuits"].values()))
        self.assertEqual(
            set(entry),
            {
                "provider",
                "model",
                "provider_wide",
                "armed",
                "error_class",
                "failures",
                "opened_at",
                "until",
                "reason",
            },
        )
        for field in ("prompt", "content", "message", "completion", "body"):
            self.assertNotIn(field, raw)

    def test_the_stored_file_is_not_world_readable(self) -> None:
        self.store.record_failure("opencode_go", "mimo-v2.6-flash", "exhausted", base_seconds=300)
        mode = stat.S_IMODE(os.stat(self.path).st_mode)
        self.assertEqual(mode, 0o600)

    def test_a_probe_is_never_remembered(self) -> None:
        self.store.record_failure("opencode_go", "mimo-v2.6-flash", "exhausted", base_seconds=300)
        # The window ends and a probe is claimed.
        self.clock.advance(EXHAUSTED_BASE_SECONDS + 10)
        allowed, _reason = self.store.check("opencode_go", "mimo-v2.6-flash")
        self.assertTrue(allowed, "the probe must be allowed through")

        # A restart must not leave the route blocked by a probe that no
        # longer exists.
        restarted = CircuitStore(self.path, clock=self.clock)
        allowed, reason = restarted.check("opencode_go", "mimo-v2.6-flash")
        self.assertTrue(allowed, reason)

    def test_an_old_entry_is_forgotten(self) -> None:
        self.store.record_failure("opencode_go", "mimo-v2.6-flash", "exhausted", base_seconds=300)
        self.clock.advance(MAX_ENTRY_AGE_SECONDS + 10)
        restarted = CircuitStore(self.path, clock=self.clock)
        self.assertEqual(restarted.remaining("opencode_go", "mimo-v2.6-flash"), 0)
        allowed, _reason = restarted.check("opencode_go", "mimo-v2.6-flash")
        self.assertTrue(allowed)

    def test_the_store_stays_bounded(self) -> None:
        for index in range(MAX_ENTRIES + 20):
            self.store.record_failure(
                "opencode_go", f"model-{index}", "exhausted", base_seconds=300
            )
        self.assertLessEqual(len(self.store._entries), MAX_ENTRIES)

    # --- classes ---------------------------------------------------------

    def test_an_exhaustion_opens_for_a_long_time_and_covers_the_provider(self) -> None:
        self.store.record_failure("opencode_go", "mimo-v2.6-flash", "exhausted", base_seconds=300)
        self.assertGreaterEqual(
            self.store.remaining("opencode_go", "mimo-v2.6-flash"),
            EXHAUSTED_BASE_SECONDS,
            "an exhausted allowance does not come back in five minutes",
        )
        # The account's allowance is gone, so another route at the same
        # provider is held as well.
        allowed, reason = self.store.check("opencode_go", "some-other-model")
        self.assertFalse(allowed, reason)
        self.assertIn("provider", reason)

    def test_a_rate_limit_opens_the_route_only(self) -> None:
        self.store.record_failure(
            "opencode_go", "mimo-v2.6-flash", "rate_limited", base_seconds=300, retry_after=1800
        )
        self.assertGreaterEqual(self.store.remaining("opencode_go", "mimo-v2.6-flash"), 1800)
        allowed, _reason = self.store.check("opencode_go", "some-other-model")
        self.assertTrue(allowed, "a rate limit on one route does not hold the whole provider")

    def test_a_transient_failure_needs_a_pattern(self) -> None:
        self.store.record_failure(
            "opencode_go", "mimo-v2.6-flash", "transient", base_seconds=300
        )
        allowed, reason = self.store.check("opencode_go", "mimo-v2.6-flash")
        self.assertTrue(
            allowed,
            f"one blip is not an outage, so the route stays usable ({reason})",
        )

        self.store.record_failure(
            "opencode_go", "mimo-v2.6-flash", "transient", base_seconds=300
        )
        allowed, reason = self.store.check("opencode_go", "mimo-v2.6-flash")
        self.assertFalse(allowed, f"a pattern must hold the route ({reason})")

    def test_a_counted_blip_never_blocks_the_route(self) -> None:
        """A failure that only counts must not hold the route, not even
        through the probe mechanism."""
        self.store.record_failure(
            "opencode_go", "mimo-v2.6-flash", "transient", base_seconds=300
        )
        for attempt in range(3):
            allowed, reason = self.store.check("opencode_go", "mimo-v2.6-flash")
            self.assertTrue(
                allowed,
                f"the route must stay usable (attempt {attempt}): {reason}",
            )
        self.assertEqual(self.store.snapshot(), {}, "a counted blip is not a circuit")

    def test_a_success_resets_the_pattern(self) -> None:
        self.store.record_failure("opencode_go", "mimo-v2.6-flash", "transient", base_seconds=300)
        self.store.record_success("opencode_go", "mimo-v2.6-flash")
        self.store.record_failure("opencode_go", "mimo-v2.6-flash", "transient", base_seconds=300)
        allowed, _reason = self.store.check("opencode_go", "mimo-v2.6-flash")
        self.assertTrue(allowed, "the route recovered, so the count starts again")

    # --- escalation ------------------------------------------------------

    def test_the_window_escalates_with_consecutive_failures(self) -> None:
        self.store.record_failure("opencode_go", "mimo-v2.6-flash", "rate_limited", base_seconds=300)
        first = self.store.remaining("opencode_go", "mimo-v2.6-flash")
        self.clock.advance(1)
        self.store.record_failure("opencode_go", "mimo-v2.6-flash", "rate_limited", base_seconds=300)
        second = self.store.remaining("opencode_go", "mimo-v2.6-flash")
        self.assertGreater(second, first, "repeated failures must back off further")

    def test_the_window_is_capped(self) -> None:
        for _ in range(30):
            self.store.record_failure(
                "opencode_go", "mimo-v2.6-flash", "rate_limited", base_seconds=300
            )
        self.assertLessEqual(
            self.store.remaining("opencode_go", "mimo-v2.6-flash"),
            MAX_BACKOFF_SECONDS,
        )

    # --- the bounded exhaustion policy -----------------------------------

    def test_an_exhausted_provider_hold_never_exceeds_the_cap(self) -> None:
        """However many times the account reports exhaustion, the local
        hold never grows past the configured cap: a reopened subscription
        window must not sit behind an opaque multi-hour circuit."""
        for _ in range(6):
            self.store.record_failure("chatgpt_codex", "gpt-6-luna", "exhausted", base_seconds=300)
            self.clock.advance(EXHAUSTED_BASE_SECONDS + 5)
        self.assertLessEqual(
            self.store.remaining("chatgpt_codex"),
            EXHAUSTED_MAX_SECONDS,
            "a provider-wide exhausted hold must never exceed the cap",
        )
        self.assertLessEqual(
            self.store.remaining("chatgpt_codex", "gpt-6-luna"),
            EXHAUSTED_MAX_SECONDS,
        )

    def test_the_cap_beats_a_larger_alias_cooldown_for_exhaustion(self) -> None:
        self.store.record_failure("chatgpt_codex", "gpt-6-luna", "exhausted", base_seconds=7200)
        remaining = self.store.remaining("chatgpt_codex", "gpt-6-luna")
        self.assertLessEqual(
            remaining,
            EXHAUSTED_MAX_SECONDS,
            "the bounded exhaustion cap wins over a larger alias cooldown",
        )
        self.assertGreaterEqual(remaining, EXHAUSTED_BASE_SECONDS)

    def test_the_exhausted_cap_is_configurable(self) -> None:
        store = CircuitStore(exhausted_max_seconds=600)
        store.record_failure("chatgpt_codex", "gpt-6-luna", "exhausted", base_seconds=300)
        self.assertLessEqual(store.remaining("chatgpt_codex", "gpt-6-luna"), 600)
        self.assertEqual(store.exhausted_max_seconds, 600)

    # --- recovery telemetry ----------------------------------------------

    def test_details_expose_the_recovery_telemetry(self) -> None:
        opened_at = self.clock.now
        self.store.record_failure("chatgpt_codex", "gpt-6-luna", "exhausted", base_seconds=300)
        entry = self.store.details()["chatgpt_codex/*"]
        self.assertEqual(entry["scope"], "provider")
        self.assertEqual(entry["error_class"], "exhausted")
        self.assertEqual(entry["failures"], 1)
        self.assertEqual(entry["opened_at"], opened_at)
        self.assertEqual(entry["state"], "open")
        self.assertFalse(entry["probe_in_flight"])
        self.assertEqual(entry["next_probe_at"], entry["until"])

        # Once the window passes and a probe is claimed, the telemetry says
        # a probe is in flight and that the route is eligible now.
        self.clock.advance(EXHAUSTED_MAX_SECONDS + 10)
        self.store.check("chatgpt_codex", "gpt-6-luna")
        entry = self.store.details()["chatgpt_codex/*"]
        self.assertEqual(entry["state"], "half_open")
        self.assertTrue(entry["probe_in_flight"])
        self.assertEqual(entry["next_probe_at"], self.clock.now)

    # --- half open -------------------------------------------------------

    def test_preview_does_not_claim_a_half_open_probe(self) -> None:
        self.store.record_failure("opencode_go", "mimo-v2.6-flash", "exhausted", base_seconds=300)
        self.clock.advance(EXHAUSTED_BASE_SECONDS + 10)
        allowed, reason = self.store.preview("opencode_go", "mimo-v2.6-flash")
        self.assertTrue(allowed, reason)
        # Previewing a route must leave the atomic claim available to its
        # eventual selected invocation.
        allowed, reason = self.store.check("opencode_go", "mimo-v2.6-flash")
        self.assertTrue(allowed, reason)
        allowed, reason = self.store.check("opencode_go", "mimo-v2.6-flash")
        self.assertFalse(allowed)
        self.assertIn("probe", reason)

    def test_release_probe_allows_retry_after_unexpected_failure(self) -> None:
        self.store.record_failure("opencode_go", "mimo-v2.6-flash", "exhausted", base_seconds=300)
        self.clock.advance(EXHAUSTED_BASE_SECONDS + 10)
        self.assertTrue(self.store.check("opencode_go", "mimo-v2.6-flash")[0])
        self.store.release_probe("opencode_go", "mimo-v2.6-flash")
        self.assertTrue(self.store.check("opencode_go", "mimo-v2.6-flash")[0])

    def test_exactly_one_probe_is_let_through(self) -> None:
        self.store.record_failure("opencode_go", "mimo-v2.6-flash", "exhausted", base_seconds=300)
        self.clock.advance(EXHAUSTED_BASE_SECONDS + 10)

        allowed, _reason = self.store.check("opencode_go", "mimo-v2.6-flash")
        self.assertTrue(allowed, "the probe must be let through")
        allowed, reason = self.store.check("opencode_go", "mimo-v2.6-flash")
        self.assertFalse(allowed, "only one probe at a time")
        self.assertIn("probe", reason)

    def test_a_successful_probe_closes_the_circuit(self) -> None:
        self.store.record_failure("opencode_go", "mimo-v2.6-flash", "exhausted", base_seconds=300)
        self.clock.advance(EXHAUSTED_BASE_SECONDS + 10)
        self.store.check("opencode_go", "mimo-v2.6-flash")
        self.store.record_success("opencode_go", "mimo-v2.6-flash")
        allowed, reason = self.store.check("opencode_go", "mimo-v2.6-flash")
        self.assertTrue(allowed, reason)
        self.assertEqual(self.store.remaining("opencode_go", "mimo-v2.6-flash"), 0)

    def test_a_failed_probe_rearms_within_the_cap_and_keeps_the_reason(self) -> None:
        self.store.record_failure("opencode_go", "mimo-v2.6-flash", "exhausted", base_seconds=300)
        self.clock.advance(EXHAUSTED_BASE_SECONDS + 10)
        self.store.check("opencode_go", "mimo-v2.6-flash")
        self.store.record_failure("opencode_go", "mimo-v2.6-flash", "exhausted", base_seconds=300)
        remaining = self.store.remaining("opencode_go", "mimo-v2.6-flash")
        self.assertGreater(remaining, 0, "a failed probe must re-arm the circuit")
        self.assertLessEqual(
            remaining,
            EXHAUSTED_MAX_SECONDS,
            "a failed probe re-arms within the bounded policy, not beyond it",
        )
        details = self.store.details()["opencode_go/*"]
        self.assertEqual(details["error_class"], "exhausted")
        self.assertEqual(details["failures"], 2)
        self.assertIn("2 consecutive failures", details["reason"])

    def test_an_abandoned_probe_does_not_block_the_route_for_ever(self) -> None:
        self.store.record_failure("opencode_go", "mimo-v2.6-flash", "exhausted", base_seconds=300)
        self.clock.advance(EXHAUSTED_BASE_SECONDS + 10)
        self.store.check("opencode_go", "mimo-v2.6-flash")
        # The probe never comes back.
        self.clock.advance(600)
        allowed, reason = self.store.check("opencode_go", "mimo-v2.6-flash")
        self.assertTrue(allowed, reason)

    # --- the operator's lever ---------------------------------------------

    def test_an_operator_can_open_and_clear_a_circuit(self) -> None:
        self.store.set("opencode_go", 600, reason="the operator opened this")
        allowed, reason = self.store.check("opencode_go", "mimo-v2.6-flash")
        self.assertFalse(allowed)
        self.assertIn("the operator opened this", reason)
        self.store.clear("opencode_go")
        allowed, _reason = self.store.check("opencode_go", "mimo-v2.6-flash")
        self.assertTrue(allowed)

    def test_clearing_one_provider_returns_only_that_providers_entries(self) -> None:
        self.store.record_failure("chatgpt_codex", "gpt-6-luna", "exhausted", base_seconds=300)
        self.store.record_failure(
            "opencode_go", "mimo-v2.6-flash", "rate_limited", base_seconds=300, retry_after=60
        )
        cleared = self.store.clear("chatgpt_codex")
        self.assertEqual(
            {entry["key"] for entry in cleared},
            {"chatgpt_codex/*", "chatgpt_codex/gpt-6-luna"},
        )
        self.assertGreater(
            self.store.remaining("opencode_go", "mimo-v2.6-flash"),
            0,
            "clearing one provider must never touch another provider's circuits",
        )
        for entry in cleared:
            self.assertEqual(entry["provider"], "chatgpt_codex")
            for field in ("token", "api_key", "credential", "secret"):
                self.assertNotIn(field, entry)


class RouterIntegrationTests(unittest.TestCase):
    """The router opens the circuit, and records the skip."""

    def setUp(self) -> None:
        from tests.test_end_to_end import GatewayHarness

        self.harness = GatewayHarness()
        self.addCleanup(self.harness.close)
        with self.harness.config.mutate() as config:
            from gateway.config import Alias, ModelEntry, ProviderState, RouteRef

            config.providers["opencode_go"] = ProviderState(
                provider_id="opencode_go",
                kind="opencode_go",
                state="connected",
                models={
                    "mimo-v2.6-flash": ModelEntry(
                        model_id="mimo-v2.6-flash",
                        enabled=True,
                        dialect="openai_chat",
                        source="discovered",
                    )
                },
            )
            config.aliases["coding"] = Alias(
                name="coding",
                enabled=True,
                primary=RouteRef("chatgpt_codex", "gpt-6-luna"),
                fallbacks=[RouteRef("opencode_go", "mimo-v2.6-flash")],
                fallback_triggers=["exhausted", "rate_limited", "upstream_unavailable"],
                fallback_authorized=True,
                fallback_confirmed_providers=["opencode_go"],
            )

    def _request(self):
        return self.harness.request(
            "POST",
            "/v1/chat/completions",
            body={"model": "coding", "messages": [{"role": "user", "content": "hi"}]},
        )

    def test_an_exhaustion_opens_a_circuit_and_the_next_request_is_skipped(self) -> None:
        from gateway.adapters.base import AdapterError
        from tests.test_end_to_end import FAKE_PROVIDER, FakeAdapter

        self.harness.registry.register(
            FakeAdapter(fail_with=AdapterError("exhausted", provider_id=FAKE_PROVIDER))
        )
        self._request()
        circuits = self.harness.router.circuits
        self.assertGreater(circuits.remaining(FAKE_PROVIDER, "gpt-6-luna"), 0)

        # The next request skips the exhausted route, and says why.
        self._request()
        event = self.harness.router._ledger.recent()[0]
        skipped = event["skipped"]
        self.assertTrue(skipped)
        self.assertIn("cooldown", skipped[0]["reason"])
        self.assertIn("exhausted", skipped[0]["reason"])

    def test_a_success_closes_the_circuit(self) -> None:
        from gateway.adapters.base import AdapterError
        from tests.test_end_to_end import FAKE_PROVIDER, FakeAdapter

        # A clock the test controls, so the recovery window can be reached
        # without waiting an hour.
        clock = _Clock()
        self.harness.router._circuits = CircuitStore(clock=clock)

        self.harness.registry.register(
            FakeAdapter(fail_with=AdapterError("rate_limited", provider_id=FAKE_PROVIDER))
        )
        self._request()
        self.assertGreater(self.harness.router.circuits.remaining(FAKE_PROVIDER, "gpt-6-luna"), 0)

        # The window ends. The primary is probed, it answers, and the
        # circuit closes.
        clock.advance(400)
        self.harness.registry.register(FakeAdapter())
        self._request()
        self.assertEqual(self.harness.router.circuits.remaining(FAKE_PROVIDER, "gpt-6-luna"), 0)

    def test_a_fallback_does_not_close_the_primarys_circuit(self) -> None:
        from gateway.adapters.base import AdapterError
        from tests.test_end_to_end import FAKE_PROVIDER, FakeAdapter

        self.harness.registry.register(
            FakeAdapter(fail_with=AdapterError("rate_limited", provider_id=FAKE_PROVIDER))
        )
        # The request is served by the fallback, which succeeds.
        self._request()
        self.assertGreater(
            self.harness.router.circuits.remaining(FAKE_PROVIDER, "gpt-6-luna"),
            0,
            "a healthy fallback does not prove that the failed route recovered",
        )


if __name__ == "__main__":
    unittest.main()
