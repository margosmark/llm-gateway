"""Regression tests: a client disconnect is recorded as ``client_abort``.

A successful route is written to the ledger *before* the HTTP layer consumes
the stream. When the client disconnects, that record must be corrected to
``client_abort`` - distinctly from a provider failure - and the provider
failure count must not change. These tests use the real ``RequestLedger``
and the abort seam with synthetic adapters; no network, no credentials.
"""

from __future__ import annotations

import unittest

from gateway.config import ERROR_CLASSES, ERROR_TRANSIENT
from gateway.core.abort import (
    CLIENT_ABORT,
    AbortOutcome,
    RouteContext,
    abort_upstream,
    classify_late_error,
)
from gateway.core.client_abort import (
    CLIENT_ABORT_ERROR_CLASS,
    correct_ledger_for_abort,
    install_client_abort_correction,
    is_installed,
    is_provider_error_class,
    provider_error_count,
)
from gateway.observability import RequestEvent, RequestLedger, utc_now_iso


class _Adapter:
    provider_id = "synthetic"

    def __init__(self) -> None:
        self.cancel_calls = 0

    def cancel(self, _request: object) -> None:
        self.cancel_calls += 1


class _Stream:
    def __init__(self) -> None:
        self.close_calls = 0
        self._chunks = [b"one", b"two"]

    def close(self) -> None:
        self.close_calls += 1

    def __iter__(self):
        return self

    def __next__(self) -> bytes:
        if self._chunks:
            return self._chunks.pop(0)
        raise StopIteration


def _record_success(ledger: RequestLedger, request_id: str) -> None:
    """Record the outcome routing writes for a route that answered."""
    ledger.record(
        RequestEvent(
            request_id=request_id,
            timestamp=utc_now_iso(),
            requested_alias="coding",
            serving_provider="synthetic",
            serving_model="upstream-model",
            status=200,
            error_class=None,
            latency_ms=12,
            streamed=True,
        )
    )


def _record_provider_failure(ledger: RequestLedger, request_id: str) -> None:
    ledger.record(
        RequestEvent(
            request_id=request_id,
            timestamp=utc_now_iso(),
            requested_alias="coding",
            serving_provider="synthetic",
            serving_model="upstream-model",
            status=502,
            error_class=ERROR_TRANSIENT,
            latency_ms=30,
        )
    )


def _find(ledger: RequestLedger, request_id: str) -> dict:
    for event in ledger.recent(limit=100):
        if event["request_id"] == request_id:
            return event
    raise AssertionError(f"no ledger record for {request_id}")


class ClientAbortClassificationTests(unittest.TestCase):
    def setUp(self) -> None:
        # The correction is installed on import; make the state explicit.
        install_client_abort_correction()

    def test_client_abort_error_class_is_not_a_provider_failure(self) -> None:
        self.assertEqual(CLIENT_ABORT_ERROR_CLASS, CLIENT_ABORT)
        self.assertEqual(CLIENT_ABORT_ERROR_CLASS, "client_abort")
        # The whole point: a client abort is not one of the provider classes.
        self.assertNotIn(CLIENT_ABORT, ERROR_CLASSES)
        self.assertFalse(is_provider_error_class(CLIENT_ABORT))
        self.assertTrue(is_provider_error_class(ERROR_TRANSIENT))

    def test_disconnect_amends_success_record_to_client_abort(self) -> None:
        ledger = RequestLedger()
        _record_success(ledger, "req-1")
        provider_failures_before = provider_error_count(ledger)
        totals_before = ledger.totals()

        adapter = _Adapter()
        stream = _Stream()
        ctx = RouteContext(
            request_id="req-1",
            provider="synthetic",
            model="upstream-model",
            adapter=adapter,
            request=object(),
            stream=stream,
            ledger=ledger,
        )
        outcome = abort_upstream(ctx)

        # The upstream was told to stop...
        self.assertTrue(outcome.aborted)
        self.assertTrue(outcome.cancelled)
        self.assertEqual(adapter.cancel_calls, 1)
        self.assertEqual(stream.close_calls, 1)
        # ...and the already-written success record is corrected.
        record = _find(ledger, "req-1")
        self.assertEqual(record["error_class"], CLIENT_ABORT)

        # It is not a provider error, and provider failures are unchanged.
        self.assertEqual(provider_error_count(ledger), provider_failures_before)
        self.assertEqual(ledger.totals()["rate_limited"], totals_before["rate_limited"])
        self.assertEqual(ledger.totals()["exhausted"], totals_before["exhausted"])

    def test_correction_is_idempotent(self) -> None:
        ledger = RequestLedger()
        _record_success(ledger, "req-2")
        ctx = RouteContext(request_id="req-2", ledger=ledger, adapter=_Adapter())

        first = abort_upstream(ctx)
        second = abort_upstream(ctx)

        self.assertFalse(first.already_aborted)
        self.assertTrue(second.already_aborted)
        self.assertEqual(_find(ledger, "req-2")["error_class"], CLIENT_ABORT)
        self.assertEqual(provider_error_count(ledger), 0)
        self.assertEqual(ledger.totals()["errors"], 1)  # counted once, not twice.

    def test_provider_failure_record_is_left_untouched(self) -> None:
        ledger = RequestLedger()
        _record_provider_failure(ledger, "req-err")
        self.assertEqual(provider_error_count(ledger), 1)

        # A different request aborts; it must not reclassify the provider error.
        ctx = RouteContext(request_id="req-other", ledger=ledger)
        abort_upstream(ctx)

        self.assertEqual(_find(ledger, "req-err")["error_class"], ERROR_TRANSIENT)
        self.assertEqual(provider_error_count(ledger), 1)

    def test_client_abort_and_upstream_refusal_stay_distinct(self) -> None:
        # A late provider error after an abort is a client abort...
        ctx = RouteContext()

        class ProviderError(Exception):
            error_class = ERROR_TRANSIENT

        error = ProviderError()
        self.assertEqual(classify_late_error(ctx, error), ERROR_TRANSIENT)
        abort_upstream(ctx)
        self.assertEqual(classify_late_error(ctx, error), CLIENT_ABORT)

    def test_correction_without_ledger_or_request_id_is_a_no_op(self) -> None:
        self.assertFalse(correct_ledger_for_abort(RouteContext(), AbortOutcome()))
        ledger = RequestLedger()
        _record_success(ledger, "req-3")
        self.assertFalse(
            correct_ledger_for_abort(RouteContext(ledger=ledger, request_id=""), AbortOutcome())
        )

    def test_correction_survives_a_failing_ledger(self) -> None:
        class _BrokenLedger:
            def amend(self, *_args: object, **_kwargs: object) -> bool:
                raise RuntimeError("ledger unavailable")

        ctx = RouteContext(request_id="req-x", ledger=_BrokenLedger(), adapter=_Adapter())
        outcome = abort_upstream(ctx)  # must not raise.
        self.assertTrue(outcome.aborted)
        self.assertTrue(outcome.cancelled)

    def test_install_is_idempotent_and_active_on_import(self) -> None:
        self.assertTrue(is_installed())
        self.assertFalse(install_client_abort_correction())  # already installed.

    def tearDown(self) -> None:
        # Leave the process-wide correction installed (routing relies on it).
        install_client_abort_correction()


class ProviderErrorCountTests(unittest.TestCase):
    def test_counts_only_provider_failure_classes(self) -> None:
        ledger = RequestLedger()
        _record_success(ledger, "a")
        _record_provider_failure(ledger, "b")
        _record_provider_failure(ledger, "c")
        # Amend one success to a client abort; provider count must stay at 2.
        RouteContext(request_id="a", ledger=ledger)
        correct_ledger_for_abort(RouteContext(request_id="a", ledger=ledger), AbortOutcome())
        self.assertEqual(_find(ledger, "a")["error_class"], CLIENT_ABORT)
        self.assertEqual(provider_error_count(ledger), 2)

    def test_none_ledger_is_zero(self) -> None:
        self.assertEqual(provider_error_count(None), 0)


if __name__ == "__main__":
    unittest.main()
