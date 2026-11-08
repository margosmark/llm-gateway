"""Tests for the provider-neutral client-abort seam."""

from __future__ import annotations

import threading
import unittest

from gateway.core.abort import (
    CLIENT_ABORT,
    RouteContext,
    abort_upstream,
    classify_late_error,
    find_route_context,
    register_route_context,
    release_route_context,
)


class _Adapter:
    provider_id = "synthetic"

    def __init__(self) -> None:
        self.cancel_calls = 0
        self.lock = threading.Lock()

    def cancel(self, _request: object) -> None:
        with self.lock:
            self.cancel_calls += 1


class _Stream:
    def __init__(self) -> None:
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1

    def __iter__(self):
        return self

    def __next__(self) -> bytes:
        raise StopIteration


class AbortTests(unittest.TestCase):
    def test_abort_is_idempotent_and_cancels_adapter_once(self) -> None:
        adapter = _Adapter()
        ctx = RouteContext(adapter=adapter, request=object(), request_id="req-1")

        first = abort_upstream(ctx)
        second = abort_upstream(ctx)

        self.assertTrue(first.aborted)
        self.assertFalse(first.already_aborted)
        self.assertTrue(first.cancellation_supported)
        self.assertTrue(first.cancellation_attempted)
        self.assertTrue(first.cancelled)
        self.assertTrue(second.already_aborted)
        self.assertFalse(second.cancellation_attempted)
        self.assertEqual(adapter.cancel_calls, 1)

    def test_concurrent_abort_signals_adapter_once(self) -> None:
        adapter = _Adapter()
        ctx = RouteContext(adapter=adapter, request=object())
        barrier = threading.Barrier(9)
        outcomes = []
        failures = []

        def abort() -> None:
            try:
                barrier.wait()
                outcomes.append(abort_upstream(ctx))
            except BaseException as exc:  # noqa: BLE001 - re-raised on the main test thread.
                failures.append(exc)

        threads = [threading.Thread(target=abort) for _ in range(8)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=2)

        self.assertFalse(failures)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(adapter.cancel_calls, 1)
        self.assertEqual(sum(not outcome.already_aborted for outcome in outcomes), 1)
        self.assertEqual(len(outcomes), 8)

    def test_stream_is_closed_and_metadata_is_safe(self) -> None:
        stream = _Stream()
        ctx = RouteContext(stream=stream, provider="synthetic", model="upstream-model", request_id="req-2")

        outcome = abort_upstream(ctx, "downstream_disconnect")

        self.assertTrue(outcome.cancelled)
        self.assertEqual(stream.close_calls, 1)
        self.assertEqual(outcome.reason, "downstream_disconnect")
        self.assertEqual(outcome.to_dict()["provider"], "synthetic")
        self.assertEqual(outcome.to_dict()["model"], "upstream-model")
        self.assertNotIn("request", outcome.to_dict())

    def test_late_provider_error_is_client_abort(self) -> None:
        class ProviderError(Exception):
            error_class = "transient"

        ctx = RouteContext()
        error = ProviderError()
        self.assertEqual(classify_late_error(ctx, error), "transient")
        abort_upstream(ctx)
        self.assertEqual(classify_late_error(ctx, error), CLIENT_ABORT)

    def test_context_registry_tracks_identity_and_release(self) -> None:
        ctx = RouteContext(request_id="req-3")
        register_route_context(ctx)
        self.assertIs(find_route_context("req-3"), ctx)
        release_route_context(ctx)
        self.assertIsNone(find_route_context("req-3"))

    def test_no_cancellation_hook_is_reported_as_unsupported(self) -> None:
        ctx = RouteContext()
        outcome = abort_upstream(ctx)
        self.assertTrue(outcome.aborted)
        self.assertFalse(outcome.cancellation_supported)
        self.assertFalse(outcome.cancellation_attempted)
        self.assertFalse(outcome.cancelled)

    def test_the_registry_is_bounded_so_finished_requests_are_forgotten(self) -> None:
        from gateway.core.abort import CONTEXT_REGISTRY_LIMIT

        oldest = RouteContext(request_id="req-oldest")
        register_route_context(oldest)
        bulk = [RouteContext(request_id=f"req-bulk-{index}") for index in range(CONTEXT_REGISTRY_LIMIT)]
        newest = RouteContext(request_id="req-newest")
        try:
            for context in bulk:
                register_route_context(context)
            register_route_context(newest)

            self.assertIsNone(
                find_route_context("req-oldest"),
                "an unbounded registry would pin every request's upstream payload "
                "for the lifetime of the process",
            )
            self.assertIs(find_route_context("req-newest"), newest)
            self.assertIs(find_route_context("req-bulk-0"), None)
        finally:
            # Leave the process-wide registry as this test found it.
            for context in bulk:
                release_route_context(context)
            release_route_context(newest)


if __name__ == "__main__":
    unittest.main()
