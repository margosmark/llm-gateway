"""The upstream model id must replace the alias.

The client asks for an alias such as ``coding``. The provider only knows
its own model ids, so the payload the gateway sends upstream must carry the
**upstream** model id.

Sending the alias upstream makes the provider answer:

    {"error": {"type": "server_error", "message": "Upstream request failed:
     Model is unavailable."}}

which the gateway then classifies as a malformed request - a confusing
message for what is really a substitution the gateway owns.
"""

from __future__ import annotations

import json
import unittest

from gateway.adapters.base import ProviderRequest
from tests.test_end_to_end import FAKE_PROVIDER, FakeAdapter, GatewayHarness


class UpstreamModelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = GatewayHarness()
        self.addCleanup(self.harness.close)
        with self.harness.config.mutate() as config:
            config.aliases["coding"] = _alias()

    def test_the_upstream_receives_the_model_id_not_the_alias(self) -> None:
        status, _, _ = self.harness.request(
            "POST",
            "/v1/chat/completions",
            body={"model": "coding", "messages": [{"role": "user", "content": "hi"}]},
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(self.harness.adapter.calls), 1)
        call = self.harness.adapter.calls[0]
        self.assertEqual(call.model, "gpt-6-luna")
        self.assertEqual(
            call.payload["model"],
            "gpt-6-luna",
            "the upstream payload must carry the provider's model id, not the alias",
        )

    def test_the_alias_is_not_leaked_upstream(self) -> None:
        self.harness.request(
            "POST",
            "/v1/chat/completions",
            body={"model": "coding", "messages": [{"role": "user", "content": "hi"}]},
        )
        payload = json.dumps(self.harness.adapter.calls[0].payload)
        self.assertNotIn('"coding"', payload)

    def test_the_caller_payload_is_not_mutated(self) -> None:
        body = {"model": "coding", "messages": [{"role": "user", "content": "hi"}]}
        self.harness.request("POST", "/v1/chat/completions", body=body)
        # The caller's own mapping keeps the alias it asked for.
        self.assertEqual(body["model"], "coding")

    def test_the_alias_is_still_what_the_ledger_records(self) -> None:
        self.harness.request(
            "POST",
            "/v1/chat/completions",
            body={"model": "coding", "messages": [{"role": "user", "content": "hi"}]},
        )
        events = self.harness.router._ledger.recent()
        self.assertEqual(events[0]["requested_alias"], "coding")
        self.assertEqual(events[0]["serving_model"], "gpt-6-luna")


def _alias():
    from gateway.config import Alias, RouteRef

    return Alias(
        name="coding",
        enabled=True,
        primary=RouteRef(FAKE_PROVIDER, "gpt-6-luna"),
    )


if __name__ == "__main__":
    unittest.main()
