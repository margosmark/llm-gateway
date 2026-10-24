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

import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Iterator

from gateway.adapters.base import Adapter, AdapterError, CLASS_MESSAGES, ProviderRequest, ProviderResponse
from gateway.bridge.model import ConversionError
from gateway.bridge.requests import convert_request, payload_has_image_input
from gateway.circuits import CircuitStore
from gateway.config import (
    ERROR_AUTH,
    ERROR_EXHAUSTED,
    ERROR_MALFORMED,
    ERROR_POLICY,
    ERROR_RATE_LIMITED,
    ERROR_ROUTE_GONE,
    ERROR_TRANSIENT,
    ERROR_UNKNOWN,
    PHASES,
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

#: Maps an error class onto the fallback trigger it satisfies. A route the
#: upstream no longer serves satisfies the "upstream unavailable" trigger:
#: the route is unavailable, and the owner has said another may serve the
#: request. It is not a policy decision, and it is not a temporary
#: condition, so it does not put the whole provider into cooldown.
TRIGGER_FOR_ERROR = {
    ERROR_EXHAUSTED: TRIGGER_EXHAUSTED,
    ERROR_RATE_LIMITED: TRIGGER_RATE_LIMITED,
    ERROR_TRANSIENT: TRIGGER_UPSTREAM_UNAVAILABLE,
    ERROR_ROUTE_GONE: TRIGGER_UPSTREAM_UNAVAILABLE,
}

#: Error classes that never trigger a fallback unless the owner explicitly
#: defines a safe policy for that class.
OWNER_POLICY_CLASSES = (ERROR_AUTH, ERROR_POLICY, ERROR_MALFORMED)


def _contains_image_input(value: Any) -> bool:
    """Whether a request contains an image part in either supported dialect."""
    if isinstance(value, dict):
        if value.get("type") in ("image_url", "input_image"):
            return True
        return any(_contains_image_input(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_image_input(item) for item in value)
    return False


def _requested_tool_capabilities(payload: dict[str, Any]) -> tuple[bool, bool]:
    """Return whether function and/or provider-hosted tools are requested."""
    tools = payload.get("tools")
    if not isinstance(tools, list):
        return False, False
    functions = any(
        isinstance(tool, dict) and tool.get("type") in (None, "function")
        for tool in tools
    )
    builtins = any(
        isinstance(tool, dict) and tool.get("type") not in (None, "function")
        for tool in tools
    )
    return functions, builtins


def _contains_builtin_tools(payload: dict[str, Any]) -> bool:
    return _requested_tool_capabilities(payload)[1]


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


def _unsupported_payload_fields(adapter: Adapter | None) -> frozenset[str]:
    """Request fields this adapter's upstream rejects with a 400.

    Read off the adapter through the same seam as its ``dialect`` and
    ``provider_id``, so the routing core stays free of provider branches: the
    knowledge of what a given upstream refuses lives on that upstream's own
    adapter, and an adapter that declares nothing gets nothing stripped.
    """
    if adapter is None:
        return frozenset()
    declared = getattr(adapter, "UNSUPPORTED_PAYLOAD_FIELDS", None)
    if not isinstance(declared, (frozenset, set, tuple, list)):
        return frozenset()
    return frozenset(name for name in declared if isinstance(name, str))


class RoutingError(Exception):
    """A routing failure that the caller turns into an HTTP response."""

    def __init__(self, message: str, code: str = "routing_error", status: int = 400, detail: Any = None, retry_after_seconds: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status
        self.detail = detail
        self.retry_after_seconds = retry_after_seconds

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"error": {"message": self.message, "type": self.code}}
        if self.detail is not None:
            payload["error"]["detail"] = self.detail
        if self.retry_after_seconds is not None:
            payload["error"]["retry_after_seconds"] = self.retry_after_seconds
        return payload


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
    circuit_blocked: bool = False

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
    #: Phase-routing metadata, when the alias has an enabled phase policy.
    #: Metadata only: the phase, its source, and the confidence bucket. Never
    #: the request content or the Jev answer distribution.
    phase: str | None = None
    phase_source: str | None = None  # "caller" | "jev" | "default"
    phase_confidence_bucket: str | None = None  # e.g. ">=0.80"
    phase_inference_note: str | None = None

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
            "phase": self.phase,
            "phase_source": self.phase_source,
            "phase_confidence_bucket": self.phase_confidence_bucket,
            "phase_inference_note": self.phase_inference_note,
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
        circuits: CircuitStore | None = None,
    ) -> None:
        self._config = config
        self._registry = registry
        self._ledger = ledger
        #: The open circuits. They are remembered on disk, so a restart
        #: during an outage does not defeat them.
        self._circuits = circuits if circuits is not None else CircuitStore()

    @property
    def circuits(self) -> CircuitStore:
        return self._circuits

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
            supports_previous_response_id=getattr(entry, "supports_previous_response_id", False),
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
        needs_builtin_tools: bool = False,
        needs_image_input: bool = False,
        needs_previous_response_id: bool = False,
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
            allowed, circuit_reason = self._circuits.check(route.provider, route.model)
            if not allowed:
                candidate.excluded_reason = circuit_reason
                candidate.circuit_blocked = True
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
            if needs_builtin_tools and not capability.supports_builtin_tools:
                candidate.excluded_reason = "this model does not support builtin tools"
                candidates.append(candidate)
                continue
            if needs_image_input and not capability.supports_image_input:
                candidate.excluded_reason = "this model does not support image input"
                candidates.append(candidate)
                continue
            if needs_previous_response_id and not capability.supports_previous_response_id:
                candidate.excluded_reason = "this model does not support previous_response_id continuation"
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

    # --- phase routing ---------------------------------------------------

    def _bounded_state(self, payload: dict[str, Any]) -> str:
        """A short, bounded text view of the request for Jev inference.

        This is request-derived content that is sent to the Jev provider
        when — and only when — the owner has enabled inference for the
        alias. It is bounded so a large prompt cannot be forwarded wholesale,
        and it is never logged or recorded.
        """
        pieces: list[str] = []
        messages = payload.get("messages")
        if isinstance(messages, list):
            for message in messages[-4:]:
                if not isinstance(message, dict):
                    continue
                content = message.get("content")
                if isinstance(content, str):
                    pieces.append(content)
                elif isinstance(content, list):
                    for part in content:
                        if isinstance(part, dict) and isinstance(part.get("text"), str):
                            pieces.append(part["text"])
        elif isinstance(payload.get("input"), str):
            pieces.append(payload["input"])
        text = "\n".join(pieces).strip()
        return text[:2000]

    def _infer_phase_jev(self, alias: Alias, payload: dict[str, Any]) -> tuple[str | None, str | None, str]:
        """Ask Jev for a phase, bounded and closed-set.

        Returns ``(phase, confidence_bucket, note)``. ``phase`` is ``None``
        when inference could not produce a confident, known phase — the
        caller then falls back to the ordinary route. The note explains why.
        Never raises: a Jev failure must not fail the completion request.
        """
        inference = alias.phase_routing.inference if alias.phase_routing else None
        if inference is None or not inference.enabled or not inference.model:
            return None, None, "inference is not enabled for this alias"
        adapter = self._registry.get(inference.provider)
        if adapter is None or not hasattr(adapter, "invoke_systemone"):
            return None, None, f"no System One adapter is registered for provider '{inference.provider}'"
        config = self._config.snapshot()
        provider = config.providers.get(inference.provider)
        entry = provider.models.get(inference.model) if provider else None
        if entry is None or not entry.enabled:
            return None, None, f"inference model '{inference.model}' is not enabled"
        state = self._bounded_state(payload)
        if not state:
            return None, None, "the request carried no text to classify"
        question = {
            "type": "choice",
            "instructions": "Choose the phase that best describes this request.",
            "criteria": {
                "plan": "Analysis, design, investigation, or a proposed approach.",
                "implement": "Writing or modifying code or configuration.",
                "validate": "Testing, reviewing, verifying, or troubleshooting existing work.",
            },
        }
        try:
            body = adapter.invoke_systemone(
                {"model": inference.model, "state": state, "questions": {"phase": question}}
            )
            data = json.loads(body.decode("utf-8")) if body else {}
        except Exception:  # noqa: BLE001 - a Jev failure must never fail the completion.
            return None, None, "Jev inference failed; the ordinary route is used"
        answers = data.get("answers") if isinstance(data, dict) else None
        answer = answers.get("phase") if isinstance(answers, dict) else None
        if not isinstance(answer, dict) or answer.get("type") != "choice":
            return None, None, "Jev returned no usable phase answer"
        choice = answer.get("choice")
        confidence = answer.get("confidence")
        bucket = None
        if isinstance(confidence, (int, float)) and not isinstance(confidence, bool):
            bucket = f">={inference.minimum_confidence:.2f}" if confidence >= inference.minimum_confidence else f"<{inference.minimum_confidence:.2f}"
        if choice not in PHASES:
            return None, bucket, "Jev chose an unknown phase; the ordinary route is used"
        if not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or confidence < inference.minimum_confidence:
            return None, bucket, "Jev confidence was below the threshold; the ordinary route is used"
        return choice, bucket, "Jev selected a confident phase"

    def _resolve_phase(
        self,
        alias: Alias,
        payload: dict[str, Any],
        caller_phase: str | None,
        needs_tools: bool,
    ) -> tuple[str | None, str, str | None, str | None]:
        """Resolve the effective phase for a request.

        Returns ``(phase, source, confidence_bucket, note)``. Precedence:
        a valid caller phase wins; otherwise inference runs only when it is
        enabled and the request is safe to classify (no tools, has text).
        Anything uncertain falls back to the ordinary route.
        """
        policy = alias.phase_routing
        if policy is None or not policy.enabled:
            return None, "default", None, None
        if caller_phase is not None and caller_phase in PHASES:
            # The caller was explicit, so inference never overrides it. A
            # phase with no configured route is recorded and the ordinary
            # route is used; it is not silently re-classified.
            if caller_phase in policy.routes:
                return caller_phase, "caller", None, None
            return caller_phase, "caller", None, "no route is configured for this phase; the ordinary route is used"
        if needs_tools:
            return None, "default", None, "phase inference skipped: the request carried tools"
        phase, bucket, note = self._infer_phase_jev(alias, payload)
        if phase is not None and phase in policy.routes:
            return phase, "jev", bucket, note
        return None, "default", bucket, note

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
        caller_phase: str | None = None,
    ) -> RoutedResponse:
        """Resolve an alias and send the request, applying the fallback policy.

        ``caller_phase`` is a validated phase hint (``plan``/``implement``/
        ``validate``) already stripped from the request payload by the
        handler. It is honoured only when the alias has an enabled phase
        policy that maps that phase to a route.
        """
        alias = self.resolve_alias(alias_name)

        needs_tools, needs_builtin_tools = _requested_tool_capabilities(payload)
        needs_image_input = payload_has_image_input(payload, source_dialect)
        needs_previous_response_id = bool(payload.get("previous_response_id"))
        reasoning_effort = _requested_reasoning_effort(payload)

        candidates = self.candidates(
            alias,
            source_dialect=source_dialect,
            stream=stream,
            needs_tools=needs_tools,
            needs_builtin_tools=needs_builtin_tools,
            needs_image_input=needs_image_input,
            needs_previous_response_id=needs_previous_response_id,
            reasoning_effort=reasoning_effort if isinstance(reasoning_effort, str) else None,
        )
        if not candidates:
            raise RoutingError(f"alias '{alias_name}' has no routes", code="no_routes", status=409)

        eligible = [c for c in candidates if c.eligible]
        if not eligible:
            # A capability refusal is a public response, so it must not
            # disclose upstream identifiers: no provider ids, model ids,
            # labels or dialects of the considered routes. The caller gets a
            # generic refusal; the owner sees per-route capability facts in
            # the console catalog and during route validation.
            raise RoutingError(
                f"no eligible route for alias '{alias_name}'",
                code="no_eligible_route",
                status=503,
                retry_after_seconds=self._circuits.retry_after_for(
                    [(c.route.provider, c.route.model) for c in candidates if c.circuit_blocked]
                ),
            )

        # Phase routing is opt-in per alias and resolved before the route
        # loop, so a phase decision never happens after a stream has begun
        # or after a tool may have executed. The selected route is moved to
        # the front of the eligible list; it is not a fallback, so it does
        # not set fallback_used.
        phase, phase_source, phase_bucket, phase_note = self._resolve_phase(
            alias, payload, caller_phase, needs_tools
        )
        phase_selected_route: RouteRef | None = None
        if phase is not None:
            target = alias.phase_routing.routes.get(phase) if alias.phase_routing else None
            if target is not None:
                for index, candidate in enumerate(eligible):
                    if candidate.route == target.route:
                        eligible.insert(0, eligible.pop(index))
                        phase_selected_route = target.route
                        break

        attribution = Attribution(requested_alias=alias_name)
        attribution.phase = phase
        attribution.phase_source = phase_source
        attribution.phase_confidence_bucket = phase_bucket
        attribution.phase_inference_note = phase_note
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
        if (
            eligible
            and eligible[0].route != (alias.primary or eligible[0].route)
            and eligible[0].route != phase_selected_route
        ):
            # The primary was skipped, so a later route served the request.
            # A route chosen by the owner's phase policy is not a fallback:
            # it is the primary for that phase, so it is not reported as one.
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
            degradation_reasons: list[str] = []
            dropped_fields: list[str] = []
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
                degradation_reasons.append(candidate.degraded_reason)

            # An adapter may declare request fields its upstream rejects
            # outright. A caller that sets one is doing nothing unusual -
            # OpenAI's own Chat dialect accepts all of them - so excluding
            # the route would fail a request a usable route can serve. The
            # field is dropped for this route and the substitution is
            # recorded, never silent.
            unsupported = _unsupported_payload_fields(candidate.adapter)
            if unsupported:
                dropped_fields = sorted(name for name in unsupported if name in upstream_payload)
                if dropped_fields:
                    for name in dropped_fields:
                        upstream_payload.pop(name, None)
                    degradation_reasons.append(
                        "this provider's upstream rejects these request fields with a 400 "
                        f"malformed-request error ({', '.join(dropped_fields)}); "
                        "they were dropped for this route"
                    )

            if degradation_reasons:
                degradation = {
                    "provider": candidate.route.provider,
                    "model": candidate.route.model,
                    "reason": " ".join(degradation_reasons),
                }
                if dropped_fields:
                    degradation["dropped_fields"] = dropped_fields

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
                        "reason": CLASS_MESSAGES.get(exc.error_class, CLASS_MESSAGES[ERROR_UNKNOWN]),
                    }
                )
                self._maybe_open_circuit(alias, candidate, exc)

                if is_last:
                    break
                allowed, reason = self._fallback_permitted(
                    alias, exc.error_class, eligible[index + 1], needs_tools
                )
                if not allowed:
                    exc.message = f"{CLASS_MESSAGES.get(exc.error_class, CLASS_MESSAGES[ERROR_UNKNOWN])} (fallback not used: {reason})"
                    break
                attribution.fallback_reason = reason
                attribution.fallback_used = True
                continue

            attribution.serving_provider = candidate.route.provider
            attribution.serving_model = candidate.route.model
            # The route answered, so any circuit on it is closed. A success
            # resets the failure count, so a route that recovers is not
            # benched again by an old failure.
            self._circuits.record_success(candidate.route.provider, candidate.route.model)
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
                retry_after_seconds=self._circuits.retry_after_for(
                    [(c.route.provider, c.route.model) for c in eligible]
                ),
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
                phase=attribution.phase,
                phase_source=attribution.phase_source,
            )
        )

    def _maybe_open_circuit(self, alias: Alias, candidate: Candidate, error: AdapterError) -> None:
        """Open the circuit a qualifying failure deserves.

        The provider's own ``Retry-After`` is honoured when it asks for
        **longer** than the local policy. Waiting too little is the worse
        error, so the window is the longer of the two - and the reason is
        recorded, so the skip says which one it was.

        The class decides the rest: a confirmed exhaustion opens for a
        long time and covers the provider, a rate limit opens the route
        for as long as the provider asked, and a transient failure needs a
        pattern before it opens anything.
        """
        trigger = TRIGGER_FOR_ERROR.get(error.error_class)
        if trigger is None:
            return
        if trigger not in alias.fallback_triggers:
            return
        self._circuits.record_failure(
            provider=candidate.route.provider,
            model=candidate.route.model,
            error_class=error.error_class,
            base_seconds=alias.cooldown_seconds,
            retry_after=error.retry_after,
        )

    # --- helpers for the console -----------------------------------------

    def published_models(self) -> list[dict[str, Any]]:
        """The published alias catalogue, as ``/v1/models`` renders it."""
        config = self._config.snapshot()
        return [{"id": alias.name} for alias in sorted(config.published_aliases(), key=lambda a: a.name)]
