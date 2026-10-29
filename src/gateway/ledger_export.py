"""Bounded metadata-only request ledger exports."""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Iterable
from typing import Any

from gateway.observability import redact

MAX_EXPORT_ROWS = 1_000
DEFAULT_EXPORT_ROWS = 500
FIELDS = ("timestamp", "alias", "serving_provider", "serving_model", "outcome", "note")


def _clean(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [_clean(item) for item in value]
    if isinstance(value, dict):
        return {key: _clean(item) for key, item in value.items()}
    return value


def export_records(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Allow-list and redact only the six metadata columns shown by console."""
    records = []
    for event in events:
        outcome = event.get("error_class") or (
            f"status {event['status']}" if event.get("status") is not None else ""
        )
        if event.get("fallback_used"):
            outcome += " (fallback)"
        degradations = event.get("degradations") or []
        note = "; ".join(
            str(item.get("reason", "")) for item in degradations if isinstance(item, dict)
        )
        records.append(_clean({
            "timestamp": event.get("timestamp"),
            "alias": event.get("requested_alias"),
            "serving_provider": event.get("serving_provider"),
            "serving_model": event.get("serving_model"),
            "outcome": outcome,
            "note": note,
        }))
    return records


def render_json(records: list[dict[str, Any]]) -> bytes:
    return json.dumps(records, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def render_csv(records: list[dict[str, Any]]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=FIELDS, lineterminator="\r\n")
    writer.writeheader()
    writer.writerows(records)
    return output.getvalue().encode("utf-8")
