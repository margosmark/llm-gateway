"""The capability model.

Every route in the gateway carries a capability row. It answers two
questions:

Image input and built-in/hosted tools are hard routing requirements: the
router must exclude models that publish either capability as unsupported.
Previous-response continuation is also fail-closed: absent per-model support
metadata is unknown, so the router treats it as unsupported rather than
risking a request the provider cannot continue. Chat Completions conversion
also refuses this stateful field directly because that dialect cannot express
it; see ``gateway.bridge.requests.to_chat_completions``.

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
    #: The context available to a client. This is the provider's published
    #: input budget after headroom when it publishes one; otherwise it is the
    #: documented model window. The gateway does not invent a reserve.
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


# --- image evidence: discovery authority -> stored state -------------------


@dataclass(frozen=True)
class ImageEvidence:
    """One exact slug's documented image verdict, with the page it came from.

    ``supports_image_input`` is the verdict the source states for **that
    model id** - it is false both for "the page says text only" and for "no
    page was ever read", and ``source`` tells them apart: a verdict without a
    source is not evidence and is never carried through. Audio and video
    modalities are deliberately not represented - the capability model has one
    image flag, and inventing flags the router cannot act on would be a claim
    of support nothing checks.
    """

    supports_image_input: bool
    source: str
    observed_at: str | None = None


def image_evidence(adapter: Any) -> dict[str, ImageEvidence]:
    """The exact-slug image evidence an *adapter* can vouch for.

    An adapter is the seam that holds a provider's own documentation: the
    OpenCode Go adapter reads the dated catalogue snapshot, the ChatGPT/Codex
    adapter carries its own transcription (its catalogue endpoint publishes no
    image field at all). Both expose it through the optional
    ``image_input_evidence`` hook, keyed by the **exact** upstream model id.
    An adapter that declares nothing - or a slug declared without a source -
    contributes no evidence, so the stored row keeps its fail-closed default.
    Nothing here is ever inferred from a provider name, a model family or a
    wire dialect.
    """
    hook = getattr(adapter, "image_input_evidence", None)
    if not callable(hook):
        return {}
    try:
        declared = hook()
    except Exception:
        # Reconciliation is fail-closed: an adapter that cannot answer
        # contributes nothing, and a broken hook must never stop the gateway
        # from booting or be read as a claim of image support.
        return {}
    if not isinstance(declared, dict):
        return {}
    evidence: dict[str, ImageEvidence] = {}
    for model_id, row in declared.items():
        if not isinstance(model_id, str) or not model_id or not isinstance(row, ImageEvidence):
            continue
        if not isinstance(row.supports_image_input, bool) or not isinstance(row.source, str) or not row.source:
            continue
        evidence[model_id] = row
    return evidence


def apply_image_evidence(
    config: Any,
    evidence_by_provider: dict[str, dict[str, ImageEvidence]],
) -> list[tuple[str, str]]:
    """Bring stored model rows up to the evidence their provider holds.

    A capability that exists in the source-backed catalogue but not in the
    stored state is indistinguishable from ``false``: the router, the alias
    catalogue and every client read the **stored** row. A row written before
    the evidence field existed - or before the evidence was transcribed -
    stays text-only forever otherwise, which is how every published alias
    came to advertise ``supports_image_input: false`` while the catalogue
    already named image-capable models.

    So each boot re-applies the evidence that is already on disk (the dated
    catalogue snapshot, an adapter's own transcription). The rules:

    * **Exact slug only, promotion only.** A stored row flips to
      image-capable only when its own provider vouches for *that* model id
      with a source URL. Sibling slugs never share a verdict - the MiMo rows
      disagree with one another on image support - and a restart never
      removes a capability: a row whose evidence says "text only" or says
      nothing at all is left exactly as the owner has it, because "unknown
      stays false" is about not inventing a claim, not about revoking one.
    * **No new rows.** Exposure is owner policy: reconciliation never adds,
      enables or disables a model, and never touches a provider that has no
      stored rows yet.
    * **No provider call.** It reads local files and local maps only.

    Returns the ``(provider_id, model_id)`` rows that changed, so the caller
    reports the reconciliation instead of performing it silently.
    """
    changed: list[tuple[str, str]] = []
    for provider_id, evidence in evidence_by_provider.items():
        if not evidence:
            continue
        provider = config.providers.get(provider_id)
        if provider is None:
            continue
        for model_id, entry in provider.models.items():
            verdict = evidence.get(model_id)
            if verdict is None or not verdict.supports_image_input:
                continue
            if not entry.supports_image_input:
                entry.supports_image_input = True
                changed.append((provider_id, model_id))
    return changed
