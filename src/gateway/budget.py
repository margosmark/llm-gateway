"""Local monthly alias budgets derived from recorded usage metadata."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from gateway.costs import calculate_cost


def month_window(now: datetime | None = None) -> tuple[datetime, datetime]:
    current = now or datetime.now(UTC)
    current = current.astimezone(UTC)
    start = current.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start, end


def alias_month_spend(events: list[dict[str, Any]], alias: str, now: datetime | None = None) -> dict[str, Any]:
    start, end = month_window(now)
    total = 0.0
    unknown = False
    for event in events:
        if event.get("requested_alias") != alias:
            continue
        try:
            timestamp = datetime.fromisoformat(str(event.get("timestamp", "")))
        except (ValueError, TypeError):
            continue
        if timestamp.tzinfo is None or not start <= timestamp.astimezone(UTC) < end:
            continue
        status = event.get("status")
        if not isinstance(status, int) or status < 200 or status >= 300:
            continue
        model = event.get("serving_model")
        if not isinstance(model, str):
            unknown = True
            continue
        counters = event.get("token_counters")
        cost = calculate_cost(counters if isinstance(counters, dict) else None, model, event.get("serving_provider"))
        if cost["amount_usd"] is None:
            unknown = True
        else:
            total += cost["amount_usd"]
    return {"amount_usd": None if unknown else total, "reset_at": end.isoformat().replace("+00:00", "Z"), "window_start": start.isoformat().replace("+00:00", "Z")}


def budget_status(events: list[dict[str, Any]], alias: str, cap: float | None, now: datetime | None = None, history_complete: bool = True) -> dict[str, Any]:
    spend = alias_month_spend(events, alias, now)
    if not history_complete:
        spend["amount_usd"] = None
    known = spend["amount_usd"] is not None
    return {
        "budget_usd": cap,
        "spend_usd": spend["amount_usd"],
        "capped": bool(cap is not None and known),
        "reset_at": spend["reset_at"],
        "window_start": spend["window_start"],
        "soft": True,
    }


def over_budget(events: list[dict[str, Any]], alias: str, cap: float | None, now: datetime | None = None, history_complete: bool = True) -> dict[str, Any] | None:
    status = budget_status(events, alias, cap, now, history_complete)
    if status["capped"] and status["spend_usd"] >= cap:
        return status
    return None
