"""The capability model.

Every route in the gateway carries a capability row. It answers two
questions:

1. **May this route serve this request?** Compatibility is checked *before*
   route eligibility, and the owner sees the reason during route validation.
2. **Which conversions may the bridge perform?** The capability row doubles
   as the bridge contract: it states, per route, what can be translated and
   what must be refused.

Dialect is a property of the **model**, not of the provider. OpenCode Go
serves four wire protocols under one provider; ``gpt-6-luna`` is served over
the Responses protocol while ``mimo-v2.6-flash`` is served over Chat
Completions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

# Wire dialects.
DIALECT_OPENAI_RESPONSES = "openai_responses"
DIALECT_OPENAI_CHAT = "openai_chat"
DIALECT_ANTHROPIC_MESSAGES = "anthropic_messages"
DIALECT_GOOGLE_V1BETA = "google_v1beta"

DIALECTS = (
    DIALECT_OPENAI_RESPONSES,
    DIALECT_OPENAI_CHAT,
    DIALECT_ANTHROPIC_MESSAGES,
    DIALECT_GOOGLE_V1BETA,
)

# Retention classes, taken from the provider's published privacy table. They
# drive the warning shown next to a cross-provider fallback.
RETENTION_30_DAYS = "30_days"
RETENTION_ZERO_DAYS = "0_days"
RETENTION_TRAINING_PERMITTED = "training_permitted"
RETENTION_UNKNOWN = "unknown"

RETENTION_LABELS = {
    RETENTION_30_DAYS: "30-day retention (abuse monitoring)",
    RETENTION_ZERO_DAYS: "0-day retention",
    RETENTION_TRAINING_PERMITTED: "prompts may be used for training",
    RETENTION_UNKNOWN: "retention unknown",
}

# Reasoning efforts, per model. Luna tops out at max and has no ultra.
REASONING_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")


@dataclass(frozen=True)
class Capability:
    """What one provider/model can do."""

    dialect: str
    supports_streaming: bool = True
    supports_function_tools: bool = True
    supports_builtin_tools: bool = False
    reasoning_efforts: tuple[str, ...] = ()
    supports_image_input: bool = False
    supports_previous_response_id: bool = False
    #: The model's own context window, as the provider reports it.
    context_window: int | None = None
    #: The context available for **input**, after the provider reserves
    #: headroom for system prompts, tool overhead and the model's own
    #: output. This is the number a client can actually rely on.
    context_limit: int | None = None
    max_output_tokens: int | None = None
    retention_class: str = RETENTION_UNKNOWN
    notes: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "dialect": self.dialect,
            "supports_streaming": self.supports_streaming,
            "supports_function_tools": self.supports_function_tools,
            "supports_builtin_tools": self.supports_builtin_tools,
            "reasoning_efforts": list(self.reasoning_efforts),
            "supports_image_input": self.supports_image_input,
            "supports_previous_response_id": self.supports_previous_response_id,
            "context_window": self.context_window,
            "context_limit": self.context_limit,
            "max_output_tokens": self.max_output_tokens,
            "retention_class": self.retention_class,
            "retention_label": RETENTION_LABELS.get(self.retention_class, RETENTION_UNKNOWN),
            "notes": self.notes,
        }

    def supports_effort(self, effort: str | None) -> bool:
        if effort is None:
            return True
        if not self.reasoning_efforts:
            return False
        return effort in self.reasoning_efforts

    def supports_conversion_to(self, target: str) -> bool:
        """Whether the bridge may convert this dialect into *target*."""
        if target == self.dialect:
            return True
        return self.dialect in _CONVERTIBLE_FROM and target in _CONVERTIBLE_FROM[self.dialect]


#: The bridge converts between the two OpenAI-family dialects. The Anthropic
#: and Google dialects are served natively by their provider and are *not*
#: converted - a request that would need such a conversion is refused.
_CONVERTIBLE_FROM: dict[str, tuple[str, ...]] = {
    DIALECT_OPENAI_CHAT: (DIALECT_OPENAI_RESPONSES,),
    DIALECT_OPENAI_RESPONSES: (DIALECT_OPENAI_CHAT,),
}


def convertible_dialects(source: str) -> tuple[str, ...]:
    return _CONVERTIBLE_FROM.get(source, ())


@dataclass
class ModelInfo:
    """A model as the provider describes it."""

    model_id: str
    dialect: str
    context_window: int | None = None
    max_output_tokens: int | None = None
    reasoning_efforts: tuple[str, ...] = ()
    supports_streaming: bool = True
    supports_function_tools: bool = True
    supports_builtin_tools: bool = False
    supports_image_input: bool = False
    supports_previous_response_id: bool = False
    context_window: int | None = None
    context_limit: int | None = None
    retention_class: str = RETENTION_UNKNOWN
    monthly_limit_usd: float | None = None
    limits_source: str = "unknown"
    limits_observed_at: str | None = None
    source: str = "manual"
    notes: str | None = None

    def capability(self) -> Capability:
        return Capability(
            dialect=self.dialect,
            supports_streaming=self.supports_streaming,
            supports_function_tools=self.supports_function_tools,
            supports_builtin_tools=self.supports_builtin_tools,
            reasoning_efforts=self.reasoning_efforts,
            supports_image_input=self.supports_image_input,
            supports_previous_response_id=self.supports_previous_response_id,
            context_window=self.context_window,
            context_limit=self.context_limit,
            max_output_tokens=self.max_output_tokens,
            retention_class=self.retention_class,
            notes=self.notes,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "dialect": self.dialect,
            "capability": self.capability().to_dict(),
            "monthly_limit_usd": self.monthly_limit_usd,
            "limits_source": self.limits_source,
            "limits_observed_at": self.limits_observed_at,
            "source": self.source,
            "notes": self.notes,
        }


#: The share of a context window a provider considers usable for input,
#: reserving the rest for system prompts, tool overhead and the model's own
#: output. This matches the reference client's published default.
DEFAULT_EFFECTIVE_CONTEXT_PERCENT = 95


def resolve_context_window(
    context_window: int | None,
    max_context_window: int | None,
) -> int | None:
    """The model's own context window.

    ``max_context_window`` is a ceiling that only applies when a client
    explicitly overrides the limit, so the **smaller** of the two is the
    window that applies by default.
    """
    values = [value for value in (context_window, max_context_window) if isinstance(value, int) and value > 0]
    if not values:
        return None
    return min(values)


def effective_context_limit(
    context_window: int | None,
    percent: int | None = None,
) -> int | None:
    """The context available for input, after the provider's headroom."""
    if not isinstance(context_window, int) or context_window <= 0:
        return None
    share = percent if isinstance(percent, int) and 0 < percent <= 100 else DEFAULT_EFFECTIVE_CONTEXT_PERCENT
    return (context_window * share) // 100


def alias_context_limit(routes: Iterable[tuple[str, int | None]]) -> dict[str, Any]:
    """The effective input limit for an alias.

    The limit is the **smallest** of the limits of the routes that could
    serve the request, because a fallback may serve it. If any eligible
    route's limit is unknown, the alias's limit is unknown too: a bound
    that cannot be verified is not a bound.
    """
    listed = list(routes)
    if not listed:
        return {"limit": None, "known": False, "reason": "the alias has no route"}
    unknown = [label for label, limit in listed if limit is None]
    if unknown:
        return {
            "limit": None,
            "known": False,
            "reason": (
                "the limit of "
                + ", ".join(unknown)
                + " is not published, so the limit of the alias cannot be stated"
            ),
        }
    limits = [limit for _, limit in listed if limit is not None]
    return {
        "limit": min(limits),
        "known": True,
        "reason": "the smallest limit among the routes that could serve this alias",
    }


def merge_capabilities(infos: Iterable[ModelInfo]) -> dict[str, ModelInfo]:
    """Index model infos by id. Later entries win; first entry is kept otherwise."""
    merged: dict[str, ModelInfo] = {}
    for info in infos:
        merged.setdefault(info.model_id, info)
    return merged
