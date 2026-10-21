"""The routing core.

This module contains **no provider-specific branch**. A provider is an
adapter behind a seam; the router only knows about aliases, routes,
capabilities, error classes, and the rules for when a fallback is allowed.

The rules, in the order they matter:

1. **Eligibility first.** A route that is disabled, unconfigured, in
   cooldown, known-exhausted, or capability-incompatible is not eligible.
2. **Fail closed.** If no eligible route remains, the error names the
   routes that were excluded and why. The gateway never silently serves a
   different model.
3. **The hard stops.** The gateway never switches provider after streamed
   output has begun, after a request with tools or other side effects may
   have executed, after ambiguous delivery, or for an authentication,
   authorisation, policy or malformed-request failure unless the owner has
   explicitly defined a safe policy for that class.
4. **Cross-provider fallback is opt-in twice.** The alias must have
   fallback authorised *and* the specific fallback provider must appear in
   the alias's confirmed-provider list.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Iterator

from gateway.adapters.base import Adapter, AdapterError, ProviderRequest, ProviderResponse
from gateway.bridge.model import ConversionError
from gateway.bridge.requests import convert_request
from gateway.config import (
    ERROR_AUTH,
    ERROR_EXHAUSTED,
    ERROR_MALFORMED,
    ERROR_POLICY,
    ERROR_RATE_LIMITED,
    ERROR_TRANSIENT,
    ERROR_UNKNOWN,
    Alias,
    ConfigStore,
    RouteRef,
    TRIGGER_EXHAUSTED,
    TRIGGER_EXPLICIT_COOLDOWN,
    TRIGGER_RATE_LIMITED,
    TRIGGER_UPSTREAM_UNAVAILABLE,
)
from gateway.core.capabilities import (
    DIALECT_OPENAI_CHAT,
    DIALECT_OPENAI_RESPONSES,
    Capability,
)
from gateway.observability import RequestEvent, utc_now_iso
from gateway.session import derive_session

#: Maps an error class onto the fallback trigger it satisfies.
TRIGGER_FOR_ERROR = {
    ERROR_EXHAUSTED: TRIGGER_EXHAUSTED,
    ERROR_RATE_LIMITED: TRIGGER_RATE_LIMITED,
    ERROR_TRANSIENT: TRIGGER_UPSTREAM_UNAVAILABLE,
}

#: Error classes that never trigger a fallback unless the owner explicitly
#: defines a safe policy for that class.
OWNER_POLICY_CLASSES = (ERROR_AUTH, ERROR_POLICY, ERROR_MALFORMED)


def _requested_reasoning_effort(payload: dict[str, Any]) -> str | None:
    """Return an enabled reasoning effort, never treating an explicit disable as a capability request."""
    effort = payload.get("reasoning_effort")
    reasoning = payload.get("reasoning")
    if isinstance(reasoning, dict):
        if reasoning.get("enabled") is False:
            return None
        if effort is None:
            effort = reasoning.get("effort")
    return effort if isinstance(effort, str) and effort != "none" else None


class RoutingError(Exception):
    """A routing failure that the caller turns into an HTTP response."""

    def __init__(self, message: str, code: str = "routing_error", status: int = 400, detail: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status
        self.detail = detail

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"error": {"message": self.message, "type": self.code}}
        if self.detail is not None:
            payload["error"]["detail"] = self.detail
        return payload


@dataclass
class CooldownState:
    """Local cooldown timers.

    A cooldown is a **local retry policy**, never evidence that the provider
    reset. There are no background probes.
    """

    _until: dict[str, float] = field(default_factory=dict)
    #: Why each cooldown was set, so a skip can say so.
    _reasons: dict[str, str] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def set(self, provider_id: str, seconds: int, reason: str | None = None) -> float:
        until = time.monotonic() + max(0, seconds)
        with self._lock:
            self._until[provider_id] = until
            self._reasons[provider_id] = reason or ""
        return until

    def active(self, provider_id: str) -> bool:
        with self._lock:
            until = self._until.get(provider_id)
        return until is not None and until > time.monotonic()

    def remaining(self, provider_id: str) -> int:
        with self._lock:
            until = self._until.get(provider_id)
        if until is None:
            return 0
        return max(0, int(until - time.monotonic()))

    def reason(self, provider_id: str) -> str:
        """Why the cooldown was set, if it was set for a stated reason."""
        with self._lock:
            return self._reasons.get(provider_id, "")

    def clear(self, provider_id: str | None = None) -> None:
        with self._lock:
            if provider_id is None:
                self._until.clear()
                self._reasons.clear()
            else:
                self._until.pop(provider_id, None)
                self._reasons.pop(provider_id, None)

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            now = time.monotonic()
            return {key: max(0, int(value - now)) for key, value in self._until.items() if value > now}

    def details(self) -> dict[str, dict[str, object]]:
        """The remaining time and the reason for each cooldown."""
        with self._lock:
            now = time.monotonic()
            return {
                key: {"remaining_seconds": max(0, int(value - now)), "reason": self._reasons.get(key, "")}
                for key, value in self._until.items()
                if value > now
            }


@dataclass
class Candidate:
    """One route considered for a request."""

    route: RouteRef
    adapter: Adapter | None = None
    dialect: str = ""
    capability: Capability | None = None
    excluded_reason: str | None = None
    #: Set when the route can serve the request but something the client
    #: asked for has to be dropped to do it. It is never silent: the
    #: substitution is recorded and reported.
    degraded_reason: str | None = None

    @property
    def eligible(self) -> bool:
        return self.excluded_reason is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.route.provider,
            "model": self.route.model,
            "label": self.route.label(),
            "dialect": self.dialect,
            "eligible": self.eligible,
            "excluded_reason": self.excluded_reason,
            "degraded_reason": self.degraded_reason,
        }


@dataclass
class Attribution:
    """Which provider and model actually served a request."""

    requested_alias: str
    serving_provider: str | None = None
    serving_model: str | None = None
    fallback_used: bool = False
    fallback_reason: str | None = None
    attempts: list[dict[str, Any]] = field(default_factory=list)
    #: Routes that were not eligible, with the reason. A request served by a
    #: route other than the primary must say why the primary was skipped,
    #: otherwise it looks like an unexplained fallback.
    skipped: list[dict[str, Any]] = field(default_factory=list)
    #: What was dropped to serve the request on this route, and why.
    degradations: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "requested_alias": self.requested_alias,
            "serving_provider": self.serving_provider,
            "serving_model": self.serving_model,
            "fallback_used": self.fallback_used,
            "fallback_reason": self.fallback_reason,
            "attempts": self.attempts,
            "skipped": self.skipped,
            "degradations": self.degradations,
        }


@dataclass
class RoutedResponse:
    response: ProviderResponse
    attribution: Attribution
    request: ProviderRequest


class Router:
    """Resolves an alias to a provider and enforces the fallback policy."""

    def __init__(
        self,
        config: ConfigStore,
        registry,
        ledger=None,
        cooldown: CooldownState | None = None,
    ) -> None:
        self._config = config
        self._registry = registry
        self._ledger = ledger
        self._cooldown = cooldown or CooldownState()

    @property
    def cooldown(self) -> CooldownState:
        return self._cooldown

    # --- candidate selection --------------------------------------------

    def _model_entry(self, config, route: RouteRef):
        provider = config.providers.get(route.provider)
        if provider is None:
            return None
        return provider.models.get(route.model)

    def _capability_for(self, entry) -> Capability:
        if entry is None:
            return Capability(dialect="")
        return Capability(
            dialect=entry.dialect or "",
            supports_streaming=entry.supports_streaming,
            supports_function_tools=entry.supports_function_tools,
            supports_builtin_tools=entry.supports_builtin_tools,
            reasoning_efforts=tuple(entry.reasoning_efforts),
            supports_image_input=entry.supports_image_input,
            context_window=entry.context_window,
            retention_class=entry.retention_class,
            notes=entry.notes,
        )

    def candidates(
        self,
        alias: Alias,
        source_dialect: str = DIALECT_OPENAI_CHAT,
        stream: bool = False,
        needs_tools: bool = False,
        reasoning_effort: str | None = None,
    ) -> list[Candidate]:
        """Build the ordered candidate list for an alias."""
        config = self._config.snapshot()
        candidates: list[Candidate] = []

        for route in alias.all_routes():
            candidate = Candidate(route=route)
            provider = config.providers.get(route.provider)
            if provider is None:
                candidate.excluded_reason = "provider is not configured"
                candidates.append(candidate)
                continue
            if not provider.enabled_models():
                candidate.excluded_reason = "no model at this provider is enabled"
                candidates.append(candidate)
                continue
            entry = self._model_entry(config, route)
            if entry is None:
                candidate.excluded_reason = "model is not known at this provider"
                candidates.append(candidate)
                continue
            if not entry.enabled:
                candidate.excluded_reason = "model is not enabled for exposure"
                candidates.append(candidate)
                continue
            if self._cooldown.active(route.provider):
                reason = self._cooldown.reason(route.provider)
                suffix = f" ({reason})" if reason else ""
                candidate.excluded_reason = (
                    f"provider is in a local cooldown for {self._cooldown.remaining(route.provider)}s{suffix}"
                )
                candidates.append(candidate)
                continue

            dialect = entry.dialect or ""
            candidate.dialect = dialect
            capability = self._capability_for(entry)
            candidate.capability = capability
            candidate.adapter = self._registry.get(route.provider)

            if not dialect:
                candidate.excluded_reason = "the wire dialect for this model is not documented"
                candidates.append(candidate)
                continue
            if candidate.adapter is None:
                candidate.excluded_reason = "no adapter is registered for this provider"
                candidates.append(candidate)
                continue
            if dialect != source_dialect and not capability.supports_conversion_to(source_dialect):
                candidate.excluded_reason = (
                    f"this model is served over the {dialect} dialect, which the gateway does not translate"
                )
                candidates.append(candidate)
                continue
            if stream and not capability.supports_streaming:
                candidate.excluded_reason = "this model does not support streaming"
                candidates.append(candidate)
                continue
            if needs_tools and not capability.supports_function_tools:
                candidate.excluded_reason = "this model does not support function tools"
                candidates.append(candidate)
                continue
            if reasoning_effort and not capability.supports_effort(reasoning_effort):
                # A reasoning effort is a *preference*, not a capability: the
                # model can still answer without it. Excluding the route
                # would fail the whole request when a usable route exists -
                # and most OpenCode Go models publish no reasoning levels at
                # all, so that would exclude every one of them from any
                # reasoning request. The effort is dropped for this route
                # and the substitution is recorded, never silent.
                candidate.degraded_reason = (
                    f"the requested reasoning effort '{reasoning_effort}' is not published for "
                    "this model; it will be dropped for this route"
                )

            candidates.append(candidate)

        return candidates

    def resolve_alias(self, alias_name: str) -> Alias:
        config = self._config.snapshot()
        alias = config.aliases.get(alias_name)
        if alias is None:
            raise RoutingError(
                f"alias '{alias_name}' is not published",
                code="unknown_alias",
                status=404,
            )
        if not alias.enabled:
            raise RoutingError(
                f"alias '{alias_name}' is not enabled",
                code="alias_disabled",
                status=404,
            )
        if alias.primary is None:
            raise RoutingError(
                f"alias '{alias_name}' has no primary route",
                code="alias_incomplete",
                status=409,
            )
        return alias

    # --- invocation ------------------------------------------------------

    def _fallback_permitted(
        self,
        alias: Alias,
        failed_class: str,
        next_candidate: Candidate,
        side_effects_possible: bool,
    ) -> tuple[bool, str]:
        """Decide whether the next candidate may be tried.

        Returns ``(allowed, reason)``. The reason explains a refusal.
        """
        if side_effects_possible:
            return False, "the request carried tools, so a provider switch could repeat a side effect"
        if not alias.fallback_authorized:
            return False, "cross-provider fallback is not authorised for this alias"
        if next_candidate.route.provider not in alias.fallback_confirmed_providers:
            return False, (
                f"fallback to provider '{next_candidate.route.provider}' has not been confirmed by the owner"
            )

        trigger = TRIGGER_FOR_ERROR.get(failed_class)
        if trigger is None:
            if failed_class in OWNER_POLICY_CLASSES:
                if failed_class not in alias.fallback_error_classes:
                    return False, (
                        f"the owner has not defined a safe fallback policy for '{failed_class}' failures"
                    )
                return True, f"the owner defined a safe fallback policy for '{failed_class}' failures"
            return False, f"'{failed_class}' failures never trigger a fallback"

        if trigger not in alias.fallback_triggers:
            return False, f"the '{trigger}' trigger is not enabled for this alias"
        return True, f"the '{trigger}' trigger is enabled for this alias"

    def invoke(
        self,
        alias_name: str,
        payload: dict[str, Any],
        source_dialect: str = DIALECT_OPENAI_CHAT,
        stream: bool = False,
        request_id: str = "",
        session_hint: str | None = None,
        profile_id: str | None = None,
        client_ip: str | None = None,
    ) -> RoutedResponse:
        """Resolve an alias and send the request, applying the fallback policy."""
        alias = self.resolve_alias(alias_name)

        needs_tools = bool(payload.get("tools"))
        reasoning_effort = _requested_reasoning_effort(payload)

        candidates = self.candidates(
            alias,
            source_dialect=source_dialect,
            stream=stream,
            needs_tools=needs_tools,
            reasoning_effort=reasoning_effort if isinstance(reasoning_effort, str) else None,
        )
        if not candidates:
            raise RoutingError(f"alias '{alias_name}' has no routes", code="no_routes", status=409)

        eligible = [c for c in candidates if c.eligible]
        if not eligible:
            detail = [c.to_dict() for c in candidates]
            raise RoutingError(
                f"no eligible route for alias '{alias_name}'",
                code="no_eligible_route",
                status=503,
                detail=detail,
            )

        attribution = Attribution(requested_alias=alias_name)
        # A route that was not eligible is recorded, so the record explains
        # why the request was not served by the primary.
        for candidate in candidates:
            if not candidate.eligible:
                attribution.skipped.append(
                    {
                        "provider": candidate.route.provider,
                        "model": candidate.route.model,
                        "reason": candidate.excluded_reason,
                    }
                )
        if eligible and eligible[0].route != (alias.primary or eligible[0].route):
            # The primary was skipped, so a later route served the request.
            skipped_primary = next(
                (entry for entry in attribution.skipped if alias.primary is not None
                 and entry["provider"] == alias.primary.provider
                 and entry["model"] == alias.primary.model),
                None,
            )
            reason = (
                f"the primary route was skipped: {skipped_primary['reason']}"
                if skipped_primary
                else "the primary route was not eligible"
            )
            attribution.fallback_used = True
            attribution.fallback_reason = reason
        last_error: AdapterError | None = None
        started = time.monotonic()
        # The upstream conversation identifier is derived here, so that every
        # path that reaches a provider has one - the API surface, the
        # console's alias test, and any future caller. OpenCode Go refuses a
        # request without it.
        conversation = derive_session(profile_id, alias_name, client_ip, session_hint)

        for index, candidate in enumerate(eligible):
            is_last = index == len(eligible) - 1
            try:
                upstream_payload = convert_request(payload, source_dialect, candidate.dialect)
            except ConversionError as exc:
                last_error = AdapterError(
                    ERROR_POLICY,
                    exc.message,
                    provider_id=candidate.route.provider,
                )
                attribution.attempts.append(
                    {
                        "provider": candidate.route.provider,
                        "model": candidate.route.model,
                        "outcome": "conversion_refused",
                        "reason": exc.message,
                    }
                )
                if is_last:
                    break
                allowed, reason = self._fallback_permitted(
                    alias, ERROR_POLICY, eligible[index + 1], needs_tools
                )
                if not allowed:
                    last_error = AdapterError(ERROR_POLICY, f"{last_error.message} ({reason})", provider_id=candidate.route.provider)
                    break
                attribution.fallback_reason = reason
                attribution.fallback_used = True
                continue

            # The client asked for an *alias*. The provider only knows its
            # own model ids, so the payload must carry the upstream model
            # id, never the alias. Sending the alias upstream makes the
            # provider answer "model is unavailable".
            upstream_payload = dict(upstream_payload)
            upstream_payload["model"] = candidate.route.model

            degradation: dict[str, Any] | None = None
            if candidate.degraded_reason:
                # Drop what this route cannot honour, and say so.
                upstream_payload.pop("reasoning_effort", None)
                reasoning = upstream_payload.get("reasoning")
                if isinstance(reasoning, dict):
                    upstream_payload["reasoning"] = {
                        key: value for key, value in reasoning.items() if key != "effort"
                    }
                    if not upstream_payload["reasoning"]:
                        upstream_payload.pop("reasoning")
                degradation = {
                    "provider": candidate.route.provider,
                    "model": candidate.route.model,
                    "reason": candidate.degraded_reason,
                }

            request = ProviderRequest(
                model=candidate.route.model,
                payload=upstream_payload,
                stream=stream,
                alias=alias_name,
                request_id=request_id,
                # The upstream session identifier is namespaced by provider, so
                # two providers never share a conversation key and a fallback
                # does not inherit the primary's session.
                session_id=f"{candidate.route.provider}.{conversation}",
                dialect=candidate.dialect,
                client_ip=client_ip,
            )
            if needs_tools:
                request.with_side_effects_possible()

            try:
                response = candidate.adapter.invoke(request)  # type: ignore[union-attr]
            except AdapterError as exc:
                last_error = exc
                attribution.attempts.append(
                    {
                        "provider": candidate.route.provider,
                        "model": candidate.route.model,
                        "outcome": "error",
                        "error_class": exc.error_class,
                        "reason": exc.message,
                    }
                )
                self._maybe_cooldown(alias, candidate, exc)

                if is_last:
                    break
                allowed, reason = self._fallback_permitted(
                    alias, exc.error_class, eligible[index + 1], needs_tools
                )
                if not allowed:
                    exc.message = f"{exc.message} (fallback not used: {reason})"
                    break
                attribution.fallback_reason = reason
                attribution.fallback_used = True
                continue

            attribution.serving_provider = candidate.route.provider
            attribution.serving_model = candidate.route.model
            if degradation is not None:
                # Only the route that actually served the request records a
                # substitution. A route that failed did not serve anything.
                attribution.degradations.append(degradation)
            self._record(
                request_id=request_id,
                alias_name=alias_name,
                payload=payload,
                attribution=attribution,
                status=response.status,
                error_class=None,
                latency_ms=int((time.monotonic() - started) * 1000),
                stream=stream,
                profile_id=profile_id,
            )
            return RoutedResponse(response=response, attribution=attribution, request=request)

        # Every eligible route failed.
        self._record(
            request_id=request_id,
            alias_name=alias_name,
            payload=payload,
            attribution=attribution,
            status=502 if last_error is not None else 503,
            error_class=last_error.error_class if last_error is not None else "unknown",
            latency_ms=int((time.monotonic() - started) * 1000),
            stream=stream,
            profile_id=profile_id,
        )
        if last_error is None:
            raise RoutingError(
                f"no eligible route for alias '{alias_name}'",
                code="no_eligible_route",
                status=503,
            )
        # A provider failure is an upstream failure. It is reported as 502
        # even when the provider rejected its own credential, because the
        # caller's credential was not what was rejected.
        raise RoutingError(
            last_error.message,
            code=last_error.error_class,
            status=502,
            detail={"attempts": attribution.attempts},
        )

    def _record(
        self,
        request_id: str,
        alias_name: str,
        payload: dict[str, Any],
        attribution: Attribution,
        status: int | None,
        error_class: str | None,
        latency_ms: int,
        stream: bool,
        profile_id: str | None,
    ) -> None:
        """Record one request outcome.

        Every terminal path is recorded - success, provider failure, and
        failed fallback - with a real timestamp and a measured latency. The
        record is metadata only: the alias, the provider that served or
        failed, the outcome and the duration. Never the request or the
        response.
        """
        if self._ledger is None:
            return
        self._ledger.record(
            RequestEvent(
                request_id=request_id,
                timestamp=utc_now_iso(),
                requested_alias=alias_name,
                requested_model=payload.get("model") if isinstance(payload, dict) else None,
                serving_provider=attribution.serving_provider,
                serving_model=attribution.serving_model,
                status=status,
                error_class=error_class,
                latency_ms=latency_ms,
                fallback_reason=attribution.fallback_reason,
                fallback_used=attribution.fallback_used,
                streamed=stream,
                profile_id=profile_id,
                skipped=attribution.skipped,
                attempts=attribution.attempts,
                degradations=attribution.degradations,
            )
        )

    def _maybe_cooldown(self, alias: Alias, candidate: Candidate, error: AdapterError) -> None:
        """Apply the local cooldown policy after a qualifying failure.

        The provider's own ``Retry-After`` is honoured when it asks for
        **longer** than the local policy. Waiting too little is the worse
        error, so the window is the longer of the two - and the reason is
        recorded, so the skip says which one it was.
        """
        trigger = TRIGGER_FOR_ERROR.get(error.error_class)
        if trigger is None:
            return
        if trigger not in alias.fallback_triggers:
            return
        policy = alias.cooldown_seconds
        asked = error.retry_after
        if policy > 0:
            seconds = policy
            reason = f"the local cooldown policy for this alias is {policy}s"
        elif asked:
            seconds = asked
            reason = f"the provider asked us to wait {asked}s"
        else:
            return
        if asked and asked > seconds:
            seconds = asked
            reason = f"the provider asked us to wait {asked}s"
        self._cooldown.set(candidate.route.provider, seconds, reason)

    # --- helpers for the console -----------------------------------------

    def published_models(self) -> list[dict[str, Any]]:
        """The published alias catalogue, as ``/v1/models`` renders it."""
        config = self._config.snapshot()
        return [{"id": alias.name} for alias in sorted(config.published_aliases(), key=lambda a: a.name)]
