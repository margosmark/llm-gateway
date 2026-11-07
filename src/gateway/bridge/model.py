"""Normalised request and stream models.

The bridge converts between the two OpenAI-family dialects. It does so
through an intermediate *normalised* representation rather than by mapping
field to field, so that both directions share one definition of what a
request means and one definition of what an event means. That is what makes
round-trip invariants testable.

Normalisation is lossy in exactly one direction, and that direction is
explicit: a Responses request can carry stateful continuation
(``previous_response_id``) and hosted tools, which a Chat Completions
request cannot express. When a request needs a capability the *target*
dialect cannot express, the conversion refuses rather than dropping it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class ConversionError(Exception):
    """Raised when a request cannot be converted safely.

    The message is user-facing and must never contain request content or
    credential material.
    """

    def __init__(self, message: str, capability: str | None = None) -> None:
        # Bridge sites interpolate the offending client value into the
        # message. A lone surrogate from a JSON "\ud800" escape cannot be
        # UTF-8 encoded, so verbatim text would poison every downstream
        # consumer: attempts[] reasons in the ledger ring, the owner-console
        # state render, the error envelope. backslashreplace rewrites the
        # surrogate to literal escape text and leaves every encodable
        # character byte-identical (F9).
        message = message.encode("utf-8", "backslashreplace").decode("utf-8")
        super().__init__(message)
        self.message = message
        self.capability = capability


# --- content -----------------------------------------------------------

TEXT = "text"
IMAGE = "image"


@dataclass
class ContentPart:
    kind: str
    text: str | None = None
    image_url: str | None = None
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        if self.kind == TEXT:
            return {"type": TEXT, "text": self.text or ""}
        return {"type": IMAGE, "image_url": self.image_url, "detail": self.detail}


# --- items -------------------------------------------------------------

ITEM_MESSAGE = "message"
ITEM_FUNCTION_CALL = "function_call"
ITEM_FUNCTION_CALL_OUTPUT = "function_call_output"

ROLE_SYSTEM = "system"
ROLE_DEVELOPER = "developer"
ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"
ROLE_TOOL = "tool"


@dataclass
class InputItem:
    kind: str
    role: str | None = None
    content: list[ContentPart] = field(default_factory=list)
    call_id: str | None = None
    name: str | None = None
    arguments: str | None = None
    output: str | None = None

    @property
    def text(self) -> str:
        return "".join(part.text or "" for part in self.content if part.kind == TEXT)


@dataclass
class FunctionTool:
    name: str
    description: str | None = None
    parameters: dict[str, Any] = field(default_factory=dict)


@dataclass
class NormalisedRequest:
    """A request expressed in a dialect-neutral way."""

    model: str
    instructions: str | None = None
    items: list[InputItem] = field(default_factory=list)
    tools: list[FunctionTool] = field(default_factory=list)
    #: Hosted (built-in) tools, carried so that a conversion which cannot
    #: express them can *refuse* rather than silently drop them.
    hosted_tools: list[dict[str, Any]] = field(default_factory=list)
    stream: bool = False
    max_output_tokens: int | None = None
    #: The canonical thinking level: a level from
    #: ``gateway.thinking.CANONICAL_LEVELS``, or ``DISABLED_LEVEL`` for
    #: "do not reason". Carried so a conversion renders it per dialect
    #: instead of losing the control.
    reasoning_effort: str | None = None
    #: A Responses-dialect reasoning *summary* request. It is carried
    #: rather than dropped, because Chat Completions cannot express it:
    #: the target dialect refuses it instead of ignoring it.
    reasoning_summary: str | None = None
    temperature: float | None = None
    top_p: float | None = None
    stop: list[str] = field(default_factory=list)
    previous_response_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)

    def system_text(self) -> str | None:
        """System/developer content, which the two dialects place differently."""
        chunks = [
            item.text
            for item in self.items
            if item.kind == ITEM_MESSAGE and item.role in (ROLE_SYSTEM, ROLE_DEVELOPER)
        ]
        joined = "\n".join(chunk for chunk in chunks if chunk)
        return joined or None


# --- stream events -----------------------------------------------------

EV_TEXT = "text"
EV_REASONING = "reasoning"
EV_TOOL_CALL_START = "tool_call_start"
EV_TOOL_CALL_DELTA = "tool_call_delta"
EV_TOOL_CALL_END = "tool_call_end"
EV_USAGE = "usage"
EV_DONE = "done"
EV_ERROR = "error"


@dataclass
class StreamEvent:
    """One normalised thing that happened upstream."""

    kind: str
    text: str | None = None
    call_id: str | None = None
    tool_index: int | None = None
    name: str | None = None
    arguments_delta: str | None = None
    arguments: str | None = None
    usage: dict[str, int] | None = None
    finish_reason: str | None = None
    error: dict[str, Any] | None = None
    response_id: str | None = None
    model: str | None = None
    created: int | None = None


# Chat Completions finish reasons, and their Responses equivalents.
FINISH_STOP = "stop"
FINISH_LENGTH = "length"
FINISH_TOOL_CALLS = "tool_calls"
FINISH_CONTENT_FILTER = "content_filter"

_FINISH_TO_RESPONSES = {
    FINISH_STOP: "completed",
    FINISH_LENGTH: "incomplete",
    FINISH_TOOL_CALLS: "completed",
    FINISH_CONTENT_FILTER: "failed",
}

_FINISH_FROM_RESPONSES = {
    "completed": FINISH_STOP,
    "incomplete": FINISH_LENGTH,
    "failed": FINISH_CONTENT_FILTER,
}
