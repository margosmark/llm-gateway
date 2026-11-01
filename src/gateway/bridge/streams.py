"""Stream conversion between the two OpenAI-family dialects.

The hard part of the bridge is here, because a stream of Chat Completions
chunks and a stream of Responses events carry the same information in
different shapes, and the shapes differ *mid-stream*: tool calls are
addressed by index in one and by item id in the other.

The approach is to normalise every incoming event, then render the
normalised events in the target dialect, keeping the state needed to make
both sides line up:

* tool-call **indices** (Chat) are mapped to **call ids** (Responses) and
  back, so a tool call and its arguments stay attached to each other;
* the **terminal event** is always emitted - ``response.completed`` for the
  Responses dialect, a chunk carrying ``finish_reason`` for Chat
  Completions;
* **usage counters** are carried across, and their *absence* stays absent
  rather than becoming a zero.

A converted stream is never buffered to completion. Each incoming frame
produces zero or more outgoing frames immediately.
"""

from __future__ import annotations

import json
import time
from typing import Any, Iterable, Iterator

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
    StreamEvent,
)
from gateway.core.capabilities import DIALECT_OPENAI_CHAT, DIALECT_OPENAI_RESPONSES

#: Canonical usage keys, whichever dialect the upstream speaks.
USAGE_PROMPT = "prompt_tokens"
USAGE_COMPLETION = "completion_tokens"
USAGE_TOTAL = "total_tokens"

#: Upper bound on a single provider-reported token counter. Counters are
#: provider-controlled integers inside otherwise valid JSON, so a body may
#: legally carry a 401-digit ``prompt_tokens``: it overflows every ``int`` to
#: ``float`` conversion on the owner-console read path (HTTP 500) and
#: serialises into a response body as a JSON number the console's
#: ``JSON.parse`` reads as ``Infinity``. 10**9 tokens per counter is already
#: far past any real request; above it a counter is not a count this gateway
#: will claim - it is unknown.
MAX_TOKEN_COUNTER = 10**9

#: The usage-dictionary fields that carry token counts (either dialect).
USAGE_FIELDS = (USAGE_PROMPT, "input_tokens", USAGE_COMPLETION, "output_tokens", USAGE_TOTAL)


def usable_token_count(value: Any) -> int | None:
    """A token counter this gateway will claim, or None when it refuses.

    Refuses non-integers, ``bool`` (an ``int`` subclass), negatives and
    anything above :data:`MAX_TOKEN_COUNTER`. None means *unknown*: the
    caller must never substitute zero for it.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < 0 or value > MAX_TOKEN_COUNTER:
        return None
    return value


def reported_counters_unusable(usage: dict[str, Any]) -> bool:
    """Whether the provider *reported a number* this gateway refuses to use.

    Only reported numbers count: an absent, null or non-numeric counter is
    "not reported" and stays dropped as before, while a number outside the
    usable range is a refusal that has to be recorded as such - silently
    dropping it next to a smaller, still-priced counter would turn a refusal
    into a claim.
    """
    values = [usage.get(name) for name in USAGE_FIELDS if name in usage]
    for details_name in ("prompt_tokens_details", "input_tokens_details"):
        details = usage.get(details_name)
        if isinstance(details, dict):
            for name in ("cached_tokens", "cache_read"):
                if name in details:
                    values.append(details[name])
    return any(isinstance(value, (int, float)) and usable_token_count(value) is None
               for value in values)


# --- SSE framing -------------------------------------------------------


def iter_sse_frames(raw: bytes) -> Iterator[tuple[str | None, str]]:
    """Split an SSE byte blob into ``(event_name, data)`` pairs.

    Handles frames split across chunk boundaries by buffering the remainder.
    """
    buffer = ""
    for piece in raw.decode("utf-8", errors="replace").split("\n"):
        buffer += piece + "\n"
    while "\n\n" in buffer:
        frame, buffer = buffer.split("\n\n", 1)
        name: str | None = None
        data_lines: list[str] = []
        for line in frame.split("\n"):
            if line.startswith(":"):
                continue
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
        if data_lines:
            yield name, "\n".join(data_lines)


def sse_frame(data: str, event: str | None = None) -> bytes:
    """Render one SSE frame."""
    prefix = f"event: {event}\n" if event else ""
    return f"{prefix}data: {data}\n\n".encode("utf-8")


def sse_done() -> bytes:
    return b"data: [DONE]\n\n"


# --- source: Chat Completions ------------------------------------------


def chat_chunk_to_events(chunk: dict[str, Any]) -> list[StreamEvent]:
    """Normalise one Chat Completions chunk."""
    if not isinstance(chunk, dict):
        return []
    events: list[StreamEvent] = []

    response_id = chunk.get("id") if isinstance(chunk.get("id"), str) else None
    model = chunk.get("model") if isinstance(chunk.get("model"), str) else None
    created = chunk.get("created") if isinstance(chunk.get("created"), int) else None

    choices = chunk.get("choices")
    for choice in choices if isinstance(choices, list) else []:
        if not isinstance(choice, dict):
            continue
        delta = choice.get("delta")
        if isinstance(delta, dict):
            content = delta.get("content")
            if isinstance(content, str) and content:
                events.append(StreamEvent(kind=EV_TEXT, text=content, response_id=response_id, model=model, created=created))
            reasoning = delta.get("reasoning_content")
            if isinstance(reasoning, str) and reasoning:
                events.append(
                    StreamEvent(kind=EV_REASONING, text=reasoning, response_id=response_id, model=model, created=created)
                )
            tool_calls = delta.get("tool_calls")
            if isinstance(tool_calls, list):
                for call in tool_calls:
                    if not isinstance(call, dict):
                        continue
                    index = call.get("index") if isinstance(call.get("index"), int) else None
                    spec = call.get("function")
                    spec = spec if isinstance(spec, dict) else {}
                    arguments = spec.get("arguments")
                    call_id = call.get("id") if isinstance(call.get("id"), str) else None
                    name = spec.get("name") if isinstance(spec.get("name"), str) else None
                    if call_id or name:
                        events.append(
                            StreamEvent(
                                kind=EV_TOOL_CALL_START,
                                call_id=call_id,
                                name=name,
                                tool_index=index,
                                response_id=response_id,
                                model=model,
                                created=created,
                            )
                        )
                    if isinstance(arguments, str) and arguments:
                        events.append(
                            StreamEvent(
                                kind=EV_TOOL_CALL_DELTA,
                                call_id=call_id,
                                arguments_delta=arguments,
                                tool_index=index,
                                response_id=response_id,
                                model=model,
                                created=created,
                            )
                        )
        finish = choice.get("finish_reason")
        if isinstance(finish, str) and finish:
            events.append(
                StreamEvent(
                    kind=EV_DONE,
                    finish_reason=finish,
                    response_id=response_id,
                    model=model,
                    created=created,
                )
            )

    usage = chunk.get("usage")
    if isinstance(usage, dict):
        normalised = normalise_usage(usage)
        if normalised:
            events.append(
                StreamEvent(kind=EV_USAGE, usage=normalised, response_id=response_id, model=model, created=created)
            )

    return events


def normalise_usage(usage: dict[str, Any]) -> dict[str, int]:
    """Normalise usage counters from either dialect.

    Chat Completions uses ``prompt_tokens`` / ``completion_tokens``;
    Responses uses ``input_tokens`` / ``output_tokens``. Missing counters stay
    missing - they are never invented as zero. A counter outside
    :data:`MAX_TOKEN_COUNTER` is refused the same way: it reads as missing,
    never as a number, and :func:`reported_counters_unusable` tells callers
    the refusal happened so it can be recorded instead of silently dropped.
    """
    result: dict[str, int] = {}
    prompt = usable_token_count(usage.get("prompt_tokens", usage.get("input_tokens")))
    completion = usable_token_count(usage.get("completion_tokens", usage.get("output_tokens")))
    total = usable_token_count(usage.get("total_tokens"))
    if prompt is not None:
        result[USAGE_PROMPT] = prompt
    if completion is not None:
        result[USAGE_COMPLETION] = completion
    if total is not None:
        result[USAGE_TOTAL] = total
    elif prompt is not None and completion is not None and prompt + completion <= MAX_TOKEN_COUNTER:
        result[USAGE_TOTAL] = prompt + completion
    return result


# --- source: Responses --------------------------------------------------


def responses_event_to_events(event: dict[str, Any]) -> list[StreamEvent]:
    """Normalise one Responses stream event."""
    if not isinstance(event, dict):
        return []
    event_type = event.get("type")
    if not isinstance(event_type, str):
        return []

    response_id = None
    response = event.get("response")
    if isinstance(response, dict) and isinstance(response.get("id"), str):
        response_id = response["id"]

    if event_type == "response.output_text.delta":
        delta = event.get("delta")
        if isinstance(delta, str) and delta:
            return [StreamEvent(kind=EV_TEXT, text=delta, response_id=response_id)]
        return []

    if event_type == "response.reasoning_summary_text.delta":
        delta = event.get("delta")
        if isinstance(delta, str) and delta:
            return [StreamEvent(kind=EV_REASONING, text=delta, response_id=response_id)]
        return []

    if event_type == "response.output_item.added":
        item = event.get("item")
        if isinstance(item, dict) and item.get("type") == "function_call":
            return [
                StreamEvent(
                    kind=EV_TOOL_CALL_START,
                    call_id=item.get("call_id") if isinstance(item.get("call_id"), str) else None,
                    name=item.get("name") if isinstance(item.get("name"), str) else None,
                    tool_index=event.get("output_index") if isinstance(event.get("output_index"), int) else None,
                    response_id=response_id,
                )
            ]
        return []

    if event_type == "response.function_call_arguments.delta":
        delta = event.get("delta")
        if isinstance(delta, str) and delta:
            return [
                StreamEvent(
                    kind=EV_TOOL_CALL_DELTA,
                    arguments_delta=delta,
                    tool_index=event.get("output_index") if isinstance(event.get("output_index"), int) else None,
                    response_id=response_id,
                )
            ]
        return []

    if event_type in ("response.function_call_arguments.done", "response.output_item.done"):
        item = event.get("item")
        arguments = None
        if isinstance(item, dict) and item.get("type") == "function_call":
            arguments = item.get("arguments") if isinstance(item.get("arguments"), str) else ""
        elif event_type == "response.function_call_arguments.done":
            arguments = event.get("arguments") if isinstance(event.get("arguments"), str) else ""
        if arguments is None:
            return []
        return [
            StreamEvent(
                kind=EV_TOOL_CALL_END,
                arguments=arguments,
                tool_index=event.get("output_index") if isinstance(event.get("output_index"), int) else None,
                response_id=response_id,
            )
        ]

    if event_type in ("response.completed", "response.incomplete", "response.failed"):
        usage = None
        error = None
        finish = FINISH_STOP
        if event_type == "response.incomplete":
            finish = FINISH_LENGTH
        elif event_type == "response.failed":
            finish = FINISH_CONTENT_FILTER
        if isinstance(response, dict):
            raw_usage = response.get("usage")
            if isinstance(raw_usage, dict):
                usage = normalise_usage(raw_usage)
            raw_error = response.get("error")
            if isinstance(raw_error, dict):
                error = raw_error
        events: list[StreamEvent] = []
        if usage:
            events.append(StreamEvent(kind=EV_USAGE, usage=usage, response_id=response_id))
        events.append(
            StreamEvent(kind=EV_DONE, finish_reason=finish, response_id=response_id, error=error)
        )
        return events

    if event_type in ("error", "response.failed.error"):
        message = event.get("message")
        if not isinstance(message, str):
            message = "upstream stream reported an error"
        return [StreamEvent(kind=EV_ERROR, error={"message": message})]

    return []


# --- target renderers ---------------------------------------------------


class StreamState:
    """State shared by the renderers so both sides of a conversion line up."""

    def __init__(self, model: str, response_id: str | None = None) -> None:
        self.model = model
        self.response_id = response_id or "resp_" + hashlib_hex()
        self.created = int(time.time())
        self.usage: dict[str, int] = {}
        self.text_buffer = ""
        self.tool_calls: dict[int, dict[str, Any]] = {}
        self.tool_order: list[int] = []
        self.item_ids: dict[int, str] = {}

    def register_tool(self, index: int | None, call_id: str | None, name: str | None) -> int:
        key = index if index is not None else len(self.tool_order)
        entry = self.tool_calls.setdefault(key, {"call_id": None, "name": None, "arguments": ""})
        if call_id and not entry["call_id"]:
            entry["call_id"] = call_id
        if name and not entry["name"]:
            entry["name"] = name
        if key not in self.tool_order:
            self.tool_order.append(key)
        if key not in self.item_ids:
            self.item_ids[key] = "fc_" + hashlib_hex()
        return key

    def tool_for(self, event: StreamEvent) -> int:
        for key, entry in self.tool_calls.items():
            if event.call_id and entry["call_id"] == event.call_id:
                return key
        return self.register_tool(event.tool_index, event.call_id, event.name)

    def next_output_index(self) -> int:
        return len(self.tool_order)


def hashlib_hex() -> str:
    import hashlib
    import os

    return hashlib.sha256(os.urandom(16)).hexdigest()[:20]


def events_to_chat_chunks(events: Iterable[StreamEvent], state: StreamState) -> list[dict[str, Any]]:
    """Render normalised events as Chat Completions chunks."""
    chunks: list[dict[str, Any]] = []

    def envelope() -> dict[str, Any]:
        return {
            "id": state.response_id,
            "object": "chat.completion.chunk",
            "created": state.created,
            "model": state.model,
        }

    for event in events:
        if event.kind == EV_TEXT:
            chunk = envelope()
            chunk["choices"] = [{"index": 0, "delta": {"content": event.text}, "finish_reason": None}]
            chunks.append(chunk)
        elif event.kind == EV_REASONING:
            chunk = envelope()
            chunk["choices"] = [
                {"index": 0, "delta": {"reasoning_content": event.text}, "finish_reason": None}
            ]
            chunks.append(chunk)
        elif event.kind == EV_TOOL_CALL_START:
            key = state.tool_for(event)
            entry = state.tool_calls[key]
            chunk = envelope()
            chunk["choices"] = [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": key,
                                "id": entry["call_id"],
                                "type": "function",
                                "function": {"name": entry["name"] or "", "arguments": ""},
                            }
                        ]
                    },
                    "finish_reason": None,
                }
            ]
            chunks.append(chunk)
        elif event.kind == EV_TOOL_CALL_DELTA:
            key = state.tool_for(event)
            entry = state.tool_calls[key]
            entry["arguments"] += event.arguments_delta or ""
            chunk = envelope()
            chunk["choices"] = [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {"index": key, "function": {"arguments": event.arguments_delta or ""}}
                        ]
                    },
                    "finish_reason": None,
                }
            ]
            chunks.append(chunk)
        elif event.kind == EV_TOOL_CALL_END:
            key = state.tool_for(event)
            entry = state.tool_calls[key]
            if event.arguments is not None:
                entry["arguments"] = event.arguments
        elif event.kind == EV_USAGE:
            state.usage.update(event.usage or {})
            # OpenAI's own Chat Completions shape puts usage in its own
            # trailing chunk, after the chunk that carries finish_reason.
            chunk = envelope()
            chunk["choices"] = []
            chunk["usage"] = {
                USAGE_PROMPT: event.usage.get(USAGE_PROMPT, 0),
                USAGE_COMPLETION: event.usage.get(USAGE_COMPLETION, 0),
                USAGE_TOTAL: event.usage.get(USAGE_TOTAL, 0),
            }
            chunks.append(chunk)
        elif event.kind == EV_DONE:
            chunk = envelope()
            chunk["choices"] = [
                {"index": 0, "delta": {}, "finish_reason": event.finish_reason or FINISH_STOP}
            ]
            chunks.append(chunk)
        elif event.kind == EV_ERROR:
            chunk = envelope()
            chunk["choices"] = [
                {
                    "index": 0,
                    "delta": {},
                    "finish_reason": FINISH_CONTENT_FILTER,
                    "error": event.error or {"message": "upstream stream error"},
                }
            ]
            chunks.append(chunk)
    return chunks


def events_to_responses_events(events: Iterable[StreamEvent], state: StreamState) -> list[dict[str, Any]]:
    """Render normalised events as Responses stream events."""
    payloads: list[dict[str, Any]] = []

    for event in events:
        if event.kind == EV_TEXT:
            payloads.append(
                {
                    "type": "response.output_text.delta",
                    "item_id": "msg_" + state.response_id,
                    "output_index": 0,
                    "content_index": 0,
                    "delta": event.text,
                }
            )
        elif event.kind == EV_REASONING:
            payloads.append(
                {
                    "type": "response.reasoning_summary_text.delta",
                    "item_id": "rs_" + state.response_id,
                    "output_index": 0,
                    "summary_index": 0,
                    "delta": event.text,
                }
            )
        elif event.kind == EV_TOOL_CALL_START:
            key = state.tool_for(event)
            entry = state.tool_calls[key]
            payloads.append(
                {
                    "type": "response.output_item.added",
                    "output_index": key,
                    "item": {
                        "id": state.item_ids[key],
                        "type": "function_call",
                        "status": "in_progress",
                        "call_id": entry["call_id"],
                        "name": entry["name"] or "",
                        "arguments": "",
                    },
                }
            )
        elif event.kind == EV_TOOL_CALL_DELTA:
            key = state.tool_for(event)
            entry = state.tool_calls[key]
            entry["arguments"] += event.arguments_delta or ""
            payloads.append(
                {
                    "type": "response.function_call_arguments.delta",
                    "item_id": state.item_ids[key],
                    "output_index": key,
                    "delta": event.arguments_delta,
                }
            )
        elif event.kind == EV_TOOL_CALL_END:
            key = state.tool_for(event)
            entry = state.tool_calls[key]
            if event.arguments is not None:
                entry["arguments"] = event.arguments
            payloads.append(
                {
                    "type": "response.function_call_arguments.done",
                    "item_id": state.item_ids[key],
                    "output_index": key,
                    "arguments": entry["arguments"],
                }
            )
        elif event.kind == EV_USAGE:
            state.usage.update(event.usage or {})
        elif event.kind == EV_DONE:
            status = "completed"
            event_name = "response.completed"
            if event.finish_reason == FINISH_LENGTH:
                status = "incomplete"
                event_name = "response.incomplete"
            elif event.finish_reason == FINISH_CONTENT_FILTER:
                status = "failed"
                event_name = "response.failed"
            response: dict[str, Any] = {
                "id": state.response_id,
                "object": "response",
                "status": status,
                "model": state.model,
                "output": [],
            }
            if state.usage:
                response["usage"] = {
                    "input_tokens": state.usage.get(USAGE_PROMPT, 0),
                    "output_tokens": state.usage.get(USAGE_COMPLETION, 0),
                    "total_tokens": state.usage.get(USAGE_TOTAL, 0),
                }
            if event.error:
                response["error"] = event.error
            payloads.append({"type": event_name, "response": response})
        elif event.kind == EV_ERROR:
            payloads.append({"type": "error", "message": (event.error or {}).get("message", "upstream stream error")})

    return payloads


# --- the public conversion ---------------------------------------------


class StreamConverter:
    """Converts one upstream stream into the target dialect.

    Two ordering rules are enforced here, and both exist because the two
    dialects disagree about when usage counters arrive:

    * The **terminal event is always emitted.** A Responses stream ends with
      ``response.completed``; a Chat Completions stream ends with a chunk
      carrying ``finish_reason``. If the upstream stream ends without one,
      the response is reported as incomplete rather than silently dropped.
    * **Usage lands where the target dialect expects it.** Chat Completions
      puts usage in its own trailing chunk after the finish chunk; the
      Responses terminal event carries usage itself.
    """

    def __init__(self, source: str, target: str, model: str) -> None:
        self.source = source
        self.target = target
        self.state = StreamState(model=model)
        self._pending_usage: StreamEvent | None = None
        self._pending_done: StreamEvent | None = None
        self._done_emitted = False

    def _normalise(self, payload: dict[str, Any]) -> list[StreamEvent]:
        if self.source == DIALECT_OPENAI_CHAT:
            return chat_chunk_to_events(payload)
        if self.source == DIALECT_OPENAI_RESPONSES:
            return responses_event_to_events(payload)
        return []

    def _render(self, events: Iterable[StreamEvent]) -> list[dict[str, Any]]:
        if self.target == DIALECT_OPENAI_CHAT:
            return events_to_chat_chunks(events, self.state)
        if self.target == DIALECT_OPENAI_RESPONSES:
            return events_to_responses_events(events, self.state)
        return []

    def feed_payload(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        return self.feed_events(self._normalise(payload))

    def feed_events(self, events: Iterable[StreamEvent]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for event in events:
            if event.kind == EV_USAGE:
                self.state.usage.update(event.usage or {})
                if self.target == DIALECT_OPENAI_CHAT:
                    # Held until the finish chunk has been emitted, so the
                    # usage chunk lands after it, as OpenAI's shape does.
                    self._pending_usage = event
                continue
            if event.kind == EV_DONE:
                self._done_emitted = True
                if self.target == DIALECT_OPENAI_RESPONSES:
                    # The Responses terminal event carries usage, so it is
                    # emitted once the stream has ended and every counter is
                    # known.
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
                # A stream that ends without a finish_reason was truncated.
                # Report that rather than pretending it completed.
                out.extend(self._render([StreamEvent(kind=EV_DONE, finish_reason=FINISH_LENGTH)]))
                self._done_emitted = True
            return out
        if self.target == DIALECT_OPENAI_RESPONSES:
            done = self._pending_done or StreamEvent(kind=EV_DONE, finish_reason=FINISH_LENGTH)
            out.extend(self._render([done]))
            self._done_emitted = True
            return out
        return out


def convert_stream(
    frames: Iterable[bytes],
    source: str,
    target: str,
    model: str,
) -> Iterator[bytes]:
    """Convert an upstream SSE byte stream into *target* dialect frames.

    Frames are emitted as they arrive. Nothing is buffered to completion.
    """
    if source == target:
        for frame in frames:
            yield frame
        return

    converter = StreamConverter(source, target, model)
    buffer = ""
    for raw in frames:
        buffer += raw.decode("utf-8", errors="replace")
        while "\n\n" in buffer:
            frame, buffer = buffer.split("\n\n", 1)
            data_lines = [
                line[5:].lstrip()
                for line in frame.split("\n")
                if line.startswith("data:")
            ]
            if not data_lines:
                continue
            data = "\n".join(data_lines)
            if data.strip() == "[DONE]":
                continue
            try:
                payload = json.loads(data)
            except (json.JSONDecodeError, ValueError):
                continue
            for item in converter.feed_payload(payload):
                yield sse_frame(json.dumps(item, ensure_ascii=False, separators=(",", ":")))
    for item in converter.finish():
        yield sse_frame(json.dumps(item, ensure_ascii=False, separators=(",", ":")))


def convert_stream_events(
    payloads: Iterable[dict[str, Any]],
    source: str,
    target: str,
    model: str,
) -> Iterator[dict[str, Any]]:
    """Convert already-parsed upstream events into target-dialect events."""
    converter = StreamConverter(source, target, model)
    for payload in payloads:
        for item in converter.feed_payload(payload):
            yield item
    for item in converter.finish():
        yield item
