"""Alias and route validation.

A published alias that points at an unknown provider or model is worse than
no alias: it appears in ``/v1/models`` and then fails during routing, which
is exactly the failure the owner sees as "the model is listed but does not
work".

So a route is checked **before** the configuration is written:

* the primary must name a registered provider and an enabled, known model;
* every fallback must as well;
* a fallback may not repeat another route, and may not be the primary.

The check is deliberately strict. A route that cannot be served is refused
at the point the owner creates it, not discovered later.
"""

from __future__ import annotations

from typing import Any, Iterable

from gateway.config import ConfigStore, PhaseRouting, RouteRef
from gateway.identifiers import ValidationError, validate_model_id


def _describe(ref: RouteRef) -> str:
    return f"{ref.provider} → {ref.model}"


def validate_route(
    config: ConfigStore,
    registry,
    provider_id: Any,
    model_id: Any,
    field: str,
) -> RouteRef:
    """Validate one route reference against the live configuration."""
    if not isinstance(provider_id, str) or not provider_id:
        raise ValidationError(f"the {field} must name a provider", field)
    if not isinstance(model_id, str) or not model_id:
        raise ValidationError(f"the {field} must name a model", field)

    model = validate_model_id(model_id)

    if registry.get(provider_id) is None:
        raise ValidationError(
            f"the {field} names provider '{provider_id}', which is not registered",
            field,
        )

    snapshot = config.snapshot()
    provider = snapshot.providers.get(provider_id)
    if provider is None:
        raise ValidationError(
            f"the {field} names provider '{provider_id}', which has no configuration",
            field,
        )
    entry = provider.models.get(model)
    if entry is None:
        raise ValidationError(
            f"the {field} names model '{model}', which is not known at "
            f"'{provider_id}'. Refresh the catalogue or enter the model first.",
            field,
        )
    if not entry.enabled:
        raise ValidationError(
            f"the {field} names model '{model}', which is not enabled for exposure",
            field,
        )
    return RouteRef(provider=provider_id, model=model)


def validate_weighted_routes(config: ConfigStore, registry, weighted_routes: Any) -> list[tuple[RouteRef, int]]:
    """Validate an alias's distribution pool, keeping it distinct from fallbacks."""
    if not isinstance(weighted_routes, list) or len(weighted_routes) < 2:
        raise ValidationError("a weighted pool must contain at least two routes", "weighted_routes")
    resolved: list[tuple[RouteRef, int]] = []
    seen: set[tuple[str, str]] = set()
    for index, item in enumerate(weighted_routes):
        field = f"weighted route {index + 1}"
        if not isinstance(item, dict):
            raise ValidationError(f"the {field} must be an object", field)
        weight = item.get("weight")
        if isinstance(weight, bool) or not isinstance(weight, int) or not 1 <= weight <= 1000:
            raise ValidationError(f"the {field} weight must be an integer from 1 to 1000", field)
        ref = validate_route(config, registry, item.get("provider"), item.get("model"), field)
        key = (ref.provider, ref.model)
        if key in seen:
            raise ValidationError(f"the {field} repeats an earlier pool route", field)
        seen.add(key)
        resolved.append((ref, weight))
    return resolved


def validate_alias_routes(
    config: ConfigStore,
    registry,
    primary: Any,
    fallbacks: Iterable[Any],
) -> tuple[RouteRef | None, list[RouteRef]]:
    """Validate an alias's primary route and its ordered fallbacks."""
    primary_ref: RouteRef | None = None
    if primary is not None:
        if not isinstance(primary, dict):
            raise ValidationError("the primary route must be an object", "primary")
        primary_ref = validate_route(
            config, registry, primary.get("provider"), primary.get("model"), "primary route"
        )

    resolved: list[RouteRef] = []
    seen: set[tuple[str, str]] = set()
    if primary_ref is not None:
        seen.add((primary_ref.provider, primary_ref.model))

    for index, item in enumerate(fallbacks or []):
        field = f"fallback {index + 1}"
        if not isinstance(item, dict):
            raise ValidationError(f"the {field} must be an object", field)
        ref = validate_route(
            config, registry, item.get("provider"), item.get("model"), field
        )
        key = (ref.provider, ref.model)
        if key in seen:
            if primary_ref is not None and key == (primary_ref.provider, primary_ref.model):
                raise ValidationError(
                    f"the {field} repeats the primary route ({_describe(ref)})", field
                )
            raise ValidationError(
                f"the {field} repeats an earlier route ({_describe(ref)})", field
            )
        seen.add(key)
        resolved.append(ref)

    return primary_ref, resolved


def validate_phase_routing(
    config: ConfigStore,
    registry,
    phase_routing: Any,
) -> PhaseRouting | None:
    """Validate an alias's optional phase-routing policy.

    Every phase route must name a registered provider and an enabled model,
    exactly like an alias primary or fallback. The inference model, when
    inference is enabled, must also be a known enabled model at its provider.
    Anything invalid is refused rather than persisted.
    """
    if phase_routing is None:
        return None
    if not isinstance(phase_routing, dict):
        raise ValidationError("phase_routing must be an object", "phase_routing")

    raw_routes = phase_routing.get("routes")
    routes: dict = {}
    if raw_routes is not None:
        if not isinstance(raw_routes, dict):
            raise ValidationError("phase routes must be an object", "phase_routing.routes")
        for phase_key, value in raw_routes.items():
            if phase_key not in ("plan", "implement", "validate"):
                raise ValidationError(
                    f"unknown phase '{phase_key}'; expected plan, implement, or validate",
                    "phase_routing.routes",
                )
            if not isinstance(value, dict):
                raise ValidationError(f"the '{phase_key}' phase route must be an object", "phase_routing.routes")
            ref = validate_route(
                config, registry, value.get("provider"), value.get("model"),
                f"phase route '{phase_key}'",
            )
            effort = value.get("effort")
            routes[phase_key] = {
                "phase": phase_key,
                "route": ref.to_dict(),
                "effort": effort if isinstance(effort, str) and effort else None,
            }

    default_route_raw = phase_routing.get("default_route")
    default_route: RouteRef | None = None
    if default_route_raw is not None:
        if not isinstance(default_route_raw, dict):
            raise ValidationError("the phase default_route must be an object", "phase_routing.default_route")
        default_route = validate_route(
            config, registry, default_route_raw.get("provider"), default_route_raw.get("model"),
            "phase default route",
        )

    inference_raw = phase_routing.get("inference")
    inference = None
    if inference_raw is not None:
        if not isinstance(inference_raw, dict):
            raise ValidationError("inference must be an object", "phase_routing.inference")
        enabled = bool(inference_raw.get("enabled", False))
        provider = str(inference_raw.get("provider") or "opencode_console")
        model = inference_raw.get("model")
        if enabled:
            if registry.get(provider) is None:
                raise ValidationError(
                    f"inference names provider '{provider}', which is not registered",
                    "phase_routing.inference.provider",
                )
            if not isinstance(model, str) or not model:
                raise ValidationError(
                    "inference must name a model when enabled",
                    "phase_routing.inference.model",
                )
            snapshot = config.snapshot()
            prov = snapshot.providers.get(provider)
            entry = prov.models.get(model) if prov else None
            if entry is None or not entry.enabled:
                raise ValidationError(
                    f"inference model '{model}' is not a known enabled model at '{provider}'",
                    "phase_routing.inference.model",
                )
        confidence = inference_raw.get("minimum_confidence")
        if confidence is not None and not (
            isinstance(confidence, (int, float)) and not isinstance(confidence, bool)
            and 0.0 <= float(confidence) <= 1.0
        ):
            raise ValidationError(
                "inference minimum_confidence must be between 0 and 1",
                "phase_routing.inference.minimum_confidence",
            )
        inference = {
            "enabled": enabled,
            "provider": provider,
            "model": model if isinstance(model, str) else "",
            "minimum_confidence": confidence if confidence is not None else 0.8,
            "on_uncertain": inference_raw.get("on_uncertain") if inference_raw.get("on_uncertain") in ("default",) else "default",
        }

    return PhaseRouting.from_dict({
        "enabled": bool(phase_routing.get("enabled", False)),
        "routes": routes,
        "default_route": default_route.to_dict() if default_route else None,
        "inference": inference,
    })
