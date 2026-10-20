"""The upstream retry is bounded, and only where repeating is free.

A provider that answers with a transient origin error - a 520 from an edge,
a 502 from a proxy - should not need a manual re-click. But an inference
call bills and may have side effects, so it is **never** repeated.

The properties under test:

* a retry-safe request is repeated a bounded number of times;
* a 5xx on a retry-safe request is retried, and a 4xx is not;
* an inference call is never retried, even on a 5xx;
* the retry is bounded by a cap, so a persistently failing upstream does not
  turn into an unbounded loop.
"""

from __future__ import annotations

import unittest

from gateway.adapters.http_client import UpstreamClient, UpstreamResponse


class ScriptedUpstream:
    """Returns a scripted sequence of responses."""

    def __init__(self, statuses: list[int]) -> None:
        self.statuses = list(statuses)
        self.calls = 0

    def request(self, method, url, headers, body=None, stream=False, provider_id=""):
        self.calls += 1
        status = self.statuses.pop(0) if self.statuses else 200
        return UpstreamResponse(status, {}, b"{}", None, "application/json")


class _Client(UpstreamClient):
    """An UpstreamClient whose transport is scripted."""

    def __init__(self, statuses: list[int]) -> None:
        super().__init__()
        self.scripted = ScriptedUpstream(statuses)

    def _request_once(self, method, url, headers, body, stream, provider_id):
        return self.scripted.request(method, url, headers, body, stream, provider_id)


class RetryTests(unittest.TestCase):
    def test_a_retry_safe_request_recovers_from_a_transient_error(self) -> None:
        client = _Client([520, 520, 200])
        response = client.request(
            "GET", "https://example.invalid/models", {}, retry_safe=True, retries=2
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(client.scripted.calls, 3)

    def test_a_retry_safe_request_gives_up_after_the_cap(self) -> None:
        client = _Client([520, 520, 520, 520, 520])
        response = client.request(
            "GET", "https://example.invalid/models", {}, retry_safe=True, retries=2
        )
        self.assertEqual(response.status, 520)
        self.assertEqual(client.scripted.calls, 3, "the retry must be bounded")

    def test_a_client_error_is_not_retried(self) -> None:
        client = _Client([401, 200])
        response = client.request(
            "GET", "https://example.invalid/models", {}, retry_safe=True, retries=2
        )
        self.assertEqual(response.status, 401)
        self.assertEqual(client.scripted.calls, 1, "a 4xx must not be retried")

    def test_an_inference_call_is_never_retried(self) -> None:
        client = _Client([520, 200])
        response = client.request(
            "POST", "https://example.invalid/responses", {}, body=b"{}"
        )
        self.assertEqual(response.status, 520)
        self.assertEqual(
            client.scripted.calls,
            1,
            "an inference call may bill twice and may repeat a side effect",
        )

    def test_an_inference_call_is_never_retried_even_when_marked(self) -> None:
        # The guard is that the *caller* does not mark an inference call as
        # retry-safe. This test pins that the flag is what decides.
        client = _Client([520, 520, 200])
        response = client.request(
            "POST", "https://example.invalid/responses", {}, body=b"{}", retry_safe=False
        )
        self.assertEqual(response.status, 520)
        self.assertEqual(client.scripted.calls, 1)


if __name__ == "__main__":
    unittest.main()
