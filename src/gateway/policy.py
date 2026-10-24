"""The alias request policy, applied in one place.

The owner configures a ``policy`` block on an alias (``gateway.config``):
defaults for values the client did not specify, caps that clamp what the
client did specify, and deny rules that refuse or force. This module is
the single place those rules are applied, and the router applies it on
**every** path that reaches a provider - the API surface, the console's
alias test, and any future caller - *before* any dialect conversion.

The order is fixed by ``docs/TODO.md`` T21:

1. the client's value,
2. the owner's default, if the client gave none,
3. the owner's cap, if it clamps the value now standing,
4. the deny rules.

Three properties this module exists to guarantee:

* **One vocabulary.** The owner writes ``thinking_level: high`` once, in
  the canonical vocabulary ``gateway.thinking`` publishes. The value is
  read from and written to the *source* dialect's own spelling
  (``reasoning_effort`` for Chat Completions, ``reasoning.effort`` for
  Responses) so the bridge remains the only code that renders per
  dialect, and a policy change can never leave the flat and the nested
  spelling of one request disagreeing.
* **Never silent, never invented.** Every value the policy changes comes
  back as a :class:`PolicyChange`; the router attributes it to the route
  that actually served the request and the ledger records it. A refusal
  names its rule. A level outside the canonical vocabulary is *not*
  silently trusted past a cap: a cap is a ceiling, and a level the
  gateway cannot order cannot be shown to be under it.
* **No content is retained.** A change record names the field, the old
  and the new *control value*, and the rule - never the prompt, the
  completion, or any part of the message list. The one text-valued
  control (``system``) is recorded as its length in characters.

The input payload is never mutated: the caller keeps the client's own
request, and the policy returns the request to send on.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from gateway.bridge.model import ROLE_DEVELOPER, ROLE_SYSTEM
from gateway.config import Alias, AliasPolicy
from gateway.core.capabilities import DIALECT_OPENAI_CHAT, DIALECT_OPENAI_RESPONSES
from gateway.thinking import CANONICAL_LEVELS, DISABLED_LEVEL

#: The change kinds, in the order the rules that produce them are applied.
KIND_DEFAULTED = "defaulted"
KIND_CLAMPED = "clamped"
KIND_FORCED = "forced"


class PolicyViolation(Exception):
    """A request the alias policy refuses.

    Raised before any provider work: the message is actionable and the
    rule is named, so the caller knows which line of the alias policy to
    change. It never carries request content.
    """

    def __init__(self, alias_name: str, rule: str, message: str) -> None:
        super().__init__(message)
        self.alias_name = alias_name
        self.rule = rule
        self.message = message
        self.code = "policy_denied"
        self.status = 403


@dataclass(frozen=True)
class PolicyChange:
    """One thing the policy changed about a request.

    ``value`` and ``previous`` are control values only. For the ``system``
    control they are the length in characters, never the text: what the
    ledger keeps is metadata.
    """

    kind: str
    field: str
    value: Any
    previous: Any
    rule: str

    @property
    def reason(self) -> str:
        """The ledger's account of what changed and why."""
        if self.field == "system":
            return (
                f"the alias policy applied the owner's default system instruction "
                f"({self.value} characters; the client specified none)"
            )
        if self.kind == KIND_CLAMPED:
            return (
                f"the alias policy clamped {self.field} from {self.previous!r} to "
                f"{self.value!r} (the owner's cap '{self.rule}')"
            )
        if self.kind == KIND_FORCED:
            return (
                f"the alias policy forced {self.field} to {self.value!r} "
                f"(the owner's deny rule '{self.rule}')"
            )
        return (
            f"the alias policy applied the owner's default {self.field} "
            f"{self.value!r} (the client specified none)"
        )


# --- reading the client's own values -----------------------------------

#: The spellings of the thinking control the reader can reconcile. A
#: spelling the gateway cannot model reads as *specified but unchangeable*
#: so the policy never layers a second, disagreeing spelling on top; the
#: bridge refuses such a request on a converted path.
_FLAT = "flat"
_NESTED = "nested"
_BOTH = "both"


def _numeric(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _client_tokens(payload: dict[str, Any], source_dialect: str) -> tuple[int | None, str | None]:
    """The client's output budget and the key it was spelled with.

    The key order matches the bridge's own reader, so the policy clamps
    the value the conversion would actually use.
    """
    if source_dialect == DIALECT_OPENAI_RESPONSES:
        value = payload.get("max_output_tokens")
        if isinstance(value, int) and not isinstance(value, bool):
            return value, "max_output_tokens"
        return None, None
    for key in ("max_completion_tokens", "max_tokens"):
        value = payload.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value, key
    return None, None


def _client_level(payload: dict[str, Any]) -> tuple[bool, str | None, str | None]:
    """The client's thinking level: ``(specified, level, spelling)``.

    Every spelling the bridge reconciles is read here - the flat
    ``reasoning_effort``, the nested ``reasoning.effort``, and the
    disabled forms ``reasoning.enabled: false`` and the level ``none``.
    A control that contradicts itself reads as specified-but-unmodelled
    (``True, None, None``): the request is the bridge's to refuse, and
    the policy neither defaults nor clamps a request it cannot read.
    """
    if "reasoning_effort" not in payload and "reasoning" not in payload:
        return False, None, None
    nested = payload.get("reasoning")
    nested = nested if isinstance(nested, dict) else None
    if "reasoning" in payload and nested is None:
        return True, None, None
    flat = payload.get("reasoning_effort")
    flat = flat if isinstance(flat, str) else None
    if "reasoning_effort" in payload and flat is None:
        return True, None, None
    effort = nested.get("effort") if nested is not None else None
    effort = effort if isinstance(effort, str) else None
    if nested is not None and "effort" in nested and effort is None:
        return True, None, None
    if nested is not None and nested.get("enabled") is False:
        if (flat or effort) not in (None, DISABLED_LEVEL):
            return True, None, None
        return True, DISABLED_LEVEL, _NESTED if effort is not None else _FLAT
    if flat is not None and effort is not None:
        if flat != effort:
            return True, None, None
        return True, flat, _BOTH
    if flat is not None:
        return True, flat, _FLAT
    if effort is not None:
        return True, effort, _NESTED
    return True, None, None


def _write_level(payload: dict[str, Any], level: str, spelling: str) -> None:
    """Write one level in the spelling the client used - never two.

    When the client specified no spelling at all (a default being
    applied), the source dialect's own spelling is chosen: the flat
    ``reasoning_effort`` for Chat Completions, the nested
    ``reasoning.effort`` for Responses.
    """
    if spelling in (_FLAT, _BOTH):
        payload["reasoning_effort"] = level
    if spelling in (_NESTED, _BOTH):
        existing = payload.get("reasoning")
        nested: dict[str, Any] = dict(existing) if isinstance(existing, dict) else {}
        nested["effort"] = level
        payload["reasoning"] = nested


def _exceeds_cap(level: str, cap: str) -> bool:
    """Whether a level sits above a cap in the canonical order.

    A level the gateway does not publish cannot be ordered, and a level
    that cannot be shown to be under the ceiling does not pass it: the
    cap is the owner's protection, and the clamp is recorded either way.
    """
    try:
        rank = CANONICAL_LEVELS.index(level)
    except ValueError:
        return True
    return rank > CANONICAL_LEVELS.index(cap)


def _client_has_system(payload: dict[str, Any], source_dialect: str) -> bool:
    """Whether the client specified its own system instruction.

    The two dialects place it differently - a ``system``/``developer``
    message for Chat Completions, ``instructions`` for Responses - and
    the policy reads the source dialect it was given.
    """
    if source_dialect == DIALECT_OPENAI_RESPONSES:
        instructions = payload.get("instructions")
        return isinstance(instructions, str) and bool(instructions.strip())
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return False
    for message in messages:
        if not isinstance(message, dict) or message.get("role") not in (ROLE_SYSTEM, ROLE_DEVELOPER):
            continue
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return True
        if isinstance(content, list) and any(
            isinstance(part, dict) and str(part.get("text") or "").strip() for part in content
        ):
            return True
    return False


# --- applying the rules -------------------------------------------------

def _apply_defaults_and_caps(
    payload: dict[str, Any],
    policy: AliasPolicy,
    source_dialect: str,
    changes: list[PolicyChange],
) -> None:
    """The client's value, then the default, then the cap - per control."""
    defaults = policy.defaults
    caps = policy.caps

    # temperature: one spelling in both dialects, and no cap in the
    # published vocabulary of the policy block.
    client = _numeric(payload.get("temperature"))
    if client is None and defaults is not None and defaults.temperature is not None:
        payload["temperature"] = defaults.temperature
        changes.append(
            PolicyChange(KIND_DEFAULTED, "temperature", defaults.temperature, None, "defaults.temperature")
        )

    # max_output_tokens: spelled per dialect, and the client's own key is
    # preserved when the cap clamps it in place.
    client_tokens, key = _client_tokens(payload, source_dialect)
    value = client_tokens
    if value is None and defaults is not None and defaults.max_output_tokens is not None:
        key = "max_output_tokens" if source_dialect == DIALECT_OPENAI_RESPONSES else "max_completion_tokens"
        value = defaults.max_output_tokens
        payload[key] = value
        changes.append(
            PolicyChange(KIND_DEFAULTED, "max_output_tokens", value, None, "defaults.max_output_tokens")
        )
    if (
        value is not None
        and key is not None
        and caps is not None
        and caps.max_output_tokens is not None
        and value > caps.max_output_tokens
    ):
        payload[key] = caps.max_output_tokens
        changes.append(
            PolicyChange(KIND_CLAMPED, "max_output_tokens", caps.max_output_tokens, value, "caps.max_output_tokens")
        )

    # thinking_level: an ordinal ceiling in the canonical order, written
    # in the one spelling the client used.
    specified, client_level, spelling = _client_level(payload)
    level = client_level
    if not specified and defaults is not None and defaults.thinking_level is not None:
        level = defaults.thinking_level
        spelling = _FLAT if source_dialect == DIALECT_OPENAI_CHAT else _NESTED
        _write_level(payload, level, spelling)
        changes.append(
            PolicyChange(KIND_DEFAULTED, "thinking_level", level, None, "defaults.thinking_level")
        )
    if level is not None and spelling is not None and caps is not None and caps.thinking_level is not None:
        if _exceeds_cap(level, caps.thinking_level):
            _write_level(payload, caps.thinking_level, spelling)
            changes.append(
                PolicyChange(KIND_CLAMPED, "thinking_level", caps.thinking_level, level, "caps.thinking_level")
            )

    # system: added only when the client specified none, in the source
    # dialect's own place for it.
    if defaults is not None and defaults.system is not None and not _client_has_system(payload, source_dialect):
        if source_dialect == DIALECT_OPENAI_RESPONSES:
            payload["instructions"] = defaults.system
        else:
            messages = payload.get("messages")
            if isinstance(messages, list):
                payload["messages"] = [{"role": ROLE_SYSTEM, "content": defaults.system}, *messages]
        # The record carries the instruction's length, never its text.
        changes.append(PolicyChange(KIND_DEFAULTED, "system", len(defaults.system), None, "defaults.system"))


def apply_alias_policy(
    alias: Alias,
    payload: dict[str, Any],
    source_dialect: str = DIALECT_OPENAI_CHAT,
) -> tuple[dict[str, Any], list[PolicyChange]]:
    """Apply the alias's policy to one request.

    Returns the request to send on and every change the policy made, in
    the order the rules were applied. Raises :class:`PolicyViolation`
    when a deny rule refuses the request outright. The input payload is
    never mutated.
    """
    policy = alias.policy
    if policy is None:
        return payload, []

    # The deny refusal comes first because it makes the rest moot: a
    # request the policy refuses must be refused before any of its other
    # rules touch it, and before the caller spends anything on it. The
    # tools test is exactly the router's own - a request "carries tools"
    # when ``bool(payload["tools"])`` - so the refusal and the capability
    # check can never disagree.
    if policy.deny is not None and policy.deny.tools and bool(payload.get("tools")):
        raise PolicyViolation(
            alias.name,
            "deny.tools",
            f"alias '{alias.name}' denies tools (policy rule 'deny.tools'); "
            "remove the tools from the request, or change the alias policy",
        )

    updated = dict(payload)
    changes: list[PolicyChange] = []
    _apply_defaults_and_caps(updated, policy, source_dialect, changes)

    # The forced value, last. `store` is dialect-neutral and top-level in
    # both dialects the gateway serves.
    deny = policy.deny
    if deny is not None and deny.store is not None and updated.get("store") is not deny.store:
        previous = updated.get("store")
        changes.append(
            PolicyChange(
                KIND_FORCED,
                "store",
                deny.store,
                previous if isinstance(previous, bool) else None,
                "deny.store",
            )
        )
        updated["store"] = deny.store

    return updated, changes
