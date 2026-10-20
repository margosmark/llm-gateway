"""Buffered response conversion.

A converted request must receive a converted response. The streaming path
already does this; this module covers the non-streaming path, where a
Chat Completions client would otherwise receive a Responses object - or the
other way round - and fail to parse it.

Two functions, one per direction:

* :func:`responses_body_to_chat` - a Responses object becomes a Chat
  Completions object;
* :func:`chat_body_to_responses` - a Chat Completions object becomes a
  Responses object.

Both are lossless for the fields that matter: the assistant's text, the
tool calls with their identifiers, the finish reason, and the usage
counters. Fields with no counterpart in the target dialect are dropped
rather than invented, and the mapping is documented where it happens.
"""

from __future__ import annotations

from typing import Any

CHAT_OBJECT = "chat.completion"
RESPONSE_OBJECT = "response"

_FINISH_FROM_STATUS = {
    "completed": "stop",
    "incomplete": "length",
    "failed": "content_filter",
    "cancelled": "stop",
}

_STATUS_FROM_FINISH = {
    "stop": "completed",
    "length": "incomplete",
    "tool_calls": "completed",
    "content_filter": "failed",
}


def _text_of(item: dict[str, Any]) -> str:
    """The assistant text of a Responses output item."""
    content = item.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") in ("output_text", "text"):
            text = part.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


def responses_body_to_chat(body: dict[str, Any]) -> dict[str, Any]:
    """Convert a Responses object into a Chat Completions object."""
    if not isinstance(body, dict):
        return body

    output = body.get("output")
    output = output if isinstance(output, list) else []

    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for item in output:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "message":
            text_parts.append(_text_of(item))
        elif item_type == "function_call":
            tool_calls.append(
                {
                    "id": item.get("call_id") or item.get("id") or "",
                    "type": "function",
                    "function": {
                        "name": item.get("name") or "",
                        "arguments": item.get("arguments") or "",
                    },
                }
            )
        # A reasoning item has no Chat Completions counterpart and is
        # dropped rather than invented as content.

    status = body.get("status")
    finish_reason = _FINISH_FROM_STATUS.get(status if isinstance(status, str) else "", "stop")
    if tool_calls:
        finish_reason = "tool_calls"

    message: dict[str, Any] = {"role": "assistant", "content": "".join(text_parts)}
    if tool_calls:
        message["tool_calls"] = tool_calls

    response_id = body.get("id")
    chat_id = "chatcmpl-" + str(response_id)[len("resp_") :] if isinstance(response_id, str) else ""

    usage = body.get("usage")
    chat_usage: dict[str, Any] = {}
    if isinstance(usage, dict):
        if isinstance(usage.get("input_tokens"), int):
            chat_usage["prompt_tokens"] = usage["input_tokens"]
        if isinstance(usage.get("output_tokens"), int):
            chat_usage["completion_tokens"] = usage["output_tokens"]
        if isinstance(usage.get("total_tokens"), int):
            chat_usage["total_tokens"] = usage["total_tokens"]

    result: dict[str, Any] = {
        "id": chat_id,
        "object": CHAT_OBJECT,
        "created": body.get("created_at") or 0,
        "model": body.get("model") or "",
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": finish_reason,
            }
        ],
    }
    if chat_usage:
        result["usage"] = chat_usage
    return result


def chat_body_to_responses(body: dict[str, Any]) -> dict[str, Any]:
    """Convert a Chat Completions object into a Responses object."""
    if not isinstance(body, dict):
        return body

    choices = body.get("choices")
    choices = choices if isinstance(choices, list) else []

    output: list[dict[str, Any]] = []
    text_parts: list[str] = []
    status = "completed"

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
                    text = part.get("text")
                    if isinstance(text, str):
                        text_parts.append(text)

        tool_calls = message.get("tool_calls")
        if isinstance(tool_calls, list):
            for call in tool_calls:
                if not isinstance(call, dict):
                    continue
                spec = call.get("function")
                spec = spec if isinstance(spec, dict) else {}
                output.append(
                    {
                        "type": "function_call",
                        "id": "fc_" + str(call.get("id") or ""),
                        "call_id": call.get("id") or "",
                        "name": spec.get("name") or "",
                        "arguments": spec.get("arguments") or "",
                    }
                )

        finish = choice.get("finish_reason")
        if isinstance(finish, str):
            status = _STATUS_FROM_FINISH.get(finish, "completed")

    if text_parts:
        output.insert(
            0,
            {
                "type": "message",
                "id": "msg_" + str(body.get("id") or ""),
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": "".join(text_parts)}],
            },
        )

    chat_id = body.get("id")
    response_id = "resp_" + str(chat_id)[len("chatcmpl-") :] if isinstance(chat_id, str) else ""

    usage = body.get("usage")
    response_usage: dict[str, Any] = {}
    if isinstance(usage, dict):
        if isinstance(usage.get("prompt_tokens"), int):
            response_usage["input_tokens"] = usage["prompt_tokens"]
        if isinstance(usage.get("completion_tokens"), int):
            response_usage["output_tokens"] = usage["completion_tokens"]
        if isinstance(usage.get("total_tokens"), int):
            response_usage["total_tokens"] = usage["total_tokens"]

    result: dict[str, Any] = {
        "id": response_id,
        "object": RESPONSE_OBJECT,
        "created_at": body.get("created") or 0,
        "status": status,
        "model": body.get("model") or "",
        "output": output,
    }
    if response_usage:
        result["usage"] = response_usage
    return result
