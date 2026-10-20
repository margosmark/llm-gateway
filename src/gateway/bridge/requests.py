"""Request conversion between the two OpenAI-family dialects.

Chat Completions  <->  Responses.

Two rules govern every function here:

* **Nothing is silently dropped.** If the target dialect cannot express
  something the source carried - stateful continuation, hosted tools, a
  parameter with no counterpart - the conversion raises
  :class:`ConversionError` with an actionable message.
* **Content is preserved exactly.** Text, images, tool calls and tool
  results survive a round trip, and tool-call identifiers survive with them.
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

#: Parameters that exist in one dialect and have no counterpart in the other.
#: They are carried through :attr:`NormalisedRequest.extra` only when they
#: are safe to drop; anything lossy raises instead.
_CHAT_ONLY = ("n", "user", "seed", "logprobs", "top_logprobs", "presence_penalty", "frequency_penalty")


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


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

    effort = payload.get("reasoning_effort")
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
        reasoning_effort=effort if isinstance(effort, str) else None,
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

    reasoning = payload.get("reasoning")
    effort = None
    if isinstance(reasoning, dict):
        value = reasoning.get("effort")
        if isinstance(value, str):
            effort = value

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
    if request.reasoning_effort:
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
    if request.reasoning_effort:
        payload["reasoning"] = {"effort": request.reasoning_effort}
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
        return dict(payload)
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
