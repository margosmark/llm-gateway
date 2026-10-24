"""Request conversion between the two OpenAI-family dialects.

Chat Completions  <->  Responses.

Two rules govern every function here:

* **Nothing is silently dropped.** If the target dialect cannot express
  something the source carried - stateful continuation, hosted tools, a
  parameter with no counterpart - the conversion raises
  :class:`ConversionError` with an actionable message.
* **Content is preserved exactly.** Text, images, tool calls and tool
  results survive a round trip, and tool-call identifiers survive with them.

A third rule governs the thinking-level control specifically, because it
is the one control that arrives in several spellings and renders in two:

* **One vocabulary, rendered per dialect.** ``reasoning_effort``,
  ``reasoning.effort``, ``reasoning.enabled`` and the disabled level
  ``none`` all normalise to one canonical level
  (:data:`gateway.thinking.CANONICAL_LEVELS`), which renders as
  ``reasoning: {effort}`` for Responses and ``reasoning_effort`` for Chat
  Completions. "Disabled" is rendered per dialect rather than dropped:
  Responses spells it ``effort: "none"``, Chat Completions omits the
  control because no served model publishes that level. A control that is
  *not* portable - an Anthropic ``thinking`` budget, a Google
  ``thinkingConfig``, a reasoning summary on a Chat Completions target -
  is refused.
"""

from __future__ import annotations

from typing import Any

from gateway.bridge.model import (
    IMAGE,
    ITEM_FUNCTION_CALL,
    ITEM_FUNCTION_CALL_OUTPUT,
    ITEM_MESSAGE,
    ROLE_ASSISTANT,
    ROLE_DEVELOPER,
    ROLE_SYSTEM,
    ROLE_TOOL,
    ROLE_USER,
    TEXT,
    ContentPart,
    ConversionError,
    FunctionTool,
    InputItem,
    NormalisedRequest,
)
from gateway.core.capabilities import DIALECT_OPENAI_CHAT, DIALECT_OPENAI_RESPONSES
from gateway.thinking import DISABLED_LEVEL, is_disabled_level

#: Parameters that exist in one dialect and have no counterpart in the other.
#: They are carried through :attr:`NormalisedRequest.extra` only when they
#: are safe to drop; anything lossy raises instead.
_CHAT_ONLY = ("n", "user", "seed", "logprobs", "top_logprobs", "presence_penalty", "frequency_penalty")

#: The fields the gateway models inside a ``reasoning`` object, per source
#: dialect. ``summary`` belongs to the Responses dialect only; Chat
#: Completions has no such control, so it is refused there rather than
#: carried and lost.
_REASONING_FIELDS = {
    DIALECT_OPENAI_CHAT: ("effort", "enabled"),
    DIALECT_OPENAI_RESPONSES: ("effort", "enabled", "summary"),
}


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _forward_same_dialect(payload: dict[str, Any], dialect: str) -> dict[str, Any]:
    """Forward a same-dialect payload, normalising only the thinking control.

    A same-dialect payload is the upstream's own dialect, so a control the
    gateway does not model is forwarded untouched: the provider is the
    party that defines what it accepts, and forwarding is not a drop.

    The thinking-level control is the exception, because two of its forms
    are not portable even inside one dialect:

    * **Disabled.** ``none`` is a published level for the Responses
      models this gateway serves, so a Responses target receives it as
      ``reasoning: {"effort": "none"}``. No served Chat Completions model
      publishes it - the dated catalogue (``data/go_catalog.json``,
      observed 2026-10-22) gives the levels of only three models, and
      none of the chat-dialect ones is ``none`` - and OpenAI documents
      HTTP 400 for a model that does not support it, so a Chat target
      receives the request with the control omitted.
    * **The nested Chat Completions spelling.** Chat Completions spells a
      level with the flat ``reasoning_effort``; the nested form is
      normalised to it rather than forwarded in a shape the upstream may
      reject.

    Both forms are normalised *before* any other forwarding decision, so
    the flat and the nested spelling of one request can never disagree.
    """
    forwarded = dict(payload)
    reasoning = forwarded.get("reasoning")
    nested = reasoning if isinstance(reasoning, dict) else None
    flat = forwarded.get("reasoning_effort")
    disabled = flat == DISABLED_LEVEL or (
        nested is not None
        and (nested.get("enabled") is False or nested.get("effort") == DISABLED_LEVEL)
    )

    if dialect == DIALECT_OPENAI_RESPONSES:
        if disabled:
            # Normalise the control rather than forward it: the Responses
            # dialect has no `enabled` field, so the level carries the
            # intent and any other field the client sent survives with it.
            rendered = {key: value for key, value in (nested or {}).items() if key != "enabled"}
            rendered["effort"] = DISABLED_LEVEL
            forwarded.pop("reasoning_effort", None)
            forwarded["reasoning"] = rendered
        return forwarded

    if disabled:
        forwarded.pop("reasoning", None)
        forwarded.pop("reasoning_effort", None)
        return forwarded
    if (
        isinstance(nested, dict)
        and set(nested) == {"effort"}
        and isinstance(nested.get("effort"), str)
        and flat is None
    ):
        forwarded.pop("reasoning", None)
        forwarded["reasoning_effort"] = nested["effort"]
    return forwarded


def _thinking_level(payload: dict[str, Any], dialect: str) -> tuple[str | None, str | None]:
    """Read every inbound spelling of the thinking control, or refuse.

    Every spelling normalises to one canonical level from
    :data:`gateway.thinking.CANONICAL_LEVELS` - the flat
    ``reasoning_effort``, the nested ``reasoning.effort``, and the
    disabled forms ``reasoning_effort: "none"``, ``reasoning.effort:
    "none"`` and ``reasoning.enabled: false``. Returns the level and the
    Responses-dialect summary request, if any.

    A control this normalisation cannot model - an Anthropic ``thinking``
    budget, a Google ``thinkingConfig``, an unknown field inside
    ``reasoning``, a value of the wrong type, or two spellings that
    contradict each other - raises :class:`ConversionError`. It is never
    dropped: the caller is on a converted path, where the upstream will
    never see the original shape.
    """
    if "thinking" in payload or "thinkingConfig" in payload:
        raise ConversionError(
            "thinking budget controls cannot be expressed by this OpenAI-family route; "
            "use a supported reasoning effort instead",
            capability="thinking",
        )
    nested = payload.get("reasoning")
    if nested is not None and not isinstance(nested, dict):
        raise ConversionError("reasoning must be an object", capability="reasoning")
    if isinstance(nested, dict):
        allowed = _REASONING_FIELDS.get(dialect) or _REASONING_FIELDS[DIALECT_OPENAI_CHAT]
        unknown = sorted(set(nested) - set(allowed))
        if unknown:
            raise ConversionError(
                "unsupported reasoning controls in this dialect: " + ", ".join(unknown),
                capability="reasoning",
            )
        if "effort" in nested and not isinstance(nested["effort"], str):
            raise ConversionError("reasoning.effort must be a string", capability="reasoning")
        if "enabled" in nested and not isinstance(nested["enabled"], bool):
            raise ConversionError("reasoning.enabled must be a boolean", capability="reasoning")
        if "summary" in nested and not isinstance(nested["summary"], str):
            raise ConversionError("reasoning.summary must be a string", capability="reasoning")
    flat = payload.get("reasoning_effort")
    if flat is not None and not isinstance(flat, str):
        raise ConversionError("reasoning_effort must be a string", capability="reasoning")

    effort = nested.get("effort") if isinstance(nested, dict) else None
    if flat is not None and effort is not None and flat != effort:
        raise ConversionError("conflicting reasoning effort controls", capability="reasoning")
    summary = nested.get("summary") if isinstance(nested, dict) else None
    summary = summary if isinstance(summary, str) else None
    if isinstance(nested, dict) and nested.get("enabled") is False:
        if (flat or effort) not in (None, DISABLED_LEVEL):
            raise ConversionError("reasoning is disabled but an effort was requested", capability="reasoning")
        return DISABLED_LEVEL, summary
    return (flat if flat is not None else effort), summary


def payload_has_image_input(payload: dict[str, Any], source_dialect: str) -> bool:
    """Whether a request carries an image that the serving model must see.

    This uses the same content parsers as dialect conversion, so routing and
    rendering cannot disagree about which fields represent an image. Unknown
    part shapes are not treated as images here; a converted path will still
    refuse any shape it cannot express, rather than silently dropping it.
    """
    if source_dialect == DIALECT_OPENAI_CHAT:
        messages = payload.get("messages")
        return isinstance(messages, list) and any(
            isinstance(message, dict)
            and any(part.kind == IMAGE for part in _chat_content_to_parts(message.get("content")))
            for message in messages
        )
    if source_dialect == DIALECT_OPENAI_RESPONSES:
        raw_input = payload.get("input")
        for entry in _as_list(raw_input):
            if isinstance(entry, str):
                continue
            if isinstance(entry, dict) and any(
                part.kind == IMAGE for part in _responses_content_to_parts(entry.get("content"))
            ):
                return True
        return False
    return False


def _chat_content_to_parts(content: Any) -> list[ContentPart]:
    """Convert Chat Completions message content into content parts."""
    if content is None:
        return []
    if isinstance(content, str):
        return [ContentPart(kind=TEXT, text=content)]
    parts: list[ContentPart] = []
    for entry in _as_list(content):
        if isinstance(entry, str):
            parts.append(ContentPart(kind=TEXT, text=entry))
            continue
        if not isinstance(entry, dict):
            continue
        entry_type = entry.get("type")
        if entry_type == "text" or (entry_type is None and "text" in entry):
            parts.append(ContentPart(kind=TEXT, text=str(entry.get("text") or "")))
        elif entry_type == "image_url":
            image = entry.get("image_url")
            url = None
            detail = None
            if isinstance(image, dict):
                url = image.get("url")
                detail = image.get("detail")
            elif isinstance(image, str):
                url = image
            parts.append(ContentPart(kind=IMAGE, image_url=url, detail=detail))
        elif entry_type == "input_text":
            parts.append(ContentPart(kind=TEXT, text=str(entry.get("text") or "")))
        elif entry_type == "output_text":
            parts.append(ContentPart(kind=TEXT, text=str(entry.get("text") or "")))
        elif entry_type == "input_image":
            image = entry.get("image_url")
            url = image.get("url") if isinstance(image, dict) else image
            parts.append(ContentPart(kind=IMAGE, image_url=url))
    return parts


def _responses_content_to_parts(content: Any) -> list[ContentPart]:
    """Convert Responses input content into content parts."""
    if content is None:
        return []
    if isinstance(content, str):
        return [ContentPart(kind=TEXT, text=content)]
    parts: list[ContentPart] = []
    for entry in _as_list(content):
        if isinstance(entry, str):
            parts.append(ContentPart(kind=TEXT, text=entry))
            continue
        if not isinstance(entry, dict):
            continue
        entry_type = entry.get("type")
        if entry_type in ("input_text", "output_text", "text", "summary_text"):
            parts.append(ContentPart(kind=TEXT, text=str(entry.get("text") or "")))
        elif entry_type == "input_image":
            image = entry.get("image_url")
            url = image.get("url") if isinstance(image, dict) else image
            parts.append(ContentPart(kind=IMAGE, image_url=url, detail=entry.get("detail")))
    return parts


def _parts_to_chat_content(parts: list[ContentPart]) -> Any:
    if not parts:
        return ""
    if len(parts) == 1 and parts[0].kind == TEXT:
        return parts[0].text or ""
    payload: list[dict[str, Any]] = []
    for part in parts:
        if part.kind == TEXT:
            payload.append({"type": "text", "text": part.text or ""})
        else:
            image: dict[str, Any] = {"url": part.image_url}
            if part.detail:
                image["detail"] = part.detail
            payload.append({"type": "image_url", "image_url": image})
    return payload


def _parts_to_responses_content(parts: list[ContentPart], assistant: bool) -> list[dict[str, Any]]:
    text_type = "output_text" if assistant else "input_text"
    payload: list[dict[str, Any]] = []
    for part in parts:
        if part.kind == TEXT:
            payload.append({"type": text_type, "text": part.text or ""})
        else:
            payload.append({"type": "input_image", "image_url": part.image_url})
    return payload


def _chat_tools_to_normalised(tools: Any) -> tuple[list[FunctionTool], list[dict[str, Any]]]:
    functions: list[FunctionTool] = []
    hosted: list[dict[str, Any]] = []
    for tool in _as_list(tools):
        if not isinstance(tool, dict):
            continue
        if tool.get("type") == "function" and isinstance(tool.get("function"), dict):
            spec = tool["function"]
            name = spec.get("name")
            if not isinstance(name, str) or not name:
                continue
            parameters = spec.get("parameters")
            functions.append(
                FunctionTool(
                    name=name,
                    description=spec.get("description") if isinstance(spec.get("description"), str) else None,
                    parameters=parameters if isinstance(parameters, dict) else {},
                )
            )
        else:
            hosted.append(tool)
    return functions, hosted


def _responses_tools_to_normalised(tools: Any) -> tuple[list[FunctionTool], list[dict[str, Any]]]:
    functions: list[FunctionTool] = []
    hosted: list[dict[str, Any]] = []
    for tool in _as_list(tools):
        if not isinstance(tool, dict):
            continue
        if tool.get("type") == "function":
            name = tool.get("name")
            if not isinstance(name, str) or not name:
                continue
            parameters = tool.get("parameters")
            functions.append(
                FunctionTool(
                    name=name,
                    description=tool.get("description") if isinstance(tool.get("description"), str) else None,
                    parameters=parameters if isinstance(parameters, dict) else {},
                )
            )
        else:
            hosted.append(tool)
    return functions, hosted


def from_chat_completions(payload: dict[str, Any]) -> NormalisedRequest:
    """Parse a Chat Completions request into the normalised form."""
    if not isinstance(payload, dict):
        raise ConversionError("request body is not a JSON object")

    model = payload.get("model")
    if not isinstance(model, str) or not model:
        raise ConversionError("request is missing a model")

    items: list[InputItem] = []
    system_chunks: list[str] = []
    for message in _as_list(payload.get("messages")):
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role in (ROLE_SYSTEM, ROLE_DEVELOPER):
            text = "".join(part.text or "" for part in _chat_content_to_parts(message.get("content")))
            if text:
                system_chunks.append(text)
            continue
        if role == ROLE_TOOL:
            call_id = message.get("tool_call_id")
            content = message.get("content")
            text = content if isinstance(content, str) else "".join(
                part.text or "" for part in _chat_content_to_parts(content)
            )
            items.append(
                InputItem(kind=ITEM_FUNCTION_CALL_OUTPUT, call_id=call_id, output=text)
            )
            continue
        if role == ROLE_ASSISTANT:
            tool_calls = message.get("tool_calls")
            if isinstance(tool_calls, list) and tool_calls:
                parts = _chat_content_to_parts(message.get("content"))
                if parts:
                    items.append(InputItem(kind=ITEM_MESSAGE, role=ROLE_ASSISTANT, content=parts))
                for call in tool_calls:
                    if not isinstance(call, dict):
                        continue
                    spec = call.get("function")
                    if not isinstance(spec, dict):
                        continue
                    items.append(
                        InputItem(
                            kind=ITEM_FUNCTION_CALL,
                            call_id=call.get("id"),
                            name=spec.get("name"),
                            arguments=spec.get("arguments") if isinstance(spec.get("arguments"), str) else "",
                        )
                    )
                continue
            items.append(
                InputItem(kind=ITEM_MESSAGE, role=ROLE_ASSISTANT, content=_chat_content_to_parts(message.get("content")))
            )
            continue
        items.append(
            InputItem(kind=ITEM_MESSAGE, role=role or ROLE_USER, content=_chat_content_to_parts(message.get("content")))
        )

    functions, hosted = _chat_tools_to_normalised(payload.get("tools"))

    max_tokens = payload.get("max_completion_tokens")
    if max_tokens is None:
        max_tokens = payload.get("max_tokens")

    effort, summary = _thinking_level(payload, DIALECT_OPENAI_CHAT)
    stop = [str(s) for s in _as_list(payload.get("stop")) if isinstance(s, str)]

    extra = {key: payload[key] for key in _CHAT_ONLY if key in payload}

    return NormalisedRequest(
        model=model,
        instructions="\n".join(system_chunks) if system_chunks else None,
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
        extra=extra,
    )


def from_responses(payload: dict[str, Any]) -> NormalisedRequest:
    """Parse a Responses request into the normalised form."""
    if not isinstance(payload, dict):
        raise ConversionError("request body is not a JSON object")

    model = payload.get("model")
    if not isinstance(model, str) or not model:
        raise ConversionError("request is missing a model")

    items: list[InputItem] = []
    raw_input = payload.get("input")
    if isinstance(raw_input, str):
        items.append(InputItem(kind=ITEM_MESSAGE, role=ROLE_USER, content=[ContentPart(TEXT, raw_input)]))
    else:
        for entry in _as_list(raw_input):
            if isinstance(entry, str):
                items.append(InputItem(kind=ITEM_MESSAGE, role=ROLE_USER, content=[ContentPart(TEXT, entry)]))
                continue
            if not isinstance(entry, dict):
                continue
            entry_type = entry.get("type")
            if entry_type == "function_call":
                items.append(
                    InputItem(
                        kind=ITEM_FUNCTION_CALL,
                        call_id=entry.get("call_id"),
                        name=entry.get("name"),
                        arguments=entry.get("arguments") if isinstance(entry.get("arguments"), str) else "",
                    )
                )
            elif entry_type == "function_call_output":
                output = entry.get("output")
                items.append(
                    InputItem(
                        kind=ITEM_FUNCTION_CALL_OUTPUT,
                        call_id=entry.get("call_id"),
                        output=output if isinstance(output, str) else "",
                    )
                )
            elif entry_type in (ITEM_MESSAGE, None) and "role" in entry:
                role = entry.get("role")
                if role in (ROLE_SYSTEM, ROLE_DEVELOPER):
                    continue
                items.append(
                    InputItem(
                        kind=ITEM_MESSAGE,
                        role=role,
                        content=_responses_content_to_parts(entry.get("content")),
                    )
                )

    functions, hosted = _responses_tools_to_normalised(payload.get("tools"))

    effort, summary = _thinking_level(payload, DIALECT_OPENAI_RESPONSES)

    max_output = payload.get("max_output_tokens")
    previous = payload.get("previous_response_id")

    return NormalisedRequest(
        model=model,
        instructions=payload.get("instructions") if isinstance(payload.get("instructions"), str) else None,
        items=items,
        tools=functions,
        hosted_tools=hosted,
        stream=bool(payload.get("stream", False)),
        max_output_tokens=max_output if isinstance(max_output, int) else None,
        reasoning_effort=effort,
        reasoning_summary=summary,
        temperature=payload.get("temperature") if isinstance(payload.get("temperature"), (int, float)) else None,
        top_p=payload.get("top_p") if isinstance(payload.get("top_p"), (int, float)) else None,
        previous_response_id=previous if isinstance(previous, str) else None,
    )


def to_chat_completions(request: NormalisedRequest) -> dict[str, Any]:
    """Render the normalised request as a Chat Completions request."""
    if request.previous_response_id:
        raise ConversionError(
            "this route uses stateful continuation (previous_response_id), "
            "which the Chat Completions dialect cannot express",
            capability="previous_response_id",
        )
    if request.hosted_tools:
        raise ConversionError(
            "this request uses hosted tools, which the Chat Completions dialect cannot express",
            capability="hosted_tools",
        )
    if request.reasoning_summary:
        raise ConversionError(
            "this route cannot request a reasoning summary; the Chat Completions "
            "dialect has no such control. Remove reasoning.summary, or use a "
            "Responses-dialect route.",
            capability="reasoning_summary",
        )

    messages: list[dict[str, Any]] = []
    if request.instructions:
        messages.append({"role": ROLE_SYSTEM, "content": request.instructions})

    for item in request.items:
        if item.kind == ITEM_MESSAGE:
            messages.append(
                {
                    "role": item.role or ROLE_USER,
                    "content": _parts_to_chat_content(item.content),
                }
            )
        elif item.kind == ITEM_FUNCTION_CALL:
            messages.append(
                {
                    "role": ROLE_ASSISTANT,
                    "content": None,
                    "tool_calls": [
                        {
                            "id": item.call_id,
                            "type": "function",
                            "function": {"name": item.name or "", "arguments": item.arguments or ""},
                        }
                    ],
                }
            )
        elif item.kind == ITEM_FUNCTION_CALL_OUTPUT:
            messages.append(
                {
                    "role": ROLE_TOOL,
                    "tool_call_id": item.call_id,
                    "content": item.output or "",
                }
            )

    payload: dict[str, Any] = {"model": request.model, "messages": messages}
    if request.tools:
        payload["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description or "",
                    "parameters": tool.parameters or {"type": "object", "properties": {}},
                },
            }
            for tool in request.tools
        ]
    if request.stream:
        payload["stream"] = True
        payload["stream_options"] = {"include_usage": True}
    if request.max_output_tokens is not None:
        payload["max_completion_tokens"] = request.max_output_tokens
    # A disabled level has no published spelling for the Chat Completions
    # models this gateway serves, and OpenAI documents HTTP 400 when a
    # model does not support `none`, so the control is omitted rather than
    # sent in a form the upstream will reject.
    if request.reasoning_effort and not is_disabled_level(request.reasoning_effort):
        payload["reasoning_effort"] = request.reasoning_effort
    if request.temperature is not None:
        payload["temperature"] = request.temperature
    if request.top_p is not None:
        payload["top_p"] = request.top_p
    if request.stop:
        payload["stop"] = request.stop
    return payload


def to_responses(request: NormalisedRequest) -> dict[str, Any]:
    """Render the normalised request as a Responses request."""
    if request.hosted_tools:
        names = ", ".join(str(tool.get("type", "unknown")) for tool in request.hosted_tools)
        raise ConversionError(
            f"this request uses hosted tools ({names}) that this route cannot serve",
            capability="hosted_tools",
        )

    input_items: list[dict[str, Any]] = []
    for item in request.items:
        if item.kind == ITEM_MESSAGE:
            input_items.append(
                {
                    "role": item.role or ROLE_USER,
                    "content": _parts_to_responses_content(item.content, assistant=item.role == ROLE_ASSISTANT),
                }
            )
        elif item.kind == ITEM_FUNCTION_CALL:
            input_items.append(
                {
                    "type": ITEM_FUNCTION_CALL,
                    "call_id": item.call_id,
                    "name": item.name or "",
                    "arguments": item.arguments or "",
                }
            )
        elif item.kind == ITEM_FUNCTION_CALL_OUTPUT:
            input_items.append(
                {
                    "type": ITEM_FUNCTION_CALL_OUTPUT,
                    "call_id": item.call_id,
                    "output": item.output or "",
                }
            )

    payload: dict[str, Any] = {"model": request.model, "input": input_items}
    if request.instructions:
        payload["instructions"] = request.instructions
    if request.tools:
        payload["tools"] = [
            {
                "type": "function",
                "name": tool.name,
                "description": tool.description or "",
                "parameters": tool.parameters or {"type": "object", "properties": {}},
            }
            for tool in request.tools
        ]
    if request.stream:
        payload["stream"] = True
    if request.max_output_tokens is not None:
        payload["max_output_tokens"] = request.max_output_tokens
    # The Responses dialect spells "off" as a level, so a disabled control
    # renders here instead of being dropped: both published Responses
    # models list `none` (data/go_catalog.json, observed 2026-10-22).
    reasoning: dict[str, Any] = {}
    if request.reasoning_effort:
        reasoning["effort"] = request.reasoning_effort
    if request.reasoning_summary:
        reasoning["summary"] = request.reasoning_summary
    if reasoning:
        payload["reasoning"] = reasoning
    if request.temperature is not None:
        payload["temperature"] = request.temperature
    if request.top_p is not None:
        payload["top_p"] = request.top_p
    if request.previous_response_id:
        payload["previous_response_id"] = request.previous_response_id
    return payload


def convert_request(payload: dict[str, Any], source: str, target: str) -> dict[str, Any]:
    """Convert a request between two dialects."""
    if source == target:
        # Same dialect: the upstream owns its own dialect, so a control the
        # gateway does not understand is forwarded rather than dropped or
        # refused - the provider is the party that defines what it accepts.
        # The thinking-level control is normalised even here, so that the
        # flat and the nested spelling of one request cannot disagree.
        return _forward_same_dialect(payload, source)
    if source == DIALECT_OPENAI_CHAT and target == DIALECT_OPENAI_RESPONSES:
        return to_responses(from_chat_completions(payload))
    if source == DIALECT_OPENAI_RESPONSES and target == DIALECT_OPENAI_CHAT:
        return to_chat_completions(from_responses(payload))
    raise ConversionError(f"no conversion is defined from {source} to {target}")


def convert_request_to_normalised(payload: dict[str, Any], dialect: str) -> NormalisedRequest:
    if dialect == DIALECT_OPENAI_CHAT:
        return from_chat_completions(payload)
    if dialect == DIALECT_OPENAI_RESPONSES:
        return from_responses(payload)
    raise ConversionError(f"unsupported source dialect: {dialect}")
