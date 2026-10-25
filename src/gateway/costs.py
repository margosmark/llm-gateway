"""Local token usage and estimates from explicitly published prices."""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from gateway.bridge.streams import iter_sse_frames, normalise_usage

_CATALOG_PATH = Path(__file__).resolve().parents[2] / "data" / "go_catalog.json"


def published_prices() -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(_CATALOG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    models = data.get("models", {}) if isinstance(data, dict) else {}
    return {
        model: {
            "input_per_m": row.get("price_input_per_m"),
            "output_per_m": row.get("price_output_per_m"),
            "cached_read_per_m": row.get("price_cached_read_per_m"),
            "source": row.get("source"),
            "observed_at": row.get("observed_at"),
        }
        for model, row in models.items() if isinstance(model, str) and isinstance(row, dict)
    }


def response_token_counters(body: bytes | None, dialect: str) -> dict[str, int] | None:
    del dialect  # Usage keys are normalised independently of response dialect.
    if not body:
        return None
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None
    usage = payload.get("usage") if isinstance(payload, dict) else None
    if not isinstance(usage, dict):
        return None
    result = normalise_usage(usage)
    details = usage.get("prompt_tokens_details", usage.get("input_tokens_details"))
    if isinstance(details, dict):
        cached = details.get("cached_tokens", details.get("cache_read"))
        if isinstance(cached, int) and cached >= 0:
            result["cached_read_tokens"] = cached
    result = {key: value for key, value in result.items() if value >= 0}
    return result or None


def stream_token_counters(frames: Iterable[bytes], dialect: str) -> dict[str, int] | None:
    del dialect
    total: dict[str, int] = {}
    for frame in frames:
        for _, data in iter_sse_frames(frame):
            try:
                payload = json.loads(data)
            except ValueError:
                continue
            usage = payload.get("usage") if isinstance(payload, dict) else None
            response = payload.get("response") if isinstance(payload, dict) else None
            if not isinstance(usage, dict) and isinstance(response, dict):
                usage = response.get("usage")
            if isinstance(usage, dict):
                total.update(normalise_usage(usage))
                details = usage.get("prompt_tokens_details", usage.get("input_tokens_details"))
                if isinstance(details, dict):
                    cached = details.get("cached_tokens", details.get("cache_read"))
                    if isinstance(cached, int) and cached >= 0:
                        total["cached_read_tokens"] = cached
                total = {key: value for key, value in total.items() if value >= 0}
    return total or None


def price_for(model: str, *, _prices: dict[str, dict[str, Any]] | None = None) -> dict[str, Any] | None:
    catalog = published_prices() if _prices is None else _prices
    price = catalog.get(model)
    if (not price or not isinstance(price.get("input_per_m"), (int, float))
            or not isinstance(price.get("output_per_m"), (int, float))
            or not isinstance(price.get("source"), str)
            or not isinstance(price.get("observed_at"), str)):
        return None
    return price


def calculate_cost(counters: dict[str, int] | None, model: str, provider: str | None = "opencode_go", *, _prices: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    # The published snapshot currently prices OpenCode Go models only; do not
    # transfer a matching model ID's price to another provider.
    catalog = published_prices() if _prices is None else _prices
    price = catalog.get(model) if provider == "opencode_go" else None
    if (not price or not isinstance(price.get("input_per_m"), (int, float))
            or not isinstance(price.get("output_per_m"), (int, float))
            or not isinstance(price.get("source"), str)
            or not isinstance(price.get("observed_at"), str)):
        price = None
    if price is None or counters is None:
        return {"amount_usd": None, "source": price.get("source") if price else None,
                "observed_at": price.get("observed_at") if price else None}
    input_tokens = counters.get("prompt_tokens", counters.get("input_tokens", 0))
    output_tokens = counters.get("completion_tokens", counters.get("output_tokens", 0))
    cached = counters.get("cached_read_tokens", counters.get("cache_read_input_tokens", 0))
    if any(not isinstance(value, int) or value < 0 for value in (input_tokens, output_tokens, cached)):
        return {"amount_usd": None, "source": price["source"], "observed_at": price["observed_at"]}
    cached_rate = price.get("cached_read_per_m")
    if cached and not isinstance(cached_rate, (int, float)):
        return {"amount_usd": None, "source": price["source"], "observed_at": price["observed_at"]}
    billable_input = max(0, input_tokens - cached)
    amount = (billable_input * price["input_per_m"] + output_tokens * price["output_per_m"] +
              cached * (cached_rate if isinstance(cached_rate, (int, float)) else 0)) / 1_000_000
    return {"amount_usd": amount, "source": price["source"], "observed_at": price["observed_at"]}


def aggregate_usage(events: list[dict[str, Any]], window_hours: int = 24, *, evicted_events: int = 0, _prices: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """Aggregate usage from supplied events; report when the hot ring is incomplete."""
    cutoff = datetime.now(UTC) - timedelta(hours=window_hours)
    prices = published_prices() if _prices is None else _prices
    groups: dict[str, dict[str, Any]] = {key: {} for key in ("alias", "provider", "model")}
    for event in events:
        try:
            timestamp = datetime.fromisoformat(str(event.get("timestamp", "")))
            if timestamp < cutoff:
                continue
        except (ValueError, TypeError):
            continue
        model = event.get("serving_model")
        raw_counters = event.get("token_counters")
        if not isinstance(model, str):
            continue
        counters = raw_counters if isinstance(raw_counters, dict) else {}
        cost = calculate_cost(counters or None, model, event.get("serving_provider"), _prices=prices)
        dimensions = (("alias", event.get("requested_alias")),
                      ("provider", event.get("serving_provider")),
                      ("model", model))
        for dimension, key in dimensions:
            if not isinstance(key, str):
                continue
            row = groups[dimension].setdefault(key, {"requests": 0, "input_tokens": 0, "output_tokens": 0,
                                                       "amount_usd": 0.0, "cost_unknown": False,
                                                       "source": cost["source"], "observed_at": cost["observed_at"]})
            row["requests"] += 1
            row["input_tokens"] += counters.get("prompt_tokens", counters.get("input_tokens", 0))
            row["output_tokens"] += counters.get("completion_tokens", counters.get("output_tokens", 0))
            if cost["amount_usd"] is None:
                row["cost_unknown"] = True
            else:
                row["amount_usd"] += cost["amount_usd"]
    for dimension in groups.values():
        for row in dimension.values():
            if evicted_events > 0:
                # The ring may have evicted in-window rows; do not present a
                # partial sum as if it were a complete window cost.
                row["cost_unknown"] = True
            if row["cost_unknown"]:
                row["amount_usd"] = None
            del row["cost_unknown"]
    return {
        "window_hours": window_hours,
        "coverage": {
            "source": "bounded_recent_events",
            "events_considered": len(events),
            "incomplete": evicted_events > 0,
        },
        **groups,
    }


def annotate_events(events: list[dict[str, Any]], *, _prices: dict[str, dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Attach calculated metadata-only costs for rendering; do not persist estimates."""
    prices = published_prices() if _prices is None else _prices
    result = []
    for event in events:
        row = dict(event)
        model, counters = row.get("serving_model"), row.get("token_counters")
        row["cost"] = calculate_cost(counters if isinstance(counters, dict) else None, model, row.get("serving_provider"), _prices=prices) if isinstance(model, str) else {
            "amount_usd": None, "source": None, "observed_at": None
        }
        result.append(row)
    return result


def per_request_costs(events: list[dict[str, Any]], *, _prices: dict[str, dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Return safe cost annotations keyed by request ID for Status rendering."""
    return [{"request_id": row.get("request_id"), **row["cost"]} for row in annotate_events(events, _prices=_prices)]
