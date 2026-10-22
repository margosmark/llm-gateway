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

import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from gateway.stores import atomic_write_json, read_json

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

# Fallback triggers. The owner selects these explicitly per alias.
TRIGGER_EXHAUSTED = "exhausted"
TRIGGER_RATE_LIMITED = "rate_limited"
TRIGGER_UPSTREAM_UNAVAILABLE = "upstream_unavailable"
TRIGGER_EXPLICIT_COOLDOWN = "explicit_cooldown"
FALLBACK_TRIGGERS = (
    TRIGGER_EXHAUSTED,
    TRIGGER_RATE_LIMITED,
    TRIGGER_UPSTREAM_UNAVAILABLE,
    TRIGGER_EXPLICIT_COOLDOWN,
)

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
    retention_class: str = "unknown"
    monthly_limit_usd: float | None = None
    limits_source: str = "unknown"
    limits_observed_at: str | None = None

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

        efforts = data.get("reasoning_efforts")
        return cls(
            model_id=model_id,
            enabled=bool(data.get("enabled", False)),
            dialect=as_str("dialect"),
            source=str(data.get("source") or "manual"),
            observed_at=as_str("observed_at"),
            limits_known=bool(data.get("limits_known", False)),
            notes=as_str("notes"),
            reasoning_efforts=[e for e in efforts or [] if isinstance(e, str)],
            supports_streaming=bool(data.get("supports_streaming", True)),
            supports_function_tools=bool(data.get("supports_function_tools", True)),
            supports_builtin_tools=bool(data.get("supports_builtin_tools", False)),
            supports_image_input=bool(data.get("supports_image_input", False)),
            context_window=as_int("context_window"),
            context_limit=as_int("context_limit"),
            retention_class=str(data.get("retention_class") or "unknown"),
            monthly_limit_usd=as_float("monthly_limit_usd"),
            limits_source=str(data.get("limits_source") or "unknown"),
            limits_observed_at=as_str("limits_observed_at"),
        )


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

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "kind": self.kind,
            "state": self.state,
            "last_test_at": self.last_test_at,
            "last_error": self.last_error,
            "catalog_observed_at": self.catalog_observed_at,
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
        )

    def enabled_models(self) -> list[ModelEntry]:
        return [entry for entry in self.models.values() if entry.enabled]


@dataclass
class Alias:
    """A friendly name that resolves to a primary route and ordered fallbacks."""

    name: str
    enabled: bool = False
    primary: RouteRef | None = None
    fallbacks: list[RouteRef] = field(default_factory=list)
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

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "enabled": self.enabled,
            "primary": self.primary.to_dict() if self.primary else None,
            "fallbacks": [ref.to_dict() for ref in self.fallbacks],
            "fallback_triggers": list(self.fallback_triggers),
            "fallback_authorized": self.fallback_authorized,
            "fallback_confirmed_providers": list(self.fallback_confirmed_providers),
            "fallback_error_classes": list(self.fallback_error_classes),
            "cooldown_seconds": self.cooldown_seconds,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "Alias | None":
        if not isinstance(data, dict):
            return None
        name = data.get("name")
        if not isinstance(name, str) or not name:
            return None
        primary = RouteRef.from_dict(data.get("primary"))
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
        return cls(
            name=name,
            enabled=bool(data.get("enabled", False)),
            primary=primary,
            fallbacks=fallbacks,
            fallback_triggers=triggers,
            fallback_authorized=bool(data.get("fallback_authorized", False)),
            fallback_confirmed_providers=confirmed,
            fallback_error_classes=safe_classes,
            cooldown_seconds=cooldown if isinstance(cooldown, int) and cooldown >= 0 else 300,
        )

    def all_routes(self) -> list[RouteRef]:
        routes: list[RouteRef] = []
        if self.primary is not None:
            routes.append(self.primary)
        routes.extend(self.fallbacks)
        return routes


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
    providers: dict[str, ProviderState] = field(default_factory=dict)
    aliases: dict[str, Alias] = field(default_factory=dict)
    profiles: dict[str, Profile] = field(default_factory=dict)
    settings: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "initialized": self.initialized,
            "access_mode": self.access_mode,
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
        return cls(
            revision=revision if isinstance(revision, int) and revision >= 0 else 0,
            initialized=bool(data.get("initialized", False)),
            access_mode=access_mode,
            providers=providers,
            aliases=aliases,
            profiles=profiles,
            settings=dict(settings) if isinstance(settings, dict) else {},
        )

    def published_aliases(self) -> list[Alias]:
        """Aliases that appear in ``/v1/models``."""
        return [alias for alias in self.aliases.values() if alias.enabled and alias.primary is not None]


class ConfigStore:
    """Thread-safe, atomically persisted configuration."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.RLock()
        self._config = GatewayConfig.from_dict(read_json(path, {}))

    @property
    def path(self) -> Path:
        return self._path

    def snapshot(self) -> GatewayConfig:
        """Return the current configuration. Makes no provider calls."""
        with self._lock:
            return GatewayConfig.from_dict(self._config.to_dict())

    def mutate(self) -> "_ConfigTransaction":
        """Open a transaction. The revision is bumped and persisted on exit."""
        return _ConfigTransaction(self)

    def replace(self, config: GatewayConfig) -> None:
        with self._lock:
            self._config = config
            self._persist()

    def _persist(self) -> None:
        atomic_write_json(self._path, self._config.to_dict(), mode=0o600)


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
                self._store._config = self._config
                self._store._persist()
        finally:
            self._store._lock.release()
        return False


def default_provider_ids() -> tuple[str, ...]:
    return ("chatgpt_codex", "opencode_go")


def provider_kind(provider_id: str) -> str:
    return provider_id


def iter_enabled_routes(config: GatewayConfig) -> Iterable[tuple[str, RouteRef]]:
    for alias in config.published_aliases():
        for route in alias.all_routes():
            yield alias.name, route
