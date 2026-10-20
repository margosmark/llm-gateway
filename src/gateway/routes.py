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

from gateway.config import ConfigStore, RouteRef
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
