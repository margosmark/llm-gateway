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
        else:
            result["cached_tokens_reported"] = True
    result = {key: value for key, value in result.items()
              if key == "cached_tokens_reported" or isinstance(value, int) and value >= 0}
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
                        total.pop("cached_tokens_reported", None)
                    else:
                        total["cached_tokens_reported"] = True
                total = {key: value for key, value in total.items()
                         if key == "cached_tokens_reported" or isinstance(value, int) and value >= 0}
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


#: Why a cost could not be computed. ``None`` means it was. The console shows
#: these instead of a bare ``null``, so "no published price" is never confused
#: with "the price exists but this request reported no usage".
COST_NO_PRICE = "no_published_price"
COST_NO_USAGE = "no_usage_reported"
COST_INVALID_USAGE = "invalid_usage"
COST_MISSING_CACHED_PRICE = "missing_cached_price"
COST_MISSING_CACHED_TOKENS = "missing_cached_tokens"


def calculate_cost(counters: dict[str, int] | None, model: str, provider: str | None = "opencode_go", *, _prices: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """Estimate one request's cost from published prices, or say why it is unknown.

    ``reason`` is ``None`` exactly when ``amount_usd`` is a number. A price
    that exists but cannot be applied (usage was never reported, the counters
    are unusable, or the catalogue prices input but not cached reads) still
    reports the price's ``source``: the price is known, the amount is not.
    """
    # The published snapshot currently prices OpenCode Go models only; do not
    # transfer a matching model ID's price to another provider.
    catalog = published_prices() if _prices is None else _prices
    price = catalog.get(model) if provider == "opencode_go" else None
    if (not price or not isinstance(price.get("input_per_m"), (int, float))
            or not isinstance(price.get("output_per_m"), (int, float))
            or not isinstance(price.get("source"), str)
            or not isinstance(price.get("observed_at"), str)):
        price = None
    if price is None:
        return {"amount_usd": None, "source": None, "observed_at": None, "reason": COST_NO_PRICE}
    source, observed_at = price["source"], price["observed_at"]
    if counters is None:
        # Usage was never reported. The published price is still known, and
        # there is no evidence that any billable tokens were counted.
        return {"amount_usd": 0.0, "source": source, "observed_at": observed_at, "reason": COST_NO_USAGE}
    input_tokens = counters.get("prompt_tokens", counters.get("input_tokens", 0))
    output_tokens = counters.get("completion_tokens", counters.get("output_tokens", 0))
    cached_key = "cached_read_tokens" if "cached_read_tokens" in counters else (
        "cache_read_input_tokens" if "cache_read_input_tokens" in counters else None
    )
    cached = counters.get(cached_key, 0) if cached_key else 0
    if any(not isinstance(value, int) or value < 0 for value in (input_tokens, output_tokens, cached)):
        return {"amount_usd": None, "source": source, "observed_at": observed_at, "reason": COST_INVALID_USAGE}
    if counters.get("cached_tokens_reported") is True and cached_key is None:
        # The response said cached tokens were involved but never said how
        # many; pricing them as zero would fabricate a smaller bill.
        return {"amount_usd": None, "source": source, "observed_at": observed_at,
                "reason": COST_MISSING_CACHED_TOKENS}
    cached_rate = price.get("cached_read_per_m")
    if cached and not isinstance(cached_rate, (int, float)):
        # Never assume cached reads are free.
        return {"amount_usd": None, "source": source, "observed_at": observed_at, "reason": COST_MISSING_CACHED_PRICE}
    billable_input = max(0, input_tokens - cached)
    amount = (billable_input * price["input_per_m"] + output_tokens * price["output_per_m"] +
              cached * (cached_rate if isinstance(cached_rate, (int, float)) else 0)) / 1_000_000
    return {"amount_usd": amount, "source": source, "observed_at": observed_at, "reason": None}


def aggregate_usage(events: list[dict[str, Any]], window_hours: int = 24, *, evicted_events: int = 0, _prices: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """Aggregate usage from supplied events; report when the hot ring is incomplete.

    The amount is the cost of the tokens this window *counts*, at the
    published price for the model - the same basis the token columns use. It
    is ``null`` only when the price itself is missing from the catalogue (or
    cannot be applied at all); a request that reported no usage contributes
    no tokens and no cost, and the window-level ``requests_without_usage``
    counter says how many there were. An evicted window stays incomplete in
    ``coverage`` - exactly like the token columns, which are partial too -
    rather than blanking a price that does exist.
    """
    cutoff = datetime.now(UTC) - timedelta(hours=window_hours)
    prices = published_prices() if _prices is None else _prices
    groups: dict[str, dict[str, Any]] = {key: {} for key in ("alias", "provider", "model")}
    requests_without_usage = 0
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
        if not counters:
            requests_without_usage += 1
        cost = calculate_cost(counters or None, model, event.get("serving_provider"), _prices=prices)
        dimensions = (("alias", event.get("requested_alias")),
                      ("provider", event.get("serving_provider")),
                      ("model", model))
        for dimension, key in dimensions:
            if not isinstance(key, str):
                continue
            row = groups[dimension].setdefault(key, _empty_group_row())
            row["requests"] += 1
            row["input_tokens"] += counters.get("prompt_tokens", counters.get("input_tokens", 0))
            row["output_tokens"] += counters.get("completion_tokens", counters.get("output_tokens", 0))
            _fold_cost(row, cost, raw_counters if isinstance(raw_counters, dict) else None)
    for dimension in groups.values():
        for key in dimension:
            dimension[key] = _finish_group_row(dimension[key])
    return {
        "window_hours": window_hours,
        "coverage": {
            "source": "bounded_recent_events",
            "events_considered": len(events),
            "requests_without_usage": requests_without_usage,
            "incomplete": evicted_events > 0,
        },
        **groups,
    }


def aggregate_rollup_rows(rows: list[dict[str, Any]], *, _prices: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """Per alias/provider/model totals for daily-rollup rows, priced at read time.

    The rollup deliberately stores no cost - a published price can change - so
    the estimate is recomputed here from the row's token counters with the
    same rules :func:`aggregate_usage` applies to the live ring. Row counters
    that are not non-negative integers are treated as missing rather than
    summed, so a damaged sidecar cannot break the owner console.
    """
    prices = published_prices() if _prices is None else _prices
    groups: dict[str, dict[str, Any]] = {key: {} for key in ("alias", "provider", "model")}
    for row in rows:
        if not isinstance(row, dict):
            continue
        model, provider = row.get("model"), row.get("provider")
        if not isinstance(model, str):
            continue
        tokens_in = _counter(row.get("input_tokens"))
        tokens_out = _counter(row.get("output_tokens"))
        cached = _counter(row.get("cached_tokens"))
        fallbacks = _counter(row.get("fallbacks"))
        cost = calculate_cost({"prompt_tokens": tokens_in, "completion_tokens": tokens_out,
                               "cached_read_tokens": cached}, model, provider, _prices=prices)
        dimensions = (("alias", row.get("alias")), ("provider", provider), ("model", model))
        for dimension, key in dimensions:
            if not isinstance(key, str):
                continue
            group = groups[dimension]
            entry = group.get(key)
            if entry is None:
                entry = _empty_group_row()
                entry.update({"cached_tokens": 0, "fallbacks": 0})
                group[key] = entry
            entry["requests"] += _counter(row.get("requests"))
            entry["input_tokens"] += tokens_in
            entry["output_tokens"] += tokens_out
            entry["cached_tokens"] += cached
            entry["fallbacks"] += fallbacks
            requests_in_row = _counter(row.get("requests")) or 1
            _fold_cost(entry, cost, {"input_tokens": tokens_in, "output_tokens": tokens_out},
                       requests_in_row, amount_stands_for_count=False)
    for dimension in groups.values():
        for key in dimension:
            dimension[key] = _finish_group_row(dimension[key])
    return groups


def _empty_group_row() -> dict[str, Any]:
    return {
        "requests": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "amount_usd": None,
        "known_amount_usd": 0.0,
        "known_cost_requests": 0,
        "unknown_cost_requests": 0,
        "unknown_cost_input_tokens": 0,
        "unknown_cost_output_tokens": 0,
        "cost_incomplete": False,
        "cost_reason": None,
        "source": None,
        "observed_at": None,
    }


def _counter(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _fold_cost(row: dict[str, Any], cost: dict[str, Any],
               counters: dict[str, Any] | None, count: int = 1,
               *, amount_stands_for_count: bool = True) -> None:
    """Fold a contribution into known subtotal and explicit unknown coverage.

    ``count`` scales the *request* counters: live events fold one at a time,
    while a rollup row stands for every request in that group for the day.
    ``amount_stands_for_count`` says whether ``cost['amount_usd']`` prices a
    single request (live events: multiply it) or the whole row's tokens
    (rollup rows: already the group total, so add it once).
    """
    reason = cost.get("reason")
    amount = cost.get("amount_usd")
    if reason in (None, COST_NO_USAGE):
        # A published price with no reported usage is a known $0.00, not an
        # unknown: the request contributed no billable tokens to this window.
        row["known_amount_usd"] += (amount or 0.0) * (count if amount_stands_for_count else 1)
        row["known_cost_requests"] += count
    else:
        row["unknown_cost_requests"] += count
        if reason == COST_NO_PRICE:
            row["cost_reason"] = COST_NO_PRICE
        elif row["cost_reason"] is None or row["cost_reason"] == COST_NO_USAGE:
            row["cost_reason"] = reason
        if counters is None:
            row["unknown_cost_input_tokens"] += 0
            row["unknown_cost_output_tokens"] += 0
        else:
            row["unknown_cost_input_tokens"] += _counter(counters.get("prompt_tokens", counters.get("input_tokens", 0)))
            row["unknown_cost_output_tokens"] += _counter(counters.get("completion_tokens", counters.get("output_tokens", 0)))
        row["cost_incomplete"] = True
        row["cost_reason"] = row["cost_reason"] or reason
    if cost.get("source") is not None and row["source"] is None:
        row["source"] = cost["source"]
        row["observed_at"] = cost.get("observed_at")


def _finish_group_row(row: dict[str, Any]) -> dict[str, Any]:
    """Expose only the subtotal backed by known prices, never imply completeness."""
    row["amount_usd"] = row["known_amount_usd"] if row["known_cost_requests"] else None
    return row


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
