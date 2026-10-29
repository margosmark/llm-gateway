"""Build a side-effect-free preview of the routed upstream request."""

from __future__ import annotations

from typing import Any

from gateway.bridge.model import ConversionError
from gateway.bridge.requests import convert_request
from gateway.core.routing import Router, RoutingError, _requested_reasoning_effort
from gateway.session import derive_session


def preview_request(
    router: Router, alias_name: str, payload: dict[str, Any], *,
    source_dialect: str, profile_id: str | None, client_ip: str | None,
    session_hint: str | None = None,
) -> dict[str, Any]:
    """Return the primary route's exact converted payload without invoking it.

    Fallbacks are not previewed: their choice depends on a provider result.
    Eligibility and the same conversion/degradation rules as invocation apply.
    """
    alias = router.resolve_alias(alias_name)
    needs_tools = bool(payload.get("tools"))
    effort = _requested_reasoning_effort(payload)
    candidates = router.candidates(
        alias, source_dialect=source_dialect, stream=bool(payload.get("stream")),
        needs_tools=needs_tools, reasoning_effort=effort,
    )
    if not candidates:
        raise RoutingError(f"alias '{alias_name}' has no routes", code="no_routes", status=409)
    eligible = [candidate for candidate in candidates if candidate.eligible]
    if not eligible:
        raise RoutingError(
            f"no eligible route for alias '{alias_name}'", code="no_eligible_route",
            status=503, detail=[candidate.to_dict() for candidate in candidates],
        )
    candidate = eligible[0]
    try:
        upstream = convert_request(payload, source_dialect, candidate.dialect)
    except ConversionError as exc:
        raise RoutingError(exc.message, code="conversion_refused", status=422) from exc
    upstream = dict(upstream)
    upstream["model"] = candidate.route.model
    degradation = candidate.degraded_reason
    if degradation:
        upstream.pop("reasoning_effort", None)
        reasoning = upstream.get("reasoning")
        if isinstance(reasoning, dict):
            upstream["reasoning"] = {key: value for key, value in reasoning.items() if key != "effort"}
            if not upstream["reasoning"]:
                upstream.pop("reasoning")
    conversation = derive_session(profile_id, alias_name, client_ip, session_hint)
    return {
        "model": candidate.route.model, "dialect": candidate.dialect,
        "session_id": f"{candidate.route.provider}.{conversation}",
        "thinking_level": effort if not degradation else None,
        "tools": upstream.get("tools", []), "payload": upstream,
        "degradation": degradation,
        "route": {"provider": candidate.route.provider, "model": candidate.route.model},
        "fallbacks": "not evaluated; depends on an upstream result",
    }
