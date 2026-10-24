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
from dataclasses import dataclass, field, replace
from typing import Any, Iterator

from gateway.adapters.base import Adapter, AdapterError, CLASS_MESSAGES, ProviderRequest, ProviderResponse
from gateway.provider_concurrency import ProviderConcurrencyLimit
from gateway.bridge.model import ConversionError
# The anthropic bridge is a superset dispatcher: it handles every Anthropic
# pair and delegates the OpenAI-family pairs to gateway.bridge.requests, so
# routing needs only this one seam regardless of the route's dialect.
from gateway.bridge.anthropic import convert_request, payload_has_image_input
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
    TRIGGER_PROBE_CONTENTION,
    TRIGGER_RATE_LIMITED,
    TRIGGER_ROUTE_INELIGIBLE,
    TRIGGER_UPSTREAM_UNAVAILABLE,
)
from gateway.core.abort import RouteContext, register_route_context

# T11-ANCHOR: client-abort ledger correction - importing the module is
# what installs the observer that corrects a disconnect's ledger row.
from gateway.core import client_abort as _client_abort  # noqa: F401
from gateway.core.capabilities import (
    DIALECT_OPENAI_CHAT,
    DIALECT_OPENAI_RESPONSES,
    Capability,
)
from gateway.observability import RequestEvent, utc_now_iso
from gateway.policy import PolicyChange, PolicyViolation, apply_alias_policy
from gateway.session import derive_session
from gateway.thinking import DISABLED_LEVEL, is_disabled_level
from gateway.core.weighted_pool import select_weighted

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
    """Return the requested reasoning level, including the canonical disabled level.

    Disabled is a preference like other reasoning levels. Keeping it in the
    same per-route publication check ensures a model that does not publish
    ``none`` gets a recorded degradation instead of a silent upstream control.
    """
    effort = payload.get("reasoning_effort")
    reasoning = payload.get("reasoning")
    if isinstance(reasoning, dict):
        if reasoning.get("enabled") is False:
            return DISABLED_LEVEL
        if effort is None:
            effort = reasoning.get("effort")
    if not isinstance(effort, str):
        return None
    return DISABLED_LEVEL if is_disabled_level(effort) else effort


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

    def __init__(
        self,
        message: str,
        code: str = "routing_error",
        status: int = 400,
        detail: Any = None,
        retry_after_seconds: int | None = None,
        capability: str | None = None,
        log_attribution: dict[str, Any] | None = None,
    ) -> None:
        # Client text (model names, aliases) is interpolated into routing
        # messages. Rewrite any lone surrogate to escape text at capture so
        # it can never reach an UTF-8 encode boundary; clean messages are
        # byte-identical (F9).
        message = message.encode("utf-8", "backslashreplace").decode("utf-8")
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status
        self.detail = detail
        self.retry_after_seconds = retry_after_seconds
        #: Set when the refusal is *about* a capability the alias cannot
        #: meet, so the client is told which one without being told which
        #: provider or model it was turned away from.
        self.capability = capability
        #: How the request was routed, including the route it failed on.
        #: Kept off ``to_dict()``: an error *body* stays generic, while the
        #: access log and the durable ledger may say which alias and route
        #: were involved. Without it a 502 names nothing.
        self.log_attribution = log_attribution

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"error": {"message": self.message, "type": self.code}}
        if self.capability is not None:
            payload["error"]["capability"] = self.capability
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
    """Which provider and model actually served a request.

    ``serving_provider`` / ``serving_model`` always name the *last route
    tried*. On a request that was served that is the serving route; on a
    request that failed it is the route the failure came from, so a 502 is
    attributable rather than recorded as null, null.
    """

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
    route_context: RouteContext | None = None


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
            # Enumeration must never claim a recovery probe; only the
            # invocation path claims, through CircuitStore.claim.
            allowed, circuit_reason = self._circuits.preview(route.provider, route.model)
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
                # A reasoning effort is a *preference*, not a capability. The
                # route stays eligible and the per-route substitution is
                # visible in attribution, including disabled `none`.
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
        if alias.primary is None and not alias.weighted_routes:
            raise RoutingError(
                f"alias '{alias_name}' has no primary route",
                code="alias_incomplete",
                status=409,
            )
        return alias

    # --- the alias request policy (T21) ---------------------------------

    def _apply_policy(
        self,
        alias: Alias,
        payload: dict[str, Any],
        source_dialect: str,
    ) -> tuple[dict[str, Any], list[PolicyChange]]:
        """Apply the alias's request policy, refusing what it denies.

        The policy is the owner's, so a refusal is an actionable 4xx
        that names the rule, not a provider failure and not a silently
        stripped request. It is applied here, in the router, on the same
        seam the session derivation lives on: one place every path that
        reaches a provider passes through - the API surface, the alias
        test, and any future caller - never a handler.
        """
        try:
            return apply_alias_policy(alias, payload, source_dialect)
        except PolicyViolation as exc:
            raise RoutingError(
                exc.message,
                code=exc.code,
                status=exc.status,
                detail={"alias": alias.name, "rule": exc.rule},
            ) from exc

    # --- invocation ------------------------------------------------------

    def _fallback_permitted(
        self,
        alias: Alias,
        failed_class: str,
        next_candidate: Candidate,
        side_effects_possible: bool,
        pooled_stream: bool = False,
    ) -> tuple[bool, str]:
        """Decide whether the next candidate may be tried after a failure.

        Returns ``(allowed, reason)``. The reason explains a refusal.
        """
        allowed, reason = self._switch_preflight(alias, next_candidate, side_effects_possible, pooled_stream)
        if not allowed:
            return allowed, reason

        trigger = TRIGGER_FOR_ERROR.get(failed_class)
        if trigger is None:
            if failed_class in OWNER_POLICY_CLASSES:
                if failed_class not in alias.fallback_error_classes:
                    return False, (
                        f"the owner has not defined a safe fallback policy for '{failed_class}' failures"
                    )
                return True, f"the owner defined a safe fallback policy for '{failed_class}' failures"
            return False, f"'{failed_class}' failures never trigger a fallback"
        return self._trigger_permitted(alias, trigger)

    def _skip_fallback_permitted(
        self,
        alias: Alias,
        trigger: str,
        next_candidate: Candidate,
    ) -> tuple[bool, str]:
        """Decide whether the next candidate may serve a **skipped** route.

        A primary that was never invoked - in cooldown, disabled, a
        capability mismatch, or held by another request's live recovery
        probe - hands the request to a later, cross-provider route, and
        that switch passes the owner controls of
        :meth:`_owner_switch_controls` plus an enabled trigger. The skip
        contributes its own trigger instead of an error class.

        The side-effects gate of :meth:`_switch_preflight` is
        deliberately **not** applied here. That gate exists so a provider
        that may already have executed a tool is never retried on another
        route; every skip path runs before this request has reached any
        provider, so no tool can have run and no side effect can repeat.
        Applying it here refused every tool-bearing request for as long as
        the primary sat in a cooldown, even with the fallback fully
        authorised and its trigger enabled (R50). What arms that gate is
        what an upstream actually received - the request carrying tools
        **and** an attempt being recorded - and neither skip call site can
        have recorded an attempt, because a route skipped before
        invocation cannot have executed anything (R50). Returns
        ``(allowed, reason)``.
        """
        allowed, reason = self._owner_switch_controls(alias, next_candidate)
        if not allowed:
            return allowed, reason
        return self._trigger_permitted(alias, trigger)

    @staticmethod
    def _switch_preflight(
        alias: Alias,
        next_candidate: Candidate,
        side_effects_possible: bool,
        pooled_stream: bool = False,
    ) -> tuple[bool, str]:
        """The gate for a switch away from a route this request **tried**.

        A failed attempt may have executed a tool, so a tool-bearing
        request is never retried elsewhere; only then do the owner's
        controls apply.
        """
        if side_effects_possible:
            return False, "the request carried tools, so a provider switch could repeat a side effect"
        if pooled_stream:
            # A weighted pool is a deliberate distribution, not a failure
            # path. The spec's hard stop for pool failures: it never
            # happens after a stream. The failure surfaces on this alias.
            return False, (
                "the request is a stream and the route was chosen by the alias's "
                "weighted pool, so a pool failure never switches route"
            )
        return Router._owner_switch_controls(alias, next_candidate)

    @staticmethod
    def _owner_switch_controls(alias: Alias, next_candidate: Candidate) -> tuple[bool, str]:
        """The owner controls every cross-provider switch shares, in fixed order.

        Authorization and confirmation of the next provider. The
        side-effects check lives on :meth:`_switch_preflight` for
        attempted routes only, and the trigger check is separate on both
        paths - a skipped route contributes its own trigger.
        """
        if not alias.fallback_authorized:
            return False, "cross-provider fallback is not authorised for this alias"
        if next_candidate.route.provider not in alias.fallback_confirmed_providers:
            return False, (
                f"fallback to provider '{next_candidate.route.provider}' has not been confirmed by the owner"
            )
        return True, ""

    @staticmethod
    def _trigger_permitted(alias: Alias, trigger: str) -> tuple[bool, str]:
        if trigger not in alias.fallback_triggers:
            return False, f"the '{trigger}' trigger is not enabled for this alias"
        return True, f"the '{trigger}' trigger is enabled for this alias"

    # --- skip attribution ------------------------------------------------

    def _skip_trigger(self, candidate: Candidate, *, vanished: str) -> str:
        """The fallback trigger a *skipped* primary candidate satisfies.

        A candidate excluded by enumeration is first checked for *why*:
        only a circuit hold can carry an origin, so a capability or
        configuration skip maps to ``route_ineligible`` and never
        consults the circuit store. For a circuit hold the store
        classifies itself: an open window carries the failure class it
        was opened by, so an **automatic** cooldown keeps its own
        recorded trigger (the trigger the owner already enabled is the
        one that opened the window, so those keep working unchanged); a
        window placed by the **operator** carries no class and maps to
        ``explicit_cooldown``; a live recovery probe maps to
        ``probe_contention`` - an automatic event with its own class,
        never folded into the operator's.

        ``vanished`` is returned when the hold ended between the moment
        the route was skipped and this decision.
        """
        if not candidate.circuit_blocked:
            return TRIGGER_ROUTE_INELIGIBLE
        return self._hold_trigger(candidate.route, vanished=vanished)

    def _claim_skip_trigger(self, route: RouteRef) -> str:
        """The trigger for a route whose invocation-boundary claim was refused.

        The candidate passed enumeration, so the hold appeared moments
        ago: a reopened window classifies itself through the store, and
        an origin that is already gone can only have been the live probe
        this request lost the race to - a candidate cannot be skipped
        here for any capability reason.
        """
        return self._hold_trigger(route, vanished=TRIGGER_PROBE_CONTENTION)

    def _hold_trigger(self, route: RouteRef, *, vanished: str) -> str:
        origin = self._circuits.skip_origin(route.provider, route.model)
        if origin is None:
            return vanished
        if origin == "probe":
            return TRIGGER_PROBE_CONTENTION
        trigger = TRIGGER_FOR_ERROR.get(origin.partition(":")[2])
        if trigger is not None:
            return trigger
        return TRIGGER_EXPLICIT_COOLDOWN

    def _fallback_refusal(
        self, alias_name: str, reason: str, routes: list[tuple[str, str]]
    ) -> RoutingError:
        """The refusal for a switch the owner's policy declined.

        Same shape as the pre-loop ``no eligible route`` refusal: a 503
        with a bounded ``Retry-After`` when a circuit is part of the
        story. A skip refusal is a public response, and the capability
        refusal above proves public responses name no upstream
        identifiers - so the one reason that would name the unconfirmed
        fallback provider is reported without it, while every other
        reason carries the exact gate that declined.
        """
        if "has not been confirmed" in reason:
            reason = "the fallback provider has not been confirmed by the owner"
        return RoutingError(
            f"no eligible route for alias '{alias_name}' (fallback not used: {reason})",
            code="fallback_not_permitted",
            status=503,
            retry_after_seconds=self._circuits.retry_after_for(routes),
        )

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

    def _publishes_image_route(self, alias: Alias) -> bool:
        """Whether this alias has an enabled route that accepts images.

        A configuration fact, deliberately independent of eligibility: a
        benched image-capable route still counts as published, so the
        image-specific refusal never claims the owner failed to configure
        one. It reads the stored rows only and makes no provider call.
        """
        config = self._config.snapshot()
        for route in alias.all_routes():
            entry = self._model_entry(config, route)
            if entry is not None and entry.enabled and entry.supports_image_input:
                return True
        return False

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
        client_request_id: str | None = None,
    ) -> RoutedResponse:
        """Resolve an alias and send the request, applying the fallback policy.

        ``caller_phase`` is a validated phase hint (``plan``/``implement``/
        ``validate``) already stripped from the request payload by the
        handler. It is honoured only when the alias has an enabled phase
        policy that maps that phase to a route.
        """
        alias = self.resolve_alias(alias_name)
        conversation = derive_session(profile_id, alias_name, client_ip, session_hint)
        #: True when the serving route was chosen by a weighted pool. A
        #: pool is a deliberate distribution, so a failure inside it never
        #: switches to a different route once the request is a stream.
        from_weighted_pool = bool(alias.weighted_routes)
        if alias.weighted_routes:
            pool_routes = [item.route for item in alias.weighted_routes]
            selected, pool_fallbacks = select_weighted(
                pool_routes, [item.weight for item in alias.weighted_routes], conversation, alias_name
            )
            alias = replace(alias, primary=selected, weighted_routes=[], fallbacks=pool_fallbacks + alias.fallbacks)


        # The owner's request policy is applied here, at the top of the
        # invocation: before the request facts (tools, images, thinking
        # level) are read for candidate selection, and before phase
        # inference can call a provider. A denied request is therefore
        # refused with nothing spent, and an owner default or cap is
        # part of what every route is judged against - on every path
        # that reaches a provider, because they all enter here.
        payload, policy_changes = self._apply_policy(alias, payload, source_dialect)

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
            #
            # An image has one extra requirement: the refusal must *say*
            # what was refused, because "no eligible route" does not tell a
            # client that its attachment was the reason. That is only said
            # when the configuration really has no image-capable route for
            # this alias - a route that accepts images but is benched,
            # disabled or otherwise unavailable keeps the generic wording,
            # since "publishes no image-capable route" would then be false.
            image_refusal = needs_image_input and not self._publishes_image_route(alias)
            retry_after = self._circuits.retry_after_for(
                [(c.route.provider, c.route.model) for c in candidates if c.circuit_blocked]
            )
            if image_refusal:
                raise RoutingError(
                    f"alias '{alias_name}' publishes no route that accepts image input; "
                    "resend the request without the image, or publish an image-capable "
                    "model for this alias",
                    code="no_eligible_route",
                    status=503,
                    capability="image_input",
                )
            raise RoutingError(
                f"no eligible route for alias '{alias_name}'",
                code="no_eligible_route",
                status=503,
                retry_after_seconds=retry_after,
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
        started = time.monotonic()
        if (
            eligible
            and eligible[0].route != (alias.primary or eligible[0].route)
            and eligible[0].route != phase_selected_route
        ):
            # The primary was skipped, so a later route is about to serve
            # the request. A route chosen by the owner's phase policy is
            # not a fallback: it is the primary for that phase, so it is
            # not reported as one and needs no fallback authorization.
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
            # The switch itself is a cross-provider fallback and passes
            # the same owner gate as a failed attempt: authorization,
            # confirmation of the next provider, and an enabled trigger -
            # the trigger that names *this* skip. A declined switch
            # refuses the request instead of serving it elsewhere.
            if alias.primary is not None:
                primary_candidate = next(
                    (c for c in candidates if c.route == alias.primary), None
                )
                trigger = TRIGGER_ROUTE_INELIGIBLE
                if primary_candidate is not None:
                    trigger = self._skip_trigger(
                        primary_candidate, vanished=TRIGGER_EXPLICIT_COOLDOWN
                    )
                # Side effects are only possible when an upstream actually
                # received this request. A primary skipped here was never
                # invoked - no attempt is recorded - so a tool-bearing
                # request may still take this switch. The owner controls
                # and the skip's trigger above are unchanged (R50).
                allowed, permission_reason = self._skip_fallback_permitted(
                    alias, trigger, eligible[0]
                )
                if not allowed:
                    attribution.fallback_reason = f"fallback refused: {permission_reason}"
                    refusal = self._fallback_refusal(
                        alias_name,
                        permission_reason,
                        [(c.route.provider, c.route.model) for c in candidates],
                    )
                    # The owner's gate stopped this request, and a
                    # stopped request is an outcome the ledger owns: it
                    # records that nobody served it, without claiming a
                    # fallback for a switch that never happened.
                    self._record(
                        request_id=request_id,
                        alias_name=alias_name,
                        payload=payload,
                        attribution=attribution,
                        status=refusal.status,
                        error_class=ERROR_POLICY,
                        latency_ms=int((time.monotonic() - started) * 1000),
                        stream=stream,
                        profile_id=profile_id,
                    )
                    raise refusal
            attribution.fallback_used = True
            attribution.fallback_reason = reason
        last_error: AdapterError | None = None
        #: Set when the owner's policy refused the switch away from a
        #: skipped primary mid-loop; the tail records the empty result
        #: and then raises it.
        skip_refusal: RoutingError | None = None
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
                # Same as the failure path: a request that never succeeds
                # still names the route it was refused on.
                attribution.serving_provider = candidate.route.provider
                attribution.serving_model = candidate.route.model
                if is_last:
                    break
                allowed, reason = self._fallback_permitted(
                    alias, ERROR_POLICY, eligible[index + 1], needs_tools,
                    pooled_stream=from_weighted_pool and stream,
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

            for changed in policy_changes:
                if changed.field == "store":
                    # A forced retention value is dialect-neutral and the
                    # bridge does not model it, so it is re-asserted after
                    # conversion: `store` reaches the provider on a
                    # converted route exactly as it would same-dialect.
                    upstream_payload["store"] = changed.value

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

            # The route is claimed **here**, immediately before the provider
            # is invoked, and only after every local check that could
            # decline this route has passed. Claiming earlier - while the
            # candidates were still being assembled - let a route that was
            # never invoked hold a half-open probe for
            # ``PROBE_TIMEOUT_SECONDS``, which pinned the primary to its
            # fallback long after the provider had recovered.
            claimed, circuit_reason, probe_owner = self._circuits.claim(
                candidate.route.provider, candidate.route.model
            )
            if not claimed:
                # Another request is probing this route right now, or the
                # circuit is in cooldown again. The route is skipped, the
                # same way an ineligible candidate is.
                attribution.skipped.append(
                    {
                        "provider": candidate.route.provider,
                        "model": candidate.route.model,
                        "reason": circuit_reason,
                    }
                )
                if is_last:
                    break
                if (
                    not attribution.fallback_used
                    and alias.primary is not None
                    and candidate.route == alias.primary
                ):
                    # The primary could not be claimed, so a later route
                    # is about to serve the request. The record says
                    # why - and the switch passes the same owner gate as
                    # every other fallback: authorization, confirmation
                    # of the next provider, and an enabled trigger (the
                    # one that names this skip, classified above). A
                    # declined switch stops the loop instead of serving
                    # the request on the unauthorised route.
                    trigger = self._claim_skip_trigger(candidate.route)
                    # The claim refused before invocation, so unless an
                    # earlier candidate was already invoked this request
                    # never reached a provider (R50).
                    allowed, permission_reason = self._skip_fallback_permitted(
                        alias, trigger, eligible[index + 1]
                    )
                    if not allowed:
                        attribution.fallback_reason = f"fallback refused: {permission_reason}"
                        skip_refusal = self._fallback_refusal(
                            alias_name,
                            permission_reason,
                            [(c.route.provider, c.route.model) for c in eligible],
                        )
                        break
                    attribution.fallback_used = True
                    attribution.fallback_reason = (
                        f"the primary route was skipped: {circuit_reason}"
                    )
                continue

            # Everything this candidate does after a successful claim runs
            # under this one guard, and the guard releases **this claim's
            # owner token** on every exit path: success, a failure that
            # falls back, and any exception. ``release_probe`` ignores an
            # owner token that does not match, so a live probe claimed by
            # another request is never cleared here, and releasing an
            # already-released claim is a no-op, so the paths that released
            # explicitly before are unchanged.
            #
            # The guard has to be this wide because an exception raised
            # inside the ``except AdapterError`` handler below - while
            # ``_maybe_open_circuit()`` is processing the failure - is not
            # caught by a sibling ``except``, and neither is one raised by
            # the post-claim recording that follows the invocation. Either
            # one used to escape with the claim still held, so the route
            # refused every probe until the probe timeout passed while
            # nothing was probing it.
            try:
                try:
                    response = candidate.adapter.invoke(request)  # type: ignore[union-attr]
                except AdapterError as exc:
                    last_error = exc
                    attempt: dict[str, Any] = {
                        "provider": candidate.route.provider,
                        "model": candidate.route.model,
                        "outcome": "error",
                        "error_class": exc.error_class,
                        # The class message would blame the provider for
                        # a refusal the gateway itself made.
                        "reason": (
                            "the gateway's own in-flight cap refused this request"
                            if isinstance(exc, ProviderConcurrencyLimit)
                            else CLASS_MESSAGES.get(exc.error_class, CLASS_MESSAGES[ERROR_UNKNOWN])
                        ),
                    }
                    if exc.rejected_fields:
                        # A shape rejection is only diagnosable from the field it
                        # named. That name - never the body's text - is carried
                        # into the attempt so the failure is attributable.
                        attempt["rejected_fields"] = list(exc.rejected_fields)
                        attempt["reason"] = (
                            f"{attempt['reason']} (rejected fields: {', '.join(exc.rejected_fields)})"
                        )
                    attribution.attempts.append(attempt)
                    # A request that never succeeds must still name the route it
                    # failed on. The success path sets these to the serving
                    # route; without this the durable ledger records null
                    # serving_provider and serving_model for exactly the
                    # failures an operator most needs to diagnose.
                    attribution.serving_provider = candidate.route.provider
                    attribution.serving_model = candidate.route.model
                    # A local capacity refusal is not evidence about the
                    # provider: opening a circuit here would turn "retry
                    # later" into a multi-minute bench of a route that was
                    # never at fault, so the caller would then see 503
                    # instead of the fast 429 it was promised.
                    if not isinstance(exc, ProviderConcurrencyLimit):
                        self._maybe_open_circuit(alias, candidate, exc, request_id)

                    if is_last:
                        break
                    allowed, reason = self._fallback_permitted(
                        alias, exc.error_class, eligible[index + 1], needs_tools,
                        pooled_stream=from_weighted_pool and stream,
                    )
                    if not allowed:
                        # The gateway's own capacity refusal keeps its
                        # message: the generic class text would say the
                        # *provider* rate-limited a request the gateway
                        # refused locally, and the caller needs the
                        # "retry later" answer it was promised.
                        if not isinstance(exc, ProviderConcurrencyLimit):
                            exc.message = f"{CLASS_MESSAGES.get(exc.error_class, CLASS_MESSAGES[ERROR_UNKNOWN])} (fallback not used: {reason})"
                        break
                    attribution.fallback_reason = reason
                    attribution.fallback_used = True
                    continue

                # T11-ANCHOR: client-abort propagation
                route_context = RouteContext(
                    request_id=request_id,
                    provider=candidate.route.provider,
                    model=candidate.route.model,
                    adapter=candidate.adapter,
                    request=request,
                    stream=response.stream,
                    ledger=self._ledger,
                )
                register_route_context(route_context)
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
                for changed in policy_changes:
                    # The owner's policy changed this request, and the change is
                    # recorded against the route that actually served it - the
                    # same ledger form a dropped reasoning effort takes, so a
                    # clamped value is never invisible.
                    attribution.degradations.append(
                        {
                            "provider": candidate.route.provider,
                            "model": candidate.route.model,
                            "kind": changed.kind,
                            "field": changed.field,
                            "from": changed.previous,
                            "to": changed.value,
                            "reason": changed.reason,
                        }
                    )
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
                    client_request_id=client_request_id,
                )
                return RoutedResponse(
                    response=response,
                    attribution=attribution,
                    request=request,
                    route_context=route_context,
                )
            finally:
                self._circuits.release_probe(
                    candidate.route.provider, candidate.route.model, probe_owner
                )

        # Every eligible route failed.
        # A provider failure is an upstream failure and is reported as
        # 502, even when the provider rejected its own credential, because
        # the caller's credential was not what was rejected. The one
        # exception is the gateway's *own* capacity refusal
        # (ProviderConcurrencyLimit carries http_status=429): nothing went
        # upstream, so it is a local 429 with no queueing.
        failure_status = getattr(last_error, "http_status", 502) if last_error is not None else 503
        self._record(
            request_id=request_id,
            alias_name=alias_name,
            payload=payload,
            attribution=attribution,
            status=failure_status,
            error_class=(
                last_error.error_class
                if last_error is not None
                else (ERROR_POLICY if skip_refusal is not None else "unknown")
            ),
            latency_ms=int((time.monotonic() - started) * 1000),
            stream=stream,
            profile_id=profile_id,
            client_request_id=client_request_id,
        )
        if skip_refusal is not None:
            raise skip_refusal
        if last_error is None:
            raise RoutingError(
                f"no eligible route for alias '{alias_name}'",
                code="no_eligible_route",
                status=503,
                retry_after_seconds=self._circuits.retry_after_for(
                    [(c.route.provider, c.route.model) for c in eligible]
                ),
                # The access log still needs to know which alias and which
                # routes were considered; the response body stays generic.
                log_attribution=attribution.to_dict(),
            )
        # A provider failure is an upstream failure. It is reported as 502
        # even when the provider rejected its own credential, because the
        # caller's credential was not what was rejected - except for the
        # gateway's own capacity refusal, which carries its own status.
        raise RoutingError(
            last_error.message,
            code=last_error.error_class,
            status=failure_status,
            detail={"attempts": attribution.attempts},
            # The access log and the durable ledger need the alias and the
            # route the request died on; the response body does not.
            log_attribution=attribution.to_dict(),
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
        client_request_id: str | None = None,
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
                client_request_id=client_request_id,
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

    def _maybe_open_circuit(
        self, alias: Alias, candidate: Candidate, error: AdapterError, request_id: str = ""
    ) -> None:
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
            request_id=request_id,
        )

    # --- helpers for the console -----------------------------------------

    def published_models(self) -> list[dict[str, Any]]:
        """The published alias catalogue, as ``/v1/models`` renders it."""
        config = self._config.snapshot()
        return [{"id": alias.name} for alias in sorted(config.published_aliases(), key=lambda a: a.name)]
