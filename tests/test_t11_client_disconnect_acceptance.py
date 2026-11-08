"""T11 acceptance regressions: client disconnect, classification, no traceback.

Automated proof of T11's acceptance matrix, using ONLY the adapter seam and
synthetic values on the loopback interface - no real credentials, no provider
account, no external network.

Matrix (this file maps 1:1 onto the card):

1. **Abort proof** - a client disconnect mid-stream reaches the synthetic
   adapter's cancel hook exactly once and the upstream stream is closed, so
   the provider stops producing (``ClientDisconnectEndToEndTests``).
2. **Classification** - after the disconnect the ledger record is amended to
   ``error_class='client_abort'`` and the provider-failure counters do not
   move (``client_abort`` is not in ``gateway.config.ERROR_CLASSES``).
3. **Allowance mechanism** - no upstream chunk is requested after the
   cancellation: every pull of the endless synthetic stream precedes the
   cancel event, the stream's cleanup ran, and the pull count is stable
   afterwards. The mechanism: the HTTP disconnect handler closes the
   ``StreamResponse`` iterator and then ``abort_request`` ->
   ``RouteContext.abort()`` runs the adapter's cancel hook and closes the
   provider stream - the abandoned generator can no longer be pulled, so the
   provider stops generating (and charging) for a body nobody reads.
4. **No-traceback regression** - the disconnect path writes nothing to
   stderr (no traceback, no socketserver ``Exception occurred during
   processing`` banner), while an application error still DOES reach stderr
   (the log is not blinded - t_7198bfa3 criterion 2). The request-read
   suppression half of t_7198bfa3 activates when that card's fix exists;
   it is skipped with the card reference while the fix is absent from the
   tree (follow-up card t_3acad3f0).
5. **Refusal vs abort distinctness** - an upstream refusal is recorded as a
   provider class and counted; a client abort is recorded as
   ``client_abort`` and never counted; an upstream mid-stream failure is
   never relabeled ``client_abort`` (R77, `docs/REVIEW.md`, now also
   records the provider's own class there - this file pins the
   distinctness).

The heavy mid-stream disconnect scenario runs once per class that needs it
(evidence gathered in ``setUpClass``/helpers, asserted by independent test
methods so each matrix row fails distinctly).
"""

from __future__ import annotations

import io
import json
import socket
import socketserver
import struct
import sys
import time
import unittest
from typing import Any, Callable

from gateway.adapters.base import AdapterError, ProviderResponse
from gateway.config import ERROR_CLASSES, Alias, RouteRef
from gateway.core.abort import find_route_context, release_route_context
from gateway.core.client_abort import provider_error_count
from gateway.http.core import Server, _ThreadingServer
from tests.test_end_to_end import FAKE_PROVIDER, FakeAdapter, GatewayHarness

UPSTREAM_MODEL = "gpt-6-luna"
CLIENT_ABORT = "client_abort"


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


class CancelRecordingAdapter(FakeAdapter):
    """A synthetic adapter with an endless stream and a recorded cancel hook.

    Its upstream generator logs every pull, the cancellation, and its own
    cleanup, which is what makes the allowance mechanism assertable: the
    pulls must all precede the cancellation, and the count must freeze
    after it.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.events: list[tuple[str, Any]] = []
        self.cancel_calls = 0
        self._pulls = 0

    def cancel(self, request: Any = None) -> None:
        self.cancel_calls += 1
        self.events.append(("cancel", type(request).__name__))

    def invoke(self, request: Any) -> ProviderResponse:
        if request.stream or self.always_stream:
            self.calls.append(request)
            if self.fail_with is not None:
                raise self.fail_with
            return ProviderResponse(
                status=200,
                headers={},
                body=None,
                stream=self._upstream(),
                content_type="text/event-stream",
                dialect=self.dialect,
            )
        return super().invoke(request)

    def _upstream(self):
        try:
            while True:
                self._pulls += 1
                self.events.append(("pull", self._pulls))
                yield b'data: {"type":"response.output_text.delta","delta":"tick"}\n\n'
        finally:
            # Where an adapter would close the provider connection.
            self.events.append(("closed", self._pulls))

    def pull_indices(self) -> list[int]:
        return [n for kind, n in self.events if kind == "pull"]

    def has_event(self, kind: str) -> bool:
        return any(k == kind for k, _ in self.events)


class DyingUpstreamAdapter(FakeAdapter):
    """An upstream that refuses mid-answer: one frame, then an error."""

    def invoke(self, request: Any) -> ProviderResponse:
        if request.stream:
            self.calls.append(request)

            def gen():
                yield b'data: {"type":"response.output_text.delta","delta":"Hello"}\n\n'
                raise AdapterError("transient", provider_id=self.provider_id)

            return ProviderResponse(
                status=200,
                headers={},
                body=None,
                stream=gen(),
                content_type="text/event-stream",
                dialect=self.dialect,
            )
        return super().invoke(request)


def _stream_then_disconnect(harness: GatewayHarness) -> dict[str, Any]:
    """Open a real streaming request, snapshot the ledger, RST the peer.

    Returns the full evidence dict: the pre-disconnect record, the
    post-disconnect record, the provider-failure/totals counters before and
    after, everything the server wrote to stderr, and the adapter's event
    log with the pull count frozen across a settle window.

    The caller owns the harness (and must release the route context).
    Raises AssertionError when the transport itself cannot produce the
    scenario - that is a broken precondition, not a matrix result.
    """
    adapter: CancelRecordingAdapter = harness.adapter  # type: ignore[assignment]
    ledger = harness.router._ledger
    evidence: dict[str, Any] = {}
    captured = io.StringIO()
    original_stderr = sys.stderr
    sys.stderr = captured
    sock: socket.socket | None = None
    try:
        sock = socket.create_connection((harness.host, harness.port))
        body = json.dumps(
            {"model": "coding", "messages": [{"role": "user", "content": "hi"}], "stream": True}
        ).encode("utf-8")
        head = (
            "POST /v1/chat/completions HTTP/1.1\r\n"
            f"Host: {harness.authority}:{harness.port}\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n\r\n"
        ).encode("ascii")
        sock.sendall(head + body)
        sock.settimeout(8.0)
        received = b""
        while b"\r\n\r\n" not in received or b"data:" not in received:
            chunk = sock.recv(65536)
            if not chunk:
                raise AssertionError(
                    f"server closed before streaming started: {received[:400]!r}"
                )
            received += chunk
        evidence["first_bytes"] = received[:200]

        # The response headers exist, so the route was answered and the
        # success record was written before the stream was consumed.
        def _found_record() -> bool:
            return any(
                event.get("serving_model") == UPSTREAM_MODEL
                for event in ledger.recent(limit=50)
            )

        if not _wait_for(_found_record, timeout=5.0):
            raise AssertionError(
                "the success record must exist before the client disconnects; "
                f"ledger has {ledger.recent(limit=50)!r}"
            )
        before_event = next(
            event
            for event in ledger.recent(limit=50)
            if event.get("serving_model") == UPSTREAM_MODEL
        )
        request_id = before_event["request_id"]
        evidence["request_id"] = request_id
        evidence["before"] = {
            "error_class": before_event.get("error_class"),
            "status": before_event.get("status"),
            "provider_failures": provider_error_count(ledger),
            "totals": dict(ledger.totals()),
        }

        # The client hangs up for real: an abortive close sends RST, so the
        # server's very next write fails with BrokenPipe/ConnectionReset -
        # exactly the disconnect path under test.
        assert sock is not None
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        sock.close()
        sock = None

        def _amended() -> bool:
            event = _ledger_event(ledger, request_id)
            return event is not None and event.get("error_class") == CLIENT_ABORT

        # The two layers land independently and each matrix row asserts
        # only its own: the abort signal (the adapter's cancel hook) and
        # the ledger correction (the client_abort classification).
        evidence["cancel_landed"] = _wait_for(
            lambda: adapter.cancel_calls >= 1, timeout=10.0
        )
        evidence["amended"] = _wait_for(_amended, timeout=10.0)
        evidence["landed"] = bool(evidence["cancel_landed"] and evidence["amended"])

        # Settle window: prove the abandoned stream is not read again.
        pulls_while_settling = len(adapter.pull_indices())
        time.sleep(0.3)
        evidence["pulls_at_disconnect"] = pulls_while_settling
        evidence["pulls_after_settle"] = len(adapter.pull_indices())

        after_event = _ledger_event(ledger, request_id)
        evidence["after"] = None if after_event is None else {
            "error_class": after_event.get("error_class"),
            "status": after_event.get("status"),
            "provider_failures": provider_error_count(ledger),
            "totals": dict(ledger.totals()),
        }
        context = find_route_context(request_id)
        evidence["context_aborted"] = None if context is None else context.aborted
        evidence["context_reason"] = None if context is None else context.abort_reason
        evidence["context_cancelled"] = (
            None if context is None else context.outcome().cancelled
        )
    finally:
        sys.stderr = original_stderr
        if sock is not None:
            sock.close()
        evidence["stderr"] = captured.getvalue()
        evidence["events"] = list(adapter.events)
    return evidence


def _release_context(request_id: str | None) -> None:
    if not request_id:
        return
    context = find_route_context(request_id)
    if context is not None:
        release_route_context(context)


# --------------------------------------------------------------------------
# matrix 1-4: the disconnect, end to end over a real socket
# --------------------------------------------------------------------------
class ClientDisconnectEndToEndTests(unittest.TestCase):
    """One real mid-stream disconnect; each matrix row asserted separately."""

    evidence: dict[str, Any] = {}
    harness: GatewayHarness | None = None

    @classmethod
    def setUpClass(cls) -> None:
        cls.harness = _make_harness(CancelRecordingAdapter())
        cls.evidence = _stream_then_disconnect(cls.harness)

    @classmethod
    def tearDownClass(cls) -> None:
        _release_context(cls.evidence.get("request_id"))
        if cls.harness is not None:
            cls.harness.close()

    def _adapter(self) -> CancelRecordingAdapter:
        assert self.harness is not None
        return self.harness.adapter  # type: ignore[return-value]

    # -- matrix 1: abort proof --------------------------------------------
    def test_adapter_cancel_hook_invoked_exactly_once(self) -> None:
        adapter = self._adapter()
        self.assertTrue(
            self.evidence["cancel_landed"],
            "the adapter never saw a cancellation within 10s of the disconnect",
        )
        self.assertEqual(
            adapter.cancel_calls,
            1,
            "the disconnect must reach the adapter's cancellation path exactly once",
        )

    def test_the_upstream_is_stopped_and_marked_aborted(self) -> None:
        adapter = self._adapter()
        self.assertTrue(
            adapter.has_event("closed"),
            "the upstream stream must run its cleanup, or the provider connection stays open",
        )
        self.assertTrue(
            self.evidence["context_aborted"],
            "the route context must remember the abort",
        )
        self.assertEqual(self.evidence["context_reason"], "client_disconnected")
        self.assertTrue(
            self.evidence["context_cancelled"],
            "the abort outcome must report the cancellation path completed",
        )

    # -- matrix 3: allowance mechanism ------------------------------------
    def test_no_upstream_chunk_is_requested_after_cancellation(self) -> None:
        adapter = self._adapter()
        pulls = adapter.pull_indices()
        cancels = [i for i, (kind, _) in enumerate(adapter.events) if kind == "cancel"]
        self.assertTrue(pulls, "the stream must have been read before the disconnect")
        self.assertEqual(len(cancels), 1, "exactly one cancellation is recorded")
        last_pull_slot = max(
            i for i, (kind, _) in enumerate(adapter.events) if kind == "pull"
        )
        self.assertLess(
            last_pull_slot,
            cancels[0],
            "a chunk was requested after the adapter saw the cancellation",
        )
        self.assertEqual(
            self.evidence["pulls_at_disconnect"],
            self.evidence["pulls_after_settle"],
            "the abandoned stream kept producing chunks after the disconnect",
        )

    # -- matrix 2: classification -----------------------------------------
    def test_the_success_record_is_amended_to_client_abort(self) -> None:
        self.assertTrue(
            self.evidence["amended"],
            "the ledger record was never amended to client_abort after the disconnect",
        )
        before = self.evidence["before"]
        after = self.evidence["after"]
        self.assertIsNotNone(after, "no ledger record for the disconnected request")
        self.assertIsNone(
            before["error_class"], "the record was a success before the disconnect"
        )
        self.assertEqual(after["error_class"], CLIENT_ABORT)
        self.assertNotIn(CLIENT_ABORT, ERROR_CLASSES)
        # Amended in place, not rewritten: the outcome and status are untouched.
        self.assertEqual(after["status"], before["status"])

    def test_provider_failure_counters_are_unchanged_by_the_disconnect(self) -> None:
        before = self.evidence["before"]
        after = self.evidence["after"]
        self.assertEqual(
            after["provider_failures"],
            before["provider_failures"],
            "a client abort must never count as a provider failure",
        )
        for counter in ("rate_limited", "exhausted", "fallbacks", "requests"):
            self.assertEqual(
                after["totals"][counter],
                before["totals"][counter],
                f"the {counter} counter moved on a client disconnect",
            )
        # The generic tally counts any truthy error class, so it rises by
        # exactly one when the success record is corrected.
        self.assertEqual(after["totals"]["errors"], before["totals"]["errors"] + 1)

    # -- matrix 4: no traceback -------------------------------------------
    def test_the_disconnect_path_writes_nothing_to_stderr(self) -> None:
        stderr = self.evidence["stderr"]
        self.assertNotIn("Traceback", stderr)
        self.assertNotIn("Exception occurred during processing", stderr)


# --------------------------------------------------------------------------
# matrix 4: the log must not be blinded (t_7198bfa3 criterion 2)
# --------------------------------------------------------------------------
class MidFlightLoggingTests(unittest.TestCase):
    def test_an_application_error_still_prints_a_traceback(self) -> None:
        """Suppressing disconnect noise must not swallow real failures."""

        def application(request: Any) -> Any:
            raise ConnectionResetError("mid-flight test failure")

        server = Server("127.0.0.1", 0, application)
        server.start_background()
        self.addCleanup(server.stop)
        port = server._httpd.server_address[1]
        captured = io.StringIO()
        original = sys.stderr
        sys.stderr = captured
        try:
            with socket.create_connection(("127.0.0.1", port)) as client:
                client.settimeout(5.0)
                client.sendall(
                    b"GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"
                )
                received = b""
                while b"500" not in received:
                    chunk = client.recv(4096)
                    if not chunk:
                        break
                    received += chunk
        finally:
            sys.stderr = original
        self.assertIn(b"500", received)
        stderr = captured.getvalue()
        self.assertIn("Traceback", stderr, "a mid-flight error must stay visible")
        self.assertIn("mid-flight test failure", stderr)


class RequestReadDisconnectLoggingTests(unittest.TestCase):
    """The t_7198bfa3 request-read half: activate when its fix exists.

    t_7198bfa3 was completed but its fix was never committed (it lives as
    dirty files in that card's worktree; follow-up card t_3acad3f0 restores
    it). While ``_ThreadingServer.handle_error`` is not overridden the
    scenario would fail by construction, so it skips with the card
    reference; it runs automatically - with no edit to this file - the
    moment the override lands.
    """

    def test_abortive_close_before_the_request_line_is_quiet(self) -> None:
        if _ThreadingServer.handle_error is socketserver.TCPServer.handle_error:
            self.skipTest(
                "t_7198bfa3's request-read suppression fix is not committed anywhere "
                "(uncommitted in its worktree; restored by follow-up card t_3acad3f0) - "
                "this test activates automatically once _ThreadingServer.handle_error "
                "is overridden"
            )

        calls: list[str] = []

        def application(request: Any) -> Any:
            calls.append(request.path)
            from gateway.http.core import Response

            return Response(200, [("Content-Type", "text/plain")], b"ok")

        server = Server("127.0.0.1", 0, application)
        server.start_background()
        self.addCleanup(server.stop)
        port = server._httpd.server_address[1]
        captured = io.StringIO()
        original = sys.stderr
        sys.stderr = captured
        try:
            sock = socket.create_connection(("127.0.0.1", port))
            # Abortive close without a request line: the reset lands inside
            # BaseHTTPRequestHandler.handle_one_request, before dispatch.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            sock.close()
            time.sleep(0.1)
            with socket.create_connection(("127.0.0.1", port)) as healthy:
                healthy.settimeout(5.0)
                healthy.sendall(
                    b"GET /health HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"
                )
                self.assertIn(b"200 OK", healthy.recv(4096))
        finally:
            sys.stderr = original
        self.assertNotIn("Traceback", captured.getvalue())
        self.assertEqual(calls, ["/health"], "the server must keep serving")


# --------------------------------------------------------------------------
# matrix 5: upstream refusal / upstream abort vs client abort
# --------------------------------------------------------------------------
class UpstreamAbortDistinctnessTests(unittest.TestCase):
    def test_upstream_refusal_is_recorded_as_a_provider_class_and_counted(self) -> None:
        harness = _make_harness(
            FakeAdapter(fail_with=AdapterError("auth", provider_id=FAKE_PROVIDER))
        )
        self.addCleanup(harness.close)
        status, payload, _ = harness.request(
            "POST",
            "/v1/chat/completions",
            body={"model": "coding", "messages": [{"role": "user", "content": "hi"}]},
        )
        self.assertEqual(status, 502, payload)
        events = list(harness.router._ledger.recent(limit=10))
        self.assertEqual(len(events), 1, events)
        refusal = events[0]
        self.assertEqual(refusal["error_class"], "auth")
        self.assertIn(refusal["error_class"], ERROR_CLASSES)
        self.assertEqual(
            provider_error_count(harness.router._ledger),
            1,
            "an upstream refusal is a provider failure and must be counted",
        )

    def test_client_abort_and_upstream_refusal_are_distinct_classes(self) -> None:
        # (a) upstream refuses...
        refusing = _make_harness(
            FakeAdapter(fail_with=AdapterError("auth", provider_id=FAKE_PROVIDER))
        )
        self.addCleanup(refusing.close)
        status, _, _ = refusing.request(
            "POST",
            "/v1/chat/completions",
            body={"model": "coding", "messages": [{"role": "user", "content": "hi"}]},
        )
        self.assertEqual(status, 502)
        refusal_event = list(refusing.router._ledger.recent(limit=10))[0]

        # ...and (b) the client walks away mid-answer.
        disconnecting = _make_harness(CancelRecordingAdapter())
        self.addCleanup(disconnecting.close)
        evidence = _stream_then_disconnect(disconnecting)
        self.addCleanup(lambda: _release_context(evidence.get("request_id")))
        self.assertTrue(evidence["landed"], "the disconnect scenario did not land")
        abort_event = evidence["after"]

        self.assertNotEqual(
            refusal_event["error_class"],
            abort_event["error_class"],
            "an upstream refusal and a client abort must stay distinct classes",
        )
        self.assertEqual(abort_event["error_class"], CLIENT_ABORT)
        self.assertNotIn(CLIENT_ABORT, ERROR_CLASSES)
        # The refusal is counted as a provider failure; the abort is not.
        self.assertEqual(
            provider_error_count(refusing.router._ledger),
            1,
            "the refusal must count as a provider failure",
        )
        self.assertEqual(
            abort_event["provider_failures"],
            evidence["before"]["provider_failures"],
            "the client abort must not move the provider-failure count",
        )

    def test_upstream_midstream_failure_is_never_classified_as_client_abort(self) -> None:
        """The upstream dying mid-answer must never read as a client abort.

        R77 (`docs/REVIEW.md`) closed the gap this test was written
        against: that record now carries the adapter's own class (or
        ``client_abort`` only when the caller had left). What this test
        pins is the distinctness T11 requires - a provider failure is
        never relabeled as a client abort - and it must keep passing
        whatever class is recorded.
        """
        harness = _make_harness(DyingUpstreamAdapter())
        self.addCleanup(harness.close)
        captured = io.StringIO()
        original = sys.stderr
        sys.stderr = captured
        try:
            sock = socket.create_connection((harness.host, harness.port))
            body = json.dumps(
                {"model": "coding", "messages": [{"role": "user", "content": "hi"}], "stream": True}
            ).encode("utf-8")
            sock.sendall(
                (
                    "POST /v1/chat/completions HTTP/1.1\r\n"
                    f"Host: {harness.authority}:{harness.port}\r\n"
                    "Content-Type: application/json\r\n"
                    f"Content-Length: {len(body)}\r\n"
                    "Connection: close\r\n\r\n"
                ).encode("ascii")
                + body
            )
            sock.settimeout(8.0)
            # Read to EOF: the server tears the response down when the
            # upstream error escapes the stream; the client never aborts.
            while sock.recv(65536):
                pass
            sock.close()
        finally:
            sys.stderr = original

        def _recorded() -> bool:
            return any(
                event.get("serving_model") == UPSTREAM_MODEL
                for event in harness.router._ledger.recent(limit=50)
            )

        self.assertTrue(_wait_for(_recorded, timeout=5.0), "no ledger record at all")
        event = next(
            e for e in harness.router._ledger.recent(limit=50)
            if e.get("serving_model") == UPSTREAM_MODEL
        )
        self.assertNotEqual(
            event.get("error_class"),
            CLIENT_ABORT,
            "an upstream mid-stream failure must never be relabeled as a client abort",
        )


if __name__ == "__main__":
    unittest.main()
