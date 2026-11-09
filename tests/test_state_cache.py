"""The cached ``/owner/api/state`` render.

The console snapshot is ~90KB of server-side work measured at 182ms p50
on production data, and it is the endpoint the console reads. It is now
served from a token-keyed cache of the rendered body. The properties
under test:

* a cache hit returns **byte-identical** bytes to the full render - the
  payload is the contract, the cache may not shape it;
* every mutation is visible on the **immediately following** read: a
  config write, a backup import that changes content *without* bumping
  the revision, a ledger record or amendment, a credential enrolment,
  a circuit transition **including an expiry**, and a release file
  rewritten under the running app (the identity's *result* is in the
  key; the callable object was not);
* an open circuit's ticking countdown does not evict the cache, while
  its expiry does - the key tracks circuit *state*, not per-second
  numbers;
* a body that ages is re-rendered in the background, off the read path,
  and the refreshed body is still byte-identical;
* a render that races a mutation is never pinned under the wrong token;
* steady-state reads land far under the p95 < 20ms bar - the literal bar on
  an idle box, and under CPU contention a relative gate against the same
  request machinery served from ``/health`` in the same process (a raw
  wall-clock bar measures the box, not the cache).
"""

from __future__ import annotations

import json
import math
import os
import time
import unittest

from gateway.circuits import CircuitStore
from gateway.observability import LimitSnapshot, RequestEvent, RequestLedger
from tests.test_end_to_end import FAKE_PROVIDER, GatewayHarness

#: The load-independent budget under contention: the state p95 is gated
#: against this multiple of a same-process ``/health`` calibration loop (the
#: identical client request machinery with no product work behind it), with
#: the literal 20ms bar always kept as the floor. 6.0 is ~2.5x the worst
#: state/calibration p95 ratio measured across 15 rep pairs at load1 17-25
#: (1.00-2.38, interleaved 1:1 so both loops see the same load), i.e.
#: headroom against tail-sample noise rather than a licence for product
#: work: a cache hit that really re-rendered would measure seconds (the
#: cold render measured 0.66-2.4s in setUpClass), two orders over any
#: budget here, and the cache's *behaviour* (no re-render on a hit) is
#: asserted structurally by the tests above - this bar only claims speed.
CALIBRATION_FACTOR = 6.0


def _load_pressure() -> float:
    """CPU contention this suite is running under: 1-min load / cores, floored at 1.0.

    A wall-clock p95 over in-process reads can never be load-independent: the
    samples pay this box's scheduler as much as the cache, so the raw 20ms bar
    reddened 6/13 reps on an unchanged tree at load1 18-25 (observed 22.8-
    37.5ms). Same treatment family as ``tests/test_ledger_store.py``: the
    literal bar is enforced whenever the box can measure it (pressure <= 1.0),
    under contention the gate becomes the relative invariant the bar exists
    for (see ``CALIBRATION_FACTOR``), and every measured value is printed.
    """
    return max(1.0, os.getloadavg()[0] / (os.cpu_count() or 1))


class StateCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = GatewayHarness()
        self.render_calls = 0
        original = self.harness.app._render_state

        def spy(request):
            self.render_calls += 1
            return original(request)

        self.harness.app._state_cache._render = spy

    def tearDown(self) -> None:
        self.harness.close()

    # --- helpers ---------------------------------------------------------

    @property
    def ledger(self) -> RequestLedger:
        ledger = self.harness.app.ledger
        self.assertIsNotNone(ledger)
        assert ledger is not None
        return ledger

    def raw_state(self, path: str = "/owner/api/state") -> bytes:
        """The exact response body bytes served for *path*."""
        status, body, _ = self.harness.request("GET", path, raw=True)
        self.assertEqual(status, 200, body)
        assert isinstance(body, bytes)
        return body

    def state(self, path: str = "/owner/api/state") -> dict:
        status, payload, _ = self.harness.request("GET", path)
        self.assertEqual(status, 200, payload)
        assert isinstance(payload, dict)
        return payload

    # --- byte identity ---------------------------------------------------

    def test_cache_hit_is_byte_identical_to_a_full_render(self) -> None:
        first = self.raw_state()          # cold: full render
        cold_renders = self.render_calls
        second = self.raw_state()         # warm: cache hit
        self.assertEqual(self.render_calls, cold_renders, "second read must be served from cache")
        self.assertEqual(first, second)
        self.harness.app._state_cache.clear()
        third = self.raw_state()          # forced full render again
        self.assertEqual(self.render_calls, cold_renders + 1)
        self.assertEqual(first, third)

    def test_days_variant_gets_its_own_byte_identical_entry(self) -> None:
        # days=99 clamps to the maximum and echoes requested_days, so the
        # variant is visible in the payload as well as in the token.
        default = self.raw_state()
        wide = self.raw_state("/owner/api/state?days=99")
        self.assertEqual(self.render_calls, 2)
        self.assertEqual(self.raw_state(), default)
        self.assertEqual(self.raw_state("/owner/api/state?days=99"), wide)
        self.assertEqual(self.render_calls, 2, "each variant must hit its own entry")
        self.assertEqual(json.loads(wide)["analytics"]["requested_days"], 99)

    # --- mutation visible on the immediately following read --------------

    def test_config_write_is_visible_on_the_next_read(self) -> None:
        before = self.state()
        with self.harness.config.mutate() as snapshot:
            snapshot.access_mode = "open" if snapshot.access_mode != "open" else "lan_noauth"
        after = self.state()
        self.assertEqual(after["access_mode"], self.harness.config.snapshot().access_mode)
        self.assertNotEqual(before["revision"], after["revision"])

    def test_backup_import_with_unchanged_revision_is_visible_on_the_next_read(self) -> None:
        """The revision alone is not a sound key: an import can rewrite
        content under the revision the payload carries, so the config
        file's (inode, mtime, size) stamp is part of the token too."""
        from gateway.config import Alias, RouteRef

        with self.harness.config.mutate() as snapshot:
            snapshot.aliases["alpha"] = Alias(
                name="alpha",
                enabled=True,
                primary=RouteRef(FAKE_PROVIDER, "gpt-6-luna"),
            )
        export_revision = self.state()["revision"]

        status, payload, _ = self.harness.request("GET", "/owner/api/backup/export")
        self.assertEqual(status, 200)
        assert isinstance(payload, dict)
        imported = json.loads(json.dumps(payload["export"]))
        aliases = imported["config"]["aliases"]
        self.assertIn("alpha", aliases)
        aliases["beta"] = dict(aliases.pop("alpha"))
        aliases["beta"]["name"] = "beta"
        self.assertEqual(imported["config"]["revision"], export_revision)

        status, result, _ = self.harness.request("POST", "/owner/api/backup/import", body=imported)
        self.assertEqual(status, 200, result)
        self.assertEqual(self.harness.config.snapshot().revision, export_revision,
                         "fixture premise: this import must not bump the revision")

        after = self.state()
        self.assertEqual([a["name"] for a in after["aliases"]], ["beta"],
                         "an import that rewrote content under an unchanged revision must not be served stale")

    def test_ledger_record_is_visible_on_the_next_read(self) -> None:
        before = self.state()
        self.ledger.record(RequestEvent(
            request_id="req-state-cache",
            timestamp="2026-10-30T12:00:00Z",
            requested_alias="fast",
            status=200,
            latency_ms=42,
        ))
        after = self.state()
        self.assertEqual(after["totals"]["requests"], before["totals"]["requests"] + 1)
        self.assertEqual(after["ledger"][0]["request_id"], "req-state-cache")

    def test_ledger_amend_is_visible_on_the_next_read(self) -> None:
        self.ledger.record(RequestEvent(
            request_id="req-amend",
            timestamp="2026-10-30T12:00:00Z",
            requested_alias="fast",
            status=None,
        ))
        before = self.state()
        self.assertTrue(self.ledger.amend("req-amend", status=200, latency_ms=7))
        after = self.state()
        amended = next(row for row in after["ledger"] if row["request_id"] == "req-amend")
        self.assertEqual(amended["status"], 200)
        self.assertEqual(amended["latency_ms"], 7)
        stale = next(row for row in before["ledger"] if row["request_id"] == "req-amend")
        self.assertNotEqual(stale["status"], amended["status"])

    def test_credential_enrolment_is_visible_on_the_next_read(self) -> None:
        before = self.state()
        provider = next(p for p in before["providers"] if p["provider_id"] == FAKE_PROVIDER)
        self.assertFalse(provider["credential_present"])
        self.harness.credentials.handle(FAKE_PROVIDER).write({"api_key": "synthetic-key"})
        after = self.state()
        provider = next(p for p in after["providers"] if p["provider_id"] == FAKE_PROVIDER)
        self.assertTrue(provider["credential_present"])

    def test_limit_observation_is_visible_on_the_next_read(self) -> None:
        before = self.state()
        self.harness.limits.put(LimitSnapshot(provider=FAKE_PROVIDER, window="day", value=1.0, limit=10.0))
        after = self.state()
        self.assertEqual(len(after["limits"]), len(before["limits"]) + 1)

    # --- circuits: state changes evict, countdowns do not -----------------

    def test_circuit_open_is_visible_and_its_expiry_re_renders_but_its_countdown_does_not(self) -> None:
        clock = {"now": 1_000_000.0}
        self.harness.router._circuits = CircuitStore(clock=lambda: clock["now"])
        self.assertEqual(self.state()["cooldowns"], {})

        self.harness.router.circuits.set(FAKE_PROVIDER, 60, error_class="policy", reason="fixture")
        after = self.state()
        self.assertIn(f"{FAKE_PROVIDER}/*", after["cooldowns"])
        renders_after_open = self.render_calls

        # The countdown ticks every second inside details()/snapshot(), but
        # the key tracks circuit state, not per-second numbers: two reads a
        # simulated minute apart must both be cache hits.
        clock["now"] += 30
        self.state()
        clock["now"] += 29
        self.state()
        self.assertEqual(self.render_calls, renders_after_open,
                         "a ticking countdown must not force a re-render")

        # The expiry itself is a state change (open -> half-open, key gone
        # from snapshot) and the next read must reflect it at once.
        clock["now"] += 2
        expired = self.state()
        self.assertEqual(self.render_calls, renders_after_open + 1,
                         "a circuit expiry must re-render on the next read")
        self.assertNotIn(f"{FAKE_PROVIDER}/*", expired["cooldowns"])
        self.assertTrue(all(entry["state"] != "open" for entry in expired["cooldown_details"].values()))

    def test_release_file_rewrite_is_visible_on_the_next_read(self) -> None:
        """``release_identity()`` re-reads ``release.json`` on every call, so
        the token must digest its **result** - the callable object is a
        per-process constant. A release written after the app is up (the
        ``scripts/release.py --build`` flow, or
        ``scripts/write_release_identity.py`` against a running volume) must
        show on the very next read, not after the background refresh."""
        import tempfile
        from pathlib import Path

        from gateway.buildinfo import release_identity

        scratch = Path(tempfile.mkdtemp())
        release_path = scratch / "release.json"
        mountinfo_path = scratch / "mountinfo"
        container_id = "c" * 64
        # The mount-table shape that binds a record to *this* container, so
        # the recorded digest is reported rather than failing closed to
        # "unknown" (see gateway.buildinfo).
        mountinfo_path.write_text(
            f"0 0 0:0 / /var/lib/docker/containers/{container_id}/resolv.conf rw - tmpfs / rw\n",
            encoding="utf-8",
        )

        def write_release(image_revision: str) -> None:
            release_path.write_text(json.dumps({
                "image_revision": image_revision,
                "container_id": container_id,
            }), encoding="utf-8")

        def identity() -> dict:
            return release_identity(release_path=release_path, mountinfo_path=mountinfo_path)

        write_release("sha256:" + "a" * 64)
        self.harness.app._release_identity = identity

        before = self.state()
        self.assertEqual(before["release"]["image_revision"], "sha256:" + "a" * 64)
        warm = self.render_calls
        self.state()  # warm: served from the cache
        self.assertEqual(self.render_calls, warm, "fixture premise: the second read must hit")

        write_release("sha256:" + "b" * 64)
        after = self.state()
        self.assertEqual(self.render_calls, warm + 1,
                         "a live release write must re-render, not serve the cached body")
        self.assertEqual(after["release"]["image_revision"], "sha256:" + "b" * 64)
        self.assertNotEqual(before["release"], after["release"])

    # --- background refresh ---------------------------------------------

    def test_aged_body_is_refreshed_off_the_read_path_and_stays_identical(self) -> None:
        first = self.raw_state()
        self.assertEqual(self.render_calls, 1)
        self.harness.app._state_cache.refresh_after = 0.0

        second = self.raw_state()  # a hit; schedules the background refresh
        self.assertEqual(second, first)
        deadline = time.monotonic() + 3.0
        while self.render_calls < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertGreaterEqual(self.render_calls, 2, "an aged read must schedule a refresh")

        third = self.raw_state()
        self.assertEqual(third, first)

    def test_body_older_than_hard_max_is_re_rendered_rather_than_served(self) -> None:
        """A long idle gap must not hand out an aged body: past ``hard_max``
        the read takes the synchronous miss path instead."""
        first = self.raw_state()
        self.assertEqual(self.render_calls, 1)
        self.harness.app._state_cache.hard_max = 0.0
        second = self.raw_state()
        self.assertEqual(self.render_calls, 2, "an over-age body must be re-rendered, not served")
        self.assertEqual(second, first)  # same state, so still byte-identical

    def test_a_render_that_races_a_mutation_is_never_pinned_under_the_wrong_token(self) -> None:
        # Build the request the same way serve() would, take a token, then
        # mutate before storing: _store_if_unchanged must refuse to pin the
        # pre-mutation body under the pre-mutation token once state moved.
        cache = self.harness.app._state_cache
        status, body, _ = self.harness.request("GET", "/owner/api/state", raw=True)
        self.assertEqual(status, 200)
        assert isinstance(body, bytes)
        cold_renders = self.render_calls

        # A raw Response + request captured from a real read:
        from gateway.http.core import Headers
        from gateway.http.core import Request as HttpRequest
        from gateway.http.core import Response as HttpResponse

        request = HttpRequest(
            method="GET",
            path="/owner/api/state",
            raw_query="",
            headers=Headers([("host", f"{self.harness.authority}:{self.harness.port}")]),
            body=b"",
            client_address=("127.0.0.1", 1),
            server_port=self.harness.port,
        )
        request.context["surface"] = "owner"
        token = cache.token(request)
        response = HttpResponse(200, [("Content-Type", "application/json; charset=utf-8")], body)

        with self.harness.config.mutate() as snapshot:
            snapshot.access_mode = "open" if snapshot.access_mode != "open" else "lan_noauth"
        entries_before = cache.entry_count
        cache._store_if_unchanged(token, request, response)
        self.assertEqual(cache.entry_count, entries_before,
                         "a body rendered before a mutation must not be pinned under its token")

        live = self.state()
        self.assertEqual(live["access_mode"], self.harness.config.snapshot().access_mode)
        self.assertEqual(live["revision"], self.harness.config.snapshot().revision)
        self.assertEqual(self.render_calls, cold_renders + 1)

    # --- latency ---------------------------------------------------------

    def test_steady_state_reads_are_far_under_the_20ms_p95_bar(self) -> None:
        """Literal 20ms bar when the box can measure it, relative gate otherwise.

        The samples are wall-clock reads of an in-process server, so on a
        shared box running at several times its core count they price this
        box's scheduler as much as the cache: the raw 20ms p95 reddened 6/13
        reps on an unchanged tree (22.8-37.5ms at load1 18-25), which makes
        the bar a flake generator by construction rather than a claim about
        the code.

        Every state read is therefore interleaved 1:1 with the same request
        machinery against ``/health`` - same client, socket, accept, dispatch
        and scheduler, no product work behind it - and under contention the
        gate becomes ``max(20ms, CALIBRATION_FACTOR x calibration p95)``: a
        steady-state read may only cost what this box is charging for a
        trivial request (plus headroom for tail-sample noise), the literal
        20ms bar stays the floor, and every measured value is printed.
        """
        pressure = _load_pressure()
        self.raw_state()  # warm
        samples: list[float] = []
        calibration: list[float] = []
        for _ in range(30):
            started = time.perf_counter()
            self.raw_state()
            samples.append((time.perf_counter() - started) * 1000.0)
            started = time.perf_counter()
            status, _, _ = self.harness.request("GET", "/health")
            self.assertEqual(status, 200)
            calibration.append((time.perf_counter() - started) * 1000.0)
        pressure = max(pressure, _load_pressure())
        samples.sort()
        calibration.sort()
        p50 = samples[len(samples) // 2]
        p95 = samples[math.ceil(0.95 * len(samples)) - 1]
        calib_p50 = calibration[len(calibration) // 2]
        calib_p95 = calibration[math.ceil(0.95 * len(calibration)) - 1]
        budget = 20.0 if pressure <= 1.0 else max(
            20.0, CALIBRATION_FACTOR * calib_p95)
        print(f"[latency-gate] state p95={p95:.2f}ms p50={p50:.2f}ms vs "
              f"calibration p95={calib_p95:.2f}ms p50={calib_p50:.2f}ms at load "
              f"pressure {pressure:.1f} (load1 {os.getloadavg()[0]:.1f} / "
              f"{os.cpu_count()} cores) -> budget {budget:.2f}ms")
        self.assertLess(p95, budget,
                        f"p95={p95:.2f}ms p50={p50:.2f}ms against calibration "
                        f"p95={calib_p95:.2f}ms (budget {budget:.2f}ms) at load "
                        f"pressure {pressure:.1f}")


if __name__ == "__main__":
    unittest.main()
