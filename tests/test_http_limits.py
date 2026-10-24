"""HTTP capacity and local per-peer rate-limit regressions."""

from __future__ import annotations

import json
import threading
import time
import unittest
import urllib.error
import urllib.request

from gateway.http.core import Server
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
            with urllib.request.urlopen(base + "/health", timeout=2) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(json.load(response), {"ok": True})
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
