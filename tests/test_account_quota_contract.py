"""The account-quota contract the tests lane owns (parent t_8b93bb78).

Six properties, all against scripted payloads - no live network:

1. **labels** - a window is named by the ``limit_window_seconds`` the
   payload itself publishes (18000 -> Session, 604800 -> Weekly); a
   window with no usable duration falls back to its *position*, so a
   weekly-only payload reads Weekly and never Session (the bug class
   Hermes fixed in #65387);
2. **payload variants** - both windows, one window, no window, and a
   body that is not a mapping at all: only a publishable window is
   published, everything else is ``empty_usage_payload``;
3. **401** - ``http_401`` with ``windows: null`` and **no refresh
   attempted**: the adapter's ``refresh()`` is never invoked and no
   ``oauth/token`` call is dialled, even though the fixture credential
   carries a usable refresh token;
4. **0 / 100** - ``used_percent`` 0 and 100 survive parse and render
   raw: 0 is a measurement, not an absence, and 100 is not clamped;
5. **no per-inference fetch** - a chat completion served by the very
   adapter that owns the usage read issues no usage call, no oauth
   call, and no live-socket dial at all;
6. **no PII in state** - neither the confident nor the unavailable
   serialized state carries ``email``, ``user_id``, ``account_id`` or
   ``access_token`` as a key or as a value.

Complements ``tests/test_account_quota_state.py`` (the publish-path
file shipped with the backend card) rather than restating it: this file
pins label derivation, variant handling, the refresh refusal and the
HTTP-layer spy.
"""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from gateway.adapters.chatgpt_codex import (
    CHATGPT_BASE,
    ISSUER,
    REASON_CREDENTIAL_MISSING,
    REASON_EMPTY_USAGE,
    USAGE_URL,
    ChatGPTCodexAdapter,
    parse_account_usage,
)
from gateway.adapters.http_client import UpstreamClient, UpstreamResponse
from gateway.config import Alias, RouteRef
from gateway.observability import AccountQuotaStore
from tests.test_end_to_end import GatewayHarness

#: Synthetic only. The fixture credential carries a *refresh* token so a
#: refresh attempt from the read path would have everything it needs to
#: fire - which is exactly what must never happen.
ACCESS_TOKEN = "synthetic-quota-access-token"
REFRESH_TOKEN = "synthetic-quota-refresh-token"
ACCOUNT_ID = "acct_synthetic_quota_fixture"

#: The identity fields the real endpoint publishes. They ride along in
#: the fixtures *on purpose*: if any of them could reach state, the
#: scans below find it there.
EMAIL = "fixture-owner@example.invalid"
USER_ID = "user_fixture_000000"

FORBIDDEN_KEYS = {"email", "user_id", "account_id", "access_token"}
FORBIDDEN_VALUES = (EMAIL, USER_ID, ACCOUNT_ID, ACCESS_TOKEN, REFRESH_TOKEN)

RESET = 1794173716
RESPONSES_URL = f"{CHATGPT_BASE}/responses"
REFRESH_URL = f"{ISSUER}/oauth/token"

#: A minimal but complete Responses object - the same shape the
#: end-to-end fixture serves, so a buffered completion converts
#: responses -> chat exactly as the real adapter's answer would.
RESPONSES_BODY = json.dumps(
    {
        "id": "resp_quota_spy",
        "object": "response",
        "model": "gpt-6-luna",
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "ok"}],
            }
        ],
    }
).encode("utf-8")


def _window(*, seconds=None, used: float = 0, reset: int = RESET) -> dict:
    entry: dict = {"used_percent": used, "reset_at": reset}
    if seconds is not None:
        entry["limit_window_seconds"] = seconds
    return entry


def _payload(*, primary: dict | None = None, secondary: dict | None = None, **extra) -> dict:
    rate_limit: dict = {}
    if primary is not None:
        rate_limit["primary_window"] = primary
    if secondary is not None:
        rate_limit["secondary_window"] = secondary
    payload: dict = {"rate_limit": rate_limit, "plan_type": "plus"}
    payload.update(extra)
    return payload


#: Two windows, durations as the live endpoint publishes them, plus the
#: identity material the allowlist must drop.
USAGE_BODY = json.dumps(
    _payload(
        primary=_window(seconds=18000, used=0),
        secondary=_window(seconds=604800, used=100),
        email=EMAIL,
        user_id=USER_ID,
        account_id=ACCOUNT_ID,
    )
).encode("utf-8")


class SpyClient:
    """The adapter's HTTP layer: records *what* was asked, never header
    values, and answers entirely from scripts - no socket is opened.

    Anything that is neither the usage read nor the inference POST
    (an ``oauth/token`` refresh, say) is answered 400 **and recorded**,
    so an attempted refresh shows up as a call instead of crashing the
    path that must not make it.
    """

    def __init__(self, usage_status: int = 200, usage_body: bytes = USAGE_BODY) -> None:
        self.usage_status = usage_status
        self.usage_body = usage_body
        self.calls: list[dict] = []

    def request(self, method, url, headers=None, body=None, stream=False, provider_id="", **kwargs):
        self.calls.append({"method": method, "url": url})
        if url == USAGE_URL:
            return UpstreamResponse(self.usage_status, {}, self.usage_body, None, "application/json")
        if url == RESPONSES_URL:
            return UpstreamResponse(200, {}, RESPONSES_BODY, None, "application/json")
        return UpstreamResponse(400, {}, b"{}", None, "application/json")

    @property
    def urls(self) -> list[str]:
        return [call["url"] for call in self.calls]


def _harness(*, usage_status: int = 200, usage_body: bytes = USAGE_BODY, credential: bool = True):
    """A loopback gateway whose chatgpt_codex adapter reads through
    ``SpyClient`` and serves an alias of its own, so a completion can be
    routed *through the adapter that owns the usage read*.

    ``credential=False`` leaves the store empty while the adapter stays
    registered: "no credential" must short-circuit before any dial.
    """
    harness = GatewayHarness()
    client = SpyClient(usage_status=usage_status, usage_body=usage_body)
    harness.app.account_quota = AccountQuotaStore()
    if credential:
        harness.credentials.provider_handle("chatgpt_codex").write(
            {
                "access_token": ACCESS_TOKEN,
                "refresh_token": REFRESH_TOKEN,
                "account_id": ACCOUNT_ID,
            }
        )
    adapter = ChatGPTCodexAdapter(
        harness.credentials.provider_handle("chatgpt_codex"),
        client=client,  # type: ignore[arg-type]
    )
    harness.registry.register(adapter)
    with harness.config.mutate() as snapshot:
        # The harness already declares chatgpt_codex / gpt-6-luna enabled
        # with the responses dialect; only the alias is missing.
        snapshot.aliases["coding"] = Alias(
            name="coding", enabled=True, primary=RouteRef("chatgpt_codex", "gpt-6-luna")
        )
    return harness, client, adapter


def _state(harness) -> dict:
    status, payload, _ = harness.request("GET", "/owner/api/state")
    if status != 200 or not isinstance(payload, dict):
        raise AssertionError(f"state render failed: {status} {payload}")
    return payload


def _quota(payload: dict) -> dict:
    rows = {row["provider_id"]: row for row in payload["provider_health"]}
    if "chatgpt_codex" not in rows:
        raise AssertionError(f"no chatgpt_codex row in {sorted(rows)}")
    block = rows["chatgpt_codex"].get("account_quota")
    if block is None:
        raise AssertionError("the chatgpt_codex row carries no account_quota block")
    return block


def _walk_keys(node, found: set) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(key, str):
                found.add(key)
            _walk_keys(value, found)
    elif isinstance(node, list):
        for value in node:
            _walk_keys(value, found)


def _forbid_live_dial(self, method, url, *args, **kwargs):
    """A guard patched over the *real* client class: any live socket a
    test did not intend fails the test by name and URL."""
    raise AssertionError(f"the real UpstreamClient dialled {method} {url}")


class LabelDerivationTests(unittest.TestCase):
    """Property 1: the payload's own duration names the window."""

    def test_both_windows_take_their_label_from_the_published_duration(self) -> None:
        parsed = parse_account_usage(
            _payload(
                primary=_window(seconds=18000, used=0),
                secondary=_window(seconds=604800, used=100),
            )
        )
        self.assertIsNone(parsed.reason)
        self.assertEqual([w.label for w in parsed.windows], ["Session", "Weekly"])

    def test_the_duration_overrides_the_window_position(self) -> None:
        # A weekly window sitting in the primary slot is still Weekly:
        # the published duration wins, position only breaks ties.
        parsed = parse_account_usage(
            _payload(
                primary=_window(seconds=604800, used=10),
                secondary=_window(seconds=18000, used=20),
            )
        )
        self.assertEqual([w.label for w in parsed.windows], ["Weekly", "Session"])

    def test_a_durationless_primary_window_is_session_by_position(self) -> None:
        parsed = parse_account_usage(_payload(primary=_window(used=5)))
        self.assertEqual([w.label for w in parsed.windows], ["Session"])

    def test_a_durationless_weekly_only_payload_is_never_labelled_session(self) -> None:
        # The #65387 bug class, stated at the parser: only a secondary
        # window, no duration to name it with -> Weekly, never Session.
        parsed = parse_account_usage(_payload(secondary=_window(used=100)))
        self.assertEqual([w.label for w in parsed.windows], ["Weekly"])
        self.assertNotIn("Session", [w.label for w in parsed.windows])

    def test_an_unknown_or_malformed_duration_falls_back_to_position(self) -> None:
        cases = [
            (_payload(primary=_window(seconds=3600, used=1)), "Session"),
            (_payload(secondary=_window(seconds=3600, used=1)), "Weekly"),
            # Wrong types never masquerade as a duration: a string that
            # *reads* like one, and the bool that is an int subclass.
            (_payload(primary=_window(seconds="18000", used=1)), "Session"),
            (_payload(secondary=_window(seconds=True, used=1)), "Weekly"),
        ]
        for payload, expected in cases:
            with self.subTest(seconds=payload["rate_limit"]):
                parsed = parse_account_usage(payload)
                self.assertEqual([w.label for w in parsed.windows], [expected])


class PayloadVariantTests(unittest.TestCase):
    """Property 2: only a publishable window is published."""

    def test_both_windows_present(self) -> None:
        parsed = parse_account_usage(
            _payload(primary=_window(seconds=18000, used=0), secondary=_window(seconds=604800, used=100))
        )
        self.assertEqual(len(parsed.windows), 2)

    def test_only_the_primary_window(self) -> None:
        parsed = parse_account_usage(_payload(primary=_window(seconds=18000, used=42)))
        self.assertIsNone(parsed.reason)
        self.assertEqual([(w.label, w.used_percent) for w in parsed.windows], [("Session", 42)])

    def test_only_the_secondary_window(self) -> None:
        parsed = parse_account_usage(_payload(secondary=_window(seconds=604800, used=99)))
        self.assertIsNone(parsed.reason)
        self.assertEqual([(w.label, w.used_percent) for w in parsed.windows], [("Weekly", 99)])

    def test_neither_window_is_unavailable(self) -> None:
        parsed = parse_account_usage(_payload())
        self.assertEqual(parsed.reason, REASON_EMPTY_USAGE)
        self.assertEqual(parsed.windows, ())
        self.assertIsNone(parsed.plan_type)

    def test_a_window_without_used_percent_or_reset_does_not_publish(self) -> None:
        # Each field is mandatory: a window missing either one is
        # dropped, and a payload whose windows all drop is unavailable -
        # never an empty list of confident-looking zeroes.
        no_percent = {"reset_at": RESET}
        no_reset = {"limit_window_seconds": 18000, "used_percent": 10}
        for broken in (no_percent, no_reset):
            with self.subTest(broken=sorted(broken)):
                parsed = parse_account_usage(_payload(primary=broken))
                self.assertEqual(parsed.reason, REASON_EMPTY_USAGE)
                self.assertEqual(parsed.windows, ())

    def test_a_body_that_is_not_a_mapping_is_unavailable(self) -> None:
        for body in (None, [], "usage", 42, {"rate_limit": "not-a-mapping"}):
            with self.subTest(body=body):
                parsed = parse_account_usage(body)
                self.assertEqual(parsed.reason, REASON_EMPTY_USAGE)
                self.assertEqual(parsed.windows, ())


class StateVariantTests(unittest.TestCase):
    """The same variants as they reach the serialized state."""

    def test_a_single_window_payload_renders_one_window(self) -> None:
        body = json.dumps(
            _payload(
                primary=_window(seconds=18000, used=0),
                email=EMAIL,
                user_id=USER_ID,
                account_id=ACCOUNT_ID,
            )
        ).encode("utf-8")
        harness, _client, _adapter = _harness(usage_body=body)
        try:
            block = _quota(_state(harness))
            self.assertIsNone(block["unavailable_reason"])
            self.assertEqual([w["label"] for w in block["windows"]], ["Session"])
            self.assertEqual(block["windows"][0]["used_percent"], 0)
        finally:
            harness.close()

    def test_a_payload_with_no_window_renders_unavailable(self) -> None:
        body = json.dumps(_payload(email=EMAIL)).encode("utf-8")
        harness, client, _adapter = _harness(usage_body=body)
        try:
            block = _quota(_state(harness))
            self.assertEqual(block["unavailable_reason"], REASON_EMPTY_USAGE)
            self.assertIsNone(block["windows"])
            # It was read - a 200 that parsed to nothing still cost
            # exactly one call, and publishes no invented numbers.
            self.assertEqual(client.urls, [USAGE_URL])
        finally:
            harness.close()

    def test_a_missing_credential_publishes_reason_without_any_dial(self) -> None:
        harness, client, _adapter = _harness(credential=False)
        try:
            block = _quota(_state(harness))
            self.assertEqual(block["unavailable_reason"], REASON_CREDENTIAL_MISSING)
            self.assertIsNone(block["windows"])
            self.assertEqual(client.calls, [], "a credentialless owner must not dial out")
        finally:
            harness.close()


class UnauthorizedReadTests(unittest.TestCase):
    """Property 3: 401 is reported, never acted on."""

    def test_401_publishes_reason_null_windows_and_never_refreshes(self) -> None:
        harness, client, adapter = _harness(usage_status=401)
        try:
            before = harness.credentials.describe("chatgpt_codex")

            refresh_attempts: list[str] = []
            # Shadow the method: if the read path ever reaches for a
            # refresh, it lands here and the assertion names it. The
            # fixture credential carries a real refresh token, so this
            # spy is the only thing standing between the test and a
            # successful (and wrong) token exchange.
            adapter.refresh = lambda: (refresh_attempts.append("refresh"), False)[1]

            block = _quota(_state(harness))
            self.assertEqual(block["unavailable_reason"], "http_401")
            self.assertIsNone(block["windows"])
            self.assertIsNone(block["plan_type"])
            self.assertEqual(block["confidence"], "none")
            self.assertNotIn("used_percent", json.dumps(block))

            self.assertEqual(refresh_attempts, [], "the usage read must never refresh")
            # Exactly one outbound call: the usage GET itself. No second
            # attempt, no oauth exchange hiding behind it.
            self.assertEqual(client.calls, [{"method": "GET", "url": USAGE_URL}])
            self.assertNotIn(REFRESH_URL, client.urls)
            self.assertEqual(
                harness.credentials.describe("chatgpt_codex"), before,
                "a 401 must leave the credential store byte-identical",
            )
        finally:
            harness.close()

    def test_a_500_is_reported_without_refreshing_too(self) -> None:
        # The refusal is not special to 401: every non-200 answer is a
        # reason string, and none of them buys a token exchange.
        harness, client, adapter = _harness(usage_status=500)
        try:
            refresh_attempts = []
            adapter.refresh = lambda: (refresh_attempts.append("refresh"), False)[1]
            block = _quota(_state(harness))
            self.assertEqual(block["unavailable_reason"], "http_500")
            self.assertIsNone(block["windows"])
            self.assertEqual(refresh_attempts, [])
            self.assertEqual(client.urls, [USAGE_URL])
        finally:
            harness.close()


class PercentEdgeTests(unittest.TestCase):
    """Property 4: 0 and 100 are measurements, rendered raw."""

    def test_zero_and_hundred_survive_the_parse_untouched(self) -> None:
        parsed = parse_account_usage(
            _payload(primary=_window(seconds=18000, used=0), secondary=_window(seconds=604800, used=100))
        )
        self.assertEqual([w.used_percent for w in parsed.windows], [0, 100])

    def test_a_zero_percent_window_is_still_a_measurement(self) -> None:
        # 0 is falsy but it is data: a lone 0% window publishes with a
        # reason of None - it must not read as "no observation".
        parsed = parse_account_usage(_payload(primary=_window(seconds=18000, used=0)))
        self.assertIsNone(parsed.reason)
        self.assertEqual(len(parsed.windows), 1)
        self.assertEqual(parsed.windows[0].used_percent, 0)

    def test_the_state_renders_zero_and_hundred_exactly(self) -> None:
        harness, _client, _adapter = _harness()
        try:
            block = _quota(_state(harness))
            self.assertIsNone(block["unavailable_reason"])
            windows = {w["label"]: w for w in block["windows"]}
            self.assertEqual(windows["Session"]["used_percent"], 0)
            self.assertEqual(windows["Weekly"]["used_percent"], 100)
        finally:
            harness.close()


class NoPerInferenceFetchTests(unittest.TestCase):
    """Property 5: the read lives in state assembly, and nowhere else."""

    def test_a_chat_completion_never_issues_a_usage_read(self) -> None:
        harness, client, _adapter = _harness()
        try:
            # The completion runs with the *real* client class forbade
            # from opening any socket: every call in this test must go
            # through the scripted layer, so a stray live dial fails by
            # URL instead of silently reaching the network.
            with patch.object(UpstreamClient, "request", new=_forbid_live_dial):
                status, payload, _ = harness.request(
                    "POST",
                    "/v1/chat/completions",
                    body={"model": "coding", "messages": [{"role": "user", "content": "hi"}]},
                )
                self.assertEqual(status, 200, payload)
                # Served by the chatgpt_codex adapter itself: exactly the
                # inference POST, and no usage / oauth call beside it.
                self.assertEqual(client.urls, [RESPONSES_URL])

                # Positive control: the state poll *does* read, once.
                block = _quota(_state(harness))
                self.assertIsNone(block["unavailable_reason"])
                self.assertEqual(client.urls.count(USAGE_URL), 1)
                self.assertNotIn(REFRESH_URL, client.urls)
        finally:
            harness.close()


class StateIdentityTests(unittest.TestCase):
    """Property 6: identity and credential material never reach state."""

    def _assert_clean(self, serialized: str, payload: dict) -> None:
        keys: set = set()
        _walk_keys(payload, keys)
        leaked_keys = sorted(keys & FORBIDDEN_KEYS)
        self.assertEqual(leaked_keys, [], f"forbidden keys in state: {leaked_keys}")
        leaked_values = [v for v in FORBIDDEN_VALUES if v in serialized]
        self.assertEqual(leaked_values, [], f"forbidden values in state: {leaked_values}")

    def test_confident_state_carries_no_identity_or_credential_material(self) -> None:
        harness, _client, _adapter = _harness()
        try:
            payload = _state(harness)
            self._assert_clean(json.dumps(payload), payload)
            # Positive controls: the patterns are live against exactly
            # the material this fixture was handed - the raw payload and
            # the stored credential - so the zero above is a result.
            raw = json.dumps(json.loads(USAGE_BODY))
            self.assertTrue(any(v in raw for v in FORBIDDEN_VALUES[:2]))
            stored = json.dumps(
                harness.credentials.provider_handle("chatgpt_codex").read()
            )
            self.assertIn(ACCESS_TOKEN, stored)
        finally:
            harness.close()

    def test_unavailable_state_carries_no_identity_or_credential_material(self) -> None:
        harness, _client, _adapter = _harness(usage_status=401)
        try:
            payload = _state(harness)
            self._assert_clean(json.dumps(payload), payload)
        finally:
            harness.close()

    def test_credentialless_state_carries_no_identity_or_credential_material(self) -> None:
        harness, _client, _adapter = _harness(credential=False)
        try:
            payload = _state(harness)
            self._assert_clean(json.dumps(payload), payload)
        finally:
            harness.close()


if __name__ == "__main__":
    unittest.main()
