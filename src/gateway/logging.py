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
import logging
import sys
import time
from typing import Any, TextIO

from gateway.observability import redact

#: The gateway's operational logger: a *handled* failure, recorded at
#: ERROR with its traceback. It is deliberately separate from
#: :class:`RequestLogger`, which is the metadata-only access log.
#:
#: Nothing configures it, so records fall through to the logging
#: module's own stderr fallback (:data:`logging.lastResort`), which
#: resolves ``sys.stderr`` when the record is emitted. That keeps a
#: handled failure's traceback visible - the log is not blinded
#: (t_7198bfa3) - while the request itself is still answered, without
#: the socketserver banner an *unhandled* exception produces.
operations_logger = logging.getLogger("gateway")


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
