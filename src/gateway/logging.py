"""Request logging.

The gateway used to log only its startup line, so a failing request left
nothing to look at. This writes one structured line per request.

The rules are the observability rules: **metadata only**.

* The method and the path are logged, without the query string - a query
  string can carry data the operator did not intend to publish.
* The body is never logged, so a prompt or a response cannot appear.
* Credentials, tokens and device codes never appear, because they are never
  in any of the recorded fields; the redaction table is applied to the line
  anyway, as a second line of defence.
* The alias, the serving provider and model, the outcome and the duration
  are logged, because those are what an operator needs to diagnose a
  failure.

Each line is one JSON object, so it can be read by eye or by a log
collector.
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any, TextIO

from gateway.observability import redact


class RequestLogger:
    """Writes one metadata-only line per request."""

    def __init__(self, stream: TextIO | None = None, enabled: bool = True) -> None:
        self._stream = stream if stream is not None else sys.stdout
        self._enabled = enabled

    def log(
        self,
        method: str,
        path: str,
        status: int,
        duration_ms: int,
        alias: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        error_class: str | None = None,
        streamed: bool = False,
        action: str | None = None,
        machine_key_id: str | None = None,
        machine_key_target: str | None = None,
    ) -> None:
        if not self._enabled:
            return
        record: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z",
            "method": method,
            "path": path,
            "status": status,
            "duration_ms": duration_ms,
        }
        if action:
            record["action"] = action
        # Audit identity for a machine-key mutation: the *display id*
        # only. The key value is never a field of this record, so no log
        # line - and no redaction miss - can carry it.
        if machine_key_id:
            record["machine_key_id"] = machine_key_id
        # The key a create/rotate/revoke *acted on*, so a revocation
        # stays attributable after the store deletes the record. Also a
        # display id only: the value is not a field here either.
        if machine_key_target:
            record["machine_key_target"] = machine_key_target
        if alias:
            record["alias"] = alias
        if provider:
            record["provider"] = provider
        if model:
            record["model"] = model
        if error_class:
            record["error_class"] = error_class
        if streamed:
            record["streamed"] = True
        line = redact(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
        try:
            self._stream.write(line + "\n")
            self._stream.flush()
        except (OSError, ValueError):  # pragma: no cover - logging must never break a request
            pass
