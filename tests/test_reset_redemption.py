"""Owner-triggered reset redemption (card t_571135f9).

The guarded ``Redeem`` action: read usage, refuse below full exhaustion,
spend exactly one banked rate-limit reset credit, map the server's own
outcome. Every property is read back from a gateway running over
loopback with a scripted HTTP fixture - no test in this file can reach a
live endpoint (the fixture adapter is constructed with an explicit
loopback base and a stub client, and the source below carries no live
Codex URL):

1. **guard** - a window at 40% ends the request before any POST, with
   the reason string, and the spy proves zero consume calls (AC1);
2. **outcomes** - ``reset`` / ``nothing_to_reset`` / ``no_credit`` /
   ``already_redeemed`` each map to their own message and status, and
   ``reset`` re-reads usage, primes the quota memo and serves the
   restored windows on the next state render (AC2);
3. **failures** - an unreadable usage, an HTTP failure, an unrecognised
   outcome: reported, never retried, never echoed, credential untouched;
4. **never automatic** - state polls never POST even with credits banked
   and windows exhausted, and the consume call is reachable from exactly
   one source file (the adapter's own method and its one caller);
5. **audit** - who/when/outcome on disk, mode 0600, surviving a restart,
   with zero identity or token material in it (AC5);
6. **machine keys** - refused with the action's own message before any
   outbound request.
"""

from __future__ import annotations

import json
import re
import stat
import unittest
import uuid
from pathlib import Path

from gateway.adapters.chatgpt_codex import (
    CHATGPT_BASE,
    RESET_CREDITS_CONSUME_PATH,
    USAGE_URL,
    ChatGPTCodexAdapter,
    codex_backend_prefix,
    usage_url_for,
)
from gateway.adapters.http_client import UpstreamResponse
from gateway.observability import REDEMPTION_AUDIT_FILE, RedemptionAudit
from tests.test_end_to_end import GatewayHarness

#: The identity fields the real usage payload publishes: the fixture
#: *carries* them so a leak into a response or an audit record is
#: provable, not assumed (positive control below).
FORBIDDEN = re.compile(r"email|user_id|access_token|Bearer |account_id")

#: A loopback base for the fixture adapter. Nothing in this file dials
#: anywhere real: the adapter is constructed with ``client=stub`` and
#: this base, so every URL it builds stays on 127.0.0.1.
LOOPBACK_BASE = "http://127.0.0.1:9/codex"

REDEEM_BODY = {"action": "redeem_reset", "provider_id": "chatgpt_codex"}


def usage_payload(
    *, banked: int, primary_used: float, secondary_used: float
) -> dict:
    """A shaped usage body carrying the identity fields on purpose."""
    return {
        "rate_limit": {
            "allowed": secondary_used < 100 and primary_used < 100,
            "limit_reached": primary_used >= 100 or secondary_used >= 100,
            "primary_window": {
                "limit_window_seconds": 18000,
                "reset_after_seconds": 9000,
                "reset_at": 1794173716,
                "used_percent": primary_used,
            },
            "secondary_window": {
                "limit_window_seconds": 604800,
                "reset_after_seconds": 500000,
                "reset_at": 1794310037,
                "used_percent": secondary_used,
            },
        },
        "credits": {"balance": "0", "has_credits": False},
        "plan_type": "plus",
        "rate_limit_reset_credits": {
            "available_count": banked,
            "applicable_available_count": banked,
            "immediate_reset_purchase_eligible": False,
        },
        # Identity material, present in the real body, never publishable.
        "email": "fixture-redeem@example.invalid",
        "user_id": "user_fixture_redeem",
        "account_id": "acct_fixture_redeem",
    }


class RedeemStub:
    """The HTTP layer behind guard + consume: ordered usage reads answered
    from a queue (the last answer repeats), one scripted consume POST.

    Records every call - method, URL, the outgoing body, header
    *presence* only - so the AC1 spy proves a POST never happened and the
    request-id contract is asserted without storing a header value.
    """

    def __init__(
        self,
        usage_bodies: list[bytes],
        *,
        usage_status: int = 200,
        consume_status: int = 200,
        consume_body: bytes = b'{"code": "reset", "windows_reset": 2}',
        consume_fail: BaseException | None = None,
    ) -> None:
        self._usage_bodies = list(usage_bodies)
        self._usage_status = usage_status
        self._consume_status = consume_status
        self._consume_body = consume_body
        self._consume_fail = consume_fail
        self.calls: list[dict] = []

    @property
    def usage_calls(self) -> list[dict]:
        return [call for call in self.calls if call["method"] == "GET"]

    @property
    def consume_calls(self) -> list[dict]:
        return [call for call in self.calls if call["method"] == "POST"]

    def request(self, method, url, headers=None, body=None, stream=False, provider_id="", **kwargs):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "body": body,
                "authorization": bool((headers or {}).get("Authorization")),
                "content_type": (headers or {}).get("Content-Type"),
                "retry_safe": kwargs.get("retry_safe"),
                "deadline": kwargs.get("deadline"),
            }
        )
        if method == "POST":
            if self._consume_fail is not None:
                raise self._consume_fail
            return UpstreamResponse(
                self._consume_status, {}, self._consume_body, None, "application/json"
            )
        payload = self._usage_bodies.pop(0) if len(self._usage_bodies) > 1 else self._usage_bodies[0]
        if self._usage_status != 200:
            return UpstreamResponse(self._usage_status, {}, b"{}", None, "application/json")
        return UpstreamResponse(200, {}, payload, None, "application/json")


def _harness(
    usage_bodies: list[bytes],
    *,
    credential: bool = True,
    **stub_kwargs,
) -> tuple[GatewayHarness, RedeemStub]:
    """A harness whose chatgpt_codex adapter speaks only to ``RedeemStub``.

    The adapter is registered with an explicit loopback base and a stub
    client: its usage read and its consume POST are both built under
    127.0.0.1 and answered in-process. The credential is synthetic and
    only ever read as a header *presence* by the stub.
    """
    harness = GatewayHarness()
    stub = RedeemStub(usage_bodies, **stub_kwargs)
    if credential:
        harness.credentials.provider_handle("chatgpt_codex").write(
            {
                "access_token": "synthetic-redeem-token",
                "account_id": "acct_synthetic_fixture",
            }
        )
    harness.registry.register(
        ChatGPTCodexAdapter(
            harness.credentials.provider_handle("chatgpt_codex"),
            client=stub,
            chatgpt_base=LOOPBACK_BASE,
        )
    )
    return harness, stub


def _redeem(harness: GatewayHarness, body: dict | None = None):
    return harness.request("POST", "/owner/api/provider", body or REDEEM_BODY)


class _ResetRedemptionBase(unittest.TestCase):
    def setUp(self) -> None:
        self._harnesses: list[GatewayHarness] = []

    def tearDown(self) -> None:
        for harness in self._harnesses:
            harness.close()

    def harness(self, usage_bodies: list[bytes], **kwargs) -> tuple[GatewayHarness, RedeemStub]:
        harness, stub = _harness(usage_bodies, **kwargs)
        self._harnesses.append(harness)
        return harness, stub

    def audit_records(self, harness: GatewayHarness) -> list[dict]:
        return harness.app.redemption_audit.recent()


class GuardTests(_ResetRedemptionBase):
    # --- AC1: the guard refuses before any POST --------------------------

    def test_a_40_percent_window_never_issues_the_consume_post(self) -> None:
        # A credit is banked (so only the window guard can refuse), yet a
        # 40% week must not spend a reset that restores FULL limits.
        payload = usage_payload(banked=1, primary_used=0, secondary_used=40)
        harness, stub = self.harness([json.dumps(payload).encode("utf-8")])

        status, body, _ = _redeem(harness)

        self.assertEqual(status, 409, body)
        self.assertEqual(body["error"]["type"], "redeem_not_exhausted")
        message = body["error"]["message"]
        self.assertIn("a reset restores your FULL 5h + weekly limits", message)
        self.assertIn("40% would waste it", message)
        # The spy's whole point: guard first, consume never.
        self.assertEqual(stub.consume_calls, [], "no POST may leave the gateway")
        self.assertEqual(len(stub.usage_calls), 1, "exactly one fresh read guards the action")
        self.assertEqual(stub.usage_calls[0]["method"], "GET")
        record = self.audit_records(harness)[0]
        self.assertEqual(record["outcome"], "not_exhausted")

    def test_a_401_usage_read_blocks_the_redeem_without_a_post(self) -> None:
        harness, stub = self.harness(
            [b"{}"], usage_status=401, credential=True
        )

        status, body, _ = _redeem(harness)

        self.assertEqual(status, 409, body)
        self.assertEqual(body["error"]["type"], "redeem_unavailable")
        self.assertIn("http_401", body["error"]["message"])
        self.assertEqual(stub.consume_calls, [], "an unconfirmable guard never POSTs")
        self.assertEqual(self.audit_records(harness)[0]["outcome"], "unavailable")

    def test_a_payload_without_any_publishable_window_is_refused(self) -> None:
        # The parser only ever returns reason=None with at least one
        # publishable window; a body that publishes none is reported
        # unavailable, so exhaustion cannot be confirmed and nothing is
        # spent (the handler's own no-window branch is belt and braces).
        payload = usage_payload(banked=1, primary_used=0, secondary_used=0)
        payload["rate_limit"] = {}
        harness, stub = self.harness([json.dumps(payload).encode("utf-8")])

        status, body, _ = _redeem(harness)

        self.assertEqual(status, 409, body)
        self.assertEqual(body["error"]["type"], "redeem_unavailable")
        self.assertIn("empty_usage_payload", body["error"]["message"])
        self.assertEqual(stub.consume_calls, [])
        self.assertEqual(self.audit_records(harness)[0]["outcome"], "unavailable")


class ConsumeContractTests(_ResetRedemptionBase):
    # --- the one POST: shape, order, request id, loopback ----------------

    def test_an_exhausted_window_spends_exactly_one_credit_with_a_fresh_request_id(self) -> None:
        payload = usage_payload(banked=1, primary_used=100, secondary_used=100)
        harness, stub = self.harness([json.dumps(payload).encode("utf-8")])

        status, body, _ = _redeem(harness)

        self.assertEqual(status, 200, body)
        self.assertTrue(body["ok"])
        self.assertEqual(body["outcome"], "reset")
        self.assertEqual(body["windows_reset"], 2)
        # Guard read first, consume second, then the post-reset re-read:
        # that order is the guard, and the third call is the fresh read
        # that feeds the primed quota block.
        self.assertEqual(
            [call["method"] for call in stub.calls], ["GET", "POST", "GET"]
        )
        consume = stub.consume_calls[0]
        # Loopback only: the whole flow stays on the fixture origin.
        self.assertTrue(
            consume["url"].startswith("http://127.0.0.1"),
            f"consume URL left the fixture origin: {consume['url']}",
        )
        self.assertTrue(consume["url"].endswith(RESET_CREDITS_CONSUME_PATH))
        self.assertEqual(consume["retry_safe"], False, "a spend is never retry-safe")
        self.assertIsNotNone(consume["deadline"], "the POST shares the read's budget")
        sent = json.loads(consume["body"].decode("utf-8"))
        self.assertEqual(list(sent), ["redeem_request_id"], "no credit_id, nothing else")
        request_id = uuid.UUID(sent["redeem_request_id"])
        self.assertEqual(request_id.version, 4, "a fresh uuid4 per attempt")

    def test_each_consume_outcome_maps_to_its_own_message(self) -> None:
        expectations = (
            ("reset", "Reset redeemed — your usage limits have been reset."),
            ("nothing_to_reset", "Limits not exhausted — credit NOT spent."),
            ("no_credit", "No credit available — nothing was redeemed."),
            ("already_redeemed", "Already redeemed — no second credit was spent."),
        )
        payload = usage_payload(banked=1, primary_used=100, secondary_used=100)
        usage = json.dumps(payload).encode("utf-8")
        for code, fragment in expectations:
            with self.subTest(code=code):
                consume_body = (
                    json.dumps({"code": code, "windows_reset": 2}).encode("utf-8")
                    if code == "reset"
                    else json.dumps({"code": code}).encode("utf-8")
                )
                harness, _stub = self.harness(
                    [usage], consume_body=consume_body
                )
                try:
                    status, body, _ = _redeem(harness)
                    self.assertEqual(status, 200, body)
                    self.assertEqual(body["outcome"], code)
                    self.assertIn(fragment, body["message"])
                    self.assertEqual(
                        self.audit_records(harness)[0]["outcome"], code
                    )
                    self.assertEqual(
                        FORBIDDEN.findall(json.dumps(body)), [], "no identity in a response"
                    )
                finally:
                    harness.close()
                    self._harnesses.remove(harness)

    def test_reset_reports_remaining_count_from_the_pre_spend_read(self) -> None:
        payload = usage_payload(banked=3, primary_used=100, secondary_used=100)
        harness, _stub = self.harness([json.dumps(payload).encode("utf-8")])

        status, body, _ = _redeem(harness)

        self.assertEqual(status, 200, body)
        self.assertEqual(body["resets_remaining"], 2, "3 banked, 1 spent")
        self.assertIn("2 banked resets remaining", body["message"])
        # Singular branch, too.
        harness2, _stub2 = self.harness(
            [json.dumps(usage_payload(banked=1, primary_used=100, secondary_used=100)).encode("utf-8")]
        )
        status, body, _ = _redeem(harness2)
        self.assertEqual(status, 200, body)
        self.assertIn("0 banked resets remaining", body["message"])

    def test_reset_primes_the_quota_and_the_next_state_serves_restored_windows(self) -> None:
        exhausted = json.dumps(
            usage_payload(banked=1, primary_used=100, secondary_used=100)
        ).encode("utf-8")
        restored = json.dumps(
            usage_payload(banked=0, primary_used=0, secondary_used=0)
        ).encode("utf-8")
        harness, stub = self.harness([exhausted, restored])
        # A cooldown is held for the provider: reported, never cleared.
        harness.router.circuits.set("chatgpt_codex", 900, reason="exhausted (fixture)")

        status, body, _ = _redeem(harness)

        self.assertEqual(status, 200, body)
        # The re-read inside the handler: restored windows in the response.
        windows = {w["label"]: w for w in body["account_quota"]["windows"]}
        self.assertEqual(windows["Weekly"]["used_percent"], 0, "the reset re-read shows restored limits")
        self.assertEqual(body["circuit_open"], True, "the held cooldown is reported")
        # ...and the cooldown is still held: this action never clears it.
        details = harness.router.circuits.details()
        self.assertTrue(details, "the circuit must still exist")
        self.assertTrue(
            all(entry["remaining_seconds"] > 0 for entry in details.values()),
            "redeeming must not clear or shorten a cooldown",
        )
        # The next state render serves the primed block: exactly two reads
        # total (guard + post-reset), no third within the same second.
        status, payload, _ = harness.request("GET", "/owner/api/state")
        self.assertEqual(status, 200)
        row = {r["provider_id"]: r for r in payload["provider_health"]}["chatgpt_codex"]
        restored_windows = {w["label"]: w for w in row["account_quota"]["windows"]}
        self.assertEqual(restored_windows["Weekly"]["used_percent"], 0)
        self.assertEqual(row["account_quota"]["resets_banked"], 0)
        self.assertEqual(
            len(stub.usage_calls), 2, "the primed memo must serve the render without a third read"
        )


class FailureMappingTests(_ResetRedemptionBase):
    def test_an_unrecognised_outcome_is_unavailable_and_never_retried(self) -> None:
        payload = usage_payload(banked=1, primary_used=100, secondary_used=100)
        harness, stub = self.harness(
            [json.dumps(payload).encode("utf-8")],
            consume_body=json.dumps(
                {"code": "surprise_outcome", "raw": "identity-soup@example.invalid"}
            ).encode("utf-8"),
        )

        status, body, _ = _redeem(harness)

        self.assertEqual(status, 502, body)
        self.assertEqual(body["error"]["type"], "redeem_unavailable")
        message = body["error"]["message"]
        self.assertIn("does not recognise", message)
        # The body never leaks: neither its code nor anything it carried.
        self.assertNotIn("surprise_outcome", json.dumps(body))
        self.assertNotIn("identity-soup", json.dumps(body))
        self.assertEqual(len(stub.consume_calls), 1, "unknown outcome, no blind retry")
        self.assertEqual(self.audit_records(harness)[0]["outcome"], "unavailable")

    def test_a_401_from_consume_reports_without_touching_the_credential(self) -> None:
        payload = usage_payload(banked=1, primary_used=100, secondary_used=100)
        harness, stub = self.harness(
            [json.dumps(payload).encode("utf-8")],
            consume_status=401,
            consume_body=b'{"error": "expired"}',
        )
        before = harness.credentials.describe("chatgpt_codex")

        status, body, _ = _redeem(harness)

        self.assertEqual(status, 502, body)
        self.assertIn("HTTP 401", body["error"]["message"])
        self.assertIn("re-sign-in is the owner's action", body["error"]["message"])
        self.assertEqual(len(stub.consume_calls), 1, "401 is reported, never retried")
        self.assertEqual(
            harness.credentials.describe("chatgpt_codex"), before,
            "this path must not refresh or rewrite the credential",
        )

    def test_a_timeout_reports_and_keeps_the_guard_intact(self) -> None:
        payload = usage_payload(banked=1, primary_used=100, secondary_used=100)
        harness, stub = self.harness(
            [json.dumps(payload).encode("utf-8")],
            consume_fail=TimeoutError("fixture timeout"),
        )

        status, body, _ = _redeem(harness)

        self.assertEqual(status, 502, body)
        self.assertIn("did not answer in time", body["error"]["message"])
        self.assertEqual(len(stub.consume_calls), 1)

    def test_an_unsupported_provider_is_refused_before_any_request(self) -> None:
        harness, stub = self.harness([b"{}"])

        status, body, _ = harness.request(
            "POST",
            "/owner/api/provider",
            {"action": "redeem_reset", "provider_id": "opencode_go"},
        )

        self.assertEqual(status, 400, body)
        self.assertEqual(body["error"]["type"], "unsupported_action")
        self.assertEqual(stub.calls, [], "a provider without reset credits is never contacted")


class NeverAutomaticTests(_ResetRedemptionBase):
    def test_state_polls_never_redeem_even_with_credits_and_exhausted_windows(self) -> None:
        payload = usage_payload(banked=2, primary_used=100, secondary_used=100)
        harness, stub = self.harness([json.dumps(payload).encode("utf-8")])

        for _ in range(3):
            status, _, _ = harness.request("GET", "/owner/api/state")
            self.assertEqual(status, 200)

        self.assertEqual(stub.consume_calls, [], "reading state must never spend a credit")
        self.assertEqual(self.audit_records(harness), [], "no attempt happened, none recorded")

    def test_the_consume_call_is_reached_from_exactly_one_source_file(self) -> None:
        """Structural never-automatic: one caller, by source scan.

        ``consume_rate_limit_reset`` may appear in the adapter (its
        definition) and in the owner action's handler - nowhere else. A
        background job, a router hook or a cooldown-triggered path would
        put a third file on this list.
        """
        repo = Path(__file__).resolve().parent.parent / "src"
        files = sorted(
            str(path.relative_to(repo))
            for path in repo.rglob("*.py")
            if "consume_rate_limit_reset" in path.read_text(encoding="utf-8")
        )
        self.assertEqual(
            files,
            ["gateway/adapters/chatgpt_codex.py", "gateway/http/app.py"],
            "the spend endpoint must stay reachable from the adapter and the owner action only",
        )
        console = (repo / "gateway" / "static" / "console.js").read_text(encoding="utf-8")
        self.assertEqual(
            console.count('"redeem_reset"'), 1,
            "the console must hold exactly one trigger for the action",
        )

    def test_the_usage_url_derivation_keeps_the_shipped_default(self) -> None:
        # The consume endpoint follows the same configured origin as the
        # usage read; with the default base the read is byte-identical to
        # the URL shipped before this card (no behaviour drift).
        # No live domain is written into this file (AC4: a tests/ grep for
        # the production host finds nothing this card added): the default
        # derivation is pinned through the shipped constants instead.
        self.assertEqual(usage_url_for(CHATGPT_BASE), USAGE_URL)
        self.assertTrue(
            codex_backend_prefix(CHATGPT_BASE).endswith("/backend-api/wham"),
            "the default base keeps the shipped wham prefix",
        )
        self.assertTrue(
            RESET_CREDITS_CONSUME_PATH.endswith("/rate-limit-reset-credits/consume")
        )
        self.assertEqual(codex_backend_prefix(LOOPBACK_BASE), "http://127.0.0.1:9/api/codex")


class MachineKeyTests(_ResetRedemptionBase):
    def test_a_machine_key_cannot_redeem_a_reset(self) -> None:
        payload = usage_payload(banked=5, primary_used=100, secondary_used=100)
        harness, stub = self.harness([json.dumps(payload).encode("utf-8")])

        created, key_payload, _ = harness.request(
            "POST",
            "/owner/api/machine-keys",
            {"action": "create", "name": "redeem-probe", "scope": "write"},
        )
        self.assertEqual(created, 200, key_payload)
        key = key_payload["machine_key"]["key"]

        status, body, _ = harness.request(
            "POST",
            "/owner/api/provider",
            REDEEM_BODY,
            headers={"Authorization": f"Bearer {key}"},
            auth=False,
        )

        self.assertEqual(status, 403, body)
        self.assertEqual(body["error"]["type"], "machine_key_forbidden")
        self.assertIn("cannot redeem a reset credit", body["error"]["message"])
        self.assertEqual(stub.calls, [], "refused before the adapter was even looked up")
        self.assertEqual(self.audit_records(harness), [], "a refused machine key is no attempt")


class AuditTests(_ResetRedemptionBase):
    def test_the_audit_records_who_when_outcome_without_token_material(self) -> None:
        payload = usage_payload(banked=1, primary_used=100, secondary_used=100)
        harness, _stub = self.harness([json.dumps(payload).encode("utf-8")])
        # Positive control: the fixture body itself trips the pattern,
        # so the empty scans below are real.
        self.assertTrue(FORBIDDEN.findall(json.dumps(payload)))

        status, body, _ = _redeem(harness)
        self.assertEqual(status, 200, body)

        records = self.audit_records(harness)
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(
            sorted(record), ["actor", "outcome", "provider", "ts"],
            "exactly who/when/outcome (plus its provider)",
        )
        self.assertEqual(record["actor"], "owner:owner")
        self.assertEqual(record["provider"], "chatgpt_codex")
        self.assertEqual(record["outcome"], "reset")
        # "when": an ISO-8601 UTC stamp, not a placeholder.
        self.assertRegex(record["ts"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

        # Neither sink may carry identity or token material.
        self.assertEqual(FORBIDDEN.findall(json.dumps(records)), [], "audit leak")
        self.assertEqual(FORBIDDEN.findall(json.dumps(body)), [], "response leak")

        # On disk: the sidecar exists next to config.json, mode 0600,
        # one JSON line, same fields - and a fresh reader replays it, so
        # the trail survives a restart.
        sidecar = harness.data_dir / REDEMPTION_AUDIT_FILE
        self.assertTrue(sidecar.exists(), "the audit sidecar must be durable")
        mode = stat.S_IMODE(sidecar.stat().st_mode)
        self.assertEqual(mode, 0o600, oct(mode))
        lines = [line for line in sidecar.read_text(encoding="utf-8").splitlines() if line]
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0]), record)
        replayed = RedemptionAudit(sidecar).recent()
        self.assertEqual(replayed, [record], "a restart must not lose the trail")

    def test_a_locally_stale_zero_count_still_defers_to_the_server(self) -> None:
        # The console disables at 0 banked, but the action's guard is the
        # window check only: the count in state may be stale (Hermes'
        # /usage reset draws on the same single pool), so the server's
        # answer is the referee - no_credit, calm 200, nothing spent by
        # this gateway.
        payload = usage_payload(banked=0, primary_used=100, secondary_used=100)
        harness, stub = self.harness(
            [json.dumps(payload).encode("utf-8")],
            consume_body=b'{"code": "no_credit"}',
        )

        status, body, _ = _redeem(harness)

        self.assertEqual(status, 200, body)
        self.assertEqual(body["outcome"], "no_credit")
        self.assertIn("No credit available", body["message"])
        self.assertEqual(len(stub.consume_calls), 1)
        self.assertEqual(
            [r["outcome"] for r in self.audit_records(harness)], ["no_credit"]
        )


class ReachabilityTests(_ResetRedemptionBase):
    def test_the_derived_consume_url_stays_on_the_fixture_origin(self) -> None:
        payload = usage_payload(banked=1, primary_used=100, secondary_used=100)
        harness, stub = self.harness([json.dumps(payload).encode("utf-8")])

        _status, body, _ = _redeem(harness)
        self.assertEqual(body["outcome"], "reset")
        for call in stub.calls:
            self.assertTrue(
                call["url"].startswith("http://127.0.0.1"),
                f"fixture traffic left loopback: {call['url']}",
            )
        self.assertEqual(
            FORBIDDEN.findall(json.dumps(body)) + FORBIDDEN.findall(
                json.dumps(self.audit_records(harness))
            ),
            [],
        )


if __name__ == "__main__":
    unittest.main()
