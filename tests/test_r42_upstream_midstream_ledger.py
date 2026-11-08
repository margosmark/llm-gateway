"""R77 regressions: an upstream that dies mid-answer is recorded, counted and signalled.

docs/REVIEW.md **R77** (opened by QA card t_1b61e593, fixed here): the
ledger record is written at route time, so a provider that failed *after*
the route answered - while the stream was being consumed - left a record
that read success and a client that was silently truncated.

Rows (1:1 with the R77 acceptance criteria), synthetic adapters and
loopback sockets only - no network, no credentials, no containers:

1. ``stream:true`` + dying upstream -> the ledger record carries
   ``error_class='transient'``, ``provider_error_count(ledger)`` and
   ``totals["errors"]`` both rise by one, the client receives exactly one
   SSE error frame and then a clean chunked EOF, and there is **no**
   ``data: [DONE]`` (``[DONE]`` stays reserved for a complete answer).
   The failure is *handled*: its traceback is logged at ERROR, and
   ``socketserver.handle_error`` never runs (no banner). The log record
   is asserted through an explicit handler, so the assertion is
   capture-agnostic: it holds whether or not the runner installs its
   own logging handlers (pytest does, unittest does not).
2. Mid-stream ``AdapterError("auth")`` -> the record says ``auth``; the
   class is no longer dropped, and it counts as a provider failure.
3. Buffered client + streaming-only dying upstream -> the client still
   gets 502, and the route-time record is amended to match it (status
   502, class from ``classify_late_error``), so ledger and access log
   agree.
4. Client disconnect mid-stream -> still ``client_abort`` with the
   provider counters unmoved. Covered by the T11 matrix on this tree
   (``tests/test_t11_client_disconnect_acceptance.py``:
   ``ClientDisconnectEndToEndTests.test_the_success_record_is_amended_to_client_abort``
   and
   ``test_provider_failure_counters_are_unchanged_by_the_disconnect``)
   and cited rather than re-run here; the distinctness guardrail
   ``UpstreamAbortDistinctnessTests::test_upstream_midstream_failure_is_never_classified_as_client_abort``
   runs in that file too and must stay green.
5. A non-``AdapterError`` exception mid-stream still reaches stderr with
   its traceback (the log is not blinded - t_7198bfa3), because only
   ``AdapterError`` is handled; the handler-level counterpart is
   ``MidFlightLoggingTests.test_an_application_error_still_prints_a_traceback``.
"""

from __future__ import annotations

import io
import json
import logging
import socket
import sys
import time
import unittest
from collections.abc import Callable
from typing import Any, ClassVar, Self

from gateway.adapters.base import AdapterError, ProviderResponse
from gateway.config import ERROR_CLASSES, Alias, RouteRef
from gateway.core.client_abort import provider_error_count
from gateway.logging import operations_logger
from tests.test_end_to_end import FAKE_PROVIDER, FakeAdapter, GatewayHarness

UPSTREAM_MODEL = "gpt-6-luna"
DELTA = b'data: {"type":"response.output_text.delta","delta":"Hello"}\n\n'
#: The in-band failure signal R77 requires, byte-exact up to the payload.
ERROR_FRAME_NEEDLE = b'data: {"error": {"message"'
TRANSIENT = "transient"
AUTH = "auth"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _alias() -> Alias:
    """The single-route alias every scenario routes through."""
    return Alias(name="coding", enabled=True, primary=RouteRef(FAKE_PROVIDER, UPSTREAM_MODEL))


def _make_harness(adapter: FakeAdapter) -> GatewayHarness:
    harness = GatewayHarness(adapter=adapter)
    with harness.config.mutate() as config:
        config.aliases["coding"] = _alias()
    return harness


def _wait_for(predicate: Callable[[], bool], timeout: float = 8.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


def _ledger_event(ledger: Any, request_id: str) -> dict | None:
    for event in ledger.recent(limit=100):
        if event.get("request_id") == request_id:
            return event
    return None


def _record_for(ledger: Any, timeout: float = 5.0) -> dict:
    """Wait for the route-time record of the single request in *ledger*."""
    def _found() -> bool:
        return any(event.get("serving_model") == UPSTREAM_MODEL for event in ledger.recent(limit=50))

    _wait_for(_found, timeout=timeout)
    event = next(
        (e for e in ledger.recent(limit=50) if e.get("serving_model") == UPSTREAM_MODEL),
        None,
    )
    assert event is not None, f"no ledger record at all: {ledger.recent(limit=50)!r}"
    return event


class DyingUpstreamAdapter(FakeAdapter):
    """An upstream that answers with one frame, then dies mid-answer.

    ``always_stream=True`` makes it a *streaming-only* upstream, which is
    the buffered-client row: the answer is aggregated after routing and
    dies inside that aggregation.
    """

    def __init__(self, error_class: str = TRANSIENT, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.error_class = error_class

    def invoke(self, request: Any) -> ProviderResponse:
        if request.stream or self.always_stream:
            self.calls.append(request)

            def gen():
                yield DELTA
                raise AdapterError(self.error_class, provider_id=self.provider_id)

            return ProviderResponse(
                status=200,
                headers={},
                body=None,
                stream=gen(),
                content_type="text/event-stream",
                dialect=self.dialect,
            )
        return super().invoke(request)


class ExplodingUpstreamAdapter(FakeAdapter):
    """An upstream whose stream dies with a *non*-AdapterError (a bug)."""

    def invoke(self, request: Any) -> ProviderResponse:
        if request.stream:
            self.calls.append(request)

            def gen():
                yield DELTA
                raise RuntimeError("mid-stream boom")

            return ProviderResponse(
                status=200,
                headers={},
                body=None,
                stream=gen(),
                content_type="text/event-stream",
                dialect=self.dialect,
            )
        return super().invoke(request)


class _GatewayLogCapture:
    """Capture the gateway operations logger through an explicit handler.

    ``operations_logger`` is deliberately handler-less: with none
    installed, ``logging.lastResort`` writes each record to ``sys.stderr``
    *at emit time*. pytest's logging plugin installs its own handlers, so
    ``lastResort`` never fires there and ambient stderr stays empty — an
    assertion on stderr then tests the runner's capture state, not the
    behaviour (it passes under unittest, fails under pytest). This handler
    is attached explicitly for the duration of the request, so the same
    record (message plus traceback) is captured under either runner.
    """

    def __init__(self) -> None:
        self.text = ""
        self._stream = io.StringIO()
        self._handler = logging.StreamHandler(self._stream)
        self._handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))

    def __enter__(self) -> Self:
        operations_logger.addHandler(self._handler)
        return self

    def __exit__(self, *exc: object) -> None:
        operations_logger.removeHandler(self._handler)
        self.text = self._stream.getvalue()


def _raw_stream_request(harness: GatewayHarness, body: dict[str, Any]) -> tuple[bytes, str, str, bool]:
    """POST *body* over a real socket; read to EOF.

    Returns ``(raw_bytes, stderr, gateway_log, timed_out)``. ``stderr`` is
    whatever was written to ``sys.stderr`` during the request (the
    socketserver banner, if one fires); ``gateway_log`` is what the
    gateway operations logger emitted, captured by an explicit handler so
    it does not depend on the runner's logging plugin. A stream that never
    ends (no chunked terminator, the R77 symptom) shows up as ``timed_out``.
    """
    captured = io.StringIO()
    logs = _GatewayLogCapture()
    original_stderr = sys.stderr
    sys.stderr = captured
    sock: socket.socket | None = None
    timed_out = False
    raw = b""
    try:
        with logs:
            sock = socket.create_connection((harness.host, harness.port))
            payload = json.dumps(body).encode("utf-8")
            sock.sendall(
                (
                    "POST /v1/chat/completions HTTP/1.1\r\n"
                    f"Host: {harness.authority}:{harness.port}\r\n"
                    "Content-Type: application/json\r\n"
                    f"Content-Length: {len(payload)}\r\n"
                    "Connection: close\r\n\r\n"
                ).encode("ascii")
                + payload
            )
            sock.settimeout(8.0)
            while True:
                try:
                    chunk = sock.recv(65536)
                except TimeoutError:
                    timed_out = True
                    break
                if not chunk:
                    break
                raw += chunk
    finally:
        sys.stderr = original_stderr
        if sock is not None:
            sock.close()
    return raw, captured.getvalue(), logs.text, timed_out


def _split(raw: bytes) -> tuple[bytes, bytes]:
    head, _, body = raw.partition(b"\r\n\r\n")
    return head, body


def _error_frame(body: bytes) -> dict[str, Any] | None:
    """The payload of the in-band SSE error event, if the body has one."""
    at = body.find(ERROR_FRAME_NEEDLE)
    if at < 0:
        return None
    line = body[at:].split(b"\n\n", 1)[0]
    return json.loads(line[len("data: "):].decode("utf-8"))


# --------------------------------------------------------------------------
# row 1: stream:true, the upstream dies mid-answer
# --------------------------------------------------------------------------
class StreamingMidStreamFailureTests(unittest.TestCase):
    """One real mid-stream death; each acceptance row asserted separately."""

    evidence: ClassVar[dict[str, Any]] = {}
    harness: GatewayHarness | None = None

    @classmethod
    def setUpClass(cls) -> None:
        harness = _make_harness(DyingUpstreamAdapter(TRANSIENT))
        ledger = harness.router._ledger
        assert ledger is not None
        totals_before = dict(ledger.totals())
        count_before = provider_error_count(ledger)
        raw, stderr, log, timed_out = _raw_stream_request(
            harness,
            {"model": "coding", "messages": [{"role": "user", "content": "hi"}], "stream": True},
        )
        head, body = _split(raw)
        event = _record_for(ledger)
        cls.harness = harness
        cls.evidence = {
            "head": head,
            "body": body,
            "stderr": stderr,
            "log": log,
            "timed_out": timed_out,
            "record": event,
            "totals_before": totals_before,
            "totals_after": dict(ledger.totals()),
            "count_before": count_before,
            "count_after": provider_error_count(ledger),
        }

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.harness is not None:
            cls.harness.close()

    def test_the_client_gets_a_clean_truncated_stream_not_a_silent_one(self) -> None:
        evidence = self.evidence
        self.assertFalse(
            evidence["timed_out"],
            "the stream never ended: no chunked terminator was written (R77 symptom)",
        )
        head, body = evidence["head"], evidence["body"]
        self.assertTrue(head.startswith(b"HTTP/1.1 200"), head)
        self.assertTrue(body.endswith(b"0\r\n\r\n"), body[-80:])
        self.assertNotIn(b"data: [DONE]", body, "[DONE] is reserved for a complete answer")

    def test_exactly_one_sse_error_event_after_the_last_good_frame(self) -> None:
        body = self.evidence["body"]
        self.assertEqual(body.count(ERROR_FRAME_NEEDLE), 1, body)
        payload = _error_frame(body)
        assert payload is not None
        error = payload["error"]
        self.assertEqual(error["type"], TRANSIENT)
        self.assertEqual(error["message"], "the provider was temporarily unavailable")
        # The signal comes after the delivered answer, not instead of it.
        first_data = body.find(b"data: ")
        self.assertGreater(body.find(ERROR_FRAME_NEEDLE), first_data)

    def test_the_record_carries_the_class_and_the_counters_rise(self) -> None:
        record = self.evidence["record"]
        self.assertEqual(record.get("error_class"), TRANSIENT)
        self.assertEqual(record.get("status"), 200, "the HTTP status really was 200")
        self.assertIn(record["error_class"], ERROR_CLASSES, "circuits and cooldowns consume it")
        self.assertEqual(
            self.evidence["count_after"],
            self.evidence["count_before"] + 1,
            "provider_error_count must count the mid-stream provider failure",
        )
        self.assertEqual(
            self.evidence["totals_after"]["errors"],
            self.evidence["totals_before"]["errors"] + 1,
            'totals["errors"] must rise by exactly one',
        )

    def test_the_failure_is_handled_not_escaped(self) -> None:
        # The log record is asserted on the explicitly captured handler
        # output, never on ambient stderr: with no handlers installed
        # ``lastResort`` writes to stderr, but pytest's logging plugin
        # installs its own handler and stderr stays empty — so a stderr
        # assertion would test the runner, not this behaviour.
        log = self.evidence["log"]
        self.assertIn("upstream failed mid-stream", log, "the failure must be logged")
        self.assertIn("Traceback", log, "the traceback must stay visible (t_7198bfa3)")
        self.assertIn("AdapterError", log)
        self.assertNotIn(
            "Exception occurred during processing",
            self.evidence["stderr"],
            "an AdapterError must not escape to socketserver.handle_error",
        )


# --------------------------------------------------------------------------
# row 2: the class is no longer dropped (auth)
# --------------------------------------------------------------------------
class MidStreamAuthClassTests(unittest.TestCase):
    evidence: ClassVar[dict[str, Any]] = {}
    harness: GatewayHarness | None = None

    @classmethod
    def setUpClass(cls) -> None:
        harness = _make_harness(DyingUpstreamAdapter(AUTH))
        ledger = harness.router._ledger
        assert ledger is not None
        count_before = provider_error_count(ledger)
        raw, stderr, log, timed_out = _raw_stream_request(
            harness,
            {"model": "coding", "messages": [{"role": "user", "content": "hi"}], "stream": True},
        )
        cls.harness = harness
        cls.evidence = {
            "body": _split(raw)[1],
            "stderr": stderr,
            "log": log,
            "timed_out": timed_out,
            "record": _record_for(ledger),
            "count_before": count_before,
            "count_after": provider_error_count(ledger),
        }

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.harness is not None:
            cls.harness.close()

    def test_the_record_says_auth(self) -> None:
        self.assertEqual(self.evidence["record"].get("error_class"), AUTH)
        self.assertEqual(self.evidence["count_after"], self.evidence["count_before"] + 1)

    def test_the_error_event_says_auth_too(self) -> None:
        payload = _error_frame(self.evidence["body"])
        assert payload is not None
        self.assertEqual(payload["error"]["type"], AUTH)
        self.assertNotIn(b"data: [DONE]", self.evidence["body"])


# --------------------------------------------------------------------------
# row 3: buffered client, streaming-only upstream that dies
# --------------------------------------------------------------------------
class BufferedLedgerTests(unittest.TestCase):
    def test_the_502_and_the_record_agree(self) -> None:
        adapter = DyingUpstreamAdapter(TRANSIENT)
        adapter.always_stream = True
        harness = _make_harness(adapter)
        self.addCleanup(harness.close)
        ledger = harness.router._ledger
        assert ledger is not None
        totals_before = dict(ledger.totals())
        count_before = provider_error_count(ledger)

        status, payload, _ = harness.request(
            "POST",
            "/v1/chat/completions",
            body={"model": "coding", "messages": [{"role": "user", "content": "hi"}]},
        )
        self.assertEqual(status, 502, payload)
        assert payload is not None
        self.assertEqual(payload["error"]["type"], TRANSIENT, payload)

        # The ledger must now say what the client and the access log say.
        deadline = time.monotonic() + 5.0
        event = None
        while time.monotonic() < deadline:
            recent = list(ledger.recent(limit=10))
            if recent and recent[0].get("status") == 502:
                event = recent[0]
                break
            time.sleep(0.05)
        assert event is not None, list(ledger.recent(limit=10))
        self.assertEqual(len(list(ledger.recent(limit=10))), 1, "amended in place, not re-recorded")
        self.assertEqual(event.get("error_class"), TRANSIENT)
        self.assertEqual(event.get("status"), 502)
        self.assertEqual(provider_error_count(ledger), count_before + 1)
        self.assertEqual(ledger.totals()["errors"], totals_before["errors"] + 1)

    def test_a_refusal_before_the_answer_is_still_recorded_at_route_time(self) -> None:
        """The contrast row: the record routing writes for itself stays put."""
        harness = _make_harness(
            FakeAdapter(fail_with=AdapterError(AUTH, provider_id=FAKE_PROVIDER))
        )
        self.addCleanup(harness.close)
        status, payload, _ = harness.request(
            "POST",
            "/v1/chat/completions",
            body={"model": "coding", "messages": [{"role": "user", "content": "hi"}]},
        )
        self.assertEqual(status, 502, payload)
        ledger = harness.router._ledger
        assert ledger is not None
        events = list(ledger.recent(limit=10))
        self.assertEqual(len(events), 1, events)
        self.assertEqual(events[0]["error_class"], AUTH)
        self.assertEqual(events[0]["status"], 502)


# --------------------------------------------------------------------------
# row 5: a non-AdapterError mid-stream still reaches stderr
# --------------------------------------------------------------------------
class UnhandledMidStreamErrorTests(unittest.TestCase):
    def test_a_non_adapter_error_still_prints_a_traceback(self) -> None:
        """Only AdapterError is handled; anything else keeps propagating.

        t_7198bfa3: the log is not blinded. The handler-level counterpart
        is ``MidFlightLoggingTests`` in the T11 acceptance file.
        """
        harness = _make_harness(ExplodingUpstreamAdapter())
        self.addCleanup(harness.close)
        ledger = harness.router._ledger
        assert ledger is not None
        _raw, stderr, _log, timed_out = _raw_stream_request(
            harness,
            {"model": "coding", "messages": [{"role": "user", "content": "hi"}], "stream": True},
        )
        self.assertFalse(timed_out, "the connection must be torn down, not hang")
        self.assertTrue(_wait_for(
            lambda: any(e.get("serving_model") == UPSTREAM_MODEL for e in ledger.recent(limit=10)),
            timeout=5.0,
        ), "no ledger record at all")
        self.assertIn("Traceback", stderr, "an unhandled application error must stay visible")
        self.assertIn("mid-stream boom", stderr)


if __name__ == "__main__":
    unittest.main()
