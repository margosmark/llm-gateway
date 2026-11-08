"""Gateway configuration.

The configuration is the non-secret half of the gateway's state: providers,
model enablement, aliases and their routes, client profiles, and the handful
of settings the owner can change from the console.

The secret half lives in :mod:`gateway.credentials` and is never merged into
this structure, so there is no code path that could accidentally serialise a
credential into a config file, a log line, or an HTTP response.

Every mutation bumps a monotonic ``revision`` counter. The console shows it,
and it gives the owner a way to tell that a change landed.
"""

from __future__ import annotations

import math
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from gateway.stores import atomic_write_json, read_json
from gateway.thinking import CANONICAL_LEVELS

# Provider connection states. These four are the only states a provider card
# may display.
STATE_NOT_CONFIGURED = "not_configured"
STATE_CONFIGURED_UNTESTED = "configured_untested"
STATE_CONNECTED = "connected"
STATE_FAILED = "failed"

PROVIDER_STATES = (
    STATE_NOT_CONFIGURED,
    STATE_CONFIGURED_UNTESTED,
    STATE_CONNECTED,
    STATE_FAILED,
)

ACCESS_LAN_NOAUTH = "lan_noauth"
ACCESS_AUTHENTICATED = "authenticated"
ACCESS_MODES = (ACCESS_LAN_NOAUTH, ACCESS_AUTHENTICATED)

# Fallback triggers. The owner selects these explicitly per alias. Every
# path that hands a request to a route other than the primary consults
# them: an upstream failure contributes its recorded failure trigger, and
# a *skipped* primary contributes the trigger named for its skip - a
# cooldown carries the failure class that opened it (or
# ``explicit_cooldown`` for an operator-placed circuit), a capability or
# configuration skip carries ``route_ineligible``, and a route held by
# another request's live recovery probe carries ``probe_contention``.
TRIGGER_EXHAUSTED = "exhausted"
TRIGGER_RATE_LIMITED = "rate_limited"
TRIGGER_UPSTREAM_UNAVAILABLE = "upstream_unavailable"
TRIGGER_EXPLICIT_COOLDOWN = "explicit_cooldown"
#: The primary was skipped for a capability or configuration reason
#: (disabled, unknown, does not serve this request's shape), and the
#: next route is about to serve instead.
TRIGGER_ROUTE_INELIGIBLE = "route_ineligible"
#: The primary is held by another request's live half-open recovery
#: probe, and the next route is about to serve instead. It is an
#: automatic event, so it is deliberately *not* folded into
#: ``explicit_cooldown``, which means the operator placed the hold.
TRIGGER_PROBE_CONTENTION = "probe_contention"
FALLBACK_TRIGGERS = (
    TRIGGER_EXHAUSTED,
    TRIGGER_RATE_LIMITED,
    TRIGGER_UPSTREAM_UNAVAILABLE,
    TRIGGER_EXPLICIT_COOLDOWN,
    TRIGGER_ROUTE_INELIGIBLE,
    TRIGGER_PROBE_CONTENTION,
)

# Phases a request can be classified into. A caller or the gateway's own
# Jev inference may name one; the gateway maps it to an owner-configured
# route. The vocabulary is closed so a hostile or malformed answer cannot
# select an arbitrary route.
PHASE_PLAN = "plan"
PHASE_IMPLEMENT = "implement"
PHASE_VALIDATE = "validate"
PHASES = (PHASE_PLAN, PHASE_IMPLEMENT, PHASE_VALIDATE)

# Error classes shared by the routing core and the adapters.
ERROR_EXHAUSTED = "exhausted"
ERROR_RATE_LIMITED = "rate_limited"
ERROR_TRANSIENT = "transient"
ERROR_AUTH = "auth"
ERROR_POLICY = "policy"
ERROR_MALFORMED = "malformed"
#: The upstream no longer serves this model on this route. It is an
#: availability fact about the route, not a policy failure and not a
#: temporary condition: retrying the same route will not help, but another
#: route may still serve the request.
ERROR_ROUTE_GONE = "route_gone"
ERROR_UNKNOWN = "unknown"
ERROR_CLASSES = (
    ERROR_EXHAUSTED,
    ERROR_RATE_LIMITED,
    ERROR_TRANSIENT,
    ERROR_AUTH,
    ERROR_POLICY,
    ERROR_MALFORMED,
    ERROR_ROUTE_GONE,
    ERROR_UNKNOWN,
)

# Classes for which the owner may explicitly define a safe fallback policy.
# Everything else fails closed by default.
OWNER_OVERRIDABLE_CLASSES = (ERROR_AUTH, ERROR_POLICY, ERROR_MALFORMED)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass
class RouteRef:
    """A provider-qualified model reference.

    The provider is always part of the reference. The same model string can
    exist at two providers with different dialects, limits and retention, so a
    bare model id is never sufficient.
    """

    provider: str
    model: str

    def to_dict(self) -> dict[str, str]:
        return {"provider": self.provider, "model": self.model}

    @classmethod
    def from_dict(cls, data: Any) -> "RouteRef | None":
        if not isinstance(data, dict):
            return None
        provider = data.get("provider")
        model = data.get("model")
        if not isinstance(provider, str) or not isinstance(model, str) or not provider or not model:
            return None
        return cls(provider=provider, model=model)

    def label(self) -> str:
        return f"{self.provider} → {self.model}"


#: The fields an owner may set by hand from the console. A record of each
#: such value is kept on the entry (see ``ModelEntry.owner_overrides``) so a
#: later refresh can merge instead of replace: it can tell the owner's value
#: from a published one, keep the former when the provider publishes nothing,
#: and report the latter when it takes it back. Nothing else is owner-editable
#: - ``enabled`` is a preference held directly on the entry, and every other
#: field is a published fact.
OWNER_EDITABLE_FIELDS = ("dialect", "notes")


@dataclass
class ModelEntry:
    """One model at one provider.

    The metadata here is what the router uses to decide whether a route may
    serve a request. It is populated by an *explicit* discovery or refresh
    action, so the router never has to call a provider to answer a routing
    question.
    """

    model_id: str
    enabled: bool = False
    dialect: str | None = None
    source: str = "manual"  # "discovered" or "manual"
    observed_at: str | None = None
    limits_known: bool = False
    notes: str | None = None
    reasoning_efforts: list[str] = field(default_factory=list)
    supports_streaming: bool = True
    supports_function_tools: bool = True
    supports_builtin_tools: bool = False
    supports_image_input: bool = False
    context_window: int | None = None
    context_limit: int | None = None
    max_output_tokens: int | None = None
    retention_class: str = "unknown"
    monthly_limit_usd: float | None = None
    limits_source: str = "unknown"
    limits_observed_at: str | None = None
    #: Provenance for the values the owner set by hand: field name ->
    #: ``{"owner": <the owner's value>, "published": <the value the provider
    #: published then, or None>}``. This is what makes an owner action able to
    #: *merge* and a refresh able to merge back rather than replace: an
    #: override survives while the provider publishes nothing for that field,
    #: and when the provider publishes a different value the published one
    #: stands and the supersession is reported instead of being silent.
    owner_overrides: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Any) -> "ModelEntry | None":
        if not isinstance(data, dict):
            return None
        model_id = data.get("model_id")
        if not isinstance(model_id, str) or not model_id:
            return None

        def as_str(key: str) -> str | None:
            value = data.get(key)
            return value if isinstance(value, str) else None

        def as_int(key: str) -> int | None:
            value = data.get(key)
            return value if isinstance(value, int) else None

        def as_float(key: str) -> float | None:
            value = data.get(key)
            return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None

        def as_bool(key: str, default: bool) -> bool:
            # Never coerce: a non-boolean (the string "false", an int, null)
            # falls back to the declared default instead of becoming True.
            value = data.get(key, default)
            return value if isinstance(value, bool) else default

        efforts = data.get("reasoning_efforts")
        # Provenance is validated before persistence, like everything else
        # here: an unknown field name or a malformed record is dropped rather
        # than loaded, so a hand-edited state file cannot smuggle a record for
        # a field the owner cannot set.
        overrides: dict[str, Any] = {}
        raw_overrides = data.get("owner_overrides")
        if isinstance(raw_overrides, dict):
            for field_name, record in raw_overrides.items():
                if field_name not in OWNER_EDITABLE_FIELDS or not isinstance(record, dict):
                    continue
                overrides[field_name] = {
                    "owner": record.get("owner"),
                    "published": record.get("published"),
                }
        return cls(
            model_id=model_id,
            enabled=as_bool("enabled", False),
            dialect=as_str("dialect"),
            source=str(data.get("source") or "manual"),
            observed_at=as_str("observed_at"),
            limits_known=as_bool("limits_known", False),
            notes=as_str("notes"),
            reasoning_efforts=[e for e in efforts or [] if isinstance(e, str)],
            supports_streaming=as_bool("supports_streaming", True),
            supports_function_tools=as_bool("supports_function_tools", True),
            supports_builtin_tools=as_bool("supports_builtin_tools", False),
            supports_image_input=as_bool("supports_image_input", False),
            context_window=as_int("context_window"),
            context_limit=as_int("context_limit"),
            max_output_tokens=as_int("max_output_tokens"),
            retention_class=str(data.get("retention_class") or "unknown"),
            monthly_limit_usd=as_float("monthly_limit_usd"),
            limits_source=str(data.get("limits_source") or "unknown"),
            limits_observed_at=as_str("limits_observed_at"),
            owner_overrides=overrides,
        )


#: Hard ceiling for ``max_in_flight``, the per-provider in-flight cap. The
#: cap refuses a request only while ``active >= limit``, so a value larger
#: than the traffic the gateway could ever generate is the cap silently
#: switched off - a persisted ``10**400`` did exactly that (finding F2 of
#: security gate `t_3fd0925e`, ``docs/REVIEW.md`` R74). Both the owner API
#: (400 ``invalid_max_in_flight``) and this parser refuse the far end; the
#: bound is documented in ``docs/RUNBOOK.md`` §Per-provider concurrency.
MAX_IN_FLIGHT_CEILING = 4096


def _positive_int(value: Any, maximum: int | None = None) -> int | None:
    """A positive ``int``, or ``None`` for anything else (never a ``bool``).

    With ``maximum``, a value above the bound is treated exactly like any
    other invalid one: dropped to ``None``, so a hand-edited config falls
    back to the default instead of carrying a value that would disable
    what the field is supposed to enforce.
    """
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    if maximum is not None and value > maximum:
        return None
    return value


@dataclass
class ProviderState:
    """Connection state for one provider. Never holds credential material."""

    provider_id: str
    kind: str
    state: str = STATE_NOT_CONFIGURED
    last_test_at: str | None = None
    last_error: str | None = None
    models: dict[str, ModelEntry] = field(default_factory=dict)
    catalog_observed_at: str | None = None
    #: The owner-configured destination for a local provider (Ollama,
    #: llama.cpp server, LM Studio). Non-secret by design: it is a host the
    #: owner typed, it is only ever rendered on the owner-authenticated
    #: state endpoint, and credentials are never allowed to live in it. It
    #: stays ``None`` until the owner saves one, which is what keeps a local
    #: provider fail-closed at boot with no outbound call.
    base_url: str | None = None
    #: The owner's per-provider in-flight cap, or ``None`` for the default
    #: (cloud providers take the global default, the local adapters stay
    #: low - see :mod:`gateway.provider_concurrency`). Non-secret, owner
    #: set from the console; read live on every acquire, so a saved value
    #: needs no restart.
    max_in_flight: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "kind": self.kind,
            "state": self.state,
            "last_test_at": self.last_test_at,
            "last_error": self.last_error,
            "catalog_observed_at": self.catalog_observed_at,
            "base_url": self.base_url,
            "max_in_flight": self.max_in_flight,
            "models": {key: value.to_dict() for key, value in sorted(self.models.items())},
        }

    @classmethod
    def from_dict(cls, data: Any) -> "ProviderState | None":
        if not isinstance(data, dict):
            return None
        provider_id = data.get("provider_id")
        kind = data.get("kind")
        if not isinstance(provider_id, str) or not isinstance(kind, str):
            return None
        state = data.get("state")
        if state not in PROVIDER_STATES:
            state = STATE_NOT_CONFIGURED
        models: dict[str, ModelEntry] = {}
        raw_models = data.get("models")
        if isinstance(raw_models, dict):
            for key, value in raw_models.items():
                entry = ModelEntry.from_dict(value)
                if entry is not None:
                    models[str(key)] = entry
        return cls(
            provider_id=provider_id,
            kind=kind,
            state=state,
            last_test_at=data.get("last_test_at") if isinstance(data.get("last_test_at"), str) else None,
            last_error=data.get("last_error") if isinstance(data.get("last_error"), str) else None,
            models=models,
            catalog_observed_at=(
                data.get("catalog_observed_at") if isinstance(data.get("catalog_observed_at"), str) else None
            ),
            # Tolerant: a config written before this field existed simply
            # has no endpoint, and a non-string value is treated as absent
            # rather than passed downstream to a URL parser.
            base_url=data.get("base_url") if isinstance(data.get("base_url"), str) and data.get("base_url") else None,
            # Tolerant the same way: only a positive integer survives the
            # round trip, so a hand-edited config with 0, a negative, a
            # bool or a string falls back to the default instead of
            # disabling the provider or crashing the boot path. A value
            # above MAX_IN_FLIGHT_CEILING is dropped the same way - it
            # would be the cap switched off, not a limit (R74).
            max_in_flight=_positive_int(data.get("max_in_flight"), maximum=MAX_IN_FLIGHT_CEILING),
        )

    def enabled_models(self) -> list[ModelEntry]:
        return [entry for entry in self.models.values() if entry.enabled]


@dataclass
class PhaseRoute:
    """One phase-to-route mapping.

    The route is a normal provider-qualified reference. It is validated the
    same way an alias primary or fallback is: the provider must exist and the
    model must be enabled, or the route is refused at save time.
    """

    phase: str
    route: RouteRef
    effort: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"phase": self.phase, "route": self.route.to_dict()}
        if self.effort is not None:
            payload["effort"] = self.effort
        return payload

    @classmethod
    def from_dict(cls, data: Any) -> "PhaseRoute | None":
        if not isinstance(data, dict):
            return None
        phase = data.get("phase")
        if phase not in PHASES:
            return None
        route = RouteRef.from_dict(data.get("route"))
        if route is None:
            return None
        effort = data.get("effort")
        return cls(phase=phase, route=route, effort=effort if isinstance(effort, str) and effort else None)


@dataclass
class JevInference:
    """An opt-in Jev phase-inference policy for one alias.

    Disabled by default. When enabled and the caller supplies no phase, the
    gateway sends a bounded, closed-set ``choice`` question to the configured
    Jev model. A choice applies only when it is one of the owner-configured
    phases and its confidence meets the threshold. Anything else uses the
    alias's ordinary primary route.
    """

    enabled: bool = False
    provider: str = "opencode_console"
    model: str = ""
    minimum_confidence: float = 0.8
    on_uncertain: str = "default"  # "default" = the alias's ordinary primary route.

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "provider": self.provider,
            "model": self.model,
            "minimum_confidence": self.minimum_confidence,
            "on_uncertain": self.on_uncertain,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "JevInference | None":
        if not isinstance(data, dict):
            return None
        model = data.get("model")
        confidence = data.get("minimum_confidence")
        on_uncertain = data.get("on_uncertain")
        return cls(
            enabled=bool(data.get("enabled", False)),
            provider=str(data.get("provider") or "opencode_console"),
            model=model if isinstance(model, str) else "",
            minimum_confidence=(
                confidence if isinstance(confidence, (int, float)) and not isinstance(confidence, bool)
                and 0.0 <= float(confidence) <= 1.0 else 0.8
            ),
            on_uncertain=on_uncertain if on_uncertain in ("default",) else "default",
        )


@dataclass
class PhaseRouting:
    """An optional, opt-in per-alias phase-routing policy.

    Routes are keyed by phase (``plan``, ``implement``, ``validate``). A
    ``default`` entry names the route used when no phase applies or inference
    is uncertain. When no ``default`` is configured, the alias's ordinary
    primary route is used.
    """

    enabled: bool = False
    routes: dict[str, PhaseRoute] = field(default_factory=dict)
    default_route: RouteRef | None = None
    inference: JevInference = field(default_factory=JevInference)

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "routes": {key: value.to_dict() for key, value in sorted(self.routes.items())},
            "default_route": self.default_route.to_dict() if self.default_route else None,
            "inference": self.inference.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Any) -> "PhaseRouting | None":
        if not isinstance(data, dict):
            return None
        routes: dict[str, PhaseRoute] = {}
        raw_routes = data.get("routes")
        if isinstance(raw_routes, dict):
            for value in raw_routes.values():
                entry = PhaseRoute.from_dict(value)
                if entry is not None:
                    routes[entry.phase] = entry
        default_route = RouteRef.from_dict(data.get("default_route"))
        inference = JevInference.from_dict(data.get("inference"))
        return cls(
            enabled=bool(data.get("enabled", False)),
            routes=routes,
            default_route=default_route,
            inference=inference if inference is not None else JevInference(),
        )

    def route_for(self, phase: str | None) -> RouteRef | None:
        """The route for a phase, falling back to the configured default."""
        if phase is not None and phase in self.routes:
            return self.routes[phase].route
        return self.default_route

    def all_routes(self) -> list[RouteRef]:
        """Every route this policy may select, de-duplicated."""
        seen: set[tuple[str, str]] = set()
        ordered: list[RouteRef] = []
        for ref in [entry.route for entry in self.routes.values()] + (
            [self.default_route] if self.default_route else []
        ):
            key = (ref.provider, ref.model)
            if key not in seen:
                seen.add(key)
                ordered.append(ref)
        return ordered


# --- the alias request policy (T21) ------------------------------------

#: The owner's default system instruction is bounded: a policy value is
#: configuration, not a payload dump, and a bound keeps the config file
#: readable and a request small.
MAX_SYSTEM_DEFAULT_LENGTH = 4000

#: The published temperature range. A default outside it is not a
#: temperature this gateway can ask for, so it is not accepted into the
#: configuration.
TEMPERATURE_MIN = 0.0
TEMPERATURE_MAX = 2.0


def _policy_temperature(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not TEMPERATURE_MIN <= number <= TEMPERATURE_MAX:
        return None
    return number


def _policy_tokens(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _policy_level(value: Any) -> str | None:
    """Accept a thinking level only from the canonical vocabulary.

    The owner-facing vocabulary is the one ``gateway.thinking``
    publishes; a level outside it would be a second vocabulary, which is
    exactly what the canonical model exists to prevent.
    """
    return value if isinstance(value, str) and value in CANONICAL_LEVELS else None


@dataclass
class PolicyDefaults:
    """Owner defaults, applied when the client specified nothing.

    A default fills a silence; it never overrides a client that asked for
    something. What it changed is recorded per request by
    ``gateway.policy`` - the ledger, not this file.
    """

    temperature: float | None = None
    max_output_tokens: int | None = None
    thinking_level: str | None = None
    system: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        if self.max_output_tokens is not None:
            payload["max_output_tokens"] = self.max_output_tokens
        if self.thinking_level is not None:
            payload["thinking_level"] = self.thinking_level
        if self.system is not None:
            payload["system"] = self.system
        return payload

    @classmethod
    def from_dict(cls, data: Any) -> PolicyDefaults | None:
        if not isinstance(data, dict):
            return None
        temperature = _policy_temperature(data.get("temperature"))
        tokens = _policy_tokens(data.get("max_output_tokens"))
        level = _policy_level(data.get("thinking_level"))
        system = data.get("system")
        system = (
            system
            if isinstance(system, str) and system.strip() and len(system) <= MAX_SYSTEM_DEFAULT_LENGTH
            else None
        )
        if temperature is None and tokens is None and level is None and system is None:
            return None
        return cls(temperature=temperature, max_output_tokens=tokens, thinking_level=level, system=system)


@dataclass
class PolicyCaps:
    """Owner ceilings, applied over the client's value.

    A cap is an ordinal ceiling for the thinking level (the canonical
    order, weakest first) and a numeric one for the output budget. A
    value the cap clamps is a degradation in the sense R19 established:
    it goes into the attribution and the ledger, never invisible.
    """

    max_output_tokens: int | None = None
    thinking_level: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if self.max_output_tokens is not None:
            payload["max_output_tokens"] = self.max_output_tokens
        if self.thinking_level is not None:
            payload["thinking_level"] = self.thinking_level
        return payload

    @classmethod
    def from_dict(cls, data: Any) -> PolicyCaps | None:
        if not isinstance(data, dict):
            return None
        tokens = _policy_tokens(data.get("max_output_tokens"))
        level = _policy_level(data.get("thinking_level"))
        if tokens is None and level is None:
            return None
        return cls(max_output_tokens=tokens, thinking_level=level)


@dataclass
class PolicyDeny:
    """A refusal and a forced value.

    ``tools`` is a refusal: a request that carries tools is refused
    outright with the rule named, never stripped - a silently stripped
    tool list would break the request the client actually made.

    ``store`` is a *forced value*, not a refusal: ``store: false`` is
    applied to every request that reaches a provider and the forcing is
    recorded. Retention is the owner's call, not the caller's.
    """

    tools: bool = False
    store: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"tools": self.tools}
        if self.store is not None:
            payload["store"] = self.store
        return payload

    @classmethod
    def from_dict(cls, data: Any) -> PolicyDeny | None:
        if not isinstance(data, dict):
            return None
        tools = bool(data.get("tools", False))
        store = data.get("store")
        store = store if isinstance(store, bool) else None
        if not tools and store is None:
            return None
        return cls(tools=tools, store=store)


@dataclass
class AliasPolicy:
    """The owner's request policy for one alias.

    Applied in one place - ``gateway.policy``, called by the router on
    every path that reaches a provider - in this order: the client's
    value, the owner's default if the client gave none, the owner's cap
    if it clamps the value now standing, then the deny rules. It applies
    before dialect conversion, so the owner writes one vocabulary
    (``thinking_level: high``) regardless of the dialect a route serves.
    """

    defaults: PolicyDefaults | None = None
    caps: PolicyCaps | None = None
    deny: PolicyDeny | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "defaults": self.defaults.to_dict() if self.defaults else None,
            "caps": self.caps.to_dict() if self.caps else None,
            "deny": self.deny.to_dict() if self.deny else None,
        }

    @classmethod
    def from_dict(cls, data: Any) -> AliasPolicy | None:
        if not isinstance(data, dict):
            return None
        defaults = PolicyDefaults.from_dict(data.get("defaults"))
        caps = PolicyCaps.from_dict(data.get("caps"))
        deny = PolicyDeny.from_dict(data.get("deny"))
        if defaults is None and caps is None and deny is None:
            # A block that rules nothing is no policy at all, so the
            # alias reads as un-policied rather than as an empty shell.
            return None
        return cls(defaults=defaults, caps=caps, deny=deny)


@dataclass
class WeightedRoute:
    """A route in an alias's deliberate traffic-distribution pool."""

    route: RouteRef
    weight: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {"route": self.route.to_dict(), "weight": self.weight}

    @classmethod
    def from_dict(cls, data: Any) -> WeightedRoute | None:
        if not isinstance(data, dict):
            return None
        route = RouteRef.from_dict(data.get("route"))
        weight = data.get("weight")
        if route is None or isinstance(weight, bool) or not isinstance(weight, int) or weight <= 0:
            return None
        return cls(route=route, weight=weight)


@dataclass
class Alias:
    """A friendly name that resolves to a primary/pool and ordered fallbacks."""

    name: str
    enabled: bool = False
    primary: RouteRef | None = None
    fallbacks: list[RouteRef] = field(default_factory=list)
    #: The fallback triggers the owner has enabled for this alias. Every
    #: cross-provider switch needs one: the recorded failure trigger when
    #: a tried provider failed, or the trigger that names a *skip* -
    #: ``explicit_cooldown`` for an operator-placed cooldown, the
    #: failure's own trigger for an automatic cooldown,
    #: ``route_ineligible`` for a capability or configuration skip, and
    #: ``probe_contention`` for a route held by another request's live
    #: recovery probe.
    fallback_triggers: list[str] = field(default_factory=list)
    fallback_authorized: bool = False
    #: Providers the owner has explicitly confirmed may receive requests
    #: assigned to this alias. Cross-provider fallback is refused unless the
    #: fallback provider appears here *and* ``fallback_authorized`` is set.
    fallback_confirmed_providers: list[str] = field(default_factory=list)
    #: Error classes the owner has explicitly defined a safe policy for.
    #: Empty by default: authentication, authorisation, policy and
    #: malformed-request failures never trigger a fallback.
    fallback_error_classes: list[str] = field(default_factory=list)
    cooldown_seconds: int = 300
    #: An optional, opt-in phase-routing policy. Disabled by default.
    phase_routing: PhaseRouting | None = None
    #: An optional local monthly budget in USD for this alias. None = unbudgeted.
    budget: float | None = None
    #: Optional group tag; aliases sharing a tag render as one console group.
    tag: str = ""
    #: An optional, opt-in request policy: owner defaults, caps and deny
    #: rules applied to every request this alias serves. Absent by
    #: default: an alias with no policy lets the client dictate every
    #: parameter, which remains the product's default posture.
    policy: AliasPolicy | None = None
    weighted_routes: list[WeightedRoute] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "enabled": self.enabled,
            "primary": self.primary.to_dict() if self.primary else None,
            "weighted_routes": [route.to_dict() for route in self.weighted_routes],
            "fallbacks": [ref.to_dict() for ref in self.fallbacks],
            "fallback_triggers": list(self.fallback_triggers),
            "fallback_authorized": self.fallback_authorized,
            "fallback_confirmed_providers": list(self.fallback_confirmed_providers),
            "fallback_error_classes": list(self.fallback_error_classes),
            "cooldown_seconds": self.cooldown_seconds,
            "phase_routing": self.phase_routing.to_dict() if self.phase_routing else None,
            "budget": self.budget,
            "tag": self.tag,
            "policy": self.policy.to_dict() if self.policy else None,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "Alias | None":
        if not isinstance(data, dict):
            return None
        name = data.get("name")
        if not isinstance(name, str) or not name:
            return None
        primary = RouteRef.from_dict(data.get("primary"))
        raw_weighted_routes = data.get("weighted_routes")
        weighted_routes = [
            route for item in raw_weighted_routes
            if (route := WeightedRoute.from_dict(item)) is not None
        ] if isinstance(raw_weighted_routes, list) else []
        fallbacks: list[RouteRef] = []
        raw_fallbacks = data.get("fallbacks")
        if isinstance(raw_fallbacks, list):
            for item in raw_fallbacks:
                ref = RouteRef.from_dict(item)
                if ref is not None:
                    fallbacks.append(ref)
        triggers = [t for t in data.get("fallback_triggers") or [] if t in FALLBACK_TRIGGERS]
        confirmed = [p for p in data.get("fallback_confirmed_providers") or [] if isinstance(p, str)]
        safe_classes = [c for c in data.get("fallback_error_classes") or [] if c in OWNER_OVERRIDABLE_CLASSES]
        cooldown = data.get("cooldown_seconds")
        phase_routing = PhaseRouting.from_dict(data.get("phase_routing"))
        budget = data.get("budget")
        tag = data.get("tag", "")
        policy = AliasPolicy.from_dict(data.get("policy"))
        return cls(
            name=name,
            enabled=bool(data.get("enabled", False)),
            weighted_routes=weighted_routes,
            primary=primary,
            fallbacks=fallbacks,
            fallback_triggers=triggers,
            fallback_authorized=bool(data.get("fallback_authorized", False)),
            fallback_confirmed_providers=confirmed,
            fallback_error_classes=safe_classes,
            cooldown_seconds=cooldown if isinstance(cooldown, int) and cooldown >= 0 else 300,
            phase_routing=phase_routing,
            budget=budget if isinstance(budget, (int, float)) and not isinstance(budget, bool) and math.isfinite(budget) and 0 < budget <= 1_000_000 else None,
            tag=tag if isinstance(tag, str) else "",
            policy=policy,
        )

    def all_routes(self) -> list[RouteRef]:
        routes: list[RouteRef] = []
        if self.weighted_routes:
            routes.extend(item.route for item in self.weighted_routes)
        elif self.primary is not None:
            routes.append(self.primary)
        routes.extend(self.fallbacks)
        if self.phase_routing is not None:
            routes.extend(self.phase_routing.all_routes())
        # De-duplicate: a phase route may repeat the primary or a fallback.
        seen: set[tuple[str, str]] = set()
        unique: list[RouteRef] = []
        for ref in routes:
            key = (ref.provider, ref.model)
            if key not in seen:
                seen.add(key)
                unique.append(ref)
        return unique


@dataclass
class Profile:
    """A client profile. Scopes which aliases a caller may use.

    Profiles scope *aliases*, never upstream identity. Every caller shares the
    owner's single upstream account.
    """

    profile_id: str
    name: str
    enabled: bool = True
    allowed_aliases: list[str] = field(default_factory=list)
    #: Whether this profile may use every published alias. It is **not** the
    #: default: a new profile denies until the owner grants access, either
    #: alias by alias or with this explicit flag.
    all_aliases: bool = False
    routing_policy: dict[str, Any] = field(default_factory=dict)
    #: Whether a caller token exists. The token *hash* lives in the credential
    #: store, never in the configuration, so no config read can leak it.
    has_token: bool = False
    token_created_at: str | None = None
    token_rotated_at: str | None = None
    anonymous: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "name": self.name,
            "enabled": self.enabled,
            "allowed_aliases": list(self.allowed_aliases),
            "all_aliases": self.all_aliases,
            "routing_policy": dict(self.routing_policy),
            "has_token": self.has_token,
            "token_created_at": self.token_created_at,
            "token_rotated_at": self.token_rotated_at,
            "anonymous": self.anonymous,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "Profile | None":
        if not isinstance(data, dict):
            return None
        profile_id = data.get("profile_id")
        name = data.get("name")
        if not isinstance(profile_id, str) or not isinstance(name, str) or not profile_id:
            return None
        allowed = [a for a in data.get("allowed_aliases") or [] if isinstance(a, str)]
        policy = data.get("routing_policy")
        return cls(
            profile_id=profile_id,
            name=name,
            enabled=bool(data.get("enabled", True)),
            allowed_aliases=allowed,
            all_aliases=bool(data.get("all_aliases", False)),
            routing_policy=dict(policy) if isinstance(policy, dict) else {},
            has_token=bool(data.get("has_token", False)),
            token_created_at=(
                data.get("token_created_at") if isinstance(data.get("token_created_at"), str) else None
            ),
            token_rotated_at=(
                data.get("token_rotated_at") if isinstance(data.get("token_rotated_at"), str) else None
            ),
            anonymous=bool(data.get("anonymous", False)),
        )


@dataclass
class GatewayConfig:
    """The whole non-secret configuration."""

    revision: int = 0
    initialized: bool = False
    access_mode: str = ACCESS_LAN_NOAUTH
    #: Owner-console session gate (the ``owner_console_auth`` config key).
    #: ``True`` - the default - means every ``/owner`` route demands a
    #: signed-in owner session. ``False`` opts out: the console serves the
    #: owner without a session. The parse is deliberately fail-closed
    #: (``from_dict``): only the literal JSON ``false`` disables the gate,
    #: so an absent key, ``null``, ``0``, ``"no"`` or any other value
    #: leaves authentication REQUIRED instead of opening the console by
    #: accident. This flag governs the browser session only - it never
    #: touches the machine key or the model client key.
    owner_console_auth: bool = True
    providers: dict[str, ProviderState] = field(default_factory=dict)
    aliases: dict[str, Alias] = field(default_factory=dict)
    profiles: dict[str, Profile] = field(default_factory=dict)
    settings: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "initialized": self.initialized,
            "access_mode": self.access_mode,
            "owner_console_auth": self.owner_console_auth,
            "providers": {key: value.to_dict() for key, value in sorted(self.providers.items())},
            "aliases": {key: value.to_dict() for key, value in sorted(self.aliases.items())},
            "profiles": {key: value.to_dict() for key, value in sorted(self.profiles.items())},
            "settings": dict(self.settings),
        }

    @classmethod
    def from_dict(cls, data: Any) -> "GatewayConfig":
        if not isinstance(data, dict):
            return cls()
        providers: dict[str, ProviderState] = {}
        raw_providers = data.get("providers")
        if isinstance(raw_providers, dict):
            for key, value in raw_providers.items():
                provider = ProviderState.from_dict(value)
                if provider is not None:
                    providers[str(key)] = provider
        aliases: dict[str, Alias] = {}
        raw_aliases = data.get("aliases")
        if isinstance(raw_aliases, dict):
            for key, value in raw_aliases.items():
                alias = Alias.from_dict(value)
                if alias is not None:
                    aliases[str(key)] = alias
        profiles: dict[str, Profile] = {}
        raw_profiles = data.get("profiles")
        if isinstance(raw_profiles, dict):
            for key, value in raw_profiles.items():
                profile = Profile.from_dict(value)
                if profile is not None:
                    profiles[str(key)] = profile
        access_mode = data.get("access_mode")
        if access_mode not in ACCESS_MODES:
            access_mode = ACCESS_LAN_NOAUTH
        settings = data.get("settings")
        revision = data.get("revision")
        # ``owner_console_auth`` parses fail-closed: the console session
        # gate stays REQUIRED unless the stored value is the literal
        # boolean ``false``. Absent, ``null``, ``0``, ``"no"``, ``1`` or a
        # nested object all mean "authentication required", and this line
        # cannot raise - a parse error must never be able to skip the
        # gate (``is not False`` is an identity test on purpose: truthy
        # coercion would let ``0`` and ``"no"`` through as "disabled").
        owner_console_auth = data.get("owner_console_auth")
        return cls(
            revision=revision if isinstance(revision, int) and revision >= 0 else 0,
            initialized=bool(data.get("initialized", False)),
            access_mode=access_mode,
            owner_console_auth=owner_console_auth is not False,
            providers=providers,
            aliases=aliases,
            profiles=profiles,
            settings=dict(settings) if isinstance(settings, dict) else {},
        )

    def published_aliases(self) -> list[Alias]:
        """Aliases that appear in ``/v1/models``."""
        return [alias for alias in self.aliases.values() if alias.enabled and (alias.primary is not None or alias.weighted_routes)]


class ConfigStore:
    """Thread-safe, atomically persisted configuration."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.RLock()
        self._config = GatewayConfig.from_dict(read_json(path, {}))
        self._snapshot = GatewayConfig.from_dict(self._config.to_dict())

    @property
    def path(self) -> Path:
        return self._path

    def snapshot(self) -> GatewayConfig:
        """Return the current immutable-by-convention configuration view."""
        with self._lock:
            return self._snapshot

    def mutate(self) -> "_ConfigTransaction":
        """Open a transaction. The revision is bumped and persisted on exit."""
        return _ConfigTransaction(self)

    def replace(self, config: GatewayConfig) -> None:
        with self._lock:
            replacement = GatewayConfig.from_dict(config.to_dict())
            self._persist(replacement)
            self._config = replacement
            self._snapshot = GatewayConfig.from_dict(replacement.to_dict())

    def replace_validated(self, config: GatewayConfig) -> None:
        """Persist a prevalidated replacement before publishing it in memory."""
        with self._lock:
            replacement = GatewayConfig.from_dict(config.to_dict())
            self._persist(replacement)
            self._config = replacement
            self._snapshot = GatewayConfig.from_dict(replacement.to_dict())

    def _persist(self, config: GatewayConfig | None = None) -> None:
        atomic_write_json(self._path, (config or self._config).to_dict(), mode=0o600)


class _ConfigTransaction:
    """Context manager that mutates the configuration under a lock."""

    def __init__(self, store: ConfigStore) -> None:
        self._store = store
        self._config: GatewayConfig | None = None

    def __enter__(self) -> GatewayConfig:
        self._store._lock.acquire()
        self._config = GatewayConfig.from_dict(self._store._config.to_dict())
        return self._config

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        try:
            if exc_type is None and self._config is not None:
                self._config.revision = self._store._config.revision + 1
                self._store._persist(self._config)
                self._store._config = self._config
                self._store._snapshot = GatewayConfig.from_dict(self._config.to_dict())
        finally:
            self._store._lock.release()
        return False


def default_provider_ids() -> tuple[str, ...]:
    return (
        "anthropic",
        "chatgpt_codex",
        "opencode_console",
        "opencode_go",
        # Local servers. They are registered like every other provider, stay
        # ``not_configured`` until the owner saves an endpoint, and are never
        # called at boot.
        "llama_cpp",
        "lm_studio",
        "ollama",
        "openai_api",
    )


def provider_kind(provider_id: str) -> str:
    return provider_id


def iter_enabled_routes(config: GatewayConfig) -> Iterable[tuple[str, RouteRef]]:
    for alias in config.published_aliases():
        for route in alias.all_routes():
            yield alias.name, route
