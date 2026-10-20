"""Aggregating a streamed upstream response into one object.

Some upstream providers are **streaming only**. The ChatGPT / Codex backend
is one: it rejects a Responses request that does not set ``stream: true``.

A client that asks for a single, non-streamed completion must therefore be
served by *consuming* the upstream stream and presenting the result as one
object. This module does that, in both dialects.

For the Responses dialect the terminal ``response.completed`` event carries
the complete final object, so that is simply taken. For the Chat Completions
dialect the chunks are accumulated.
"""

from __future__ import annotations

import json
from typing import Any, Iterable

from gateway.bridge.model import ConversionError


def _iter_events(frames: Iterable[bytes]) -> Iterable[dict[str, Any]]:
    for frame in frames:
        text = frame.decode("utf-8", "replace") if isinstance(frame, (bytes, bytearray)) else str(frame)
        for line in text.splitlines():
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                continue
            try:
                event = json.loads(data)
            except ValueError:
                continue
            if isinstance(event, dict):
                yield event


def aggregate_responses(frames: Iterable[bytes]) -> dict[str, Any]:
    """Consume a Responses stream and return the final Responses object."""
    final: dict[str, Any] | None = None
    for event in _iter_events(frames):
        kind = event.get("type")
        if kind in ("response.completed", "response.incomplete", "response.failed"):
            response = event.get("response")
            if isinstance(response, dict):
                final = response
            if kind == "response.failed" and final is None:
                final = {
                    "id": event.get("response_id") or "",
                    "object": "response",
                    "status": "failed",
                    "error": event.get("error") or event.get("message"),
                    "output": [],
                }
        elif kind == "error" and final is None:
            final = {
                "id": "",
                "object": "response",
                "status": "failed",
                "error": {"message": event.get("message") or "upstream stream error"},
                "output": [],
            }
    if final is None:
        raise ConversionError(
            "the upstream stream ended without a terminal response event",
            capability="stream_termination",
        )
    return final


def aggregate_chat(frames: Iterable[bytes]) -> dict[str, Any]:
    """Consume a Chat Completions stream and return one Chat Completions object."""
    result: dict[str, Any] = {
        "id": "",
        "object": "chat.completion",
        "created": 0,
        "model": "",
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": ""}, "finish_reason": None}
        ],
    }
    text_parts: list[str] = []
    tool_calls: dict[int, dict[str, Any]] = {}
    saw_chunk = False

    for event in _iter_events(frames):
        if "choices" not in event:
            continue
        saw_chunk = True
        result["id"] = event.get("id") or result["id"]
        result["created"] = event.get("created") or result["created"]
        result["model"] = event.get("model") or result["model"]
        if isinstance(event.get("usage"), dict):
            result["usage"] = event["usage"]
        for choice in event.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta")
            if isinstance(delta, dict):
                content = delta.get("content")
                if isinstance(content, str):
                    text_parts.append(content)
                for call in delta.get("tool_calls") or []:
                    if not isinstance(call, dict):
                        continue
                    index = call.get("index") if isinstance(call.get("index"), int) else len(tool_calls)
                    entry = tool_calls.setdefault(
                        index, {"id": None, "type": "function", "function": {"name": "", "arguments": ""}}
                    )
                    if isinstance(call.get("id"), str):
                        entry["id"] = call["id"]
                    spec = call.get("function")
                    if isinstance(spec, dict):
                        if isinstance(spec.get("name"), str) and spec["name"]:
                            entry["function"]["name"] = spec["name"]
                        if isinstance(spec.get("arguments"), str):
                            entry["function"]["arguments"] += spec["arguments"]
            finish = choice.get("finish_reason")
            if isinstance(finish, str) and finish:
                result["choices"][0]["finish_reason"] = finish

    if not saw_chunk:
        raise ConversionError(
            "the upstream stream produced no chunks",
            capability="stream_termination",
        )

    message = result["choices"][0]["message"]
    message["content"] = "".join(text_parts)
    if tool_calls:
        message["tool_calls"] = [tool_calls[key] for key in sorted(tool_calls)]
        if not result["choices"][0].get("finish_reason"):
            result["choices"][0]["finish_reason"] = "tool_calls"
    if not result["choices"][0].get("finish_reason"):
        result["choices"][0]["finish_reason"] = "stop"
    return result


def aggregate_stream(frames: Iterable[bytes], dialect: str) -> dict[str, Any]:
    """Aggregate a stream in the given dialect into one object."""
    if dialect == "openai_responses":
        return aggregate_responses(frames)
    if dialect == "openai_chat":
        return aggregate_chat(frames)
    raise ConversionError(
        f"no aggregation is defined for the {dialect} dialect",
        capability="stream_aggregation",
    )
