"""The Responses <-> Chat Completions bridge.

The bridge converts between the two OpenAI-family wire dialects through a
normalised representation, so that both directions share one definition of a
request and one of a stream event.

Design rules:

* Nothing is silently dropped. A request that needs a capability the target
  dialect cannot express is refused with an actionable error.
* Tool-call identity is preserved in both directions, across the streaming
  boundary.
* The terminal event is always emitted.
* Usage counters are carried across, and their absence stays absent.
"""

from gateway.bridge.model import (
    ConversionError,
    FunctionTool,
    InputItem,
    NormalisedRequest,
    StreamEvent,
)
from gateway.bridge.requests import (
    convert_request,
    convert_request_to_normalised,
    from_chat_completions,
    from_responses,
    to_chat_completions,
    to_responses,
)
from gateway.bridge.streams import (
    convert_stream,
    convert_stream_events,
    events_to_chat_chunks,
    events_to_responses_events,
    chat_chunk_to_events,
    responses_event_to_events,
    sse_done,
    sse_frame,
)

__all__ = [
    "ConversionError",
    "FunctionTool",
    "InputItem",
    "NormalisedRequest",
    "StreamEvent",
    "chat_chunk_to_events",
    "convert_request",
    "convert_request_to_normalised",
    "convert_stream",
    "convert_stream_events",
    "events_to_chat_chunks",
    "events_to_responses_events",
    "from_chat_completions",
    "from_responses",
    "responses_event_to_events",
    "sse_done",
    "sse_frame",
    "to_chat_completions",
    "to_responses",
]
