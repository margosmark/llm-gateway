"""The chatgpt_codex account-quota publish path (parent t_8b93bb78).

Five properties, each read back from a gateway running over loopback:

1. the ``account_quota`` block on the ``chatgpt_codex`` provider-health
   row matches a direct read of the same usage payload at the same
   minute - labels, raw ``used_percent``, ``reset_at`` to the second;
2. a missing credential and a 401 publish ``unavailable_reason`` with
   ``windows: null`` - no exception, no zeroed percentage, no invented
   reset;
3. the read is read-only and bounded: one ``GET`` of the published usage
   URL with a deadline, never a token refresh;
4. no ``/v1`` inference request triggers it, and a state poll inside the
   60s TTL costs exactly one read;
5. nothing identity-bearing survives into the serialized state.
"""

from __future__ import annotations

import dataclasses
import json
import re
import unittest
from datetime import datetime

from gateway.adapters.chatgpt_codex import (
    REASON_CREDENTIAL_MISSING,
    REASON_TIMEOUT,
    USAGE_URL,
    ChatGPTCodexAdapter,
    parse_account_usage,
)
from gateway.adapters.http_client import UpstreamResponse
from gateway.config import Alias, ModelEntry, RouteRef
from gateway.observability import AccountQuotaStore
from tests.test_end_to_end import GatewayHarness

#: The allowlist is a construction rule, so the fixture *carries* the
#: identity fields the real endpoint publishes: if any of them could
#: reach state, this pattern finds it there.
FORBIDDEN = re.compile(r"email|user_id|access_token|Bearer |account_id")

#: Shaped after the first-hand read of the live endpoint (parent
#: t_8b93bb78): two windows, plan ``plus``, balance ``"0"``, and the
#: reset-credit counters exactly as the container saw them - two
#: confirmed zeros with **no** purchase-eligibility key at all, which is
#: why that field must publish ``null``.
USAGE_PAYLOAD: dict = {
    "rate_limit": {
        "allowed": False,
        "limit_reached": True,
        "primary_window": {
            "limit_window_seconds": 18000,
            "reset_after_seconds": 18000,
            "reset_at": 1794173716,
            "used_percent": 0,
        },
        "secondary_window": {
            "limit_window_seconds": 604800,
            "reset_after_seconds": 154322,
            "reset_at": 1794310037,
            "used_percent": 100,
        },
    },
    "credits": {"balance": "0", "has_credits": False, "unlimited": False},
    "plan_type": "plus",
    "rate_limit_reset_credits": {"available_count": 0, "applicable_available_count": 0},
    # Identity, promo and spend material: present in the real body,
    # never publishable.
    "email": "fixture-owner@example.invalid",
    "user_id": "user_fixture_000000",
    "account_id": "acct_fixture_000000",
    "promo": None,
    "spend_control": {"enabled": False},
    "model_usage": {"gpt-6-luna": {"num_styled_requests": 0}},
}
USAGE_BODY = json.dumps(USAGE_PAYLOAD).encode("utf-8")


class _Clock:
    """A monotonic clock the test advances by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class UsageStub:
    """The HTTP layer behind the read: it records what was asked, never
    what was carried - header *values* are not stored, only which
    headers were present, so the recorder itself cannot smuggle token
    material into a failure message.
    """

    def __init__(
        self,
        status: int = 200,
        body: bytes = USAGE_BODY,
        fail: BaseException | None = None,
    ) -> None:
        self.status = status
        self.body = body
        self.fail = fail
        self.calls: list[dict] = []

    def request(self, method, url, headers=None, body=None, stream=False, provider_id="", **kwargs):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "authorization": bool((headers or {}).get("Authorization")),
                "account_header": "ChatGPT-Account-ID" in (headers or {}),
                "retry_safe": kwargs.get("retry_safe"),
                "deadline": kwargs.get("deadline"),
            }
        )
        if self.fail is not None:
            raise self.fail
        return UpstreamResponse(self.status, {}, self.body, None, "application/json")


def _harness(
    *,
    status: int = 200,
    body: bytes = USAGE_BODY,
    fail: BaseException | None = None,
    credential: bool = True,
) -> tuple[GatewayHarness, UsageStub, _Clock]:
    """A harness whose chatgpt_codex adapter reads through ``UsageStub``.

    ``credential=False`` leaves the credential store empty while still
    registering the adapter, so "no credential" is proven to short-circuit
    before any outbound request rather than merely never being attempted.
    """
    harness = GatewayHarness()
    stub = UsageStub(status=status, body=body, fail=fail)
    clock = _Clock()
    # The quota memo gets the test's clock: the 60s TTL is asserted by
    # advancing it, never by sleeping.
    harness.app.account_quota = AccountQuotaStore(clock=clock)
    handle = harness.credentials.provider_handle("chatgpt_codex")
    if credential:
        # Synthetic values only - this is a fixture credential, and the
        # payload assertions below forbid it from reaching state anyway.
        handle.write(
            {
                "access_token": "synthetic-usage-token",
                "account_id": "acct_synthetic_fixture",
            }
        )
    harness.registry.register(
        ChatGPTCodexAdapter(harness.credentials.provider_handle("chatgpt_codex"), client=stub)
    )
    # An alias on a *different* provider, so a completion never depends
    # on the usage path being harmless.
    with harness.config.mutate() as snapshot:
        snapshot.providers["opencode_go"].models["m-1"] = ModelEntry(
            model_id="m-1", enabled=True, dialect="openai_chat", source="discovered"
        )
        snapshot.aliases["coding"] = Alias(
            name="coding", enabled=True, primary=RouteRef("opencode_go", "m-1")
        )
    return harness, stub, clock


def _quota(payload: dict) -> dict:
    """The account_quota block, or fail loudly rather than skip."""
    rows = {row["provider_id"]: row for row in payload["provider_health"]}
    if "chatgpt_codex" not in rows:
        raise AssertionError(f"no chatgpt_codex row in {sorted(rows)}")
    block = rows["chatgpt_codex"].get("account_quota")
    if block is None:
        raise AssertionError("the chatgpt_codex row carries no account_quota block")
    return block


def _state(harness: GatewayHarness) -> tuple[int, dict]:
    status, payload, _ = harness.request("GET", "/owner/api/state")
    if status != 200 or not isinstance(payload, dict):
        raise AssertionError(f"state render failed: {status} {payload}")
    return status, payload


class AccountQuotaStateTests(unittest.TestCase):
    # --- AC1: the block matches a direct read at the same minute -------

    def test_the_state_block_matches_a_direct_usage_read_at_the_same_minute(self) -> None:
        harness, _stub, _clock = _harness()
        try:
            read_at = datetime.now().astimezone()
            _status, payload = _state(harness)
            block = _quota(payload)

            # A direct read of the same payload, compared key by key
            # against the raw endpoint values - no parser in between.
            direct = json.loads(USAGE_BODY)
            self.assertEqual(block["plan_type"], direct["plan_type"])
            self.assertIsNone(block["unavailable_reason"])
            self.assertEqual(block["confidence"], "high")
            self.assertEqual(block["source"], "account_usage_api")

            windows = {window["label"]: window for window in block["windows"]}
            self.assertEqual(sorted(windows), ["Session", "Weekly"])
            for key, label in (("primary_window", "Session"), ("secondary_window", "Weekly")):
                source = direct["rate_limit"][key]
                self.assertEqual(windows[label]["used_percent"], source["used_percent"])
                reset = datetime.fromisoformat(windows[label]["reset_at"])
                # ISO-8601 with an offset, never a naive stamp.
                self.assertIsNotNone(reset.tzinfo)
                self.assertEqual(int(reset.timestamp()), source["reset_at"])

            observed = datetime.fromisoformat(block["observed_at"])
            self.assertIsNotNone(observed.tzinfo)
            self.assertLess(
                abs((observed - read_at).total_seconds()),
                60,
                "observed_at must be stamped at the read, within the same minute",
            )
            # Raw values from the same read: the balance as the payload
            # reported it (a string stays a string).
            self.assertEqual(block["credits"], {"balance": direct["credits"]["balance"]})
        finally:
            harness.close()

    # --- AC2: unavailable shapes --------------------------------------

    def test_a_missing_credential_publishes_reason_and_null_windows_without_a_read(self) -> None:
        harness, stub, _clock = _harness(credential=False)
        try:
            _status, payload = _state(harness)
            block = _quota(payload)
            self.assertEqual(block["unavailable_reason"], REASON_CREDENTIAL_MISSING)
            self.assertIsNone(block["windows"])
            self.assertIsNone(block["plan_type"])
            self.assertIsNone(block["credits"])
            self.assertEqual(block["confidence"], "none")
            self.assertEqual(block["source"], "account_usage_api")
            self.assertIsNotNone(block["observed_at"])
            # Present in the credentialless case too - one schema.
            for key in ("resets_banked", "resets_banked_applicable", "resets_purchase_eligible"):
                self.assertIn(key, block)
                self.assertIsNone(block[key])
            # The short-circuit is proven by the recorder, not by hope:
            # the adapter was registered, and it still never dialled out.
            self.assertEqual(stub.calls, [])
            # And never a zeroed percentage dressed up as a measurement.
            self.assertNotIn("used_percent", json.dumps(block))
        finally:
            harness.close()

    def test_a_401_is_reported_and_leaves_the_credential_alone(self) -> None:
        harness, stub, _clock = _harness(status=401)
        try:
            before = harness.credentials.describe("chatgpt_codex")
            _status, payload = _state(harness)
            block = _quota(payload)
            self.assertEqual(block["unavailable_reason"], "http_401")
            self.assertIsNone(block["windows"])
            self.assertIsNone(block["plan_type"])
            self.assertEqual(block["confidence"], "none")
            self.assertNotIn("used_percent", json.dumps(block))
            # Exactly one outbound call, the usage GET itself: no token
            # refresh, no rotation, nothing that writes the store.
            self.assertEqual(len(stub.calls), 1)
            call = stub.calls[0]
            self.assertEqual(call["method"], "GET")
            self.assertEqual(call["url"], USAGE_URL)
            self.assertTrue(call["authorization"])
            self.assertTrue(call["account_header"])
            self.assertFalse(call["retry_safe"])
            self.assertIsInstance(call["deadline"], float)
            self.assertEqual(
                harness.credentials.describe("chatgpt_codex"), before, "a 401 must not rewrite the credential"
            )
        finally:
            harness.close()

    def test_a_stalled_read_is_reported_as_a_timeout_rather_than_holding_the_poll(self) -> None:
        harness, stub, _clock = _harness(fail=TimeoutError("synthetic stall"))
        try:
            _status, payload = _state(harness)
            block = _quota(payload)
            self.assertEqual(block["unavailable_reason"], REASON_TIMEOUT)
            self.assertIsNone(block["windows"])
            self.assertIsNone(block["plan_type"])
            self.assertEqual(len(stub.calls), 1)
        finally:
            harness.close()

    def test_an_unusable_payload_is_unavailable_not_a_list_of_zeroes(self) -> None:
        # A body the endpoint answered with, but which publishes no
        # window at all: the block must say so rather than render an
        # empty-but-confident list.
        body = json.dumps(
            {
                "rate_limit": {"primary_window": {"limit_window_seconds": 18000}},
                "plan_type": "plus",
                "email": "fixture-owner@example.invalid",
            }
        ).encode("utf-8")
        harness, _stub, _clock = _harness(body=body)
        try:
            _status, payload = _state(harness)
            block = _quota(payload)
            self.assertEqual(block["unavailable_reason"], "empty_usage_payload")
            self.assertIsNone(block["windows"])
            self.assertNotIn("used_percent", json.dumps(block))
        finally:
            harness.close()

    # --- AC4: cadence, not per-request --------------------------------

    def test_no_inference_request_triggers_the_usage_read(self) -> None:
        harness, stub, _clock = _harness()
        try:
            status, _payload, _headers = harness.request(
                "POST",
                "/v1/chat/completions",
                body={
                    "model": "coding",
                    "messages": [{"role": "user", "content": "hello"}],
                },
            )
            self.assertEqual(status, 200, "the control completion must succeed")
            self.assertEqual(
                stub.calls, [], "a chat completion must not trigger the account-usage fetch"
            )
            # Positive control: the state poll does fetch, exactly once.
            _status, payload = _state(harness)
            self.assertEqual(len(stub.calls), 1)
            self.assertIsNone(_quota(payload)["unavailable_reason"])
        finally:
            harness.close()

    def test_a_state_poll_inside_the_ttl_costs_exactly_one_read(self) -> None:
        harness, stub, clock = _harness()
        try:
            _status, _payload = _state(harness)
            self.assertEqual(len(stub.calls), 1)

            # A second render inside the 60s window - bypassing only the
            # response cache, which is a different layer - must not spend
            # another upstream read.
            harness.app._state_cache.clear()
            _status, _payload = _state(harness)
            self.assertEqual(len(stub.calls), 1, "a poll inside the TTL must not refetch")

            # Past the TTL the memo expires and the read happens again.
            clock.now += 61
            harness.app._state_cache.clear()
            _status, _payload = _state(harness)
            self.assertEqual(len(stub.calls), 2, "past the TTL the read must refresh")

            # A credential change invalidates immediately: the memo never
            # masks an enrolment or a disconnect behind its own window.
            harness.credentials.provider_handle("chatgpt_codex").write(
                {"access_token": "synthetic-usage-token-rotated"}
            )
            harness.app._state_cache.clear()
            _status, _payload = _state(harness)
            self.assertEqual(len(stub.calls), 3, "a credential change must re-read at once")
        finally:
            harness.close()

    # --- AC5: metadata only -------------------------------------------

    def test_the_serialized_state_carries_no_identity_or_credential_material(self) -> None:
        harness, _stub, _clock = _harness()
        try:
            _status, payload = _state(harness)
            serialized = json.dumps(payload)
            hits = FORBIDDEN.findall(serialized)
            self.assertEqual(hits, [], f"identity or credential material leaked into state: {hits}")
            # Positive control: the pattern does match the payload the
            # fixture was handed, so the zero above is a real result and
            # not a pattern that matches nothing.
            raw = json.dumps(USAGE_PAYLOAD)
            self.assertTrue(FORBIDDEN.findall(raw), "the FORBIDDEN pattern must be live")
            # And the block itself is the whole of what the read reaches.
            block = _quota(payload)
            self.assertEqual(
                sorted(block),
                [
                    "confidence",
                    "credits",
                    "observed_at",
                    "plan_type",
                    "resets_banked",
                    "resets_banked_applicable",
                    "resets_purchase_eligible",
                    "source",
                    "unavailable_reason",
                    "windows",
                ],
            )
        finally:
            harness.close()

    def test_the_block_is_published_on_the_chatgpt_codex_row_only(self) -> None:
        harness, _stub, _clock = _harness()
        try:
            _status, payload = _state(harness)
            carriers = [
                row["provider_id"]
                for row in payload["provider_health"]
                if "account_quota" in row
            ]
            self.assertEqual(carriers, ["chatgpt_codex"])
            # The rest of the row is untouched: existing consumers keep
            # reading the same keys they read before.
            row = next(r for r in payload["provider_health"] if r["provider_id"] == "chatgpt_codex")
            self.assertEqual(row["provider_id"], "chatgpt_codex")
            self.assertIn("connection_state", row)
            self.assertIn("model_count", row)
            self.assertIn("recent_requests", row)
        finally:
            harness.close()

    # --- parsing rules -------------------------------------------------

    def test_window_labels_follow_published_durations(self) -> None:
        parsed = parse_account_usage(USAGE_PAYLOAD)
        self.assertEqual([window.label for window in parsed.windows], ["Session", "Weekly"])
        self.assertEqual(parsed.plan_type, "plus")
        self.assertEqual(parsed.credits_balance, "0")
        self.assertEqual(parsed.resets_banked, 0)
        self.assertEqual(parsed.resets_banked_applicable, 0)

    def test_a_duration_less_secondary_window_is_never_labelled_session(self) -> None:
        # The Hermes #65387 bug class: only a secondary window, and no
        # duration to name it with. Positional fallback names it Weekly -
        # never Session.
        parsed = parse_account_usage(
            {
                "rate_limit": {
                    "secondary_window": {"reset_at": 1794310037, "used_percent": 100},
                },
                "plan_type": "plus",
            }
        )
        self.assertEqual([window.label for window in parsed.windows], ["Weekly"])

    def test_a_payload_publishing_no_window_at_all_is_unavailable(self) -> None:
        parsed = parse_account_usage({"rate_limit": {}, "plan_type": "plus"})
        self.assertEqual(parsed.reason, "empty_usage_payload")
        self.assertEqual(parsed.windows, ())
        self.assertIsNone(parsed.plan_type)

    def test_reset_counters_publish_null_when_the_payload_reports_nothing(self) -> None:
        # The live shape today: two confirmed zeros, no eligibility key.
        reported = parse_account_usage(USAGE_PAYLOAD)
        self.assertEqual(reported.resets_banked, 0)
        self.assertEqual(reported.resets_banked_applicable, 0)
        self.assertIsNone(reported.resets_purchase_eligible)

        # The key present but false: false is published, not null.
        with_eligibility = dict(USAGE_PAYLOAD)
        with_eligibility["rate_limit_reset_credits"] = {
            "available_count": 3,
            "applicable_available_count": 2,
            "immediate_reset_purchase_eligible": False,
        }
        published = parse_account_usage(with_eligibility)
        self.assertEqual(published.resets_banked, 3)
        self.assertEqual(published.resets_banked_applicable, 2)
        self.assertIs(published.resets_purchase_eligible, False)

        # No counters object at all: all three null, never zeroed.
        absent = dict(USAGE_PAYLOAD)
        absent.pop("rate_limit_reset_credits")
        empty = parse_account_usage(absent)
        self.assertIsNone(empty.resets_banked)
        self.assertIsNone(empty.resets_banked_applicable)
        self.assertIsNone(empty.resets_purchase_eligible)

    def test_the_parse_reads_nothing_beyond_the_allowlist(self) -> None:
        # Every field the fixture carries that must not travel: the
        # parsed object simply has nowhere to put it.
        parsed = dataclasses.asdict(parse_account_usage(USAGE_PAYLOAD))
        self.assertEqual(
            sorted(parsed),
            [
                "credits_balance",
                "plan_type",
                "reason",
                "resets_banked",
                "resets_banked_applicable",
                "resets_purchase_eligible",
                "windows",
            ],
        )
        serialized = json.dumps(parsed)
        self.assertEqual(FORBIDDEN.findall(serialized), [])
        for leaked in ("fixture-owner@example.invalid", "user_fixture_000000", "acct_fixture_000000"):
            self.assertNotIn(leaked, serialized)

    def test_the_ttl_store_runs_the_read_once_per_window(self) -> None:
        clock = _Clock()
        store = AccountQuotaStore(clock=clock)
        runs: list[str] = []

        def build() -> dict:
            runs.append("read")
            return {"windows": None}

        store.snapshot(build, invalidate="present")
        store.snapshot(build, invalidate="present")
        self.assertEqual(runs, ["read"], "the memo must serve the second call")

        clock.now += 61
        store.snapshot(build, invalidate="present")
        self.assertEqual(len(runs), 2, "past the TTL the read must run again")

        # A changed credential identity invalidates at once, even inside
        # the window.
        store.snapshot(build, invalidate="rotated")
        self.assertEqual(len(runs), 3, "a credential change must re-read immediately")

        # The published block is a copy: a consumer cannot mutate the memo.
        cached = store.snapshot(build, invalidate="rotated")
        self.assertIsNotNone(cached)
        cached["windows"] = "corrupted"
        again = store.snapshot(build, invalidate="rotated")
        self.assertIsNotNone(again)
        self.assertNotEqual(again["windows"], "corrupted")


if __name__ == "__main__":
    unittest.main()
