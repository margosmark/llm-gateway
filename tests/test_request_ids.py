"""Regression cover for the request identifier.

The ledger amends a recorded outcome by its request identifier, so two
requests that share an identifier would have one request's usage counters
written onto another request's record. The gateway used to mint
``req_<millisecond>``, which is not unique: the appliance serves requests
concurrently, and two requests routinely begin inside the same millisecond.

Every test here fails against that millisecond-only identifier.
"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from gateway.adapters.base import ProviderResponse
from gateway.config import Alias, RouteRef
from gateway.observability import RequestEvent, RequestLedger, new_request_id, utc_now_iso

from tests.test_end_to_end import FAKE_PROVIDER, GatewayHarness

#: A frozen clock. Every request below starts inside the same millisecond,
#: which is exactly the condition the millisecond-only identifier could not
#: survive.
FROZEN_CLOCK = 1_700_000_000.0


def _event(request_id: str, alias: str) -> RequestEvent:
    return RequestEvent(
        request_id=request_id,
        timestamp=utc_now_iso(),
        requested_alias=alias,
    )


def _ledger(harness: GatewayHarness) -> RequestLedger:
    ledger = harness.router._ledger
    assert ledger is not None
    return ledger


class RequestIdTests(unittest.TestCase):
    def test_identifiers_are_unique_inside_one_millisecond(self) -> None:
        with patch("time.time", return_value=FROZEN_CLOCK):
            identifiers = [new_request_id() for _ in range(1000)]
        self.assertEqual(len(set(identifiers)), 1000)

    def test_identifiers_are_unique_under_concurrency(self) -> None:
        identifiers: list[str] = []
        lock = threading.Lock()

        def mint(count: int) -> None:
            for _ in range(count):
                identifier = new_request_id()
                with lock:
                    identifiers.append(identifier)

        threads = [threading.Thread(target=mint, args=(250,)) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(identifiers), 2000)
        self.assertEqual(len(set(identifiers)), 2000)

    def test_the_durable_ledger_keeps_every_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.jsonl"
            ledger = RequestLedger(persistence_path=path)
            self.addCleanup(ledger.close)
            with patch("time.time", return_value=FROZEN_CLOCK):
                first = new_request_id()
                second = new_request_id()
            ledger.record(_event(first, "alpha"))
            ledger.record(_event(second, "beta"))
            self.assertTrue(ledger.flush())
            # A restart reads the file back. The durable store keys a row by
            # its request identifier, so two requests that shared one
            # identifier would collapse into a single row here.
            reloaded = RequestLedger(persistence_path=path)
            self.addCleanup(reloaded.close)
            events = reloaded.recent(limit=10)
        self.assertEqual(len(events), 2)
        self.assertEqual({event["requested_alias"] for event in events}, {"alpha", "beta"})

    def test_a_unique_identifier_is_amended(self) -> None:
        ledger = RequestLedger()
        ledger.record(_event("unique", "alpha"))
        self.assertTrue(ledger.amend("unique", token_counters={"prompt_tokens": 5}))
        self.assertEqual(
            ledger.recent(limit=1)[0]["token_counters"], {"prompt_tokens": 5}
        )

    def test_an_ambiguous_identifier_is_never_amended(self) -> None:
        ledger = RequestLedger()
        ledger.record(_event("duplicate", "alpha"))
        ledger.record(_event("duplicate", "beta"))
        # The identifier does not name a single request, so the counters are
        # left unknown rather than written onto the wrong request.
        self.assertFalse(ledger.amend("duplicate", token_counters={"prompt_tokens": 5}))
        events = {event["requested_alias"]: event for event in ledger.recent(limit=10)}
        self.assertIsNone(events["alpha"]["token_counters"])
        self.assertIsNone(events["beta"]["token_counters"])


class RequestAttributionTests(unittest.TestCase):
    """Two requests in one millisecond must not share usage counters."""

    def setUp(self) -> None:
        self.harness = GatewayHarness()
        self.addCleanup(self.harness.close)
        with self.harness.config.mutate() as config:
            for name in ("alpha", "beta"):
                config.aliases[name] = Alias(
                    name=name,
                    enabled=True,
                    primary=RouteRef(FAKE_PROVIDER, "gpt-6-luna"),
                )

    def test_each_request_records_its_own_usage(self) -> None:
        usage = {
            "alpha": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            "beta": {"input_tokens": 20, "output_tokens": 8, "total_tokens": 28},
        }

        def invoke(request):
            counters = usage[request.alias]
            body = json.dumps(
                {
                    "id": "resp_fake",
                    "object": "response",
                    "model": request.model,
                    "output": [
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "ok"}],
                        }
                    ],
                    "usage": counters,
                }
            ).encode("utf-8")
            return ProviderResponse(status=200, body=body)

        self.harness.adapter.invoke = invoke
        with patch("time.time", return_value=FROZEN_CLOCK):
            for alias in ("alpha", "beta"):
                status, _, _ = self.harness.request(
                    "POST",
                    "/v1/chat/completions",
                    body={"model": alias, "messages": [{"role": "user", "content": "hi"}]},
                )
                self.assertEqual(status, 200)

        events = {
            event["requested_alias"]: event
            for event in _ledger(self.harness).recent(limit=10)
        }
        self.assertEqual(set(events), {"alpha", "beta"})
        self.assertNotEqual(events["alpha"]["request_id"], events["beta"]["request_id"])
        for alias, counters in usage.items():
            self.assertEqual(
                events[alias]["token_counters"],
                {
                    "prompt_tokens": counters["input_tokens"],
                    "completion_tokens": counters["output_tokens"],
                    "total_tokens": counters["total_tokens"],
                },
            )

    def test_a_streamed_request_records_its_own_usage(self) -> None:
        frames = [
            b'data: {"type":"response.completed","response":{"usage":{"input_tokens":7,"output_tokens":2}}}\n\n'
        ]

        def invoke(request):
            return ProviderResponse(
                status=200,
                body=None,
                stream=iter(frames),
                content_type="text/event-stream",
            )

        self.harness.adapter.invoke = invoke
        with patch("time.time", return_value=FROZEN_CLOCK):
            for alias in ("alpha", "beta"):
                status, _, _ = self.harness.request(
                    "POST",
                    "/v1/responses",
                    body={"model": alias, "input": "hi", "stream": True},
                    raw=True,
                )
                self.assertEqual(status, 200)

        events = {
            event["requested_alias"]: event
            for event in _ledger(self.harness).recent(limit=10)
        }
        self.assertEqual(set(events), {"alpha", "beta"})
        self.assertNotEqual(events["alpha"]["request_id"], events["beta"]["request_id"])
        for alias in ("alpha", "beta"):
            self.assertEqual(
                events[alias]["token_counters"],
                {"prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9},
            )
