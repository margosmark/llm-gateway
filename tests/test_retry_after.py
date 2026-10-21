"""The provider's own ``Retry-After`` reaches the cooldown.

When a provider rate-limits a request, it usually says how long to wait.
The gateway used to throw that number away - the adapter did not keep the
response headers - and guessed instead, with a flat five minutes from the
alias's ``cooldown_seconds``.

The provider is the only party that knows how long its own allowance takes
to recover, so its answer is worth honouring. It is also a value from the
network, so it is bounded, and it is a *hint*, never evidence that the
provider has recovered.

The properties under test:

* both forms of the header are read - seconds and an HTTP date;
* an unusable value yields nothing, and the local policy is used instead;
* an absurd value is clamped, never taken literally;
* the window is the **longer** of the policy and the provider's ask;
* the cooldown records which of the two it used, and the skip says so.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from gateway.adapters.base import (
    MAX_RETRY_AFTER_SECONDS,
    AdapterError,
    error_from_response,
    parse_retry_after,
    retry_after_from_headers,
)


class ParsingTests(unittest.TestCase):
    def test_the_seconds_form_is_read(self) -> None:
        self.assertEqual(parse_retry_after("120"), 120)
        self.assertEqual(parse_retry_after("  120  "), 120)
        self.assertEqual(parse_retry_after("0"), 0)

    def test_an_unusable_value_yields_nothing(self) -> None:
        self.assertIsNone(parse_retry_after(None))
        self.assertIsNone(parse_retry_after(""))
        self.assertIsNone(parse_retry_after("   "))
        self.assertIsNone(parse_retry_after("soon"))
        self.assertIsNone(parse_retry_after("-5"))
        self.assertIsNone(parse_retry_after("12.5"))
        self.assertIsNone(parse_retry_after(120))

    def test_an_http_date_in_the_future_is_read(self) -> None:
        now = datetime(2026, 10, 20, 12, 0, 0, tzinfo=timezone.utc)
        later = (now + timedelta(seconds=90)).strftime("%a, %d %b %Y %H:%M:%S GMT")
        self.assertEqual(parse_retry_after(later, now=now), 90)

    def test_an_http_date_in_the_past_yields_nothing(self) -> None:
        now = datetime(2026, 10, 20, 12, 0, 0, tzinfo=timezone.utc)
        earlier = (now - timedelta(seconds=90)).strftime("%a, %d %b %Y %H:%M:%S GMT")
        self.assertIsNone(parse_retry_after(earlier, now=now))

    def test_an_absurd_value_is_clamped_rather_than_taken_literally(self) -> None:
        self.assertEqual(parse_retry_after("999999999"), MAX_RETRY_AFTER_SECONDS)
        self.assertLessEqual(MAX_RETRY_AFTER_SECONDS, 86400)

    def test_the_header_is_found_whatever_its_case(self) -> None:
        self.assertEqual(retry_after_from_headers({"Retry-After": "60"}), 60)
        self.assertEqual(retry_after_from_headers({"retry-after": "60"}), 60)
        self.assertIsNone(retry_after_from_headers({"content-type": "application/json"}))
        self.assertIsNone(retry_after_from_headers(None))


class ErrorTests(unittest.TestCase):
    def test_a_response_error_carries_the_providers_ask(self) -> None:
        error = error_from_response(
            429,
            b"{}",
            "opencode_go",
            {},
            operation="the inference request",
            headers={"retry-after": "900"},
        )
        self.assertEqual(error.error_class, "rate_limited")
        self.assertEqual(error.retry_after, 900)
        self.assertEqual(error.to_dict()["retry_after"], 900)

    def test_a_response_error_without_the_header_carries_nothing(self) -> None:
        error = error_from_response(429, b"{}", "opencode_go", {})
        self.assertIsNone(error.retry_after)

    def test_the_ask_is_metadata_and_carries_no_content(self) -> None:
        error = AdapterError("rate_limited", provider_id="opencode_go", retry_after=30)
        self.assertEqual(error.retry_after, 30)
        self.assertNotIn("prompt", error.to_dict())
        self.assertNotIn("body", error.to_dict())


class CooldownTests(unittest.TestCase):
    """The window is the longer of the policy and the provider's ask."""

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

    def _fail_with(self, error: AdapterError) -> None:
        from tests.test_end_to_end import FAKE_PROVIDER, FakeAdapter

        self.harness.registry.register(FakeAdapter(fail_with=error))

    def _request(self):
        return self.harness.request(
            "POST",
            "/v1/chat/completions",
            body={"model": "coding", "messages": [{"role": "user", "content": "hi"}]},
        )

    def test_a_longer_ask_wins_over_the_local_policy(self) -> None:
        self._fail_with(AdapterError("rate_limited", provider_id="chatgpt_codex", retry_after=3600))
        self._request()
        cooldown = self.harness.router.cooldown
        self.assertGreater(
            cooldown.remaining("chatgpt_codex"),
            300,
            "the provider asked for longer than the policy, so the longer window applies",
        )
        self.assertIn("the provider asked us to wait", cooldown.reason("chatgpt_codex"))

    def test_a_shorter_ask_does_not_shorten_the_local_policy(self) -> None:
        self._fail_with(AdapterError("rate_limited", provider_id="chatgpt_codex", retry_after=30))
        self._request()
        cooldown = self.harness.router.cooldown
        self.assertGreaterEqual(cooldown.remaining("chatgpt_codex"), 250)
        self.assertIn("the local cooldown policy", cooldown.reason("chatgpt_codex"))

    def test_no_ask_leaves_the_local_policy_alone(self) -> None:
        self._fail_with(AdapterError("rate_limited", provider_id="chatgpt_codex"))
        self._request()
        cooldown = self.harness.router.cooldown
        self.assertGreaterEqual(cooldown.remaining("chatgpt_codex"), 250)
        self.assertIn("the local cooldown policy", cooldown.reason("chatgpt_codex"))

    def test_the_skip_says_which_one_it_was(self) -> None:
        self._fail_with(AdapterError("rate_limited", provider_id="chatgpt_codex", retry_after=3600))
        self._request()
        # The next request skips the primary, and the skip says why.
        self._request()
        event = self.harness.router._ledger.recent()[0]
        skipped = event["skipped"]
        self.assertTrue(skipped, "the skipped route must be recorded")
        self.assertIn("cooldown", skipped[0]["reason"])
        self.assertIn("the provider asked us to wait", skipped[0]["reason"])

    def test_the_cooldown_details_carry_the_reason(self) -> None:
        self._fail_with(AdapterError("rate_limited", provider_id="chatgpt_codex", retry_after=3600))
        self._request()
        details = self.harness.router.cooldown.details()
        self.assertIn("chatgpt_codex", details)
        self.assertIn("the provider asked us to wait", details["chatgpt_codex"]["reason"])
        self.assertGreater(details["chatgpt_codex"]["remaining_seconds"], 300)


class AdapterEndToEndTests(unittest.TestCase):
    """A real 429 with a real header reaches the error."""

    def setUp(self) -> None:
        import tempfile
        from pathlib import Path

        from gateway.credentials import CredentialStore

        self._tmp = tempfile.TemporaryDirectory()
        self.store = CredentialStore(Path(self._tmp.name))
        self.store.provider_handle("opencode_go").write({"api_key": "synthetic"})

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_the_opencode_go_adapter_carries_the_header(self) -> None:
        from gateway.adapters.base import ProviderRequest
        from gateway.adapters.http_client import UpstreamResponse
        from gateway.adapters.opencode_go import OpenCodeGoAdapter

        class RateLimited:
            def request(self, method, url, headers, body=None, stream=False, provider_id="", **kwargs):
                return UpstreamResponse(
                    429,
                    {"retry-after": "1800", "content-type": "application/json"},
                    b'{"error":{"type":"rate_limit_exceeded"}}',
                    None,
                    "application/json",
                )

        adapter = OpenCodeGoAdapter(
            self.store.provider_handle("opencode_go"), client=RateLimited()
        )
        with self.assertRaises(AdapterError) as caught:
            adapter.invoke(
                ProviderRequest(
                    model="gpt-6-luna",
                    payload={"model": "gpt-6-luna", "input": []},
                    dialect="openai_responses",
                )
            )
        error = caught.exception
        self.assertEqual(error.error_class, "rate_limited")
        self.assertEqual(error.retry_after, 1800)


if __name__ == "__main__":
    unittest.main()
