"""Refusal-path regressions for the per-provider in-flight cap (R71/R72).

A client that resets the connection after the request body is read but
before the response headers leave the process abandons a streamed
response before its first ``next()``. Closing a never-started generator
runs no code at all, so the slot release must not live only in the
stream's ``finally``: the HTTP handler releases an unstarted stream's
lease in its own ``finally`` instead (``docs/REVIEW.md`` R71). These
tests hold the cap at one and prove every abort shape leaves the slot
usable - four unauthenticated resets used to hard-429 a local provider
until the gateway restarted, thirty-two a cloud one.

The refusal shape is driven over a real socket against the fixture's
real threaded server: the first request is parked inside the adapter
with the slot held, the client is reset while the gateway is in that
window (so the reset is provably processed before any response byte is
written), and a follow-up request must reach the provider.

R72 is the ceiling half: no positive integer can be large enough to
silently turn the cap off.
"""

from __future__ import annotations

import json
import socket
import struct
import threading
import time
import unittest
from typing import Any

from gateway.adapters.base import ProviderRequest, ProviderResponse
from gateway.config import ProviderState
from gateway.provider_concurrency import ProviderConcurrency, _CappedAdapter
from tests.test_end_to_end import FAKE_PROVIDER, FakeAdapter, GatewayHarness, _alias

_FRAMES = [
    b'data: {"type":"response.output_text.delta","delta":"Hello"}\n\n',
    b'data: {"type":"response.completed","response":{"id":"resp_1","usage":{"input_tokens":1,"output_tokens":2}}}\n\n',
]


def _streaming_request(host: str, port: int) -> bytes:
    """The F1 exploit shape: a keyless ``stream:true`` chat completion."""
    body = json.dumps(
        {"model": "coding", "messages": [{"role": "user", "content": "hi"}], "stream": True}
    ).encode("utf-8")
    head = (
        "POST /v1/chat/completions HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "Content-Type: application/json\r\n"
        "Connection: close\r\n"
        f"Content-Length: {len(body)}\r\n"
        "\r\n"
    ).encode("ascii")
    return head + body


def _reset(sock: socket.socket) -> None:
    """Close with SO_LINGER=0: an RST, not a graceful FIN."""
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    sock.close()


class _SlotHeldBase(unittest.TestCase):
    """Shared harness: cap pinned to one, alias ``coding`` published."""

    adapter: FakeAdapter

    def _harness_with_cap_one(self) -> GatewayHarness:
        harness = GatewayHarness(self.adapter)
        self.addCleanup(harness.close)
        with harness.config.mutate() as config:
            config.aliases["coding"] = _alias()
            config.providers[FAKE_PROVIDER].max_in_flight = 1
        return harness

    def _wait_until_served(self, harness: GatewayHarness) -> tuple[int, Any]:
        """A follow-up request until it reaches the provider, or the deadline.

        The first attempt can race a still-finishing first request (429 is
        then the cap doing its job), so this polls. On a leaked slot every
        attempt is a 429 until the deadline, which fails the test loudly
        instead of flaking.
        """
        deadline = time.monotonic() + 5.0
        status, payload = 0, None
        while time.monotonic() < deadline:
            status, payload, _ = harness.request(
                "POST", "/v1/responses", {"model": "coding", "input": "probe"}
            )
            if status == 200:
                return status, payload
            self.assertEqual(
                status, 429, f"unexpected follow-up status {status}: {payload}"
            )
            time.sleep(0.1)
        return status, payload


class ResetAbortCapacityTests(_SlotHeldBase):
    def test_a_reset_before_the_response_headers_leaves_the_slot_usable(self) -> None:
        """F1: an RST before the first response byte must not leak the slot.

        The first invoke is parked with the slot held; the reset lands and
        is processed while the gateway is parked there, so the header write
        that follows is guaranteed to fail before the stream is ever
        entered - the never-started-generator case from the gate's
        evidence. RED on ac515eb: the follow-up stays 429 forever and the
        provider is never reached.
        """
        gate = threading.Event()  # clear = park the next invoke
        entered = threading.Event()

        class GatedAdapter(FakeAdapter):
            def invoke(self, request: ProviderRequest) -> ProviderResponse:
                if not gate.is_set():
                    entered.set()
                    gate.wait(timeout=5)
                return super().invoke(request)

        self.adapter = GatedAdapter(stream_frames=list(_FRAMES))
        harness = self._harness_with_cap_one()

        client = socket.create_connection((harness.host, harness.port))
        self.addCleanup(client.close)
        client.sendall(_streaming_request(harness.host, harness.port))
        self.assertTrue(entered.wait(timeout=5), "the gateway never reached the provider")
        # The slot is held inside invoke. Reset now: the kernel processes
        # the RST while the gateway is parked, so its header write fails.
        _reset(client)
        gate.set()

        status, payload = self._wait_until_served(harness)
        self.assertEqual(status, 200, payload)
        self.assertEqual(len(self.adapter.calls), 2, "the follow-up never reached the provider")

    def test_a_graceful_close_after_the_stream_started_leaves_the_slot_usable(self) -> None:
        """FIN-abort must keep working: the started stream still releases.

        The client reads the headers and the first frame, then closes with
        an empty receive buffer (a graceful FIN). The gateway's next write
        fails mid-stream; the started stream's own teardown releases the
        slot (the path that already worked - this test pins it).
        """
        class PacedAdapter(FakeAdapter):
            def invoke(self, request: ProviderRequest) -> ProviderResponse:
                response = super().invoke(request)
                if response.stream is None:
                    return response
                source = response.stream

                def paced():
                    for frame in source:
                        time.sleep(0.01)
                        yield frame

                return ProviderResponse(
                    status=response.status,
                    headers=response.headers,
                    body=response.body,
                    stream=paced(),
                    content_type=response.content_type,
                    dialect=response.dialect,
                )

        self.adapter = PacedAdapter(stream_frames=[_FRAMES[0]] * 40)
        harness = self._harness_with_cap_one()

        body = json.dumps(
            {"model": "coding", "input": "hi", "stream": True}
        ).encode("utf-8")
        head = (
            "POST /v1/responses HTTP/1.1\r\n"
            f"Host: {harness.host}:{harness.port}\r\n"
            "Content-Type: application/json\r\n"
            "Connection: close\r\n"
            f"Content-Length: {len(body)}\r\n"
            "\r\n"
        ).encode("ascii")
        client = socket.create_connection((harness.host, harness.port))
        self.addCleanup(client.close)
        client.settimeout(5)
        client.sendall(head + body)

        received = b""
        while b"\r\n\r\n" not in received:
            chunk = client.recv(4096)
            self.assertTrue(chunk, "the gateway closed before the response headers")
            received += chunk
        while b"data:" not in received:
            chunk = client.recv(4096)
            self.assertTrue(chunk, "the gateway closed before the first frame")
            received += chunk
        # Drain what is already buffered so the close is a FIN, not an RST.
        client.setblocking(False)
        try:
            while True:
                if not client.recv(4096):
                    break
        except (BlockingIOError, OSError):
            pass
        client.close()

        status, payload = self._wait_until_served(harness)
        self.assertEqual(status, 200, payload)
        self.assertEqual(len(self.adapter.calls), 2, "the follow-up never reached the provider")

    def test_the_alias_probe_never_leaves_a_streaming_upstream_slot_held(self) -> None:
        """The owner's alias probe abandons its stream - it must still release.

        The probe never reads the response body, so a streaming-only
        upstream's stream is dropped without ever being entered (the same
        never-started case as F1). RED on ac515eb: the probe holds the one
        slot and the follow-up stays 429.
        """
        self.adapter = FakeAdapter(stream_frames=list(_FRAMES))
        self.adapter.always_stream = True
        harness = self._harness_with_cap_one()

        status, payload, _ = harness.request(
            "POST", "/owner/api/alias", {"action": "test", "name": "coding"}
        )
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload.get("ok"), payload)

        status, payload = self._wait_until_served(harness)
        self.assertEqual(status, 200, payload)
        self.assertEqual(len(self.adapter.calls), 2, "the follow-up never reached the provider")


class StreamLeaseUnitTests(unittest.TestCase):
    """The lease contract the handler exit path relies on.

    The lease handle is looked up the same way the HTTP handler looks it
    up, so RED on ac515eb is an assertion failure or a cap refusal - the
    missing release itself - rather than an import error.
    """

    def _capped(self):
        class Streaming:
            provider_id = "lease-provider"

            def invoke(self, request: ProviderRequest) -> ProviderResponse:
                return ProviderResponse(
                    status=200,
                    stream=iter((b"one", b"two")),
                    content_type="text/event-stream",
                    dialect="openai_responses",
                )

        limits = ProviderConcurrency(1)
        return limits, _CappedAdapter(Streaming(), limits)

    def _lease_handle(self, stream: object):
        handle = getattr(stream, "release_if_unstarted", None)
        self.assertIsNotNone(handle, "the stream must expose its lease to the request handler")
        return handle

    def test_a_never_entered_stream_gives_the_slot_back_on_the_handler_release(self) -> None:
        limits, adapter = self._capped()
        response = adapter.invoke(ProviderRequest(model="m", payload={}, stream=True))
        assert response.stream is not None
        release = self._lease_handle(response.stream)
        with self.assertRaises(Exception) as held:
            limits.acquire("lease-provider")
        self.assertEqual(held.exception.error_class, "rate_limited")
        # Nothing iterated the stream - the handler's finally still works.
        release()
        limits.acquire("lease-provider")
        limits.release("lease-provider")

    def test_the_handler_release_does_not_free_a_stream_that_started(self) -> None:
        limits, adapter = self._capped()
        response = adapter.invoke(ProviderRequest(model="m", payload={}, stream=True))
        assert response.stream is not None
        release = self._lease_handle(response.stream)
        stream = iter(response.stream)
        next(stream)  # the stream has started: the slot is genuinely held
        release()
        with self.assertRaises(Exception) as held:
            limits.acquire("lease-provider")
        self.assertEqual(held.exception.error_class, "rate_limited")
        # Finishing the stream releases it, exactly once.
        self.assertEqual(list(stream), [b"two"])
        limits.acquire("lease-provider")
        limits.release("lease-provider")

    def test_closing_a_never_entered_stream_releases_the_slot(self) -> None:
        limits, adapter = self._capped()
        response = adapter.invoke(ProviderRequest(model="m", payload={}, stream=True))
        assert response.stream is not None
        # The alias-probe path: abandons the stream without entering it.
        close = getattr(response.stream, "close", None)
        assert close is not None
        close()
        limits.acquire("lease-provider")
        limits.release("lease-provider")


class MaxInFlightCeilingTests(unittest.TestCase):
    """R72: no positive integer may be large enough to switch the cap off."""

    def test_an_absurd_cap_is_refused_and_writes_nothing(self) -> None:
        harness = GatewayHarness()
        self.addCleanup(harness.close)
        before = harness.config.snapshot().revision
        status, payload, _ = harness.request(
            "POST",
            "/owner/api/provider",
            {"action": "set_max_in_flight", "provider_id": FAKE_PROVIDER, "max_in_flight": 10**400},
        )
        self.assertEqual(status, 400, payload)
        self.assertEqual(payload["error"]["type"], "invalid_max_in_flight")
        self.assertIn("no greater than 4096", payload["error"]["message"])
        snapshot = harness.config.snapshot()
        self.assertIsNone(snapshot.providers[FAKE_PROVIDER].max_in_flight)
        self.assertEqual(snapshot.revision, before, "a refused cap must write nothing")

    def test_the_ceiling_is_the_largest_accepted_cap(self) -> None:
        harness = GatewayHarness()
        self.addCleanup(harness.close)
        status, payload, _ = harness.request(
            "POST",
            "/owner/api/provider",
            {"action": "set_max_in_flight", "provider_id": FAKE_PROVIDER, "max_in_flight": 4096},
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["max_in_flight_effective"], 4096)
        status, payload, _ = harness.request(
            "POST",
            "/owner/api/provider",
            {"action": "set_max_in_flight", "provider_id": FAKE_PROVIDER, "max_in_flight": 4097},
        )
        self.assertEqual(status, 400, payload)
        self.assertEqual(payload["error"]["type"], "invalid_max_in_flight")

    def test_a_config_stored_above_the_ceiling_falls_back_to_the_default(self) -> None:
        from gateway.provider_concurrency import effective_limit

        row = ProviderState.from_dict(
            {"provider_id": "opencode_go", "kind": "opencode_go", "max_in_flight": 10**400}
        )
        assert row is not None
        self.assertIsNone(row.max_in_flight, "an over-ceiling stored value must be dropped")
        self.assertEqual(effective_limit("opencode_go", row.max_in_flight), 32)
        at_ceiling = ProviderState.from_dict(
            {"provider_id": "opencode_go", "kind": "opencode_go", "max_in_flight": 4096}
        )
        assert at_ceiling is not None
        self.assertEqual(at_ceiling.max_in_flight, 4096)


if __name__ == "__main__":
    unittest.main()
