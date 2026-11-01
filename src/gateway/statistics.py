"""Metadata-only request statistics over bounded ledger history."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from gateway.bridge.streams import usable_token_count
from gateway.costs import calculate_cost

WINDOW_HOURS = {"hour": 1, "day": 24, "month": 24 * 30}


def _percentile(values: list[int], percentile: int) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, (len(ordered) * percentile + 99) // 100 - 1)
    return ordered[index]


def aggregate_statistics(events: list[dict[str, Any]], window: str = "day", *, _prices: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """Aggregate outcome, latency, usage and local cost metadata in a window.

    ``_prices`` threads a caller's already-materialised published-price
    snapshot into every per-event cost lookup. Without it each
    ``calculate_cost`` call re-reads the catalogue from disk — on a
    10,000-row window that turns one console poll into tens of thousands
    of file reads (the wave-2B owner-console outage).
    """
    if window not in WINDOW_HOURS:
        raise ValueError("window must be one of: hour, day, month")
    cutoff = datetime.now(UTC) - timedelta(hours=WINDOW_HOURS[window])
    groups: dict[str, dict[str, dict[str, Any]]] = {
        key: {} for key in ("alias", "provider", "model")
    }
    for event in events:
        try:
            stamp = datetime.fromisoformat(str(event.get("timestamp", "")))
            if stamp.tzinfo is None or stamp < cutoff:
                continue
        except (ValueError, TypeError, OverflowError):
            continue
        dims = [("alias", event.get("requested_alias")),
                ("provider", event.get("serving_provider")),
                ("model", event.get("serving_model"))]
        counters = event.get("token_counters")
        counters = counters if isinstance(counters, dict) else {}
        for dimension, key in dims:
            if not isinstance(key, str) or not key:
                continue
            row = groups[dimension].setdefault(key, {
                "requests": 0, "errors": 0, "error_classes": {}, "fallbacks": 0,
                "fallback_rate": 0.0, "latency_ms": [], "input_tokens": 0,
                "output_tokens": 0, "token_counted_requests": 0, "token_unknown": False,
                "estimated_cost_usd": 0.0, "cost_unknown": False,
            })
            row["requests"] += 1
            error = event.get("error_class")
            if isinstance(error, str) and error:
                row["errors"] += 1
                row["error_classes"][error] = row["error_classes"].get(error, 0) + 1
            if event.get("fallback_used") is True:
                row["fallbacks"] += 1
            latency = event.get("latency_ms")
            if isinstance(latency, int) and not isinstance(latency, bool) and latency >= 0:
                row["latency_ms"].append(latency)
            # Bounded at read time: an over-bound provider counter (a
            # 401-digit ledger row) is unknown, so the group's token columns
            # read None instead of summing a JSON number the console parses
            # as Infinity.
            input_tokens = usable_token_count(counters.get("prompt_tokens", counters.get("input_tokens")))
            output_tokens = usable_token_count(counters.get("completion_tokens", counters.get("output_tokens")))
            if input_tokens is not None and output_tokens is not None:
                row["input_tokens"] += input_tokens
                row["output_tokens"] += output_tokens
                row["token_counted_requests"] += 1
            else:
                row["token_unknown"] = True
            model = event.get("serving_model")
            provider = event.get("serving_provider")
            if isinstance(model, str) and isinstance(provider, str):
                cost = calculate_cost(counters or None, model, provider, _prices=_prices)
                if cost["amount_usd"] is None:
                    row["cost_unknown"] = True
                else:
                    row["estimated_cost_usd"] += cost["amount_usd"]
            else:
                row["cost_unknown"] = True
    for dimension in groups.values():
        for row in dimension.values():
            row["fallback_rate"] = row["fallbacks"] / row["requests"] if row["requests"] else 0.0
            values = row.pop("latency_ms")
            row["latency_ms"] = {"p50": _percentile(values, 50), "p95": _percentile(values, 95), "p99": _percentile(values, 99)}
            if row.pop("token_unknown"):
                row["input_tokens"] = None
                row["output_tokens"] = None
            if row.pop("cost_unknown"):
                row["estimated_cost_usd"] = None
    return {"window": window, "window_hours": WINDOW_HOURS[window], **groups}
