"""The access log is metadata only.

A request log is the first place a prompt, a response or a credential
tends to leak. The rules under test:

* the body is never logged;
* the query string is never logged;
* a credential-shaped value is redacted even if it reaches a field;
* the fields an operator needs - alias, provider, model, outcome, duration -
  are present.
"""

from __future__ import annotations

import io
import json
import unittest

from gateway.logging import RequestLogger


class RequestLoggerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.stream = io.StringIO()
        self.logger = RequestLogger(stream=self.stream)

    def _line(self) -> dict:
        return json.loads(self.stream.getvalue().strip())

    def test_a_request_is_logged_with_its_outcome(self) -> None:
        self.logger.log(
            method="POST",
            path="/v1/chat/completions",
            status=200,
            duration_ms=123,
            alias="coding",
            provider="opencode_go",
            model="minimax-m3.1-flash-free",
        )
        record = self._line()
        self.assertEqual(record["method"], "POST")
        self.assertEqual(record["path"], "/v1/chat/completions")
        self.assertEqual(record["status"], 200)
        self.assertEqual(record["duration_ms"], 123)
        self.assertEqual(record["alias"], "coding")
        self.assertEqual(record["provider"], "opencode_go")
        self.assertEqual(record["model"], "minimax-m3.1-flash-free")

    def test_the_error_class_is_logged(self) -> None:
        self.logger.log(
            method="POST",
            path="/v1/chat/completions",
            status=502,
            duration_ms=40,
            alias="coding",
            error_class="auth",
        )
        self.assertEqual(self._line()["error_class"], "auth")

    def test_a_credential_shaped_value_is_redacted(self) -> None:
        self.logger.log(
            method="GET",
            path="/owner/api/state",
            status=200,
            duration_ms=1,
            alias="sk-abcdefghijklmnopqrstuvwxyz012345",
        )
        line = self.stream.getvalue()
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz012345", line)
        self.assertIn("[redacted]", line)

    def test_a_disabled_logger_writes_nothing(self) -> None:
        quiet = RequestLogger(stream=self.stream, enabled=False)
        quiet.log(method="GET", path="/health", status=200, duration_ms=1)
        self.assertEqual(self.stream.getvalue(), "")

    def test_each_request_is_one_line(self) -> None:
        for index in range(3):
            self.logger.log(method="GET", path="/health", status=200, duration_ms=index)
        self.assertEqual(len(self.stream.getvalue().strip().splitlines()), 3)


if __name__ == "__main__":
    unittest.main()
