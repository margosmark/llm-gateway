"""Reproduce QA t_668683b3: a stream failure inside a weighted-pool member
must NOT fall back to a different route, even with every opt-in enabled."""
from __future__ import annotations

import sys

sys.path.insert(0, "src")
sys.path.insert(0, ".")

from gateway.adapters.base import AdapterError
from gateway.config import Alias, ModelEntry, ProviderState, RouteRef, WeightedRoute
from tests.test_end_to_end import FakeAdapter, GatewayHarness


def build():
    harness = GatewayHarness()
    second = FakeAdapter(provider_id="opencode_go", dialect="openai_responses")
    harness.registry.register(second)
    with harness.config.mutate() as config:
        config.providers["chatgpt_codex"].models["gpt-6-luna"].enabled = True
        config.providers["chatgpt_codex"].models["gpt-6-luna"].dialect = "openai_responses"
        config.providers["chatgpt_codex"].models["gpt-6-luna"].supports_streaming = True
        config.providers["opencode_go"] = ProviderState(
            provider_id="opencode_go", kind="opencode_go", state="connected",
            models={"m2": ModelEntry("m2", enabled=True, dialect="openai_responses", supports_streaming=True)},
        )
        config.aliases["coding"] = Alias(
            name="coding", enabled=True,
            weighted_routes=[
                WeightedRoute(RouteRef("chatgpt_codex", "gpt-6-luna"), 1),
                WeightedRoute(RouteRef("opencode_go", "m2"), 1),
            ],
            # every opt-in the fallback policy demands
            fallback_authorized=True,
            fallback_confirmed_providers=["chatgpt_codex", "opencode_go"],
            fallback_triggers=["exhausted", "rate_limited", "upstream_unavailable"],
        )
    return harness, second


def main() -> int:
    harness, second = build()
    first = harness.adapter

    # 1. discover which member the conversation sticks to (buffered probe)
    status, _, _ = harness.request("POST", "/v1/chat/completions", body={
        "model": "coding", "messages": [{"role": "user", "content": "hi"}],
    })
    assert status == 200, status
    selected = first if first.calls else second
    backup = second if selected is first else first

    # 2. THE SCENARIO: streaming request; the selected pool member fails.
    selected.fail_with = AdapterError("transient", provider_id=selected.provider_id)
    status, payload, _ = harness.request("POST", "/v1/chat/completions", body={
        "model": "coding", "messages": [{"role": "user", "content": "hi"}], "stream": True,
    }, raw=True)
    print(f"stream request -> status {status}")
    print(f"selected member calls: {len(selected.calls)} (1 probe + 1 stream attempt = 2)")
    print(f"backup (other route) calls: {len(backup.calls)}  <-- must be 0 for a stream")
    print(f"error: {payload[:140]!r}")

    ok = status == 502 and len(backup.calls) == 0
    print("VERDICT:", "PASS (no cross-route fallback after stream)" if ok
          else "DEFECT REPRODUCED (fell back to another route after a stream)")
    harness.close()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
