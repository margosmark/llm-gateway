"""Durable daily rollup tests: aggregation, backfill, restart and coverage.

Everything here is fixture-only: a temporary directory stands in for the
state volume and no provider, network or production ledger is touched.
"""
from __future__ import annotations

import io
import json
import stat
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from gateway.fallback_reasons import FALLBACK_LABELS
from gateway.ledger_persistence import (
    INDEX_MAX_ENTRIES,
    RollupStore,
    main,
    rebuild_rollups,
    rollup_path_for,
)
from gateway.observability import (
    DEFAULT_LEDGER_CAPACITY,
    RequestEvent,
    RequestLedger,
    scan_for_secrets,
)

# The fixture's days, anchored to the wall clock. The ledger's 30-day
# retention window runs against the real clock, so a fixed calendar date
# eventually prunes its own fixture rows out from under their assertions
# (the 2026-11-21/22 rollup bomb wave). The original relations survive the
# anchoring: DAY_OLDEST < DAY_EARLY < DAY_LATE (yesterday) < now, and every
# fixture row stays several days inside the retention window at any instant.
_TODAY = datetime.now(UTC).date()
DAY_OLDEST = (_TODAY - timedelta(days=3)).isoformat()    # was 2026-10-22
DAY_EARLY = (_TODAY - timedelta(days=2)).isoformat()     # was 2026-10-23
DAY_LATE = (_TODAY - timedelta(days=1)).isoformat()      # was 2026-10-24
#: A day provably past the 30-day window at any wall clock.
EXPIRED_DAY = (_TODAY - timedelta(days=45)).isoformat()  # was 2026-08-01

# Hand-computed expectation for the three-row fixture below, serialised the
# way the sidecar serialises rows (sorted keys, compact separators): one row
# per day x alias x provider x model, tokens summed, the 502 fallback broken
# out by reason, and the amended request counted exactly once. The fixture's
# raw reason (``primary_exhausted``) matches none of the shared rules, so it
# folds to the closed vocabulary's ``other`` bucket at ingest - counted, never
# dropped, and never persisted as a raw sentence. The two day fields are the
# only clock-coupled bytes in the template, so they are the only substitution
# ``expected_rows()`` performs; every other byte stays hand-written.
EXPECTED_ROWS_TEMPLATE = (
    '[{"alias":"coding","cached_tokens":4,"cached_tokens_unreported":0,"day":"2026-10-23","errors":{},"fallback_reasons":{},"fallbacks":0,"input_tokens":100,"model":"m-one","output_tokens":20,"provider":"opencode_go","requests":1,"requests_without_usage":0,"statuses":{"200":1}},'
    '{"alias":"coding","cached_tokens":0,"cached_tokens_unreported":0,"day":"2026-10-23","errors":{"exhausted":1},"fallback_reasons":{"other":1},"fallbacks":1,"input_tokens":0,"model":"m-two","output_tokens":0,"provider":"p-two","requests":1,"requests_without_usage":1,"statuses":{"502":1}},'
    '{"alias":"flash","cached_tokens":0,"cached_tokens_unreported":1,"day":"2026-10-24","errors":{},"fallback_reasons":{},"fallbacks":0,"input_tokens":50,"model":"m-one","output_tokens":10,"provider":"opencode_go","requests":1,"requests_without_usage":0,"statuses":{"200":1}}]'
)


def expected_rows() -> str:
    """The hand-computed expectation, re-dated to the anchored fixture days."""
    return (EXPECTED_ROWS_TEMPLATE
            .replace("2026-10-23", DAY_EARLY)
            .replace("2026-10-24", DAY_LATE))


def frozen() -> datetime:
    """Midday on the fixture's later day: after every fixture row, inside retention.

    A fixed clock let a written sidecar be compared byte for byte; anchored
    to the fixture days it keeps that property at any wall-clock instant.
    """
    return datetime.combine(date.fromisoformat(DAY_LATE), time(12, 0, 0), tzinfo=UTC)


def rows_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload["rows"], sort_keys=True, separators=(",", ":"))


def without_generated_at(payload: dict[str, Any]) -> dict[str, Any]:
    """A payload minus its wall-clock stamp, which legitimately differs per save."""
    return {key: value for key, value in payload.items() if key != "generated_at"}


def rollup_of(ledger: RequestLedger) -> dict[str, Any]:
    payload = ledger.rollup()
    assert payload is not None, "a persisted ledger must expose a rollup"
    return payload


def build_fixture(path: Path) -> RequestLedger:
    """A success row, a 502 fallback row, and a row amended after the response."""
    ledger = RequestLedger(capacity=10, persistence_path=path)
    ledger.record(RequestEvent(
        "req_a", f"{DAY_EARLY}T10:00:00Z", "coding",
        serving_provider="opencode_go", serving_model="m-one", status=200,
        token_counters={"prompt_tokens": 100, "completion_tokens": 20, "cached_read_tokens": 4},
    ))
    ledger.record(RequestEvent(
        "req_b", f"{DAY_EARLY}T11:00:00Z", "coding",
        serving_provider="p-two", serving_model="m-two", status=502,
        error_class="exhausted", fallback_used=True, fallback_reason="primary_exhausted",
    ))
    ledger.record(RequestEvent(
        "req_c", f"{DAY_LATE}T09:00:00Z", "flash",
        serving_provider="opencode_go", serving_model="m-one", status=200,
    ))
    amended = ledger.amend("req_c", token_counters={"prompt_tokens": 50, "completion_tokens": 10}, latency_ms=42)
    assert amended, "the fixture row must be amendable while it is still in the ring"
    return ledger


class RollupAggregationTests(unittest.TestCase):
    def test_rows_match_hand_computed_values_byte_for_byte(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "request-ledger.jsonl"
            ledger = build_fixture(path)
            self.assertTrue(ledger.flush())
            payload = json.loads(rollup_path_for(path).read_text())
            self.assertEqual(payload["schema"], "daily-rollup/v1")
            self.assertEqual(payload["source"], "request-ledger.jsonl")
            self.assertEqual(rows_json(payload), expected_rows())
            coverage = payload["coverage"]
            self.assertEqual(coverage["requests"], 3)
            self.assertEqual(coverage["first_row_ts"], f"{DAY_EARLY}T10:00:00Z")
            self.assertEqual(coverage["last_row_ts"], f"{DAY_LATE}T09:00:00Z")
            self.assertEqual(coverage["rows_superseded"], 1)
            self.assertEqual(coverage["rows_skipped"], 0)
            self.assertEqual(coverage["rows_expired"], 0)
            self.assertEqual(coverage["retention_days"], 30)
            self.assertFalse(coverage["incomplete"])
            ledger.close()

    def test_live_rollup_matches_the_written_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "request-ledger.jsonl"
            ledger = build_fixture(path)
            self.assertTrue(ledger.flush())
            live = rollup_of(ledger)
            self.assertEqual(rows_json(live), expected_rows())
            written = json.loads(rollup_path_for(path).read_text())
            self.assertEqual(without_generated_at(live), without_generated_at(written))
            # One row per day x alias x provider x model; no request identifiers.
            self.assertEqual(len(live["rows"]), 3)
            ledger.close()

    def test_daily_trend_sums_every_row_of_a_day(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "request-ledger.jsonl"
            ledger = build_fixture(path)
            self.assertTrue(ledger.flush())
            payload = json.loads(rollup_path_for(path).read_text())
            ledger.close()
            daily = payload["daily"]
            self.assertEqual([row["day"] for row in daily], [DAY_EARLY, DAY_LATE])
            self.assertEqual(daily[0], {
                "day": DAY_EARLY, "requests": 2, "input_tokens": 100,
                "output_tokens": 20, "cached_tokens": 4, "fallbacks": 1,
                "cached_tokens_unreported": 0, "requests_without_usage": 1,
                "fallback_reasons": {"other": 1},
                "errors": {"exhausted": 1}, "statuses": {"200": 1, "502": 1},
            })
            self.assertEqual(daily[1], {
                "day": DAY_LATE, "requests": 1, "input_tokens": 50,
                "output_tokens": 10, "cached_tokens": 0, "fallbacks": 0,
                "cached_tokens_unreported": 1, "requests_without_usage": 0,
                "fallback_reasons": {}, "errors": {}, "statuses": {"200": 1},
            })

    def test_cached_completeness_is_recorded_per_row(self) -> None:
        """One sibling reports cached reads, one reports none, one reports zero.

        The row keeps the sum of what was reported and counts what was not, so
        an absent cached counter can never be read back as a reported zero -
        and an explicit zero stays a known zero rather than being marked
        unknown.
        """
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "request-ledger.jsonl"
            ledger = RequestLedger(capacity=10, persistence_path=path)
            ledger.record(RequestEvent(
                "req_cached", f"{DAY_LATE}T10:00:00Z", "coding",
                serving_provider="opencode_go", serving_model="m-one", status=200,
                token_counters={"prompt_tokens": 100, "completion_tokens": 10, "cached_read_tokens": 4},
            ))
            ledger.record(RequestEvent(
                "req_silent", f"{DAY_LATE}T10:00:01Z", "coding",
                serving_provider="opencode_go", serving_model="m-one", status=200,
                token_counters={"prompt_tokens": 100, "completion_tokens": 10},
            ))
            ledger.record(RequestEvent(
                "req_zero", f"{DAY_LATE}T10:00:02Z", "coding",
                serving_provider="opencode_go", serving_model="m-one", status=200,
                token_counters={"prompt_tokens": 100, "completion_tokens": 10, "cached_read_tokens": 0},
            ))
            self.assertTrue(ledger.flush())
            payload = json.loads(rollup_path_for(path).read_text())
            ledger.close()
        rows = payload["rows"]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["requests"], 3)
        self.assertEqual(row["input_tokens"], 300)
        self.assertEqual(row["cached_tokens"], 4)
        # Only the silent sibling is unreported: the explicit zero counts as
        # reported, and the reported four are not diluted or reinterpreted.
        self.assertEqual(row["cached_tokens_unreported"], 1)
        self.assertEqual(row["requests_without_usage"], 0)
        daily = payload["daily"]
        self.assertEqual(daily[0]["cached_tokens_unreported"], 1)


class RollupBackfillTests(unittest.TestCase):
    def test_backfill_reproduces_the_live_rollup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "request-ledger.jsonl"
            ledger = build_fixture(path)
            self.assertTrue(ledger.flush())
            ledger.close()
            sidecar = rollup_path_for(path)
            live = json.loads(sidecar.read_text())

            payload = rebuild_rollups(path, now=frozen)
            self.assertEqual(payload["rows"], live["rows"])
            self.assertEqual(rows_json(payload), expected_rows())
            # Coverage must agree too, apart from the live-process bookkeeping
            # (how many rows this process appended, and which run rebuilt).
            def stable(coverage: dict[str, Any]) -> dict[str, Any]:
                return {key: value for key, value in coverage.items()
                        if key not in ("rows_appended", "rebuild")}

            self.assertEqual(stable(payload["coverage"]), stable(live["coverage"]))
            self.assertEqual(payload["coverage"]["rebuild"]["status"], "complete")
            self.assertEqual(payload["coverage"]["rebuild"]["trigger"], "manual")
            self.assertEqual(payload["coverage"]["rebuild"]["lines_read"], 4)

    def test_repeated_backfill_is_byte_identical(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "request-ledger.jsonl"
            ledger = build_fixture(path)
            self.assertTrue(ledger.flush())
            ledger.close()
            sidecar = rollup_path_for(path)
            rebuild_rollups(path, now=frozen)
            first = sidecar.read_bytes()
            rebuild_rollups(path, now=frozen)
            second = sidecar.read_bytes()
            self.assertEqual(first, second)

    def test_backfill_without_writing_touches_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "request-ledger.jsonl"
            ledger = build_fixture(path)
            self.assertTrue(ledger.flush())
            ledger.close()
            sidecar = rollup_path_for(path)
            sidecar.unlink()
            payload = rebuild_rollups(path, write=False, now=frozen)
            self.assertFalse(sidecar.exists())
            self.assertEqual(rows_json(payload), expected_rows())

    def test_boot_rebuild_aggregates_rows_written_before_this_process(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "request-ledger.jsonl"
            ledger = build_fixture(path)
            self.assertTrue(ledger.flush())
            ledger.close()
            # A new process over the same directory: history is already there.
            restarted = RequestLedger(capacity=10, persistence_path=path)
            payload = rollup_of(restarted)
            self.assertEqual(rows_json(payload), expected_rows())
            self.assertEqual(payload["coverage"]["rebuild"]["trigger"], "boot")
            self.assertEqual(payload["coverage"]["rebuild"]["lines_read"], 4)
            self.assertEqual(payload["coverage"]["rows_appended"], 0)
            restarted.close()


class RollupDurabilityTests(unittest.TestCase):
    def test_restart_and_ring_eviction_do_not_lose_aggregated_days(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "request-ledger.jsonl"
            ledger = RequestLedger(capacity=2, persistence_path=path)
            days = (DAY_OLDEST, DAY_OLDEST, DAY_EARLY, DAY_EARLY, DAY_LATE)
            for index, day in enumerate(days):
                ledger.record(RequestEvent(
                    f"req_{index}", f"{day}T0{index % 6}:00:00Z", "coding",
                    serving_provider="opencode_go", serving_model="m-one", status=200,
                    token_counters={"prompt_tokens": 10, "completion_tokens": 1},
                ))
            self.assertTrue(ledger.flush())
            ledger.close()

            restarted = RequestLedger(capacity=2, persistence_path=path)
            self.assertEqual(len(restarted.recent(limit=10)), 2)
            self.assertEqual(restarted.evicted, 3)
            payload = rollup_of(restarted)
            coverage = payload["coverage"]
            # The ring lost three rows; the rollup did not.
            self.assertEqual(coverage["requests"], 5)
            self.assertEqual(coverage["first_row_ts"], f"{DAY_OLDEST}T00:00:00Z")
            self.assertFalse(coverage["incomplete"])
            self.assertEqual([row["day"] for row in payload["rows"]],
                             [DAY_OLDEST, DAY_EARLY, DAY_LATE])
            self.assertEqual(sum(row["requests"] for row in payload["rows"]), 5)
            self.assertEqual(sum(row["input_tokens"] for row in payload["rows"]), 50)

            # The aggregated days keep their shape while new rows keep arriving.
            restarted.record(RequestEvent(
                "req_5", f"{DAY_LATE}T15:00:00Z", "coding",
                serving_provider="opencode_go", serving_model="m-one", status=200,
                token_counters={"prompt_tokens": 7, "completion_tokens": 2},
            ))
            self.assertTrue(restarted.flush())
            updated = rollup_of(restarted)
            self.assertEqual(updated["coverage"]["requests"], 6)
            self.assertEqual(sum(row["input_tokens"] for row in updated["rows"]), 57)
            self.assertEqual(json.loads(rollup_path_for(path).read_text())["coverage"]["requests"], 6)
            restarted.close()

    def test_rollup_is_unavailable_without_persistence(self) -> None:
        ledger = RequestLedger(capacity=3)
        self.assertIsNone(ledger.rollup())
        self.assertEqual(ledger.durability(),
                         {"enabled": False, "retention_days": 30, "billing_system": False})
        ledger.close()


class RollupCoverageTests(unittest.TestCase):
    def test_unrepresentable_rows_flip_the_coverage_flag(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "request-ledger.jsonl"
            lines = [
                json.dumps({"request_id": "ok-1", "timestamp": f"{DAY_EARLY}T08:00:00Z",
                            "requested_alias": "coding", "serving_provider": "p",
                            "serving_model": "m", "status": 200}),
                "{this line is not json",
                json.dumps({"request_id": "bad-ts", "timestamp": "whenever",
                            "requested_alias": "coding"}),
                json.dumps({"request_id": "legacy", "timestamp": f"{DAY_EARLY}T09:00:00Z",
                            "requested_alias": "coding", "unexpected": "field"}),
                json.dumps({"timestamp": f"{DAY_EARLY}T10:00:00Z", "requested_alias": "coding"}),
                json.dumps({"request_id": "expired", "timestamp": f"{EXPIRED_DAY}T00:00:00Z",
                            "requested_alias": "coding"}),
            ]
            path.write_text("\n".join(lines) + "\n")
            payload = rebuild_rollups(path, now=frozen)
            coverage = payload["coverage"]
            self.assertEqual(coverage["rebuild"]["lines_read"], 6)
            self.assertEqual(coverage["rows_skipped"], 4)
            self.assertEqual(coverage["rows_expired"], 1)
            self.assertEqual(coverage["requests"], 1)
            self.assertTrue(coverage["incomplete"])
            self.assertEqual(coverage["first_row_ts"], f"{DAY_EARLY}T08:00:00Z")
            self.assertEqual(len(payload["rows"]), 1)

    def test_a_clean_ledger_reports_complete_coverage_with_its_first_row(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "request-ledger.jsonl"
            path.write_text("\n".join([
                json.dumps({"request_id": "ok-1", "timestamp": f"{DAY_EARLY}T08:00:00Z",
                            "requested_alias": "coding", "serving_provider": "p",
                            "serving_model": "m", "status": 200}),
                json.dumps({"request_id": "no-alias", "timestamp": f"{DAY_EARLY}T12:00:00Z"}),
            ]) + "\n")
            payload = rebuild_rollups(path, now=frozen)
            coverage = payload["coverage"]
            self.assertFalse(coverage["incomplete"])
            self.assertEqual(coverage["rows_skipped"], 0)
            self.assertEqual(coverage["rows_expired"], 0)
            self.assertEqual(coverage["requests"], 2)
            self.assertEqual(coverage["first_row_ts"], f"{DAY_EARLY}T08:00:00Z")
            self.assertEqual(coverage["last_row_ts"], f"{DAY_EARLY}T12:00:00Z")
            # A row without dimensions is reported under an explicit bucket,
            # never folded into a real name and never silently dropped.
            self.assertEqual(payload["rows"][1]["alias"], "unknown")
            self.assertEqual(payload["rows"][1]["model"], "unknown")
            self.assertEqual(payload["rows"][1]["requests"], 1)
            self.assertEqual(payload["rows"][1]["statuses"], {"unknown": 1})

    def test_a_missing_ledger_file_is_reported_not_invented(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "request-ledger.jsonl"
            self.assertFalse(path.exists())
            payload = rebuild_rollups(path, now=frozen)
            self.assertEqual(payload["rows"], [])
            self.assertEqual(payload["daily"], [])
            self.assertFalse(payload["coverage"]["incomplete"])
            self.assertIsNone(payload["coverage"]["first_row_ts"])
            self.assertEqual(payload["coverage"]["requests"], 0)

    def test_an_unreadable_ledger_file_marks_coverage_partial(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "request-ledger.jsonl"
            valid = json.dumps({"request_id": "ok-1", "timestamp": f"{DAY_EARLY}T08:00:00Z",
                                "requested_alias": "coding", "status": 200})
            path.write_bytes(valid.encode("utf-8") + b"\n" + b"\xff\xfe not utf-8 \n")
            payload = rebuild_rollups(path, now=frozen)
            coverage = payload["coverage"]
            self.assertEqual(coverage["rebuild"]["status"], "partial")
            self.assertTrue(coverage["incomplete"])


class RollupMetadataTests(unittest.TestCase):
    def test_the_sidecar_holds_no_content_no_identifiers_and_no_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "request-ledger.jsonl"
            ledger = RequestLedger(capacity=10, persistence_path=path)
            ledger.record(RequestEvent(
                "req_private_1", f"{DAY_EARLY}T10:00:00Z", "coding",
                serving_provider="opencode_go", serving_model="m-one", status=200,
                token_counters={"prompt_tokens": 11, "completion_tokens": 3},
                skipped=[{"provider": "p", "reason": "unsupported"}],
                attempts=[{"provider": "p", "outcome": "error", "error_class": "transient"}],
            ))
            persistence = ledger._persistence
            assert persistence is not None
            persistence.submit({
                "request_id": "req_private_2", "timestamp": f"{DAY_EARLY}T11:00:00Z",
                "requested_alias": "coding", "status": 200,
                "prompt": "a private prompt", "messages": [{"role": "user", "content": "private"}],
            })
            self.assertTrue(ledger.flush())
            ledger.close()
            sidecar = rollup_path_for(path)
            data = sidecar.read_bytes()
            text = data.decode("utf-8")
            self.assertNotIn(b"prompt", data)
            self.assertNotIn(b"messages", data)
            self.assertNotIn(b"content", data)
            self.assertNotIn(b"request_id", data)
            self.assertNotIn(b"req_private_1", data)
            self.assertNotIn(b"req_private_2", data)
            self.assertNotIn(b"private prompt", data)
            self.assertEqual(scan_for_secrets(text), [])
            self.assertEqual(stat.S_IMODE(sidecar.stat().st_mode), 0o600)
            payload = json.loads(text)
            self.assertEqual(payload["coverage"]["requests"], 2)


class RollupCommandTests(unittest.TestCase):
    def test_rebuild_command_reports_and_writes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "request-ledger.jsonl"
            ledger = build_fixture(path)
            self.assertTrue(ledger.flush())
            ledger.close()
            sidecar = rollup_path_for(path)
            sidecar.unlink()

            output = io.StringIO()
            with redirect_stdout(output):
                code = main([str(path), "--dry-run"])
            self.assertEqual(code, 0)
            self.assertFalse(sidecar.exists())
            dry_run = json.loads(output.getvalue())
            self.assertEqual(dry_run["rows"], 3)
            self.assertTrue(dry_run["dry_run"])
            self.assertFalse(dry_run["coverage"]["incomplete"])

            output = io.StringIO()
            with redirect_stdout(output):
                code = main([str(path)])
            self.assertEqual(code, 0)
            self.assertTrue(sidecar.exists())
            self.assertEqual(json.loads(sidecar.read_text())["rows"], json.loads(expected_rows()))

    def test_rebuild_command_exits_one_for_incomplete_coverage_and_two_without_a_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "request-ledger.jsonl"
            path.write_text("{broken line\n")
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main([str(path)]), 1)
            payload = json.loads(rollup_path_for(path).read_text())
            self.assertTrue(payload["coverage"]["incomplete"])

            missing = Path(directory) / "absent.jsonl"
            with redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(main([str(missing)]), 2)
            self.assertIn("no request ledger", errors.getvalue())


class RollupIntegrationTests(unittest.TestCase):
    def test_a_real_fallback_request_reaches_the_rollup(self) -> None:
        """The sidecar is fed by the served request path, not only by direct ledger calls."""
        from gateway.adapters.base import AdapterError
        from gateway.config import Alias, ModelEntry, ProviderState, RouteRef
        from tests.test_end_to_end import FAKE_PROVIDER, FakeAdapter, GatewayHarness

        harness = GatewayHarness(FakeAdapter(fail_with=AdapterError("exhausted", provider_id=FAKE_PROVIDER)))
        self.addCleanup(harness.close)
        harness.registry.register(FakeAdapter(provider_id="opencode_go", dialect="openai_chat"))
        with harness.config.mutate() as config:
            config.providers["opencode_go"] = ProviderState(
                provider_id="opencode_go", kind="opencode_go", state="connected",
                models={"mimo-v2.6-flash": ModelEntry(
                    model_id="mimo-v2.6-flash", enabled=True, dialect="openai_chat",
                    source="discovered")},
            )
            config.aliases["coding"] = Alias(
                name="coding", enabled=True, primary=RouteRef(FAKE_PROVIDER, "gpt-6-luna"),
                fallbacks=[RouteRef("opencode_go", "mimo-v2.6-flash")],
                fallback_authorized=True, fallback_confirmed_providers=["opencode_go"],
                fallback_triggers=["exhausted"],
            )
        durable = harness.data_dir / "request-ledger.jsonl"
        ledger = RequestLedger(persistence_path=durable)
        self.addCleanup(ledger.close)
        harness.router._ledger = ledger
        harness.app.ledger = ledger

        status, _payload, _headers = harness.request(
            "POST", "/v1/chat/completions",
            body={"model": "coding", "messages": [{"role": "user", "content": "zqx-prompt-marker"}]},
        )
        self.assertEqual(status, 200)
        self.assertTrue(ledger.flush())

        sidecar = rollup_path_for(durable)
        text = sidecar.read_text()
        payload = json.loads(text)
        self.assertEqual(payload["coverage"]["requests"], 1)
        self.assertFalse(payload["coverage"]["incomplete"])
        row = payload["rows"][0]
        self.assertEqual(row["alias"], "coding")
        self.assertEqual(row["requests"], 1)
        self.assertEqual(row["statuses"], {"200": 1})
        self.assertEqual(row["fallbacks"], 1)
        self.assertEqual(sum(row["fallback_reasons"].values()), 1)
        # Metadata only, end to end: neither the prompt nor an identifier leaks.
        self.assertNotIn("zqx-prompt-marker", text)
        self.assertNotIn("request_id", text)
        self.assertEqual(scan_for_secrets(text), [])


def row_for(request_id: str, *, day: str, reason: str | None = None,
            tokens: dict[str, int] | None = None) -> dict[str, Any]:
    """One ledger-shaped row, optionally carrying a raw fallback reason."""
    return {
        "request_id": request_id,
        "timestamp": f"{day}T10:00:00Z",
        "requested_alias": "coding",
        "serving_provider": "p",
        "serving_model": "m",
        "status": 502 if reason else 200,
        "fallback_used": reason is not None,
        "fallback_reason": reason,
        "token_counters": tokens if tokens is not None else {"prompt_tokens": 10, "completion_tokens": 1},
    }


def store_in(directory: str) -> RollupStore:
    return RollupStore(Path(directory) / "request-ledger-rollup.json", "request-ledger.jsonl")


class RollupIndexBoundTests(unittest.TestCase):
    """Security gate t_eb1846b6 / S-1: the request index is count-bounded.

    The index maps a request id to its contribution so a later amendment can
    replace it instead of being counted twice. It used to grow one entry per
    request for the whole 30-day retention (measured 300k rows -> +187 MB
    RSS), the only runtime structure in the ledger with no count bound.
    """

    def test_the_index_stays_bounded_under_a_synthetic_flood(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = store_in(directory)
            total = INDEX_MAX_ENTRIES + 10_000
            today = datetime.now(UTC).strftime("%Y-%m-%d")
            for index in range(total):
                store.ingest(row_for(f"req_{index}", day=today), appended=True)
            payload = store.snapshot()
            coverage = payload["coverage"]
            self.assertEqual(coverage["index_entries"], INDEX_MAX_ENTRIES)
            self.assertEqual(coverage["index_max_entries"], INDEX_MAX_ENTRIES)
            self.assertEqual(coverage["index_evicted"], total - INDEX_MAX_ENTRIES)
            # The cap evicts index entries, never counted requests: every
            # request's contribution stays in its bucket and in the totals.
            self.assertEqual(coverage["requests"], total)
            self.assertEqual(sum(row["requests"] for row in payload["rows"]), total)
            # The remembered amendment window is bounded by the same cap.
            self.assertLessEqual(len(store._evicted_ids), INDEX_MAX_ENTRIES)

    def test_a_late_amendment_for_an_evicted_request_is_dropped_and_reported(self) -> None:
        """An evicted id must never be counted a second time (S-1 decision)."""
        with tempfile.TemporaryDirectory() as directory:
            store = store_in(directory)
            today = datetime.now(UTC).strftime("%Y-%m-%d")
            early = (datetime.now(UTC) - timedelta(days=2)).strftime("%Y-%m-%d")
            original = {"prompt_tokens": 10, "completion_tokens": 2}
            store.ingest(row_for("req_early", day=early, tokens=dict(original)), appended=True)
            for index in range(INDEX_MAX_ENTRIES + 10):
                store.ingest(row_for(f"req_{index}", day=today), appended=True)
            coverage = store.snapshot()["coverage"]
            self.assertGreater(coverage["index_evicted"], 0)
            total = INDEX_MAX_ENTRIES + 11
            self.assertEqual(coverage["requests"], total)

            # The amendment arrives anyway (a direct store write, or a ring
            # that outlives the cap): the original contribution stands, the
            # late row is dropped, and coverage says so instead of silently
            # double-counting one request.
            late = row_for("req_early", day=early,
                           tokens={"prompt_tokens": 99, "completion_tokens": 9})
            self.assertFalse(store.ingest(late, appended=True))
            payload = store.snapshot()
            coverage = payload["coverage"]
            self.assertEqual(coverage["index_amendments_dropped"], 1)
            self.assertEqual(coverage["requests"], total)
            early_rows = [row for row in payload["rows"] if row["day"] == early]
            self.assertEqual(len(early_rows), 1)
            self.assertEqual(early_rows[0]["requests"], 1)
            self.assertEqual(early_rows[0]["input_tokens"], original["prompt_tokens"])
            self.assertEqual(early_rows[0]["output_tokens"], original["completion_tokens"])

    def test_the_cap_stays_wider_than_the_ring_that_gates_amendments(self) -> None:
        """The product invariant behind the bound.

        ``RequestLedger.amend`` only writes an amendment row while the event
        is still in the bounded ring, so an id can only be amended within the
        ring window. The index cap is at least twice that window and evicts
        oldest-first by last touch, so the product can never amend an entry it
        has already evicted.
        """
        self.assertGreaterEqual(INDEX_MAX_ENTRIES, 2 * DEFAULT_LEDGER_CAPACITY)


class RollupReasonVocabularyTests(unittest.TestCase):
    """Security gate t_eb1846b6 / S-2: reasons fold to the closed vocabulary at ingest.

    A recorded reason embeds a per-second cooldown counter, a climbing
    failure count and a provider-supplied retry-after, so raw strings are
    effectively unique per request. Counting them verbatim opened one bucket
    per request and rewrote an ever-growing sidecar from the writer thread.
    """

    def test_distinct_reasons_fold_to_the_closed_vocabulary_with_counts_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = store_in(directory)
            today = datetime.now(UTC).strftime("%Y-%m-%d")
            cooldown = 3_000
            ineligible = 2_000
            unmatched = 1_000
            unclassified = 1
            for index in range(cooldown):
                store.ingest(row_for(f"req_c{index}", day=today, reason=(
                    f"the provider p is in a local cooldown for {index}s "
                    f"({index} consecutive failures); this route is held for {index}s"
                )), appended=True)
            for index in range(ineligible):
                store.ingest(row_for(f"req_i{index}", day=today, reason=(
                    f"the primary route was skipped: route {index} not eligible for alias coding"
                )), appended=True)
            for index in range(unmatched):
                store.ingest(row_for(f"req_o{index}", day=today, reason=(
                    f"something went sideways in attempt {index}"
                )), appended=True)
            # A fallback with no recorded reason at all: reported as
            # ``unknown``, never as an absent bucket.
            silent = row_for("req_u", day=today)
            silent["fallback_used"] = True
            silent["fallback_reason"] = None
            store.ingest(silent, appended=True)

            payload = store.snapshot()
            bucket = payload["rows"][0]
            counts = bucket["fallback_reasons"]
            # Bounded by the vocabulary, never by the number of requests...
            self.assertLessEqual(len(counts), len(FALLBACK_LABELS))
            self.assertEqual(set(counts), {"cooldown", "route_ineligible", "other", "unknown"})
            # ...with every event counted exactly once.
            total = cooldown + ineligible + unmatched + unclassified
            self.assertEqual(sum(counts.values()), total)
            self.assertEqual(bucket["fallbacks"], total)
            self.assertEqual(payload["coverage"]["requests"], total)

    def test_the_sidecar_stays_small_under_a_reason_flood(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sidecar = Path(directory) / "request-ledger-rollup.json"
            store = store_in(directory)
            today = datetime.now(UTC).strftime("%Y-%m-%d")
            total = 6_000
            for index in range(total):
                store.ingest(row_for(f"req_{index}", day=today, reason=(
                    f"the provider p is in a local cooldown for {index}s "
                    f"({index} consecutive failures); this route is held for {index}s"
                )), appended=True)
            payload = store.save()
            assert payload is not None, "an in-memory save must produce a payload"
            # 6,000 distinct raw reasons used to serialise ~1.3-2.5 MB and
            # rewrite the whole file from the ledger writer thread.
            size = sidecar.stat().st_size
            self.assertLess(size, 64 * 1024, f"sidecar grew to {size} bytes")
            counts = payload["rows"][0]["fallback_reasons"]
            self.assertLessEqual(len(counts), len(FALLBACK_LABELS))
            self.assertEqual(sum(counts.values()), total)


if __name__ == "__main__":
    unittest.main()
