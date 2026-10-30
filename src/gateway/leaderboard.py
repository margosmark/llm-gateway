"""The private owner-console model leaderboard: the (day, provider, model) view.

The rollup sidecar already stores one row per **(day, alias, provider,
model)** — this module projects those rows onto the grain the leaderboard is
actually about, **(day, provider, model)**, and ranks models over a window.
No new storage dimension is introduced, so the sidecar schema stays
``daily-rollup/v1`` and an existing sidecar keeps working without a rebuild.

Contract, decided with the owner (2026-10-30):

* **Private.** The payload is served to the owner console only, through the
  same trusted-LAN routes and the same security checks as
  ``/owner/api/state``. Nothing here adds a listener, a public route, an
  auth change, or a dimension the gateway does not have (no users, no
  sessions, no geo: this is a single-owner appliance).
* **Cost honesty.** The rollup stores no cost; dollars are computed at read
  time by :mod:`gateway.costs`, which returns ``amount_usd: null`` with a
  ``cost_reason`` when a price is unknown. A null stays a null here — it is
  never coerced to ``0``, never rendered as ``$0.00``, and a price is never
  transferred across providers (the catalogue prices ``opencode_go`` models
  only). ``cost_incomplete`` and ``cached_tokens_unreported`` travel with
  every entry so unknown coverage stays visible.
* **Absent is not zero.** A day with no recorded rows is reported with its
  own status (``gap`` / ``outside_coverage`` / ``no_rows`` / ``unknown``) and
  a ``null`` matrix row; only a covered day where a model genuinely had no
  requests carries a measured ``0``.
* **Shares are of the window.** ``share`` is the entry's
  input+output tokens divided by the window's input+output total. Cached
  tokens are a subset of input tokens and are never added a second time.

The heavy part runs on a **separate endpoint** (``GET <mount>/api/leaderboard``)
so ``/owner/api/state`` keeps its mandatory sub-2s bar (R56). Both paths
materialise the rollup and the price catalogue once per request.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from gateway.costs import aggregate_rollup_rows
from gateway.ledger_persistence import ROLLUP_SCHEMA

#: The ranking metric, stated in the payload rather than implied: models are
#: ordered by input + output tokens over the window. Cached tokens are a
#: subset of input tokens, so they are reported but never added again.
PRIMARY_METRIC_KEY = "total_tokens"
PRIMARY_METRIC_LABEL = "input + output tokens"
PRIMARY_METRIC_DEFINITION = (
    "input_tokens + output_tokens summed over the window; cached tokens are a "
    "subset of input tokens and are never counted a second time. Rank ties "
    "break on requests, then provider, then model."
)

#: Day statuses, mirroring the analytics series so one day is described the
#: same way in both payloads.
STATUS_UNKNOWN = "unknown"
STATUS_OPEN = "open"
STATUS_OK = "ok"
STATUS_NO_ROWS = "no_rows"
STATUS_OUTSIDE_COVERAGE = "outside_coverage"
STATUS_GAP = "gap"

#: The six counters the rollup carries per (day, provider, model).
COUNT_FIELDS = ("requests", "input_tokens", "output_tokens", "cached_tokens",
                "fallbacks", "errors")

#: The cost fields surfaced per model. They are exactly the honesty signals
#: :func:`gateway.costs.aggregate_rollup_rows` produces — nothing is dropped
#: and nothing is invented.
COST_FIELDS = ("amount_usd", "cost_reason", "cost_incomplete", "source",
               "observed_at", "known_cost_requests", "unknown_cost_requests",
               "unknown_cost_input_tokens", "unknown_cost_output_tokens",
               "cached_tokens_unreported")

DAY_KEYS = ("day", "provider", "model")


def build_leaderboard(
    rollup: dict[str, Any] | None,
    *,
    days: int = 7,
    prices: dict[str, dict[str, Any]] | None = None,
    today: datetime | None = None,
) -> dict[str, Any]:
    """Rank models over the requested window and build the date x model matrix.

    ``prices`` is the caller's price snapshot, so the catalogue is read at
    most once per request no matter how many models are priced. Rows outside
    the window are ignored, a schema this build does not recognise is refused
    rather than interpreted, and every number below is an aggregate of rollup
    rows — no request identifier, prompt, response or credential can reach
    this payload.
    """
    moment = today if today is not None else datetime.now(UTC)
    window = [(moment - timedelta(days=offset)).strftime("%Y-%m-%d")
              for offset in range(days - 1, -1, -1)]
    today_text = window[-1]
    schema = rollup.get("schema") if isinstance(rollup, dict) else None
    data: dict[str, Any] | None = (rollup if isinstance(rollup, dict)
                                   and schema == ROLLUP_SCHEMA else None)
    available = data is not None
    reason = None if available else ("durable_rollup_disabled" if rollup is None
                                     else "unrecognised_rollup_schema")

    raw_coverage = data.get("coverage") if data is not None else None
    coverage = raw_coverage if isinstance(raw_coverage, dict) else None
    first_row = coverage.get("first_row_ts") if coverage else None
    first_day = first_row[:10] if isinstance(first_row, str) and len(first_row) >= 10 else None

    rows: list[dict[str, Any]] = []
    if data is not None:
        for row in data.get("rows") or []:
            if (isinstance(row, dict)
                    and all(isinstance(row.get(key), str) for key in DAY_KEYS)
                    and window[0] <= row["day"] <= today_text):
                rows.append(row)

    # --- (day, provider, model) cells: the rollup's rows summed across aliases
    cells: dict[tuple[str, str, str], dict[str, int]] = {}
    by_provider: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        key = (row["day"], row["provider"], row["model"])
        cell = cells.get(key)
        if cell is None:
            cell = cells[key] = dict.fromkeys(COUNT_FIELDS, 0)
        for field in ("requests", "input_tokens", "output_tokens", "cached_tokens"):
            cell[field] += _counter(row.get(field))
        cell["fallbacks"] += _counter(row.get("fallbacks"))
        cell["errors"] += _errors_total(row.get("errors"))
        by_provider.setdefault(row["provider"], []).append(row)

    # --- window totals per (provider, model), then the cost of each pair.
    # Pricing runs per provider slice so a model id that exists on two
    # providers can never inherit the other provider's price: within a slice
    # every row shares one provider, and ``gateway.costs`` only prices
    # ``opencode_go`` from the published catalogue.
    totals_by_key: dict[tuple[str, str], dict[str, int]] = {}
    for (day, provider, model), cell in cells.items():
        entry = totals_by_key.setdefault((provider, model),
                                         dict.fromkeys(COUNT_FIELDS, 0))
        for field in COUNT_FIELDS:
            entry[field] += cell[field]
    cost_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for provider, provider_rows in by_provider.items():
        groups = aggregate_rollup_rows(provider_rows, _prices=prices)
        for model, entry in groups["model"].items():
            cost_by_key[(provider, model)] = entry

    window_tokens = sum(entry["input_tokens"] + entry["output_tokens"]
                        for entry in totals_by_key.values())
    ranked = [
        _ranked_entry(rank, provider, model, entry, cost_by_key.get((provider, model)),
                      window_tokens)
        for rank, (provider, model, entry) in enumerate(
            sorted(
                ((provider, model, entry)
                 for (provider, model), entry in totals_by_key.items()),
                key=lambda item: (-(item[2]["input_tokens"] + item[2]["output_tokens"]),
                                  -item[2]["requests"], item[0], item[1]),
            ),
            start=1,
        )
    ]

    day_status = _day_status(window, cells, available, first_day, today_text)
    days_payload = [{"day": day, "status": day_status[day]} for day in window]

    payload: dict[str, Any] = {
        "available": available,
        "unavailable_reason": reason,
        "source": data.get("source") if data is not None else None,
        "schema": data.get("schema") if data is not None else None,
        "generated_at": data.get("generated_at") if data is not None else None,
        "coverage": coverage,
        "days": days,
        "window": {"from": window[0], "to": today_text},
        "timezone": "UTC",
        "primary_metric": {
            "key": PRIMARY_METRIC_KEY,
            "label": PRIMARY_METRIC_LABEL,
            "definition": PRIMARY_METRIC_DEFINITION,
        },
        "gaps": [day for day in window if day_status[day] == STATUS_GAP],
        "totals": _window_totals(ranked, available, has_rows=bool(totals_by_key)),
        "ranked": ranked if available else [],
        "matrix": {
            "models": [{"rank": entry["rank"], "provider": entry["provider"],
                        "model": entry["model"]} for entry in ranked],
            "days": days_payload,
            "cells": [
                {"day": day, "status": day_status[day],
                 # null = this day carries no recorded data for that model;
                 # an object with 0s = a covered day where the model really
                 # had no requests. The two claims are never conflated.
                 "values": ([_matrix_values(cells, day, entry)
                             for entry in ranked] if day_status[day] in (STATUS_OK, STATUS_OPEN)
                            else [None] * len(ranked)),
                 }
                for day in window
            ],
        },
    }
    return payload


def _ranked_entry(rank: int, provider: str, model: str, entry: dict[str, int],
                  cost: dict[str, Any] | None, window_tokens: int) -> dict[str, Any]:
    total_tokens = entry["input_tokens"] + entry["output_tokens"]
    return {
        "rank": rank,
        "provider": provider,
        "model": model,
        "requests": entry["requests"],
        "input_tokens": entry["input_tokens"],
        "output_tokens": entry["output_tokens"],
        "cached_tokens": entry["cached_tokens"],
        "total_tokens": total_tokens,
        "share": round(total_tokens / window_tokens, 6) if window_tokens else None,
        "fallbacks": entry["fallbacks"],
        "errors": entry["errors"],
        "cost": {field: (cost.get(field) if cost is not None else None)
                 for field in COST_FIELDS},
    }


def _window_totals(ranked: list[dict[str, Any]], available: bool, *,
                   has_rows: bool) -> dict[str, Any]:
    """Window totals, with the known cost subtotal beside its unknown coverage.

    A refused rollup, or a window that holds no recorded rows at all, reports
    ``null`` counts — "nothing was measured", never a zero that would read as
    "no traffic happened".
    """
    if not available or not has_rows:
        return {field: None for field in COUNT_FIELDS} | {
            "total_tokens": None, "models": 0, "models_without_price": 0,
            "known_amount_usd": None, "cost_incomplete": None,
            "unknown_cost_requests": None, "unknown_cost_input_tokens": None,
            "unknown_cost_output_tokens": None,
        }
    totals: dict[str, Any] = dict.fromkeys(COUNT_FIELDS, 0)
    for entry in ranked:
        for field in COUNT_FIELDS:
            totals[field] += entry[field]
    totals["total_tokens"] = totals["input_tokens"] + totals["output_tokens"]
    totals["models"] = len(ranked)
    known = 0.0
    known_requests = 0
    unknown_requests = 0
    unknown_in = 0
    unknown_out = 0
    incomplete = False
    unpriced = 0
    for entry in ranked:
        cost = entry["cost"]
        if cost.get("amount_usd") is None:
            unpriced += 1
        # ``amount_usd`` is already the *known* subtotal for this entry (it is
        # null when nothing in the entry was priced), so a null can never be
        # added here as a $0.00 contribution.
        known += cost.get("amount_usd") or 0.0
        known_requests += cost.get("known_cost_requests") or 0
        unknown_requests += cost.get("unknown_cost_requests") or 0
        unknown_in += cost.get("unknown_cost_input_tokens") or 0
        unknown_out += cost.get("unknown_cost_output_tokens") or 0
        incomplete = incomplete or bool(cost.get("cost_incomplete"))
    totals["models_without_price"] = unpriced
    # Same rule as everywhere else: a subtotal exists only when at least one
    # request was priced. Zero priced requests is ``null``, never ``0.00``.
    totals["known_amount_usd"] = known if known_requests else None
    totals["cost_incomplete"] = incomplete
    totals["unknown_cost_requests"] = unknown_requests
    totals["unknown_cost_input_tokens"] = unknown_in
    totals["unknown_cost_output_tokens"] = unknown_out
    return totals


def _matrix_values(cells: dict[tuple[str, str, str], dict[str, int]], day: str,
                   entry: dict[str, Any]) -> dict[str, int]:
    """One model's cell for one covered day: measured values, zeros included."""
    cell = cells.get((day, entry["provider"], entry["model"]))
    if cell is None:
        return dict.fromkeys(COUNT_FIELDS, 0)
    return {field: cell[field] for field in COUNT_FIELDS}


def _day_status(window: list[str], cells: dict[tuple[str, str, str], Any],
                available: bool, first_day: str | None, today_text: str) -> dict[str, str]:
    """The analytics series' own status rule, so one day never reads two ways."""
    status: dict[str, str] = {}
    for day in window:
        if not available:
            status[day] = STATUS_UNKNOWN
        elif any(key[0] == day for key in cells):
            status[day] = STATUS_OPEN if day == today_text else STATUS_OK
        elif day == today_text:
            status[day] = STATUS_NO_ROWS
        elif first_day is None or day < first_day:
            status[day] = STATUS_OUTSIDE_COVERAGE
        else:
            status[day] = STATUS_GAP
    return status


def _counter(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _errors_total(counts: Any) -> int:
    if not isinstance(counts, dict):
        return 0
    return sum(_counter(count) for count in counts.values())
