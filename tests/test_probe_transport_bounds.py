"""Bounds the probe transport must hold against a hostile or broken upstream.

Every test drives the real path a probe run takes -
``run_suite`` -> ``OpenCodeGoAdapter.capability_probe_request`` ->
``UpstreamClient`` - against a synthetic loopback upstream. Nothing here
contacts a provider, and no test here needs more than a few seconds.

The upstream under test misbehaves in exactly the ways a security re-gate
found this transport to be unprotected against:

* it trickles body bytes faster than any armed socket timeout, so every
  ``recv()`` keeps succeeding while the run's absolute deadline should not
  move;
* it withholds the response head, or trickles the head instead of the body,
  where no check between body chunks can run;
* it answers a redirect - or a plain 2xx - with a body far larger than the
  response cap.

Against those, three properties have to hold: the run ends *at* its absolute
deadline with ``blocked_reason="deadline"``, a cancellation made while bytes
are still arriving ends the run promptly with ``blocked_reason="cancelled"``,
and no branch of the client buffers more than ``max_response_bytes``.
"""
from __future__ import annotations

import dataclasses
import http.server
import json
import socketserver
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any, ClassVar

from gateway.adapters.base import AdapterError
from gateway.adapters.opencode_go import OpenCodeGoAdapter
from gateway.capability_probe import PROBE_PROTOCOLS, ProbeOutcome, run_suite

MODEL = "mimo-v2.6-flash"
PROTOCOL = PROBE_PROTOCOLS[("opencode_go", MODEL)]
#: Bytes the client may be made to read past the cap: the cap itself plus
#: whatever sits in the kernel's socket buffers when it stops reading. A
#: client that buffers the response instead of cutting it at the cap lets the
#: upstream push the whole declared body, which is orders of magnitude more.
CAP = PROTOCOL.max_response_bytes
CAP_SLACK = 8 * 1024 * 1024
#: One trickled byte, faster than any socket timeout this client arms, so a
#: read keeps "progressing" without ever completing a chunk.
TRICKLE_INTERVAL = 0.02
#: The declared size of an oversized or trickled body: far beyond the cap,
#: and far beyond what the client may buffer.
HUGE = 64 * 1024 * 1024


class _Credentials:
    def read(self) -> dict[str, str]:
        return {"api_key": "synthetic-probe-key-0123456789"}


def _answer(request_body: bytes) -> bytes:
    """A well-formed Chat Completions answer that echoes the run nonce."""
    try:
        request = json.loads(request_body.decode("utf-8"))
    except ValueError:
        request = {}
    messages = request.get("messages") if isinstance(request, dict) else None
    content = messages[0].get("content") if messages else None
    nonce = content.rsplit(": ", 1)[-1] if isinstance(content, str) else "nope"
    payload = {
        "model": MODEL,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": nonce},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 12, "completion_tokens": 6, "cost": 0.00042},
    }
    return json.dumps(payload).encode("utf-8")


class _Upstream(http.server.BaseHTTPRequestHandler):
    """A loopback upstream that misbehaves one way at a time."""

    protocol_version = "HTTP/1.1"
    mode: ClassVar[str] = "happy"
    requests: ClassVar[int] = 0
    bytes_sent: ClassVar[int] = 0
    write_error: ClassVar[str | None] = None

    def log_message(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    # --- helpers -------------------------------------------------------

    def _head(self, status: int, length: int) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(length))
        if 300 <= status < 400:
            self.send_header("Location", "/somewhere-else")
        self.end_headers()

    def _write(self, chunk: bytes) -> bool:
        """Write a body chunk, reporting how far the client let us get."""
        try:
            self.wfile.write(chunk)
        except OSError as exc:
            type(self).write_error = type(exc).__name__
            return False
        type(self).bytes_sent += len(chunk)
        return True

    def _pump(self, total: int) -> None:
        """Push a whole declared body, or stop when the peer hangs up."""
        sent = 0
        while sent < total:
            block = b"x" * min(65_536, total - sent)
            if not self._write(block):
                return
            sent += len(block)

    def _trickle_head(self) -> None:
        """Send the response head one byte at a time.

        No check of the client's own runs while ``http.client`` is inside
        ``getresponse()``, so this is the phase where only the watchdog's
        socket shutdown can enforce the run's bounds.
        """
        template = (
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: 999999\r\n"
            b"\r\n"
        )
        for index in range(len(template)):
            if not self._write(template[index : index + 1]):
                return
            time.sleep(0.1)

    # --- the actual upstream -------------------------------------------

    def do_POST(self) -> None:
        type(self).requests += 1
        length = int(self.headers.get("Content-Length") or 0)
        request_body = self.rfile.read(length) if length else b""
        mode = type(self).mode

        if mode == "trickle_head":
            self._trickle_head()
            return

        if mode == "stall_head":
            # Withhold the head entirely: the request is sent, nothing
            # answers, and a cancellation still has to be honoured.
            time.sleep(8.0)
            try:
                body = _answer(request_body)
                self._head(200, len(body))
                self._write(body)
            except OSError:
                pass
            return

        if mode == "trickle_body":
            # 1 byte / 20ms, bounded to 20s so a regression fails instead of
            # hanging the suite. Faster than any armed socket timeout.
            self._head(200, HUGE // 64)
            for _ in range(1000):
                if not self._write(b"q"):
                    return
                time.sleep(TRICKLE_INTERVAL)
            return

        if mode == "redirect_big":
            self._head(302, HUGE)
            self._pump(HUGE)
            return

        if mode == "oversized":
            self._head(200, HUGE)
            self._pump(HUGE)
            return

        body = _answer(request_body)
        if mode == "slow_body":
            # Progress with pauses longer than a tenth of a second: a body
            # that keeps arriving must never be abandoned as stalled.
            self._head(200, len(body))
            half = len(body) // 2
            if not self._write(body[:half]):
                return
            time.sleep(0.35)
            self._write(body[half:])
            return

        self._head(200, len(body))
        self._write(body)


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = True


class ProbeTransportBoundsTests(unittest.TestCase):
    """The run's own bounds hold however the upstream behaves."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = _Server(("127.0.0.1", 0), _Upstream)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls._tmp = tempfile.TemporaryDirectory()
        snapshot = Path(cls._tmp.name) / "capability_snapshot.json"
        snapshot.write_text(
            json.dumps({"models": {MODEL: {"dialect": "openai_chat", "endpoint_path": "/probe"}}})
        )
        cls.adapter = OpenCodeGoAdapter(
            _Credentials(),
            base_url=f"http://127.0.0.1:{cls.server.server_address[1]}",
            snapshot_path=snapshot,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls._tmp.cleanup()

    def setUp(self) -> None:
        _Upstream.mode = "happy"
        _Upstream.requests = 0
        _Upstream.bytes_sent = 0
        _Upstream.write_error = None

    # --- helpers -------------------------------------------------------

    def _run(
        self,
        *,
        deadline: float,
        request_timeout: float,
        cancel_at: float | None = None,
        mode: str = "happy",
    ) -> tuple[ProbeOutcome, float]:
        """Run the suite over the real transport and report how long it took.

        The cancellation event is always passed, never unset unless
        *cancel_at* is given: ``handle_capability_run`` always hands the
        transport the RunGate's event, so "armed but unset" is the shape
        every production run has.
        """
        _Upstream.mode = mode
        protocol = dataclasses.replace(
            PROTOCOL,
            run_deadline_seconds=deadline,
            request_timeout_seconds=request_timeout,
        )
        cancel = threading.Event()
        if cancel_at is not None:
            timer = threading.Timer(cancel_at, cancel.set)
            timer.daemon = True
            timer.start()
        started = time.monotonic()
        outcome = run_suite(
            protocol,
            ["text_generation"],
            self.adapter.capability_probe_request,
            cancel_event=cancel,
        )
        return outcome, time.monotonic() - started

    def _transport_call(self, *, max_response_bytes: int = CAP) -> None:
        """One probe request, as the adapter makes it: one shot, capped."""
        self.adapter.capability_probe_request(
            {"model": MODEL, "messages": []},
            timeout=10.0,
            connect_timeout=5.0,
            session_id="s" * 32,
            deadline=time.monotonic() + 10.0,
            max_response_bytes=max_response_bytes,
        )

    # --- the run's absolute deadline ------------------------------------

    def test_a_trickling_body_stops_at_the_absolute_deadline(self) -> None:
        """Bytes that keep arriving must not move the run's deadline.

        The per-request timeout is 30s against a 3s run deadline: ending
        near 3s is what proves the *absolute* bound fired. Before the fix
        this run ended only when the upstream stopped trickling.
        """
        outcome, elapsed = self._run(deadline=3.0, request_timeout=30.0, mode="trickle_body")
        self.assertLess(elapsed, 6.0, f"a trickling body ran {elapsed:.1f}s against a 3s deadline")
        self.assertGreaterEqual(elapsed, 2.0, f"the run gave up at {elapsed:.2f}s, before its deadline")
        self.assertEqual(outcome.blocked_reason, "deadline")
        self.assertEqual(outcome.results[0]["status"], "inconclusive")
        self.assertEqual(outcome.request_count, 1)

    def test_a_trickling_response_head_stops_at_the_absolute_deadline(self) -> None:
        """The same bound where no between-chunk check of ours can run.

        A head trickled one byte at a time keeps every recv() inside
        ``getresponse()`` succeeding, so only interrupting the socket ends
        the run at its deadline instead of whenever the head completes.
        """
        outcome, elapsed = self._run(deadline=3.0, request_timeout=30.0, mode="trickle_head")
        self.assertLess(elapsed, 6.0, f"a trickled head ran {elapsed:.1f}s against a 3s deadline")
        self.assertGreaterEqual(elapsed, 2.0, f"the run gave up at {elapsed:.2f}s, before its deadline")
        self.assertEqual(outcome.blocked_reason, "deadline")
        self.assertEqual(outcome.results[0]["status"], "inconclusive")

    # --- cancellation ---------------------------------------------------

    def test_a_cancellation_during_a_trickling_body_is_honoured(self) -> None:
        """A cancel at 1s must end a body that never stops arriving."""
        outcome, elapsed = self._run(
            deadline=65.0, request_timeout=60.0, cancel_at=1.0, mode="trickle_body"
        )
        self.assertLess(elapsed, 5.0, f"cancellation took {elapsed:.1f}s against a trickling body")
        self.assertTrue(outcome.cancelled, f"blocked_reason={outcome.blocked_reason!r}")
        self.assertEqual(outcome.blocked_reason, "cancelled")
        self.assertEqual(outcome.results[0]["status"], "inconclusive")

    def test_a_cancellation_while_the_response_head_is_withheld_is_honoured(self) -> None:
        """A cancel at 0.6s must not wait for an upstream that never answers.

        The per-request timeout is 15s here, so without the interruption the
        run would end at 15s - as a request timeout, not as a cancellation.
        """
        outcome, elapsed = self._run(
            deadline=30.0, request_timeout=15.0, cancel_at=0.6, mode="stall_head"
        )
        self.assertLess(elapsed, 4.0, f"cancellation took {elapsed:.1f}s against a silent head")
        self.assertTrue(outcome.cancelled, f"blocked_reason={outcome.blocked_reason!r}")
        self.assertEqual(outcome.blocked_reason, "cancelled")

    # --- the response cap -----------------------------------------------

    def test_an_oversized_2xx_body_is_cut_at_the_response_cap(self) -> None:
        """The cap is enforced where it was designed to be: the 2xx body."""
        outcome, elapsed = self._run(deadline=30.0, request_timeout=30.0, mode="oversized")
        self.assertLess(elapsed, 15.0, f"the oversized body was read for {elapsed:.1f}s")
        self.assertEqual(outcome.blocked_reason, "prior_failure")
        entry = outcome.results[0]
        self.assertEqual(entry["status"], "inconclusive")
        self.assertEqual(entry["error_class"], "transport")
        self.assertLess(
            _Upstream.bytes_sent,
            CAP_SLACK,
            f"the upstream pushed {_Upstream.bytes_sent} bytes against a {CAP} byte cap",
        )
        # ...and the transport says so on the way out. The run's own entry is
        # evidence-only, so the refusal itself is asserted at the boundary
        # that raises it.
        with self.assertRaises(AdapterError) as caught:
            self._transport_call()
        self.assertIn("byte limit", str(caught.exception))

    def test_a_redirect_body_over_the_response_cap_is_never_buffered(self) -> None:
        """A 3xx must not be a way around the cap: its body is discarded."""
        _Upstream.mode = "redirect_big"
        with self.assertRaises(AdapterError):
            self._transport_call()
        self.assertEqual(_Upstream.requests, 1, "a redirect must not replay the request")
        self.assertLess(
            _Upstream.bytes_sent,
            CAP_SLACK,
            f"the redirect body pushed {_Upstream.bytes_sent} bytes against a {CAP} byte cap",
        )

    def test_a_body_exactly_at_the_cap_is_still_accepted(self) -> None:
        """The cap is a bound on allocation, not on a legitimate response."""
        outcome, elapsed = self._run(deadline=10.0, request_timeout=10.0)
        self.assertIsNone(outcome.blocked_reason)
        self.assertEqual(outcome.results[0]["status"], "observed_supported")
        self.assertLess(elapsed, 5.0)

    # --- progress is never mistaken for a stall --------------------------

    def test_a_slow_but_progressing_body_is_not_abandoned(self) -> None:
        """Pauses shorter than the read budget are not a failure.

        The read budget is what judges silence; a body that keeps arriving
        - even slowly - must run to completion, cancellation armed or not.
        """
        outcome, elapsed = self._run(deadline=10.0, request_timeout=15.0, mode="slow_body")
        self.assertIsNone(outcome.blocked_reason, f"blocked_reason={outcome.blocked_reason!r}")
        self.assertEqual(outcome.results[0]["status"], "observed_supported")
        self.assertLess(elapsed, 6.0, f"a progressing body took {elapsed:.1f}s")


if __name__ == "__main__":
    unittest.main()
