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


#: Terminal states a streamed response can reach. The value is written to
#: the access log verbatim, so it stays a closed set: an unknown outcome
#: must never be able to claim ``complete``.
STREAM_OUTCOMES = ("complete", "interrupted", "error")


def _safe_client_request_id(value: Any) -> str | None:
    """The validated client correlation id, or ``None``.

    Deferred import: :mod:`gateway.logging` is imported while the HTTP
    layer is still loading, so the validator is resolved on first use
    instead of at module load. The re-validation is deliberate - an
    access line is written from a request context, and no path may push
    an unvalidated client value into one. Logging must never raise, so a
    value that fails validation is dropped: not written, not echoed.
    """
    if not isinstance(value, str):
        return None
    from gateway.http.request_id import validate_client_request_id

    try:
        return validate_client_request_id(value)
    except ValueError:
        return None


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
        exception: str | None = None,
        request_id: str | None = None,
        client_request_id: str | None = None,
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
        # The class of an unhandled failure, so a gateway_error line names
        # its cause. A class name only - never the exception's text, which
        # could carry client-controlled content.
        if exception:
            record["exception"] = exception
        # Correlation: the gateway's own authoritative id, so this line
        # joins to its ledger row, and the caller's validated id, so the
        # operator can join both to the client's own records. Both are
        # bounded token strings; the client value is re-validated here
        # and dropped if it is not.
        if request_id:
            record["request_id"] = request_id
        safe_client_id = _safe_client_request_id(client_request_id)
        if safe_client_id:
            record["client_request_id"] = safe_client_id
        self._write(record)

    def log_stream_end(
        self,
        *,
        request_id: str,
        path: str,
        outcome: str,
        duration_ms: int,
        client_request_id: str | None = None,
        error_class: str | None = None,
    ) -> None:
        """One metadata-only line for the *terminal* state of a stream.

        The ingress line says only that the gateway answered the request
        - HTTP accepted, headers on the wire. This line says what the
        stream afterwards became: ``complete`` (every frame delivered),
        ``interrupted`` (the caller hung up before the end) or ``error``
        (the upstream died mid-answer). Both lines carry the same
        ``request_id`` and the same ``client_request_id``, so ingress,
        terminal outcome and ledger row are one join - which is what
        separates "the gateway said 200" from "the answer arrived".

        No frame content, no prompt and no completion is written here:
        the path, the ids, the outcome, the error class and a duration.
        """
        if not self._enabled:
            return
        record: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z",
            "event": "stream_end",
            "path": path,
            "request_id": request_id,
            "outcome": outcome if outcome in STREAM_OUTCOMES else "error",
            "duration_ms": max(0, int(duration_ms)),
        }
        if error_class:
            record["error_class"] = error_class
        safe_client_id = _safe_client_request_id(client_request_id)
        if safe_client_id:
            record["client_request_id"] = safe_client_id
        self._write(record)

    def _write(self, record: dict[str, Any]) -> None:
        line = redact(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
        # backslashreplace: a lone surrogate in any field must drop nothing.
        # Encoding strictly raised UnicodeEncodeError - a ValueError - which
        # the handler below swallowed, silently discarding the whole access
        # line (and a plain print of a lone surrogate to a pipe hard-crashes
        # Python). Clean lines are byte-identical (F9).
        line = line.encode("utf-8", "backslashreplace").decode("utf-8")
        try:
            self._stream.write(line + "\n")
            self._stream.flush()
        except (OSError, ValueError):  # pragma: no cover - logging must never break a request
            pass
