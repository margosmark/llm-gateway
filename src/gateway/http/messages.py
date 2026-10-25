"""The Anthropic Messages public surface: ``POST /v1/messages``.

An Anthropic-shaped caller talks to the gateway natively: it posts an
Anthropic Messages request and receives an Anthropic ``Message`` object
when it asked for a buffer, or Anthropic stream events
(``message_start`` / ``content_block_start`` / ``content_block_delta`` /
``content_block_stop`` / ``message_delta`` / ``message_stop``, each frame
carrying its ``event:`` line) when it asked for a stream.

The surface has exactly two halves:

1. **The Anthropic contract** - :func:`validate_messages_request` parses
   and checks an inbound request against Anthropic's documented shape
   before any route is selected and any provider is called.
2. **The surface entry point** - :func:`handle_messages` authorises the
   caller, validates the body, and then invokes the *established*
   inference flow (``GatewayApp._handle_inference``, the same code path
   ``/v1/chat/completions`` and ``/v1/responses`` run) with
   :data:`~gateway.core.capabilities.DIALECT_ANTHROPIC_MESSAGES` as the
   caller's dialect.

Route selection, fallback policy, the derived upstream session
identifier, degradation recording, the metadata-only access log and the
request ledger are therefore **shared code, not a second
implementation**: this surface cannot drift from the OpenAI surfaces,
and the alias never reaches a provider (the router substitutes the
upstream model id). What is Anthropic-specific here is the request
parsing; the response rendering is done by the bridge
(:mod:`gateway.bridge.anthropic`), which converts a routed OpenAI-dialect
response - buffered or streamed - into Anthropic shape and passes an
Anthropic-dialect upstream through unchanged.

**Errors keep the gateway's own envelope.** A refusal on this surface is
``{"error": {"message": ..., "type": ...}}`` with the same status codes
and error classes the rest of the API produces (``malformed_request``
400, ``unknown_model`` 404, ``model_not_allowed`` 403,
``no_eligible_route`` 503 ...), so the API's error behaviour is
preserved across surfaces and the access log keeps reading
``error.type``. What is Anthropic-shaped here is the *success* payload,
not the error envelope.

**Every check in validation is one of two kinds.** Either a field
Anthropic marks *required* is required here too (``model``,
``max_tokens``, ``messages``), or the check stops a wrongly-typed value
that the bridge would otherwise **drop silently** in conversion - a
temperature that is a string, a ``stop_sequences`` entry that is not a
string, a content block type the bridge cannot parse. The bridge refuses
fields the target dialect cannot express (an Anthropic ``thinking``
budget on an OpenAI route, ``top_k``, forced ``tool_choice``, hosted
tools), but a malformed value it cannot even parse would simply
disappear, so the boundary is enforced here, before any provider call.

**What validation deliberately does *not* refuse.** Options that are
valid Anthropic are carried per route: ``thinking`` with a budget is
honoured by an Anthropic-dialect route untouched, and is refused - by the
bridge, with its own actionable message - only when the selected route
would have to convert it, because :mod:`gateway.thinking` publishes no
budget-to-level mapping and this gateway invents none. ``top_k`` and a
forced ``tool_choice`` travel the same way: accepted on this surface,
refused at the conversion boundary rather than up front, so an
Anthropic-dialect route can still serve them.

Request options that both families share (``system``, ``messages``,
``max_tokens``, ``temperature``, ``top_p``, ``stop_sequences``,
``tools``, ``tool_choice``, ``stream``, ``metadata``, ``top_k``) are
passed through the established flow unchanged; the gateway-only phase
hint under ``metadata.personal_gateway`` is stripped before anything is
converted or forwarded, as on every other surface.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, NoReturn

from gateway.bridge.model import ROLE_ASSISTANT, ROLE_USER
from gateway.core.capabilities import DIALECT_ANTHROPIC_MESSAGES
from gateway.core.routing import RoutingError
from gateway.http.core import Request
from gateway.thinking import CANONICAL_LEVELS, is_known_level

if TYPE_CHECKING:  # pragma: no cover - typing only, no runtime import cycle
    from gateway.http.app import GatewayApp

#: The content-block types this gateway can actually carry, in inbound
#: requests. This is exactly the set ``gateway.bridge.anthropic``
#: parses; a block outside it would survive validation, reach the
#: converter, and be discarded there - a silent drop. Refusing it by
#: name, before any provider is called, keeps the bridge contract's
#: first rule ("nothing is silently dropped") true at the surface too.
CARRYABLE_BLOCK_TYPES = frozenset(
    {
        "text",
        "image",
        "input_text",
        "output_text",
        "input_image",
        "tool_use",
        "tool_result",
        "thinking",
        "redacted_thinking",
    }
)

#: The roles the Anthropic Messages dialect accepts on a message.
MESSAGE_ROLES = (ROLE_USER, ROLE_ASSISTANT)


def _refuse(message: str) -> NoReturn:
    """Raise the surface's standard 400 in the gateway's error envelope."""
    raise RoutingError(message, code="malformed_request", status=400)


def _is_number(value: Any) -> bool:
    """A real number, excluding ``bool`` (which is an ``int`` in Python)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _validate_messages(payload: dict[str, Any]) -> None:
    """Check the message array: shape, roles, and carryable content blocks."""
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        _refuse("messages must be a non-empty array of message objects")
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            _refuse(f"messages[{index}] must be an object")
        role = message.get("role")
        if role not in MESSAGE_ROLES:
            _refuse(
                f"messages[{index}].role must be 'user' or 'assistant', got {role!r}"
            )
        if "content" not in message:
            _refuse(f"messages[{index}] is missing content")
        content = message.get("content")
        if isinstance(content, str):
            continue
        if not isinstance(content, list):
            _refuse(
                f"messages[{index}].content must be a string or an array of content "
                "blocks"
            )
        for block_index, block in enumerate(content):
            if not isinstance(block, dict):
                _refuse(f"messages[{index}].content[{block_index}] must be an object")
            kind = block.get("type")
            if not isinstance(kind, str) or not kind:
                _refuse(f"messages[{index}].content[{block_index}] is missing a type")
            if kind not in CARRYABLE_BLOCK_TYPES:
                _refuse(
                    f"content block type '{kind}' is not carried by this gateway; "
                    f"supported types: {', '.join(sorted(CARRYABLE_BLOCK_TYPES))}"
                )


def _validate_thinking(payload: dict[str, Any]) -> None:
    """Validate the Anthropic thinking control against the canonical vocabulary.

    Anthropic spells reasoning as a *budget* (``thinking: {"type":
    "enabled", "budget_tokens": N}``) or as off (``{"type": "disabled"}``).
    The gateway's canonical vocabulary (:mod:`gateway.thinking`) is a set
    of *levels* and deliberately publishes no budget-to-level mapping, so
    this surface validates the Anthropic spelling and leaves the boundary
    decision - honoured natively, or refused when the selected route
    would have to convert it - to the bridge, per route.

    A caller that sends a canonical **level** here (``"high"``) is mixing
    vocabularies; it is refused with a message that names both, instead
    of the generic type error it would otherwise get.
    """
    thinking = payload.get("thinking")
    if thinking is None:
        return
    if isinstance(thinking, str) and is_known_level(thinking):
        _refuse(
            f"'{thinking}' is a reasoning level from the gateway's canonical "
            f"vocabulary ({', '.join(CANONICAL_LEVELS)}); the Anthropic Messages "
            'dialect expresses reasoning as thinking: {"type": "enabled", '
            '"budget_tokens": N} or {"type": "disabled"}'
        )
    if not isinstance(thinking, dict):
        _refuse(
            'thinking must be an object like {"type": "enabled", "budget_tokens": N} '
            'or {"type": "disabled"}'
        )
    kind = thinking.get("type")
    if kind == "enabled":
        budget = thinking.get("budget_tokens")
        if not _is_positive_int(budget):
            _refuse(
                'thinking: {"type": "enabled"} requires a positive integer '
                "budget_tokens"
            )
        return
    if kind == "disabled":
        if "budget_tokens" in thinking:
            _refuse('thinking: {"type": "disabled"} must not carry budget_tokens')
        return
    _refuse(f"thinking.type must be 'enabled' or 'disabled', got {kind!r}")


def _validate_options(payload: dict[str, Any]) -> None:
    """Type-check the request options the bridge would otherwise drop or mangle.

    Each rule matches Anthropic's published type for the field; none of
    them constrains a *value* the gateway has no evidence about (a
    temperature range, for example, is left to the route's own dialect,
    since the two served dialects publish different ranges).
    """
    for name in ("temperature", "top_p"):
        if name in payload and not _is_number(payload[name]):
            _refuse(f"{name} must be a number")

    top_k = payload.get("top_k")
    if "top_k" in payload and (isinstance(top_k, bool) or not isinstance(top_k, int)):
        _refuse("top_k must be an integer")

    if "stop_sequences" in payload:
        stops = payload["stop_sequences"]
        if not isinstance(stops, list) or any(not isinstance(s, str) for s in stops):
            _refuse("stop_sequences must be an array of strings")

    system = payload.get("system")
    if system is not None and not isinstance(system, str):
        if not isinstance(system, list) or any(
            not isinstance(block, dict)
            or block.get("type") != "text"
            or not isinstance(block.get("text"), str)
            for block in system
        ):
            _refuse("system must be a string or an array of text blocks")

    tools = payload.get("tools")
    if tools is not None:
        if not isinstance(tools, list):
            _refuse("tools must be an array")
        for index, tool in enumerate(tools):
            if not isinstance(tool, dict):
                _refuse(f"tools[{index}] must be an object")
            if "input_schema" in tool and not isinstance(tool["input_schema"], dict):
                _refuse(f"tools[{index}].input_schema must be an object")

    if "tool_choice" in payload and not isinstance(payload["tool_choice"], dict):
        _refuse("tool_choice must be an object")

    if "metadata" in payload and not isinstance(payload["metadata"], dict):
        _refuse("metadata must be an object")

    if "stream" in payload and not isinstance(payload["stream"], bool):
        _refuse("stream must be a boolean")


def validate_messages_request(payload: Any) -> None:
    """Refuse a request that is not a well-formed Anthropic Messages request.

    Raises ``RoutingError`` (400, ``malformed_request``) in the gateway's
    standard error envelope, before any route is selected and before any
    provider is called. See the module docstring for what is checked and
    what is deliberately left to the route.
    """
    if not isinstance(payload, dict):
        _refuse("request body is not valid JSON")

    model = payload.get("model")
    if not isinstance(model, str) or not model:
        _refuse("request is missing a model")

    max_tokens = payload.get("max_tokens")
    if not _is_positive_int(max_tokens):
        _refuse(
            "max_tokens is required by the Anthropic Messages API and must be a "
            "positive integer"
        )

    _validate_messages(payload)
    _validate_thinking(payload)
    _validate_options(payload)


def handle_messages(app: GatewayApp, request: Request) -> Any:
    """Serve one ``POST /v1/messages`` call.

    The order mirrors the OpenAI surfaces: the caller is authorised
    first (a bad token is a 401 whatever the body says), the body is
    parsed and the Anthropic contract checked, and only then does the
    established inference flow select a route and invoke it with the
    Anthropic Messages dialect as the caller's - which is what renders
    the buffered and the streamed response in Anthropic shape through
    the bridge.
    """
    # Same package, same helpers: authorising here (rather than inside
    # the flow) keeps this surface's 401-before-400 ordering identical
    # to /v1/chat/completions and /v1/responses.
    app._resolve_profile(request)
    try:
        payload = request.json()
    except ValueError:
        _refuse("request body is not valid JSON")
    validate_messages_request(payload)
    # The established gateway flow: profile/alias gates, route selection,
    # fallback, session derivation, ledger, and the dialect-parameterised
    # buffered/streamed rendering through gateway.bridge.anthropic.
    return app._handle_inference(request, DIALECT_ANTHROPIC_MESSAGES)
