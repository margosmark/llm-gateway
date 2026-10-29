"""Prometheus exposition for metadata already held by the gateway."""

from __future__ import annotations

from collections import Counter
from typing import Any

LATENCY_BUCKETS = (10, 50, 100, 250, 500, 1000, 2500, 5000, 10000)


def render_metrics(ledger: Any | None, cooldowns: dict[str, int] | None = None) -> str:
    """Render bounded-ledger outcomes and local cooldown timers as text format."""
    events = ledger.recent(limit=10000) if ledger is not None else []
    outcomes: Counter[str] = Counter()
    errors: Counter[str] = Counter()
    fallbacks = 0
    durations: list[int] = []
    for event in events:
        status = event.get("status")
        outcome = "success" if isinstance(status, int) and status < 400 else "error"
        outcomes[outcome] += 1
        error = event.get("error_class")
        if isinstance(error, str) and error:
            errors[error] += 1
        fallbacks += bool(event.get("fallback_used"))
        latency = event.get("latency_ms")
        if isinstance(latency, int) and latency >= 0:
            durations.append(latency)
    lines = [
        "# HELP gateway_requests_total Requests in the retained metadata ledger by outcome.",
        "# TYPE gateway_requests_total counter",
    ]
    for outcome in ("success", "error"):
        lines.append(f'gateway_requests_total{{outcome="{outcome}"}} {outcomes[outcome]}')
    lines += ["# HELP gateway_errors_total Errors in the retained metadata ledger by error class.", "# TYPE gateway_errors_total counter"]
    for name, count in sorted(errors.items()):
        lines.append(f'gateway_errors_total{{error_class="{_label(name)}"}} {count}')
    lines += ["# HELP gateway_request_latency_milliseconds Request latency in milliseconds.", "# TYPE gateway_request_latency_milliseconds histogram"]
    for bound in LATENCY_BUCKETS:
        lines.append(f'gateway_request_latency_milliseconds_bucket{{le="{bound}"}} {sum(value <= bound for value in durations)}')
    lines.append(f'gateway_request_latency_milliseconds_bucket{{le="+Inf"}} {len(durations)}')
    lines += [f"gateway_request_latency_milliseconds_sum {sum(durations)}", f"gateway_request_latency_milliseconds_count {len(durations)}"]
    lines += ["# HELP gateway_fallbacks_total Fallbacks in the retained metadata ledger.", "# TYPE gateway_fallbacks_total counter", f"gateway_fallbacks_total {fallbacks}"]
    lines += ["# HELP gateway_cooldown_remaining_seconds Remaining local cooldown duration by provider route.", "# TYPE gateway_cooldown_remaining_seconds gauge"]
    for route, remaining in sorted((cooldowns or {}).items()):
        if remaining > 0:
            lines.append(f'gateway_cooldown_remaining_seconds{{route="{_label(route)}"}} {remaining}')
    return "\n".join(lines) + "\n"


def _label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')
