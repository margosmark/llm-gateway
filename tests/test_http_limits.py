"""HTTP capacity and local per-peer rate-limit regressions."""

from __future__ import annotations

import json
import threading
import time
import unittest
import urllib.error
import urllib.request

from gateway.http.core import MAX_JSON_NESTING, Server, _check_json_nesting
from gateway.http.rate_limit import RateLimiter
from gateway.http.app import _read_static


class RateLimiterTests(unittest.TestCase):
    def test_console_calls_it_local_protection_not_access_control(self) -> None:
        html = _read_static("console.html").decode("utf-8")
        self.assertIn("Local capacity protection", html)
        self.assertIn("not an access-control boundary", html)
        self.assertIn("any device that can reach this address", html)

    def test_rolling_window_is_keyed_by_peer_ip_and_expires(self) -> None:
        limiter = RateLimiter(2, 0.05)
        self.assertTrue(limiter.allow("127.0.0.1"))
        self.assertTrue(limiter.allow("127.0.0.1"))
        self.assertFalse(limiter.allow("127.0.0.1"))
        self.assertTrue(limiter.allow("127.0.0.2"))
        time.sleep(0.06)
        self.assertTrue(limiter.allow("127.0.0.1"))

    def test_invalid_limits_are_refused(self) -> None:
        with self.assertRaises(ValueError):
            RateLimiter(0, 60)


class WorkerPoolTests(unittest.TestCase):
    def test_excess_connections_get_429_and_service_recovers(self) -> None:
        entered = threading.Event()
        release = threading.Event()

        def handler(request):
            if request.path == "/slow":
                entered.set()
                release.wait(2)
            from gateway.http.core import json_response
            return json_response({"ok": True})

        server = Server("127.0.0.1", 0, handler, max_workers=1, rate_limit_requests=100, rate_limit_window_seconds=60)
        server.start_background()
        base = f"http://127.0.0.1:{server.actual_port}"
        result = []

        def slow_request() -> None:
            try:
                urllib.request.urlopen(base + "/slow", timeout=3).read()
            except Exception as exc:  # result is only used to join cleanly
                result.append(exc)

        worker = threading.Thread(target=slow_request)
        try:
            worker.start()
            self.assertTrue(entered.wait(1))
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(base + "/fast", timeout=1)
            self.assertEqual(caught.exception.code, 429)
            payload = json.loads(caught.exception.read())
            self.assertEqual(payload["error"]["type"], "rate_limited")
            release.set()
            worker.join(2)
            # The client reads the response before the server thread has
            # necessarily returned its worker slot, so the very next
            # request can still see the pool busy on a loaded machine -
            # this flaked on base ac515eb itself (2 failures in 8 isolated
            # runs there). Probe until the service has recovered instead
            # of firing once; only the worker cap may refuse here.
            recovered = False
            deadline = time.monotonic() + 2.0
            while not recovered and time.monotonic() < deadline:
                try:
                    with urllib.request.urlopen(base + "/health", timeout=2) as response:
                        self.assertEqual(response.status, 200)
                        self.assertEqual(json.load(response), {"ok": True})
                        recovered = True
                except urllib.error.HTTPError as exc:
                    self.assertEqual(exc.code, 429)
                    time.sleep(0.05)
            self.assertTrue(recovered, "the worker pool never returned its slot")
        finally:
            release.set()
            worker.join(2)
            server.stop()

    def test_per_client_rate_limit_refuses_with_429_and_recovers_after_window(self) -> None:
        from gateway.http.core import json_response
        server = Server("127.0.0.1", 0, lambda request: json_response({"ok": True}),
                        max_workers=2, rate_limit_requests=1, rate_limit_window_seconds=0.05)
        server.start_background()
        url = f"http://127.0.0.1:{server.actual_port}/health"
        try:
            with urllib.request.urlopen(url) as response:
                self.assertEqual(response.status, 200)
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(url)
            self.assertEqual(caught.exception.code, 429)
            self.assertEqual(json.loads(caught.exception.read())["error"]["type"], "rate_limited")
            time.sleep(0.06)
            with urllib.request.urlopen(url) as response:
                self.assertEqual(response.status, 200)
        finally:
            server.stop()


class JsonNestingPreflightTests(unittest.TestCase):
    r"""SEC-F7b: the pre-decode nesting preflight must track JSON escapes.

    ``_check_json_nesting`` walks the raw body one character at a time, so
    it has to carry escape state: ``\"`` inside a string is a literal quote
    and does *not* close the string, and ``\\`` is a literal backslash whose
    following character is unescaped. The branch that set that state used to
    compare a single character against a two-character literal - dead code,
    never taken - so one escaped quote flipped guard/parser parity: a
    string's brackets were then counted as structure (valid prompt refused
    with a 400) or real structure went uncounted (the CPython recursion the
    preflight exists to prevent reached the decoder and escaped as a 500).
    """

    _BS = chr(92)

    def test_an_escaped_quote_keeps_the_scanner_inside_the_string(self) -> None:
        # Exploit B: json.loads accepts this body, so the preflight must
        # not count the bracket blob that lives inside the string.
        text = '{"k":"' + self._BS + '"' + "[" * 3500 + '"}'
        json.loads(text)
        _check_json_nesting(text)  # must not raise

    def test_an_escaped_backslash_quote_pair_keeps_the_scanner_inside(self) -> None:
        # The string content is a\"[...: the ``\\`` and the ``\"`` are both
        # escapes, so every bracket after them is still string payload.
        text = '{"k":"a' + self._BS * 3 + '"' + "[" * 3500 + '"}'
        json.loads(text)
        _check_json_nesting(text)  # must not raise

    def test_structure_behind_an_escaped_quote_is_still_counted(self) -> None:
        # Exploit A: {"k":"\"","d":[[[...]]]} - the first quote after
        # the backslash is payload (the string stays open), the second one
        # closes it, and the deep array behind it is real structure the
        # preflight must still refuse before the decoder runs.
        text = ('{"k":"' + self._BS + '"' + '"' + ',"d":'
                + "[" * (MAX_JSON_NESTING + 1) + "]" * (MAX_JSON_NESTING + 1) + "}")
        with self.assertRaises(ValueError):
            _check_json_nesting(text)

    def test_the_closing_quote_after_an_escaped_backslash_still_closes(self) -> None:
        # ``\\`` must consume exactly one character: the quote after it is
        # structural again, so the deep array behind it is counted.
        text = ('{"k":"a' + self._BS * 2 + '"' + ',"d":'
                + "[" * (MAX_JSON_NESTING + 1) + "]" * (MAX_JSON_NESTING + 1) + "}")
        with self.assertRaises(ValueError):
            _check_json_nesting(text)
