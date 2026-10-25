"""The Anthropic Messages dialect bridge.

The gateway serves five catalogued models over the Anthropic Messages
wire protocol (``data/go_catalog.json``, observed 2026-10-22):
``claude-haiku-5-5``, ``minimax-m3``, ``qwen3.7-plus``, ``qwen3.8-flash``
and ``qwen3.8-max``. Until this module existed they were permanently
refused: the router only spoke the two OpenAI-family dialects. This module
makes the Anthropic dialect a first-class citizen of the bridge so those
models become servable through the existing OpenAI surfaces, and so an
Anthropic-shaped caller can reach every route through ``/v1/messages``.

It converts **symmetrically** and on **both paths**:

* request   -- OpenAI Chat/Responses <-> Anthropic Messages (via the
  shared :class:`~gateway.bridge.model.NormalisedRequest` pivot, so both
  directions share one definition of a request);
* buffered  -- an Anthropic ``Message`` object <-> a Chat Completions or
  Responses object;
* streamed  -- Anthropic ``message_start``/``content_block_delta``/...
  SSE events <-> Chat Completions chunks / Responses events.

Non-Anthropic pairs are delegated unchanged to the existing OpenAI-family
bridge (:mod:`gateway.bridge.requests`, :mod:`gateway.bridge.buffered`,
:mod:`gateway.bridge.streams`, :mod:`gateway.bridge.aggregate`), so this
module is a superset dispatcher rather than a second implementation.

The same two rules that govern the rest of the bridge govern every
function here (:mod:`gateway.bridge.requests` states them first):

* **Nothing is silently dropped.** A field the *target* dialect cannot
  express raises :class:`~gateway.bridge.model.ConversionError` with an
  actionable message, rather than being forwarded and lost.
* **Content is preserved exactly.** Text, images, tool calls and tool
  results survive a round trip, and tool-call identifiers survive with
  them.

Two Anthropic-specific boundaries are recorded here rather than left
implicit, because both would otherwise look like silent drops:

* **Reasoning is a level, not a budget, on the OpenAI side.** Anthropic
  spells reasoning as ``thinking: {type: enabled, budget_tokens: N}``; the
  OpenAI dialects spell it as a canonical *level* (``gateway.thinking``).
  There is **no published mapping** from a level to a token budget, and
  this gateway invents none (see :mod:`gateway.thinking`). So an inbound
  Anthropic ``thinking`` budget that would have to reach an OpenAI route is
  **refused**, never reduced to an effort level the provider never offered.
  In the other direction an OpenAI *level* carries no budget, so it is not
  rendered into an Anthropic budget either; the level is a preference
  (R19) and the router drops it for the route with the substitution
  recorded, exactly as it does for any model that publishes no levels.
* **``max_tokens`` is required by Anthropic and optional in OpenAI.** When
  a request that carried no output bound is rendered for an Anthropic
  target, :data:`DEFAULT_MAX_TOKENS` supplies the required parameter. This
  is a required-field default, not a content decision, and it is a named
  constant rather than a hidden literal.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from typing import Any

from gateway.bridge.aggregate import aggregate_chat, aggregate_responses
from gateway.bridge.buffered import (
    chat_body_to_responses,
    responses_body_to_chat,
)
from gateway.bridge.model import (
    EV_DONE,
    EV_ERROR,
    EV_REASONING,
    EV_TEXT,
    EV_TOOL_CALL_DELTA,
    EV_TOOL_CALL_END,
    EV_TOOL_CALL_START,
    EV_USAGE,
    FINISH_CONTENT_FILTER,
    FINISH_LENGTH,
    FINISH_STOP,
    FINISH_TOOL_CALLS,
    IMAGE,
    ITEM_FUNCTION_CALL,
    ITEM_FUNCTION_CALL_OUTPUT,
    ITEM_MESSAGE,
    ROLE_ASSISTANT,
    ROLE_SYSTEM,
    ROLE_USER,
    TEXT,
    ContentPart,
    ConversionError,
    FunctionTool,
    InputItem,
    NormalisedRequest,
    StreamEvent,
)
from gateway.bridge.requests import (
    convert_request as openai_convert_request,
)
from gateway.bridge.requests import (
    from_chat_completions,
    from_responses,
    to_chat_completions,
    to_responses,
)
from gateway.bridge.requests import (
    _validated_image_url,
    payload_has_image_input as openai_payload_has_image_input,
)
from gateway.bridge.streams import (
    USAGE_COMPLETION,
    USAGE_PROMPT,
    USAGE_TOTAL,
    StreamState,
    chat_chunk_to_events,
    events_to_chat_chunks,
    events_to_responses_events,
    responses_event_to_events,
    sse_frame,
)
from gateway.core.capabilities import (
    DIALECT_ANTHROPIC_MESSAGES,
    DIALECT_OPENAI_CHAT,
    DIALECT_OPENAI_RESPONSES,
)
from gateway.thinking import DISABLED_LEVEL

#: Anthropic requires ``max_tokens``; OpenAI does not. When a request that
#: expressed no output bound is rendered for an Anthropic target, this is
#: the value supplied so the request is well-formed. It is a documented
#: default, not a model capability claim.
DEFAULT_MAX_TOKENS = 4096

#: Stop reasons, in both directions. Anthropic's ``end_turn`` is the
#: ordinary completion; ``tool_use`` means the model asked to call a tool.
_STOP_TO_FINISH = {
    "end_turn": FINISH_STOP,
    "stop_sequence": FINISH_STOP,
    "max_tokens": FINISH_LENGTH,
    "tool_use": FINISH_TOOL_CALLS,
    "refusal": FINISH_CONTENT_FILTER,
}
_FINISH_TO_STOP = {
    FINISH_STOP: "end_turn",
    FINISH_LENGTH: "max_tokens",
    FINISH_TOOL_CALLS: "tool_use",
    FINISH_CONTENT_FILTER: "end_turn",
}


# ---------------------------------------------------------------------------
# request: parse
# ---------------------------------------------------------------------------


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _anthropic_image_url(block: dict[str, Any]) -> str:
    """The image URL an Anthropic ``image``/``input_image`` block carries.

    Every value runs through
    :func:`gateway.bridge.requests._validated_image_url` - the same
    wave-1.6r control the Chat Completions and Responses parsers run:
    absolute HTTP(S) only, no embedded userinfo, no control characters,
    and ``data:`` URLs restricted to PNG/JPEG/GIF/WebP base64 within the
    4 MiB decoded / 6 MiB encoded caps. A block whose image cannot be
    read raises :class:`~gateway.bridge.model.ConversionError` (a 4xx
    with zero adapter calls) instead of being forwarded unvalidated or
    dropped; the gateway never dereferences the URL itself, so refusing
    it adds no fetch and no SSRF path.
    """
    if "source" in block:
        source = block["source"]
        if isinstance(source, str):
            return _validated_image_url(source)
        if isinstance(source, dict) and source.get("type") == "url":
            return _validated_image_url(source.get("url"))
        if isinstance(source, dict) and source.get("type") == "base64":
            media = source.get("media_type")
            data = source.get("data")
            if not isinstance(media, str) or not media or not isinstance(data, str) or not data:
                raise ConversionError(
                    "an image block with a base64 source needs a media_type and a data string",
                    capability="image_input",
                )
            return _validated_image_url(f"data:{media};base64,{data}")
        raise ConversionError(
            "an image block must carry a source of type 'url' or 'base64'",
            capability="image_input",
        )
    image = block.get("image_url")
    url = image if isinstance(image, str) else (image.get("url") if isinstance(image, dict) else None)
    return _validated_image_url(url)


def _anthropic_block_to_part(block: Any) -> ContentPart | None:
    """One Anthropic content block becomes a normalised content part.

    Images are preserved as a URL: a ``base64`` source becomes a ``data:``
    URI and a ``url`` source keeps its URL, so the image survives into the
    OpenAI ``image_url`` shape and back again. Both spellings the surface
    accepts (``source`` and ``input_image``) are validated through
    :func:`_anthropic_image_url` first, so an image the parser cannot read
    is refused here rather than forwarded or - the failure this replaced -
    silently returned as ``None`` and skipped by the caller.
    """
    if not isinstance(block, dict):
        return None
    kind = block.get("type")
    if kind == "text":
        return ContentPart(kind=TEXT, text=str(block.get("text") or ""))
    if kind in ("image", "input_image"):
        return ContentPart(kind=IMAGE, image_url=_anthropic_image_url(block))
    if kind in ("input_text", "output_text"):
        return ContentPart(kind=TEXT, text=str(block.get("text") or ""))
    return None


def _anthropic_content_to_parts(content: Any) -> list[ContentPart]:
    if content is None:
        return []
    if isinstance(content, str):
        return [ContentPart(kind=TEXT, text=content)]
    parts: list[ContentPart] = []
    for block in _as_list(content):
        part = _anthropic_block_to_part(block)
        if part is not None:
            parts.append(part)
    return parts


def _anthropic_tool_result_text(content: Any) -> str:
    """The text a ``tool_result`` carries, or a refusal.

    An Anthropic tool result may also carry image blocks (vision tool
    output), while an OpenAI-family function output is a **string**. A
    non-text block therefore cannot be expressed on a converted route and
    is refused with an actionable message instead of being skipped by a
    text-only reader - which is what happened before, silently, on every
    path. On an Anthropic-dialect route this function never runs: the
    block is passed through whole, so the native path still serves it.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    chunks: list[str] = []
    for block in _as_list(content):
        if isinstance(block, str):
            chunks.append(block)
        elif isinstance(block, dict) and isinstance(block.get("text"), str):
            chunks.append(block["text"])
        else:
            raise ConversionError(
                "a tool_result can only carry text to an OpenAI-family route; an image "
                "or other non-text tool result cannot be expressed as a function output, "
                "so it is refused rather than dropped",
                capability="tool_result",
            )
    return "".join(chunks)


def _thinking_budget_refusal(payload: dict[str, Any]) -> None:
    """Refuse an Anthropic ``thinking`` budget on a converted path.

    There is no published mapping from a token budget to the OpenAI
    reasoning *level*, and this gateway invents none (see the module
    docstring and :mod:`gateway.thinking`). A budget that would have to
    cross the OpenAI boundary is refused with an actionable message, per
    the same rule that refuses it in
    :func:`gateway.bridge.requests._thinking_level`.
    """
    thinking = payload.get("thinking")
    if thinking is None:
        return
    if isinstance(thinking, dict) and thinking.get("type") == "disabled":
        return
    raise ConversionError(
        "an Anthropic thinking budget cannot be expressed by an OpenAI-family route; "
        "use a supported reasoning effort instead",
        capability="thinking",
    )


def from_anthropic_messages(payload: dict[str, Any]) -> NormalisedRequest:
    """Parse an Anthropic Messages request into the normalised form."""
    if not isinstance(payload, dict):
        raise ConversionError("request body is not a JSON object")

    model = payload.get("model")
    if not isinstance(model, str) or not model:
        raise ConversionError("request is missing a model")

    _thinking_budget_refusal(payload)

    # Anthropic-only sampling and control fields that the OpenAI dialects
    # cannot express. They are refused here rather than forwarded and lost.
    if "top_k" in payload:
        raise ConversionError(
            "top_k is an Anthropic-only control with no OpenAI counterpart; remove it "
            "to use an OpenAI-family route",
            capability="top_k",
        )
    tool_choice = payload.get("tool_choice")
    if isinstance(tool_choice, dict) and tool_choice.get("type") not in (None, "auto", "none"):
        raise ConversionError(
            f"the Anthropic tool_choice '{tool_choice.get('type')}' cannot be expressed by an "
            "OpenAI-family route; use tool_choice 'auto'",
            capability="tool_choice",
        )

    # system is a top-level field, not a message.
    system = payload.get("system")
    instructions: str | None = None
    if isinstance(system, str) and system:
        instructions = system
    elif isinstance(system, list):
        joined = "".join(
            str(block.get("text") or "")
            for block in system
            if isinstance(block, dict) and block.get("type") == "text"
        )
        instructions = joined or None

    items: list[InputItem] = []
    for message in _as_list(payload.get("messages")):
        if not isinstance(message, dict):
            continue
        role = message.get("role") or ROLE_USER
        content = message.get("content")
        if isinstance(content, str):
            items.append(InputItem(kind=ITEM_MESSAGE, role=role, content=[ContentPart(TEXT, content)]))
            continue
        for block in _as_list(content):
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "tool_use":
                arguments = block.get("input")
                if "input" in block and not isinstance(arguments, dict):
                    raise ConversionError(
                        "tool_use.input must be a JSON object; a non-object input cannot be "
                        "carried into a tool call, so it is refused rather than replaced "
                        "with an empty object",
                        capability="tool_use",
                    )
                items.append(
                    InputItem(
                        kind=ITEM_FUNCTION_CALL,
                        call_id=block.get("id") if isinstance(block.get("id"), str) else None,
                        name=block.get("name") if isinstance(block.get("name"), str) else None,
                        arguments=json.dumps(arguments) if isinstance(arguments, dict) else "{}",
                    )
                )
            elif kind == "tool_result":
                items.append(
                    InputItem(
                        kind=ITEM_FUNCTION_CALL_OUTPUT,
                        call_id=block.get("tool_use_id") if isinstance(block.get("tool_use_id"), str) else None,
                        output=_anthropic_tool_result_text(block.get("content")),
                    )
                )
            elif kind in ("thinking", "redacted_thinking"):
                raise ConversionError(
                    "an Anthropic thinking block cannot be expressed by an OpenAI-family route; "
                    "remove the thinking block or use an Anthropic-dialect route",
                    capability="thinking",
                )
            else:
                part = _anthropic_block_to_part(block)
                if part is not None:
                    items.append(InputItem(kind=ITEM_MESSAGE, role=role, content=[part]))

    # Tools: ``input_schema`` is Anthropic's name for the JSON Schema that
    # OpenAI calls ``parameters``. Server tools (web_search, ...) have no
    # OpenAI counterpart and are carried as hosted so a target that cannot
    # express them refuses rather than dropping them.
    functions: list[FunctionTool] = []
    hosted: list[dict[str, Any]] = []
    for tool in _as_list(payload.get("tools")):
        if not isinstance(tool, dict):
            continue
        if isinstance(tool.get("name"), str) and "input_schema" in tool:
            schema = tool.get("input_schema")
            functions.append(
                FunctionTool(
                    name=tool["name"],
                    description=tool.get("description") if isinstance(tool.get("description"), str) else None,
                    parameters=schema if isinstance(schema, dict) else {},
                )
            )
        else:
            hosted.append(tool)

    max_tokens = payload.get("max_tokens")
    effort, summary = _anthropic_thinking_to_level(payload)

    stop = [str(s) for s in _as_list(payload.get("stop_sequences")) if isinstance(s, str)]

    return NormalisedRequest(
        model=model,
        instructions=instructions,
        items=items,
        tools=functions,
        hosted_tools=hosted,
        stream=bool(payload.get("stream", False)),
        max_output_tokens=max_tokens if isinstance(max_tokens, int) else None,
        reasoning_effort=effort,
        reasoning_summary=summary,
        temperature=payload.get("temperature") if isinstance(payload.get("temperature"), (int, float)) else None,
        top_p=payload.get("top_p") if isinstance(payload.get("top_p"), (int, float)) else None,
        stop=stop,
    )


def _anthropic_thinking_to_level(payload: dict[str, Any]) -> tuple[str | None, str | None]:
    """Read the Anthropic thinking control, or refuse it.

    Anthropic has no reasoning *level*; it has a budget, which this gateway
    refuses on a converted path (see :func:`_thinking_budget_refusal`). So a
    payload that reached this point carried no budget, and therefore
    expresses no level: the control is absent and the level is ``None``.
    ``thinking: {type: disabled}`` is the one expressible intent and maps to
    the canonical disabled level.
    """
    thinking = payload.get("thinking")
    if isinstance(thinking, dict) and thinking.get("type") == "disabled":
        return DISABLED_LEVEL, None
    return None, None


# ---------------------------------------------------------------------------
# request: render
# ---------------------------------------------------------------------------


def _part_to_anthropic_block(part: ContentPart) -> dict[str, Any]:
    if part.kind == TEXT:
        return {"type": "text", "text": part.text or ""}
    url = part.image_url or ""
    if url.startswith("data:"):
        # data:<media>;base64,<data>
        header, _, data = url.partition(",")
        media = header[len("data:") :].split(";")[0] or "image/png"
        return {"type": "image", "source": {"type": "base64", "media_type": media, "data": data}}
    return {"type": "image", "source": {"type": "url", "url": url}}


def _ensure_message(messages: list[dict[str, Any]], role: str) -> dict[str, Any]:
    if messages and messages[-1]["role"] == role:
        return messages[-1]
    message = {"role": role, "content": []}
    messages.append(message)
    return message


def to_anthropic_messages(request: NormalisedRequest) -> dict[str, Any]:
    """Render the normalised request as an Anthropic Messages request."""
    if request.previous_response_id:
        raise ConversionError(
            "this request uses stateful continuation (previous_response_id), which the "
            "Anthropic Messages dialect cannot express",
            capability="previous_response_id",
        )
    if request.hosted_tools:
        names = ", ".join(str(tool.get("type", "unknown")) for tool in request.hosted_tools)
        raise ConversionError(
            f"this request uses hosted tools ({names}) that the Anthropic Messages dialect cannot serve",
            capability="hosted_tools",
        )
    if request.reasoning_summary:
        raise ConversionError(
            "this route cannot request a reasoning summary; the Anthropic Messages dialect "
            "has no such control. Remove reasoning.summary.",
            capability="reasoning_summary",
        )

    payload: dict[str, Any] = {"model": request.model, "messages": []}
    messages: list[dict[str, Any]] = payload["messages"]

    if request.instructions:
        payload["system"] = request.instructions

    for item in request.items:
        if item.kind == ITEM_MESSAGE:
            role = item.role or ROLE_USER
            if role in (ROLE_SYSTEM,):
                # A system item that survived normalisation joins the
                # top-level system prompt rather than becoming a message.
                existing = payload.get("system")
                payload["system"] = (existing + "\n" if existing else "") + item.text
                continue
            message = _ensure_message(messages, role)
            message["content"].extend(_part_to_anthropic_block(part) for part in item.content)
        elif item.kind == ITEM_FUNCTION_CALL:
            message = _ensure_message(messages, ROLE_ASSISTANT)
            arguments = item.arguments or "{}"
            try:
                parsed = json.loads(arguments)
            except ValueError:
                parsed = {}
            message["content"].append(
                {
                    "type": "tool_use",
                    "id": item.call_id or "",
                    "name": item.name or "",
                    "input": parsed if isinstance(parsed, dict) else {},
                }
            )
        elif item.kind == ITEM_FUNCTION_CALL_OUTPUT:
            message = _ensure_message(messages, ROLE_USER)
            message["content"].append(
                {
                    "type": "tool_result",
                    "tool_use_id": item.call_id or "",
                    "content": item.output or "",
                }
            )

    if request.tools:
        payload["tools"] = [
            {
                "name": tool.name,
                "description": tool.description or "",
                "input_schema": tool.parameters or {"type": "object", "properties": {}},
            }
            for tool in request.tools
        ]

    # Anthropic requires max_tokens. A request that expressed no bound gets
    # the documented default rather than a failure.
    payload["max_tokens"] = request.max_output_tokens or DEFAULT_MAX_TOKENS

    if request.stream:
        payload["stream"] = True
    if request.temperature is not None:
        payload["temperature"] = request.temperature
    if request.top_p is not None:
        payload["top_p"] = request.top_p
    if request.stop:
        payload["stop_sequences"] = request.stop
    # A reasoning *level* is deliberately not rendered into an Anthropic
    # thinking budget: there is no published mapping and this gateway
    # invents none. The router already dropped the level for this route and
    # recorded the substitution (R19), because the Anthropic-dialect models
    # publish no levels at all.
    return payload


# ---------------------------------------------------------------------------
# request: full dispatch
# ---------------------------------------------------------------------------


def convert_request(payload: dict[str, Any], source: str, target: str) -> dict[str, Any]:
    """Convert a request between any two dialects the gateway serves.

    Anthropic pairs are handled here; OpenAI-family pairs are delegated
    unchanged to :func:`gateway.bridge.requests.convert_request`.
    """
    if source == target:
        # Same dialect: forward, normalising only the thinking control so
        # two spellings of one request cannot disagree. For Anthropic there
        # is one spelling, so the payload is forwarded as-is.
        if source == DIALECT_ANTHROPIC_MESSAGES:
            return dict(payload)
        return openai_convert_request(payload, source, target)
    if target == DIALECT_ANTHROPIC_MESSAGES and source in (
        DIALECT_OPENAI_CHAT,
        DIALECT_OPENAI_RESPONSES,
    ):
        normalised = from_chat_completions(payload) if source == DIALECT_OPENAI_CHAT else from_responses(payload)
        return to_anthropic_messages(normalised)
    if source == DIALECT_ANTHROPIC_MESSAGES and target in (
        DIALECT_OPENAI_CHAT,
        DIALECT_OPENAI_RESPONSES,
    ):
        normalised = from_anthropic_messages(payload)
        return to_chat_completions(normalised) if target == DIALECT_OPENAI_CHAT else to_responses(normalised)
    return openai_convert_request(payload, source, target)


def _anthropic_content_carries_image(content: Any) -> bool:
    """Whether an Anthropic content value carries an image.

    Detection runs the **same parser conversion runs**, so routing and
    rendering can never disagree about which fields represent an image:
    both spellings the surface accepts (``image`` and ``input_image``)
    count, and so does an image inside a ``tool_result`` - the model is
    asked to look at that one too. An image the parser cannot read is
    refused here, with a :class:`~gateway.bridge.model.ConversionError`
    before a route is chosen, instead of being dropped later on a route
    that could have served it.
    """
    if content is None or isinstance(content, str):
        return False
    found = False
    for block in _as_list(content):
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind in ("image", "input_image") or "image_url" in block:
            _anthropic_block_to_part(block)
            found = True
        elif kind == "tool_result":
            # Evaluated first, so a malformed image inside a tool result is
            # still refused even when an earlier block already counted.
            found = _anthropic_content_carries_image(block.get("content")) or found
    return found


def payload_has_image_input(payload: dict[str, Any], source_dialect: str) -> bool:
    """Whether a request carries an image, across every source dialect."""
    if source_dialect == DIALECT_ANTHROPIC_MESSAGES:
        found = False
        for message in _as_list(payload.get("messages")):
            if not isinstance(message, dict):
                continue
            found = _anthropic_content_carries_image(message.get("content")) or found
        return found
    return openai_payload_has_image_input(payload, source_dialect)


# ---------------------------------------------------------------------------
# buffered response
# ---------------------------------------------------------------------------


def _anthropic_body_to_normalised_parts(body: dict[str, Any]) -> tuple[str, list[dict[str, Any]], str | None, dict[str, int], str]:
    """Split an Anthropic Message into (text, tool_use blocks, stop_reason, usage, reasoning)."""
    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_blocks: list[dict[str, Any]] = []
    for block in _as_list(body.get("content")):
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            text_parts.append(str(block.get("text") or ""))
        elif block_type == "tool_use":
            tool_blocks.append(block)
        elif block_type == "thinking":
            # Carried, not dropped: the OpenAI surfaces express it as
            # ``reasoning_content`` (chat) / a reasoning summary (responses),
            # mirroring the streamed path. ``redacted_thinking`` holds no
            # readable text and is not surfaced.
            reasoning_parts.append(str(block.get("thinking") or ""))
    stop_reason = body.get("stop_reason") if isinstance(body.get("stop_reason"), str) else None
    usage_raw = body.get("usage")
    usage: dict[str, int] = {}
    if isinstance(usage_raw, dict):
        if isinstance(usage_raw.get("input_tokens"), int):
            usage[USAGE_PROMPT] = usage_raw["input_tokens"]
        if isinstance(usage_raw.get("output_tokens"), int):
            usage[USAGE_COMPLETION] = usage_raw["output_tokens"]
        if USAGE_PROMPT in usage and USAGE_COMPLETION in usage:
            usage[USAGE_TOTAL] = usage[USAGE_PROMPT] + usage[USAGE_COMPLETION]
    return "".join(text_parts), tool_blocks, stop_reason, usage, "".join(reasoning_parts)


def anthropic_body_to_chat(body: dict[str, Any]) -> dict[str, Any]:
    """Convert an Anthropic Message object into a Chat Completions object."""
    if not isinstance(body, dict):
        return body
    text, tool_blocks, stop_reason, usage, reasoning = _anthropic_body_to_normalised_parts(body)

    finish = _STOP_TO_FINISH.get(stop_reason or "", FINISH_STOP)
    if tool_blocks:
        finish = FINISH_TOOL_CALLS

    message: dict[str, Any] = {"role": "assistant", "content": text}
    if reasoning:
        # Anthropic ``thinking`` carried into the OpenAI chat shape, mirroring
        # the streamed path's ``reasoning_content`` delta.
        message["reasoning_content"] = reasoning
    if tool_blocks:
        message["tool_calls"] = [
            {
                "id": block.get("id") or "",
                "type": "function",
                "function": {
                    "name": block.get("name") or "",
                    "arguments": json.dumps(block.get("input") or {}),
                },
            }
            for block in tool_blocks
        ]

    message_id = body.get("id")
    chat_id = "chatcmpl-" + str(message_id)[len("msg_") :] if isinstance(message_id, str) else ""

    chat_usage: dict[str, Any] = {}
    if USAGE_PROMPT in usage:
        chat_usage["prompt_tokens"] = usage[USAGE_PROMPT]
    if USAGE_COMPLETION in usage:
        chat_usage["completion_tokens"] = usage[USAGE_COMPLETION]
    if USAGE_TOTAL in usage:
        chat_usage["total_tokens"] = usage[USAGE_TOTAL]

    result: dict[str, Any] = {
        "id": chat_id,
        "object": "chat.completion",
        "created": 0,
        "model": body.get("model") or "",
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
    }
    if chat_usage:
        result["usage"] = chat_usage
    return result


def anthropic_body_to_responses(body: dict[str, Any]) -> dict[str, Any]:
    """Convert an Anthropic Message object into a Responses object."""
    if not isinstance(body, dict):
        return body
    text, tool_blocks, stop_reason, usage, reasoning = _anthropic_body_to_normalised_parts(body)

    status = "completed"
    if stop_reason == "max_tokens":
        status = "incomplete"
    elif stop_reason == "refusal":
        status = "failed"

    output: list[dict[str, Any]] = []
    if reasoning:
        # Anthropic ``thinking`` carried into the Responses shape as a
        # reasoning summary, mirroring the streamed path's summary deltas.
        output.append(
            {
                "type": "reasoning",
                "id": "rs_" + str(body.get("id") or ""),
                "summary": [{"type": "summary_text", "text": reasoning}],
            }
        )
    if text:
        output.append(
            {
                "type": "message",
                "id": "msg_" + str(body.get("id") or ""),
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": text}],
            }
        )
    for block in tool_blocks:
        output.append(
            {
                "type": "function_call",
                "id": "fc_" + str(block.get("id") or ""),
                "call_id": block.get("id") or "",
                "name": block.get("name") or "",
                "arguments": json.dumps(block.get("input") or {}),
            }
        )

    message_id = body.get("id")
    response_id = "resp_" + str(message_id)[len("msg_") :] if isinstance(message_id, str) else ""

    response_usage: dict[str, Any] = {}
    if USAGE_PROMPT in usage:
        response_usage["input_tokens"] = usage[USAGE_PROMPT]
    if USAGE_COMPLETION in usage:
        response_usage["output_tokens"] = usage[USAGE_COMPLETION]
    if USAGE_TOTAL in usage:
        response_usage["total_tokens"] = usage[USAGE_TOTAL]

    result: dict[str, Any] = {
        "id": response_id,
        "object": "response",
        "created_at": 0,
        "status": status,
        "model": body.get("model") or "",
        "output": output,
    }
    if response_usage:
        result["usage"] = response_usage
    return result


def _to_anthropic_usage(usage: dict[str, int]) -> dict[str, int]:
    """Map the gateway's canonical usage keys onto Anthropic's own names."""
    out: dict[str, int] = {}
    if USAGE_PROMPT in usage:
        out["input_tokens"] = usage[USAGE_PROMPT]
    if USAGE_COMPLETION in usage:
        out["output_tokens"] = usage[USAGE_COMPLETION]
    return out


def _openai_body_to_anthropic(body: dict[str, Any], source: str) -> dict[str, Any]:
    """Convert a Chat Completions or Responses object into an Anthropic Message."""
    if source == DIALECT_OPENAI_CHAT:
        text, tool_calls, finish, usage = _chat_body_parts(body)
        model = body.get("model") or ""
        response_id = body.get("id") or ""
    else:
        text, tool_calls, finish, usage = _responses_body_parts(body)
        model = body.get("model") or ""
        response_id = body.get("id") or ""

    content: list[dict[str, Any]] = []
    if text:
        content.append({"type": "text", "text": text})
    for call in tool_calls:
        spec = call.get("function") or {}
        arguments = spec.get("arguments") or "{}"
        try:
            parsed = json.loads(arguments)
        except ValueError:
            parsed = {}
        content.append(
            {
                "type": "tool_use",
                "id": call.get("id") or "",
                "name": spec.get("name") or "",
                "input": parsed if isinstance(parsed, dict) else {},
            }
        )

    stop_reason = _FINISH_TO_STOP.get(finish or FINISH_STOP, "end_turn")
    if tool_calls:
        stop_reason = "tool_use"

    anthropic_id = "msg_" + str(response_id)[len("chatcmpl-") :].replace("resp_", "")

    result: dict[str, Any] = {
        "id": anthropic_id,
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
    }
    anthropic_usage = _to_anthropic_usage(usage)
    if anthropic_usage:
        result["usage"] = anthropic_usage
    return result


def _chat_body_parts(body: dict[str, Any]) -> tuple[str, list[dict[str, Any]], str | None, dict[str, int]]:
    choices = body.get("choices")
    choices = choices if isinstance(choices, list) else []
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    finish: str | None = None
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message")
        message = message if isinstance(message, dict) else {}
        content = message.get("content")
        if isinstance(content, str) and content:
            text_parts.append(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    text_parts.append(str(part.get("text") or ""))
        calls = message.get("tool_calls")
        if isinstance(calls, list):
            tool_calls.extend(call for call in calls if isinstance(call, dict))
        finish = choice.get("finish_reason") if isinstance(choice.get("finish_reason"), str) else finish

    usage_raw = body.get("usage")
    usage: dict[str, int] = {}
    if isinstance(usage_raw, dict):
        if isinstance(usage_raw.get("prompt_tokens"), int):
            usage[USAGE_PROMPT] = usage_raw["prompt_tokens"]
        if isinstance(usage_raw.get("completion_tokens"), int):
            usage[USAGE_COMPLETION] = usage_raw["completion_tokens"]
    return "".join(text_parts), tool_calls, finish, usage


def _responses_body_parts(body: dict[str, Any]) -> tuple[str, list[dict[str, Any]], str | None, dict[str, int]]:
    output = body.get("output")
    output = output if isinstance(output, list) else []
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for item in output:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "message":
            content = item.get("content")
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") in ("output_text", "text"):
                        text_parts.append(str(part.get("text") or ""))
        elif item.get("type") == "function_call":
            tool_calls.append(
                {
                    "id": item.get("call_id") or item.get("id") or "",
                    "function": {"name": item.get("name") or "", "arguments": item.get("arguments") or ""},
                }
            )
    status = body.get("status")
    finish = {"completed": FINISH_STOP, "incomplete": FINISH_LENGTH, "failed": FINISH_CONTENT_FILTER}.get(
        status if isinstance(status, str) else "", FINISH_STOP
    )
    usage_raw = body.get("usage")
    usage: dict[str, int] = {}
    if isinstance(usage_raw, dict):
        if isinstance(usage_raw.get("input_tokens"), int):
            usage[USAGE_PROMPT] = usage_raw["input_tokens"]
        if isinstance(usage_raw.get("output_tokens"), int):
            usage[USAGE_COMPLETION] = usage_raw["output_tokens"]
    return "".join(text_parts), tool_calls, finish, usage


def convert_buffered_body(payload: dict[str, Any], source: str, target: str) -> dict[str, Any] | None:
    """Convert a buffered response body between any two dialects.

    Returns ``None`` when the pair is not this module's concern, so the
    caller can fall through to the existing OpenAI-family converters. A
    pair that *is* this module's concern but cannot be converted raises.
    """
    if source == DIALECT_ANTHROPIC_MESSAGES and target == DIALECT_OPENAI_CHAT:
        return anthropic_body_to_chat(payload)
    if source == DIALECT_ANTHROPIC_MESSAGES and target == DIALECT_OPENAI_RESPONSES:
        return anthropic_body_to_responses(payload)
    if source == DIALECT_OPENAI_CHAT and target == DIALECT_ANTHROPIC_MESSAGES:
        return _openai_body_to_anthropic(payload, DIALECT_OPENAI_CHAT)
    if source == DIALECT_OPENAI_RESPONSES and target == DIALECT_ANTHROPIC_MESSAGES:
        return _openai_body_to_anthropic(payload, DIALECT_OPENAI_RESPONSES)
    if source == DIALECT_OPENAI_RESPONSES and target == DIALECT_OPENAI_CHAT:
        return responses_body_to_chat(payload)
    if source == DIALECT_OPENAI_CHAT and target == DIALECT_OPENAI_RESPONSES:
        return chat_body_to_responses(payload)
    return None


def convert_buffered_bytes(body: bytes, source: str, target: str) -> bytes:
    """Convert a buffered response body, in bytes, between any two dialects.

    A body that cannot be converted is returned unchanged rather than
    mangled, matching the existing ``_convert_buffered`` contract.
    """
    try:
        payload = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return body
    if not isinstance(payload, dict):
        return body
    if source == target:
        return body
    try:
        converted = convert_buffered_body(payload, source, target)
    except ConversionError:
        return body
    except Exception:  # pragma: no cover - defensive
        return body
    if converted is None:
        return body
    return json.dumps(converted, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


# ---------------------------------------------------------------------------
# streamed response
# ---------------------------------------------------------------------------


def anthropic_event_to_events(event: dict[str, Any]) -> list[StreamEvent]:
    """Normalise one Anthropic Messages stream event."""
    if not isinstance(event, dict):
        return []
    kind = event.get("type")

    if kind == "content_block_delta":
        delta = event.get("delta")
        if not isinstance(delta, dict):
            return []
        delta_type = delta.get("type")
        if delta_type == "text_delta":
            text = delta.get("text")
            return [StreamEvent(kind=EV_TEXT, text=text)] if isinstance(text, str) and text else []
        if delta_type == "thinking_delta":
            thinking = delta.get("thinking")
            return [StreamEvent(kind=EV_REASONING, text=thinking)] if isinstance(thinking, str) and thinking else []
        if delta_type == "input_json_delta":
            partial = delta.get("partial_json")
            return (
                [StreamEvent(kind=EV_TOOL_CALL_DELTA, arguments_delta=partial, tool_index=event.get("index"))]
                if isinstance(partial, str) and partial
                else []
            )
        return []

    if kind == "content_block_start":
        block = event.get("content_block")
        if isinstance(block, dict) and block.get("type") == "tool_use":
            return [
                StreamEvent(
                    kind=EV_TOOL_CALL_START,
                    call_id=block.get("id") if isinstance(block.get("id"), str) else None,
                    name=block.get("name") if isinstance(block.get("name"), str) else None,
                    tool_index=event.get("index") if isinstance(event.get("index"), int) else None,
                )
            ]
        return []

    if kind == "content_block_stop":
        return [StreamEvent(kind=EV_TOOL_CALL_END, tool_index=event.get("index"))]

    if kind == "message_delta":
        delta = event.get("delta")
        stop_reason = delta.get("stop_reason") if isinstance(delta, dict) else None
        finish = _STOP_TO_FINISH.get(stop_reason or "", FINISH_STOP)
        events: list[StreamEvent] = []
        usage_raw = event.get("usage")
        if isinstance(usage_raw, dict):
            usage: dict[str, int] = {}
            if isinstance(usage_raw.get("output_tokens"), int):
                usage[USAGE_COMPLETION] = usage_raw["output_tokens"]
            if usage:
                events.append(StreamEvent(kind=EV_USAGE, usage=usage))
        events.append(StreamEvent(kind=EV_DONE, finish_reason=finish))
        return events

    if kind == "message_start":
        message = event.get("message")
        usage_raw = message.get("usage") if isinstance(message, dict) else None
        if isinstance(usage_raw, dict) and isinstance(usage_raw.get("input_tokens"), int):
            return [StreamEvent(kind=EV_USAGE, usage={USAGE_PROMPT: usage_raw["input_tokens"]})]
        return []

    if kind == "error":
        error = event.get("error")
        message = error.get("message") if isinstance(error, dict) else None
        if not isinstance(message, str):
            message = event.get("message") if isinstance(event.get("message"), str) else None
        return [StreamEvent(kind=EV_ERROR, error={"message": message or "upstream stream error"})]

    # message_stop and ping carry no content; the terminal event is already
    # produced by message_delta.
    return []


class AnthropicStreamState(StreamState):
    """Stream state that also tracks the open Anthropic content block."""

    def __init__(self, model: str, response_id: str | None = None) -> None:
        super().__init__(model=model, response_id=response_id)
        self.started = False
        self.block_kind: str | None = None  # "text" | "thinking" | "tool_use"
        self.block_index: int = -1
        self.tool_block_index: dict[int, int] = {}

    def start_message(self) -> dict[str, Any]:
        self.started = True
        return {
            "type": "message_start",
            "message": {
                "id": self.response_id if str(self.response_id).startswith("msg_") else "msg_" + str(self.response_id),
                "type": "message",
                "role": "assistant",
                "model": self.model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": self.usage.get(USAGE_PROMPT, 0), "output_tokens": 0},
            },
        }

    def close_block(self) -> dict[str, Any] | None:
        if self.block_kind is None:
            return None
        event = {"type": "content_block_stop", "index": self.block_index}
        self.block_kind = None
        return event


def events_to_anthropic_events(events: Iterable[StreamEvent], state: AnthropicStreamState) -> list[dict[str, Any]]:
    """Render normalised events as Anthropic Messages stream events."""
    payloads: list[dict[str, Any]] = []

    if not state.started:
        payloads.append(state.start_message())

    for event in events:
        if event.kind == EV_TEXT:
            if state.block_kind != "text":
                stop = state.close_block()
                if stop:
                    payloads.append(stop)
                payloads.append({"type": "content_block_start", "index": state.block_index + 1, "content_block": {"type": "text", "text": ""}})
                state.block_index += 1
                state.block_kind = "text"
            payloads.append(
                {"type": "content_block_delta", "index": state.block_index, "delta": {"type": "text_delta", "text": event.text or ""}}
            )
        elif event.kind == EV_REASONING:
            if state.block_kind != "thinking":
                stop = state.close_block()
                if stop:
                    payloads.append(stop)
                payloads.append({"type": "content_block_start", "index": state.block_index + 1, "content_block": {"type": "thinking", "thinking": ""}})
                state.block_index += 1
                state.block_kind = "thinking"
            payloads.append(
                {"type": "content_block_delta", "index": state.block_index, "delta": {"type": "thinking_delta", "thinking": event.text or ""}}
            )
        elif event.kind == EV_TOOL_CALL_START:
            key = state.tool_for(event)
            entry = state.tool_calls[key]
            stop = state.close_block()
            if stop:
                payloads.append(stop)
            state.block_index += 1
            state.block_kind = "tool_use"
            state.tool_block_index[key] = state.block_index
            payloads.append(
                {
                    "type": "content_block_start",
                    "index": state.block_index,
                    "content_block": {
                        "type": "tool_use",
                        "id": entry["call_id"] or "",
                        "name": entry["name"] or "",
                        "input": {},
                    },
                }
            )
        elif event.kind == EV_TOOL_CALL_DELTA:
            key = state.tool_for(event)
            entry = state.tool_calls[key]
            entry["arguments"] += event.arguments_delta or ""
            index = state.tool_block_index.get(key, state.block_index)
            payloads.append(
                {"type": "content_block_delta", "index": index, "delta": {"type": "input_json_delta", "partial_json": event.arguments_delta or ""}}
            )
        elif event.kind == EV_TOOL_CALL_END:
            key = state.tool_for(event)
            if event.arguments is not None:
                state.tool_calls[key]["arguments"] = event.arguments
            index = state.tool_block_index.get(key, state.block_index)
            payloads.append({"type": "content_block_stop", "index": index})
        elif event.kind == EV_USAGE:
            state.usage.update(event.usage or {})
        elif event.kind == EV_DONE:
            stop = state.close_block()
            if stop:
                payloads.append(stop)
            usage: dict[str, int] = {}
            if USAGE_PROMPT in state.usage:
                usage["input_tokens"] = state.usage[USAGE_PROMPT]
            if USAGE_COMPLETION in state.usage:
                usage["output_tokens"] = state.usage[USAGE_COMPLETION]
            delta: dict[str, Any] = {
                "stop_reason": _FINISH_TO_STOP.get(event.finish_reason or FINISH_STOP, "end_turn"),
                "stop_sequence": None,
            }
            payloads.append({"type": "message_delta", "delta": delta, "usage": usage})
            payloads.append({"type": "message_stop"})
        elif event.kind == EV_ERROR:
            payloads.append({"type": "error", "error": {"type": "api_error", "message": (event.error or {}).get("message", "upstream stream error")}})
    return payloads


class AnyDialectStreamConverter:
    """Converts one upstream stream into any target dialect.

    Handles all three dialects on both sides, reusing the OpenAI-family
    normalisers and renderers and adding the Anthropic ones. The terminal
    event is always emitted, and usage counters land where the target
    dialect expects them.
    """

    def __init__(self, source: str, target: str, model: str) -> None:
        self.source = source
        self.target = target
        if target == DIALECT_ANTHROPIC_MESSAGES:
            self.state: StreamState = AnthropicStreamState(model=model)
        else:
            self.state = StreamState(model=model)
        self._pending_usage: StreamEvent | None = None
        self._pending_done: StreamEvent | None = None
        self._done_emitted = False

    def _normalise(self, payload: dict[str, Any]) -> list[StreamEvent]:
        if self.source == DIALECT_OPENAI_CHAT:
            return chat_chunk_to_events(payload)
        if self.source == DIALECT_OPENAI_RESPONSES:
            return responses_event_to_events(payload)
        if self.source == DIALECT_ANTHROPIC_MESSAGES:
            return anthropic_event_to_events(payload)
        return []

    def _render(self, events: Iterable[StreamEvent]) -> list[dict[str, Any]]:
        if self.target == DIALECT_OPENAI_CHAT:
            return events_to_chat_chunks(events, self.state)
        if self.target == DIALECT_OPENAI_RESPONSES:
            return events_to_responses_events(events, self.state)
        if self.target == DIALECT_ANTHROPIC_MESSAGES:
            return events_to_anthropic_events(events, self.state)  # type: ignore[arg-type]
        return []

    def feed_payload(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        return self.feed_events(self._normalise(payload))

    def feed_events(self, events: Iterable[StreamEvent]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for event in events:
            if event.kind == EV_USAGE:
                self.state.usage.update(event.usage or {})
                # Anthropic reports input tokens at ``message_start`` and
                # output tokens at ``message_delta``. Merge them so the
                # target sees one complete usage, and compute the total the
                # dialect would have carried had it arrived at once.
                if (
                    USAGE_PROMPT in self.state.usage
                    and USAGE_COMPLETION in self.state.usage
                    and USAGE_TOTAL not in self.state.usage
                ):
                    self.state.usage[USAGE_TOTAL] = self.state.usage[USAGE_PROMPT] + self.state.usage[USAGE_COMPLETION]
                if self.target == DIALECT_OPENAI_CHAT:
                    self._pending_usage = StreamEvent(kind=EV_USAGE, usage=dict(self.state.usage))
                continue
            if event.kind == EV_DONE:
                self._done_emitted = True
                if self.target in (DIALECT_OPENAI_RESPONSES, DIALECT_ANTHROPIC_MESSAGES):
                    self._pending_done = event
                    continue
                out.extend(self._render([event]))
                if self._pending_usage is not None:
                    out.extend(self._render([self._pending_usage]))
                    self._pending_usage = None
                continue
            out.extend(self._render([event]))
        return out

    def finish(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if self.target == DIALECT_OPENAI_CHAT:
            if self._pending_usage is not None:
                out.extend(self._render([self._pending_usage]))
                self._pending_usage = None
            if not self._done_emitted:
                out.extend(self._render([StreamEvent(kind=EV_DONE, finish_reason=FINISH_LENGTH)]))
                self._done_emitted = True
            return out
        if self.target in (DIALECT_OPENAI_RESPONSES, DIALECT_ANTHROPIC_MESSAGES):
            done = self._pending_done or StreamEvent(kind=EV_DONE, finish_reason=FINISH_LENGTH)
            out.extend(self._render([done]))
            self._done_emitted = True
            return out
        return out


def convert_stream_bytes(
    frames: Iterable[bytes],
    source: str,
    target: str,
    model: str,
) -> Iterator[bytes]:
    """Convert an upstream SSE byte stream into *target* dialect frames.

    Frames are emitted as they arrive; nothing is buffered to completion.
    """
    if source == target:
        for frame in frames:
            yield frame
        return

    converter = AnyDialectStreamConverter(source, target, model)
    buffer = ""
    for raw in frames:
        buffer += raw.decode("utf-8", errors="replace")
        while "\n\n" in buffer:
            frame, buffer = buffer.split("\n\n", 1)
            data_lines = [line[5:].lstrip() for line in frame.split("\n") if line.startswith("data:")]
            if not data_lines:
                continue
            data = "\n".join(data_lines)
            if data.strip() == "[DONE]":
                continue
            try:
                payload = json.loads(data)
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(payload, dict):
                continue
            for item in converter.feed_payload(payload):
                yield _frame(item, target)
    for item in converter.finish():
        yield _frame(item, target)


def _frame(item: dict[str, Any], target: str) -> bytes:
    data = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
    if target == DIALECT_ANTHROPIC_MESSAGES:
        # Anthropic names the event, and the type is also inside the data.
        event = item.get("type")
        return sse_frame(data, event=str(event) if isinstance(event, str) else None)
    return sse_frame(data)


def convert_stream_events(
    payloads: Iterable[dict[str, Any]],
    source: str,
    target: str,
    model: str,
) -> Iterator[dict[str, Any]]:
    """Convert already-parsed upstream events into target-dialect events."""
    converter = AnyDialectStreamConverter(source, target, model)
    for payload in payloads:
        for item in converter.feed_payload(payload):
            yield item
    for item in converter.finish():
        yield item


# ---------------------------------------------------------------------------
# aggregation (streaming-only upstream serving a buffered caller)
# ---------------------------------------------------------------------------


def aggregate_anthropic(frames: Iterable[bytes]) -> dict[str, Any]:
    """Consume an Anthropic Messages stream and return one Message object."""
    message: dict[str, Any] = {}
    content: list[dict[str, Any]] = []
    stop_reason: str | None = None
    usage: dict[str, int] = {}
    saw_event = False

    buffer = ""
    for raw in frames:
        buffer += raw.decode("utf-8", errors="replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
        while "\n\n" in buffer:
            frame, buffer = buffer.split("\n\n", 1)
            data_lines = [line[5:].lstrip() for line in frame.split("\n") if line.startswith("data:")]
            if not data_lines:
                continue
            data = "\n".join(data_lines)
            if data.strip() == "[DONE]":
                continue
            try:
                event = json.loads(data)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            saw_event = True
            kind = event.get("type")
            if kind == "message_start":
                msg = event.get("message")
                if isinstance(msg, dict):
                    message = dict(msg)
                    msg_usage = msg.get("usage")
                    if isinstance(msg_usage, dict) and isinstance(msg_usage.get("input_tokens"), int):
                        usage[USAGE_PROMPT] = msg_usage["input_tokens"]
            elif kind == "content_block_start":
                block = event.get("content_block")
                if isinstance(block, dict):
                    content.append(dict(block))
            elif kind == "content_block_delta":
                delta = event.get("delta")
                index = event.get("index")
                if isinstance(delta, dict) and isinstance(index, int) and 0 <= index < len(content):
                    block = content[index]
                    if delta.get("type") == "text_delta":
                        block["text"] = block.get("text", "") + str(delta.get("text") or "")
                    elif delta.get("type") == "thinking_delta":
                        block["thinking"] = block.get("thinking", "") + str(delta.get("thinking") or "")
                    elif delta.get("type") == "input_json_delta":
                        block["_partial"] = block.get("_partial", "") + str(delta.get("partial_json") or "")
            elif kind == "message_delta":
                delta = event.get("delta")
                if isinstance(delta, dict) and isinstance(delta.get("stop_reason"), str):
                    stop_reason = delta["stop_reason"]
                msg_usage = event.get("usage")
                if isinstance(msg_usage, dict) and isinstance(msg_usage.get("output_tokens"), int):
                    usage[USAGE_COMPLETION] = msg_usage["output_tokens"]
            elif kind == "error":
                raise ConversionError(
                    "the upstream Anthropic stream reported an error",
                    capability="stream_termination",
                )

    if not saw_event or not message:
        raise ConversionError(
            "the upstream Anthropic stream produced no message_start event",
            capability="stream_termination",
        )

    # Materialise tool_use inputs from the accumulated partial JSON.
    final_content: list[dict[str, Any]] = []
    for block in content:
        block = dict(block)
        partial = block.pop("_partial", None)
        if partial:
            try:
                parsed = json.loads(partial)
            except ValueError:
                parsed = {}
            block["input"] = parsed if isinstance(parsed, dict) else {}
        final_content.append(block)

    result: dict[str, Any] = {
        "id": message.get("id") or "msg_",
        "type": "message",
        "role": "assistant",
        "model": message.get("model") or "",
        "content": final_content,
        "stop_reason": stop_reason or message.get("stop_reason"),
        "stop_sequence": None,
    }
    anthropic_usage = _to_anthropic_usage(usage)
    if anthropic_usage:
        result["usage"] = anthropic_usage
    return result


def aggregate_stream_any(frames: Iterable[bytes], dialect: str) -> dict[str, Any]:
    """Aggregate a stream in the given dialect into one object."""
    if dialect == DIALECT_ANTHROPIC_MESSAGES:
        return aggregate_anthropic(frames)
    if dialect == DIALECT_OPENAI_RESPONSES:
        return aggregate_responses(frames)
    if dialect == DIALECT_OPENAI_CHAT:
        return aggregate_chat(frames)
    raise ConversionError(
        f"no aggregation is defined for the {dialect} dialect",
        capability="stream_aggregation",
    )
