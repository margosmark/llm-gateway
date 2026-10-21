"""Compose provider health from observed configuration and metadata only."""

from __future__ import annotations

from typing import Any


def provider_health(
    providers: list[dict[str, Any]],
    cooldown_details: dict[str, dict[str, Any]],
    ledger_events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return one safe, current health summary per provider.

    Rates are based only on bounded ledger observations for requests actually
    served by that provider. Missing observations remain unknown.
    """
    outcomes: dict[str, list[int]] = {}
    for event in ledger_events:
        provider_id = event.get("serving_provider")
        if not isinstance(provider_id, str) or not provider_id:
            continue
        counts = outcomes.setdefault(provider_id, [0, 0])
        status = event.get("status")
        if event.get("error_class") or (isinstance(status, int) and status >= 400):
            counts[1] += 1
        else:
            counts[0] += 1

    result = []
    for provider in sorted(providers, key=lambda item: item.get("provider_id", "")):
        provider_id = provider.get("provider_id")
        if not isinstance(provider_id, str):
            continue
        counts = outcomes.get(provider_id, [0, 0])
        total = sum(counts)
        cooldowns = [
            int(detail.get("remaining_seconds", 0))
            for key, detail in cooldown_details.items()
            if detail.get("provider") == provider_id
            and isinstance(detail.get("remaining_seconds"), int)
            and detail.get("remaining_seconds", 0) > 0
        ]
        model_count = len(provider.get("models", []))
        result.append({
            "provider_id": provider_id,
            "connection_state": provider.get("state", "unknown"),
            "last_test_at": provider.get("last_test_at"),
            "last_test_outcome": (
                "success" if provider.get("state") == "connected" and provider.get("last_test_at")
                else "failure" if provider.get("state") == "failed" and provider.get("last_test_at")
                else "unknown"
            ),
            "last_error": provider.get("last_error"),
            "catalog_observed_at": provider.get("catalog_observed_at"),
            "model_count": model_count,
            "cooldown_remaining_seconds": max(cooldowns) if cooldowns else 0,
            "recent_requests": total,
            "recent_success_rate": counts[0] / total if total else None,
            "recent_failure_rate": counts[1] / total if total else None,
        })
    return result
