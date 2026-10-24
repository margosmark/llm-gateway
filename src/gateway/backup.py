"""Sanitised configuration backup and strict import validation."""
from __future__ import annotations

import re
from typing import Any

from gateway.config import Alias, GatewayConfig, Profile, ProviderState
from gateway.identifiers import ValidationError, validate_alias_name, validate_model_id, validate_profile_id
from gateway.routes import validate_alias_routes

_SECRET_KEY = re.compile(r"credential|secret|password|token|hash|device.?code|account.?id|authorization", re.I)
SNAPSHOT_INTERVAL_KEY = "snapshot_interval_seconds"


def snapshot_interval_seconds(config: GatewayConfig) -> int:
    value = config.settings.get(SNAPSHOT_INTERVAL_KEY, 0)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _strip_secrets(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _strip_secrets(item) for key, item in value.items()
                if isinstance(key, str) and not _SECRET_KEY.search(key)}
    if isinstance(value, list):
        return [_strip_secrets(item) for item in value]
    return value


def export_config(config: GatewayConfig) -> dict[str, Any]:
    """Return a data-only sanitized export; exclude free-form and secret-adjacent fields."""
    payload = config.to_dict()
    payload["providers"] = {
        key: {**provider, "last_error": None,
              "models": {model_id: {k: v for k, v in model.items() if k != "notes"}
                         for model_id, model in provider["models"].items()}}
        for key, provider in payload["providers"].items()
    }
    payload["profiles"] = {
        key: {k: v for k, v in profile.items()
              if k not in ("has_token", "token_created_at", "token_rotated_at", "routing_policy")}
        for key, profile in payload["profiles"].items()
    }
    # Settings are extensible; export only the snapshot preference, not arbitrary values.
    payload["settings"] = {SNAPSHOT_INTERVAL_KEY: snapshot_interval_seconds(config)}
    return {"format": "personal-llm-gateway-config", "version": 1,
            "config": _strip_secrets(payload)}


class _CandidateStore:
    def __init__(self, candidate: GatewayConfig):
        self.candidate = candidate

    def snapshot(self) -> GatewayConfig:
        return self.candidate


def import_config(payload: Any, current: GatewayConfig, registry: Any) -> GatewayConfig:
    """Parse and fully validate an export before returning a replacement config."""
    if not isinstance(payload, dict) or payload.get("format") != "personal-llm-gateway-config" or payload.get("version") != 1:
        raise ValidationError("unsupported configuration export format", "format")
    raw = payload.get("config")
    if not isinstance(raw, dict):
        raise ValidationError("config must be an object", "config")
    if _contains_secret_key(raw):
        raise ValidationError("secret-shaped fields are not accepted in an import", "config")
    if not isinstance(raw.get("providers"), dict) or not isinstance(raw.get("aliases"), dict) or not isinstance(raw.get("profiles"), dict):
        raise ValidationError("providers, aliases, and profiles must be objects", "config")

    # Reject unknown/unregistered provider records and malformed entries rather than
    # allowing permissive from_dict() to silently drop them.
    providers: dict[str, ProviderState] = {}
    for key, value in raw["providers"].items():
        if not isinstance(key, str) or registry.get(key) is None or not isinstance(value, dict):
            raise ValidationError(f"unknown or malformed provider: {key}", "providers")
        if not isinstance(value.get("models"), dict):
            raise ValidationError(f"provider models must be an object: {key}", "providers")
        for model_key, entry in value["models"].items():
            if not isinstance(model_key, str) or not isinstance(entry, dict) or entry.get("model_id") != model_key:
                raise ValidationError("model key and model_id must match", "models")
            validate_model_id(model_key)
        provider = ProviderState.from_dict(value)
        if provider is None:
            raise ValidationError(f"invalid provider record: {key}", "providers")
        model_records = value["models"]
        if len(provider.models) != len(model_records):
            raise ValidationError("invalid model entries", "models")
        for model_record in model_records.values():
            if not isinstance(model_record.get("enabled", False), bool):
                raise ValidationError("model enabled must be boolean", "enabled")
            if model_record.get("dialect") not in (None, "openai_responses", "openai_chat", "anthropic_messages", "google_v1beta", "systemone"):
                raise ValidationError("unsupported model dialect", "dialect")
        provider = ProviderState.from_dict(value)
        if provider is None or provider.provider_id != key:
            raise ValidationError(f"invalid provider record: {key}", "providers")
        for model_key, model in provider.models.items():
            if model_key != model.model_id:
                raise ValidationError("model key must match model_id", "models")
            validate_model_id(model.model_id)
        providers[key] = provider

    candidate = GatewayConfig.from_dict(raw)
    candidate.providers = providers
    candidate_store = _CandidateStore(candidate)
    aliases: dict[str, Alias] = {}
    if not all(isinstance(value, dict) for value in raw["aliases"].values()):
        raise ValidationError("aliases must be objects", "aliases")
    for key, value in raw["aliases"].items():
        name = validate_alias_name(key)
        if not isinstance(value, dict) or value.get("name") != name:
            raise ValidationError("alias key and name must match", "aliases")
        for field, expected in (("enabled", bool), ("fallback_authorized", bool)):
            if field in value and not isinstance(value[field], expected):
                raise ValidationError(f"{field} must be boolean", field)
        for field in ("fallbacks", "fallback_triggers", "fallback_confirmed_providers", "fallback_error_classes"):
            if field in value and not isinstance(value[field], list):
                raise ValidationError(f"{field} must be a list", field)
        if any(not isinstance(item, str) for field in ("fallback_triggers", "fallback_confirmed_providers", "fallback_error_classes") for item in value.get(field, [])):
            raise ValidationError("fallback settings must contain strings", "fallbacks")
        if any(item not in ("exhausted", "rate_limited", "upstream_unavailable", "explicit_cooldown") for item in value.get("fallback_triggers", [])):
            raise ValidationError("invalid fallback trigger", "fallback_triggers")
        if any(item not in ("auth", "policy", "malformed") for item in value.get("fallback_error_classes", [])):
            raise ValidationError("invalid fallback error class", "fallback_error_classes")
        cooldown = value.get("cooldown_seconds", 300)
        if not isinstance(cooldown, int) or isinstance(cooldown, bool) or cooldown < 0:
            raise ValidationError("cooldown_seconds must be a non-negative integer", "cooldown_seconds")
        alias = Alias.from_dict(value)
        if alias is None:
            raise ValidationError(f"invalid alias: {name}", "aliases")
        primary, fallbacks = validate_alias_routes(candidate_store, registry, value.get("primary"), value.get("fallbacks", []))
        alias.primary, alias.fallbacks = primary, fallbacks
        if alias.enabled and primary is None:
            raise ValidationError("an enabled alias requires a primary route", "primary")
        if not set(alias.fallback_confirmed_providers).issubset({ref.provider for ref in fallbacks}):
            raise ValidationError("fallback confirmations must name configured fallback providers", "fallback_confirmed_providers")
        if any(provider not in registry.provider_ids() for provider in alias.fallback_confirmed_providers):
            raise ValidationError("fallback confirmation names an unknown provider", "fallback_confirmed_providers")
        alias.fallback_authorized = value.get("fallback_authorized", False)
        alias.cooldown_seconds = cooldown
        alias.fallback_triggers = list(value.get("fallback_triggers", []))
        alias.fallback_confirmed_providers = list(value.get("fallback_confirmed_providers", []))
        alias.fallback_error_classes = list(value.get("fallback_error_classes", []))
        if alias.fallback_authorized and not fallbacks:
            raise ValidationError("fallback authorization requires a fallback", "fallback_authorized")
        aliases[name] = alias
    candidate.aliases = aliases

    profiles: dict[str, Profile] = {}
    for key, value in raw["profiles"].items():
        name = validate_profile_id(key)
        if not isinstance(value, dict) or value.get("profile_id") != name:
            raise ValidationError("profile key and profile_id must match", "profiles")
        profile = Profile.from_dict(value)
        if profile is None:
            raise ValidationError(f"invalid profile: {name}", "profiles")
        for alias_name in profile.allowed_aliases:
            validate_alias_name(alias_name)
        profile.has_token = False
        profile.token_created_at = None
        profile.token_rotated_at = None
        profiles[name] = profile
    candidate.profiles = profiles
    settings = raw.get("settings", {})
    if not isinstance(settings, dict) or set(settings) - {SNAPSHOT_INTERVAL_KEY}:
        raise ValidationError("unsupported settings in configuration import", "settings")
    if SNAPSHOT_INTERVAL_KEY in settings:
        interval = settings[SNAPSHOT_INTERVAL_KEY]
        if not isinstance(interval, int) or isinstance(interval, bool) or interval not in (0, 3600, 21600, 86400):
            raise ValidationError("invalid snapshot interval", "settings")
    candidate.settings = dict(settings)
    revision = raw.get("revision", 0)
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
        raise ValidationError("revision must be a non-negative integer", "revision")
    candidate.revision = revision
    return candidate


def _contains_secret_key(value: Any) -> bool:
    if isinstance(value, dict):
        return any(_SECRET_KEY.search(str(key)) or _contains_secret_key(item) for key, item in value.items())
    if isinstance(value, list):
        return any(_contains_secret_key(item) for item in value)
    return False
