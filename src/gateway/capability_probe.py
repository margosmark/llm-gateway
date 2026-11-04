"""Bounded, opt-in capability probing for one reviewed provider/model pair.

A probe is a **paid inference request**, so every gate in here is fail-closed.
The module owns four things and nothing else:

* the *protocol registry* — the exact ``(provider_id, model_id)`` pairs whose
  bounded request/price contract has been reviewed. A pair that is not in
  :data:`PROBE_PROTOCOLS` can never be probed, whatever the caller sends;
* *pre-flight*: request construction, byte/token/time bounds, the published
  tariff and the owner's selection. All of it is evaluated **before** any
  transport is touched, so a refused preview or run sends zero requests;
* *classification*: the pure functions that turn one response into one
  per-capability evidence status. Errors, mismatches, missing tool calls and
  absent cost accounting are ``inconclusive`` — never ``observed_unsupported``
  and never ``false``;
* *execution*: :func:`run_suite`, a one-shot, non-streamed, non-retried,
  concurrency-1 runner that stops at the first failure, at a cancellation, at
  the run deadline, or when the first response carries no usable cost
  accounting.

The runner never executes a returned tool call, never writes configuration,
and never retries. It hands back sanitized evidence; persistence of that
evidence lives in :mod:`gateway.capability_discovery`.

Owner boundary: "owner" below means an explicit action from the
signed-in owner console. Owner sign-in gates the console; it does not
gate the model API, which stays keyless until a client key is set — see
``docs/TRUSTED_LAN_ADMIN_MODE.md``.
"""

from __future__ import annotations

import base64
import hashlib
import inspect
import json
import math
import re
import secrets
import struct
import threading
import time
import zlib
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Callable

from gateway.adapters.base import AdapterError, ProviderResponse
from gateway.bridge.streams import reported_counters_unusable, usable_token_count
from gateway.config import (
    ERROR_AUTH,
    ERROR_EXHAUSTED,
    ERROR_MALFORMED,
    ERROR_POLICY,
    ERROR_RATE_LIMITED,
    ERROR_ROUTE_GONE,
    ERROR_TRANSIENT,
    ERROR_UNKNOWN,
)
from gateway.costs import INVALID_USAGE_MARKER
from gateway.costs import price_for as catalog_model_price

#: The closed probe vocabulary an owner may select from.
PROBES = ("text_generation", "image_input", "function_tools")
#: Capabilities that map onto a router-effective boolean metadata field.
PROBE_TO_FIELD = {"image_input": "supports_image_input", "function_tools": "supports_function_tools"}

SUITE_VERSION = "mvp-1"
#: The reviewed transport contract this module executes. Distinct from the
#: suite version: the suite says *what* is asked, the protocol says *how*.
PROTOCOL_VERSION = "opencode_go_bounded_v1"

#: A published tariff older than this is not trustworthy enough to price a
#: paid run against, so the run is refused rather than estimated loosely.
PRICE_MAX_AGE_DAYS = 30

#: How the evidence this runner produces must be labelled in the store.
EVIDENCE_METHOD = "synthetic_inline_probe_v1"

#: The only spend policy this build accepts. A hard dollar ceiling would
#: need an enforceable provider-side cap, and none is verified for OpenCode
#: Go, so ``hard_cap_required`` is refused instead of being faked.
SPEND_POLICY_ESTIMATE = "estimate_accepted"

CONFIRMATION_TTL_SECONDS = 300
MAX_CONFIRMATIONS = 64

#: The facts the synthetic image provably contains.
BLUE_CIRCLE_COUNT = 2
RED_SQUARE_PRESENT = True

#: The fixed prompt, tool and image recipe. Gateway-owned templates only:
#: no user conversation, no owner-supplied image, no provider-supplied text.
TEXT_PROMPT_TEMPLATE = "Return exactly this run nonce and nothing else: {nonce}"
VISUAL_PROMPT = (
    "Examine the attached synthetic image. It contains blue circles and one red "
    "square. {instruction}"
)
VISUAL_TOOL_INSTRUCTION = (
    "Call the probe_visual_answer function with the number of blue circles and "
    "whether a red square is present."
)
TEXT_ONLY_INSTRUCTION = "State the number of blue circles and whether a red square is present."

TOOL_NAME = "probe_visual_answer"
TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": TOOL_NAME,
        "description": "Report the visual facts of the gateway's synthetic probe image.",
        "parameters": {
            "type": "object",
            "properties": {
                "blue_circle_count": {"type": "integer", "description": "Number of blue circles in the image."},
                "red_square_present": {"type": "boolean", "description": "Whether a red square is present."},
            },
            "required": ["blue_circle_count", "red_square_present"],
            "additionalProperties": False,
        },
    },
}

_MAX_SUMMARY = 240


class ProbeRefused(Exception):
    """A pre-flight gate refused the run. Carries a safe, caller-facing code."""

    def __init__(self, code: str, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


@dataclass(frozen=True)
class ProbeProtocol:
    """The reviewed bounds for one exact provider/model pair."""

    provider_id: str
    model_id: str
    dialect: str = "openai_chat"
    protocol_version: str = PROTOCOL_VERSION
    max_requests: int = 2
    text_output_tokens: int = 32
    visual_output_tokens: int = 64
    request_timeout_seconds: float = 15.0
    connect_timeout_seconds: float = 10.0
    run_deadline_seconds: float = 45.0
    max_request_bytes: int = 262_144
    max_response_bytes: int = 1_048_576
    max_image_bytes: int = 65_536
    concurrency: int = 1
    retries: int = 0
    streaming: bool = False

    @property
    def key(self) -> tuple[str, str]:
        return (self.provider_id, self.model_id)


#: The only pairs this gateway may ever probe. Additions require a reviewed
#: transport, a published tariff and a fresh observation date — not a guess.
PROBE_PROTOCOLS: dict[tuple[str, str], ProbeProtocol] = {
    ("opencode_go", "mimo-v2.6-flash"): ProbeProtocol(
        provider_id="opencode_go",
        model_id="mimo-v2.6-flash",
    ),
}


def protocol_for(provider_id: str, model_id: str) -> ProbeProtocol | None:
    return PROBE_PROTOCOLS.get((provider_id, model_id))


# --- pricing ---------------------------------------------------------------

def catalog_price_for(provider_id: str, model_id: str) -> dict[str, Any] | None:
    """The published tariff for this exact pair.

    Pricing evidence is scoped to the provider **and** the slug: OpenCode Go
    tariffs say nothing about another provider's billing for the same model
    string, so a lookup for any other provider returns nothing.
    """
    if provider_id != "opencode_go":
        return None
    return catalog_model_price(model_id)


def price_status(price: Any, now: datetime) -> str:
    """``known`` only for a complete, dated tariff observed recently."""
    if not isinstance(price, dict):
        return "unknown"
    if not isinstance(price.get("input_per_m"), (int, float)) or isinstance(price.get("input_per_m"), bool):
        return "unknown"
    if not isinstance(price.get("output_per_m"), (int, float)) or isinstance(price.get("output_per_m"), bool):
        return "unknown"
    if any(
        not math.isfinite(float(price[field])) or float(price[field]) < 0
        for field in ("input_per_m", "output_per_m")
    ):
        return "unknown"
    source = price.get("source")
    observed = price.get("observed_at")
    if not isinstance(source, str) or not source or not isinstance(observed, str):
        return "unknown"
    try:
        observed_on = date.fromisoformat(observed)
    except ValueError:
        return "unknown"
    age_days = (now.date() - observed_on).days
    if age_days < 0:
        # An observation date in the future is not evidence of anything.
        return "unknown"
    if age_days > PRICE_MAX_AGE_DAYS:
        return "stale"
    return "known"


# --- the synthetic image ---------------------------------------------------

def probe_png(width: int = 160, height: int = 96) -> bytes:
    """A tiny generated PNG: two blue circles and one red square.

    Built in memory from a fixed recipe. It is never fetched from a URL,
    never written to disk, and never logged — the provider only ever
    receives it inline as a base64 data URL inside the probe request.
    """
    pixels = bytearray()
    for y in range(height):
        pixels.append(0)  # PNG filter: none
        for x in range(width):
            if 30 <= x < 62 and 20 <= y < 52:
                color = (200, 30, 30)          # red square
            elif (x - 95) ** 2 + (y - 48) ** 2 <= 400:
                color = (30, 60, 200)          # blue circle 1
            elif (x - 135) ** 2 + (y - 48) ** 2 <= 256:
                color = (30, 60, 200)          # blue circle 2
            else:
                color = (255, 255, 255)
            pixels.extend(color)
    raw = bytes(pixels)

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


# --- request construction --------------------------------------------------

def _validate_probes(probes: Any) -> list[str]:
    if (
        not isinstance(probes, list)
        or not probes
        or len(probes) > len(PROBES)
        or any(not isinstance(p, str) or p not in PROBES for p in probes)
        or len(set(probes)) != len(probes)
    ):
        raise ValueError("invalid probe selection")
    return list(probes)


def build_text_request(protocol: ProbeProtocol, nonce: str) -> dict[str, Any]:
    """Request 1: one fixed gateway-owned prompt, bounded output, no tools."""
    return {
        "model": protocol.model_id,
        "messages": [{"role": "user", "content": TEXT_PROMPT_TEMPLATE.format(nonce=nonce)}],
        "max_completion_tokens": protocol.text_output_tokens,
        "stream": False,
    }


def build_visual_request(
    protocol: ProbeProtocol,
    *,
    include_image: bool,
    include_tools: bool,
    png: bytes | None = None,
) -> dict[str, Any]:
    """Request 2: image understanding and/or a function tool, never executed.

    The image is generated locally and sent inline; the tool schema is a
    closed gateway-owned object. ``tool_choice`` stays ``auto`` so that a
    plain-text answer (image evidence) and a returned call (tool evidence)
    can both occur — each is then classified independently.
    """
    instruction = VISUAL_TOOL_INSTRUCTION if include_tools else TEXT_ONLY_INSTRUCTION
    content: list[dict[str, Any]] = [{"type": "text", "text": VISUAL_PROMPT.format(instruction=instruction)}]
    if include_image:
        image = png if png is not None else probe_png()
        encoded = base64.b64encode(image).decode("ascii")
        content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}})
    payload: dict[str, Any] = {
        "model": protocol.model_id,
        "messages": [{"role": "user", "content": content}],
        "max_completion_tokens": protocol.visual_output_tokens,
        "stream": False,
    }
    if include_tools:
        payload["tools"] = [TOOL_SCHEMA]
        payload["tool_choice"] = "auto"
    return payload


@dataclass
class PlannedRequest:
    """One bounded upstream request, measured before anything is sent."""

    group: str
    capabilities: tuple[str, ...]
    payload: dict[str, Any]
    nonce: str | None = None
    byte_length: int = 0


def plan_requests(protocol: ProbeProtocol, probes: list[str] | Any) -> list[PlannedRequest]:
    """Build every request and enforce the byte/image ceilings up front.

    At most two requests: one for text, one shared by image and function
    tools. A ceiling breach raises :class:`ProbeRefused`, so a refused run
    has already decided to send nothing.
    """
    selected = _validate_probes(probes)
    plan: list[PlannedRequest] = []

    if "text_generation" in selected:
        nonce = secrets.token_hex(16)
        item = PlannedRequest("text", ("text_generation",), build_text_request(protocol, nonce), nonce=nonce)
        item.byte_length = len(_body(item))
        if item.byte_length > protocol.max_request_bytes:
            raise ProbeRefused(
                "request_too_large",
                f"The bounded text probe request exceeds the {protocol.max_request_bytes}-byte ceiling; no request was sent.",
            )
        plan.append(item)

    visual_caps = tuple(c for c in ("image_input", "function_tools") if c in selected)
    if visual_caps:
        include_image = "image_input" in selected
        image = probe_png() if include_image else None
        if image is not None and len(image) > protocol.max_image_bytes:
            raise ProbeRefused(
                "image_too_large",
                f"The generated probe image exceeds the {protocol.max_image_bytes}-byte ceiling; no request was sent.",
            )
        item = PlannedRequest(
            "visual",
            visual_caps,
            build_visual_request(
                protocol,
                include_image=include_image,
                include_tools="function_tools" in selected,
                png=image,
            ),
        )
        item.byte_length = len(_body(item))
        if item.byte_length > protocol.max_request_bytes:
            raise ProbeRefused(
                "request_too_large",
                f"The bounded visual probe request exceeds the {protocol.max_request_bytes}-byte ceiling; no request was sent.",
            )
        plan.append(item)

    if len(plan) > protocol.max_requests:  # pragma: no cover - structural guard
        raise ProbeRefused("request_limit_exceeded", "The selected probes exceed the per-run request limit.")
    return plan


def _body(item: PlannedRequest) -> bytes:
    return json.dumps(item.payload, ensure_ascii=False).encode("utf-8")


def output_tokens_for(protocol: ProbeProtocol, item: PlannedRequest) -> int:
    """The capped output of one planned request (shared by its capabilities)."""
    if item.group == "text":
        return protocol.text_output_tokens
    return protocol.visual_output_tokens


def request_estimate(
    protocol: ProbeProtocol,
    price: dict[str, Any],
    item: PlannedRequest,
) -> dict[str, Any]:
    """Tariff math for one planned request.

    The output component is exact — the token cap is what the request may
    spend. The input component treats each request byte as at least one
    token (true for UTF-8 text) and is labelled non-authoritative, because
    no preflight token counter and no documented image tokenisation exist
    for this endpoint.
    """
    output_tokens = output_tokens_for(protocol, item)
    output_usd = output_tokens / 1_000_000 * float(price["output_per_m"])
    input_usd = item.byte_length / 1_000_000 * float(price["input_per_m"])
    return {
        "currency": "USD",
        "output_tokens_max": output_tokens,
        "output_component_usd": output_usd,
        "input_tokens_upper_bound": item.byte_length,
        "input_component_usd": input_usd,
        "total_usd": output_usd + input_usd,
        "input_basis": "request_bytes_upper_bound",
        "input_authoritative": False,
        "price_source": price.get("source"),
        "price_as_of": price.get("observed_at"),
        "hard_ceiling_available": False,
        "hard_ceiling_reason": (
            "OpenCode Go publishes no request-level max-charge control and no preflight token meter for this "
            "endpoint, so no enforceable dollar ceiling can be claimed; this is an estimate only."
        ),
    }


# --- preview ---------------------------------------------------------------

def _ceiling_usd(value: Any) -> float | None:
    """The owner's spend ceiling as a finite float, or ``None`` when unusable.

    ``NaN`` and ``±inf`` fail every ordering comparison, so a gate written as
    ``ceiling <= 0`` lets them through; an integer too large to represent as a
    float raises on conversion and would behave like ``+inf``. None of those is
    a ceiling a run may be authorised against.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        ceiling = float(value)
    except OverflowError:
        return None
    return ceiling if math.isfinite(ceiling) else None

def registry_coverage(staged_model_count: int | None = None) -> dict[str, Any]:
    """What the opt-in probe allowlist covers, so a refusal reads as policy.

    The protocol registry is deliberate: a pair joins it only after its
    bounded request and pricing contract has been reviewed. The console
    surfaces this *before* the owner previews, so "not registered" is never
    mistaken for a malfunction.
    """
    summary: dict[str, Any] = {
        "registered_pairs": [list(pair) for pair in sorted(PROBE_PROTOCOLS)],
        "registered_count": len(PROBE_PROTOCOLS),
    }
    if staged_model_count is not None:
        summary["staged_model_count"] = staged_model_count
    return summary


def _validate_target(provider_id: str, model_id: str) -> None:
    if not provider_id or len(provider_id) > 64 or not model_id or len(model_id) > 256:
        raise ValueError("invalid target")


def _base_preview(provider_id: str, model_id: str, probes: list[str], revision: int) -> dict[str, Any]:
    """The complete preview shape every path returns, including refusals.

    The console renders this shape whether or not a run is possible, so an
    unavailable preview carries the same fields as a ready one. This is the
    single definition of that shape (it previously lived in
    ``capability_discovery.preview_for``, whose bundled reason text claimed
    both a missing protocol and a missing price -- factually wrong for a
    model the catalogue does price, and unreachable from the routes).
    """
    return {
        "available": False,
        "provider_id": provider_id,
        "model_id": model_id,
        "probes": list(probes),
        "suite_version": SUITE_VERSION,
        "maximum_requests": len(probes),
        "max_output_tokens": 128,
        "timeout_seconds": 15,
        "concurrency": 1,
        "retries": 0,
        "streaming": False,
        "price": {"status": "unknown", "amount": None, "currency": "USD", "source": None},
        "reason": "",
        "base_revision": revision,
        "estimate_is_not_a_bill_guarantee": True,
    }


def _tariff_price_block(price: Any, status: str) -> dict[str, Any]:
    """The published tariff as it stands, honestly labelled.

    ``status`` mirrors :func:`price_status` (``known``/``stale``/``unknown``)
    and the per-million rates travel whenever the catalogue published them,
    even when the run is blocked by another gate -- a blocked probe never
    turns a real price into "Unknown". ``amount`` stays ``None`` here: the
    dollar amount is the worst-case run *estimate*, which only exists once a
    protocol supplies the request bounds.
    """
    block: dict[str, Any] = {
        "status": status,
        "amount": None,
        "currency": "USD",
        "source": price.get("source") if isinstance(price, dict) else None,
        "as_of": price.get("observed_at") if isinstance(price, dict) else None,
    }
    if isinstance(price, dict):
        for key in ("input_per_m", "output_per_m", "cached_read_per_m"):
            value = price.get(key)
            # Mirror price_status(): only a finite, non-negative rate is a
            # price. A non-finite one would serialise as the non-RFC-8259
            # tokens NaN/Infinity, which the console's JSON.parse rejects, so
            # omit the key rather than print a lie (or unparseable JSON).
            if (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(float(value))
                and float(value) >= 0
            ):
                block[key] = float(value)
    return block


def _owner_gate(owner: Any) -> tuple[str, str] | None:
    """Validate the owner's explicit selection. Returns the first refusal."""
    if not isinstance(owner, dict):
        return (
            "route_authorization_required",
            "The owner must select this configured OpenCode Go account as the billing route before a probe can run.",
        )
    if owner.get("authorize_billing_route") is not True:
        return (
            "route_authorization_required",
            "The owner must select this configured OpenCode Go account as the billing route before a probe can run.",
        )
    policy = owner.get("spend_policy")
    if policy == "hard_cap_required":
        return (
            "hard_cap_unavailable",
            "A hard dollar ceiling was requested, but no provider-enforced spend cap is verified for this "
            "account, so the run is refused rather than estimated as guaranteed.",
        )
    if policy != SPEND_POLICY_ESTIMATE:
        return (
            "spend_policy_required",
            "The owner must accept the estimate-based spend policy (spend_policy=estimate_accepted) before a "
            "probe can run; unknown pricing fails closed.",
        )
    ceiling = _ceiling_usd(owner.get("max_spend_estimate_usd"))
    if ceiling is None or ceiling <= 0:
        return (
            "spend_ceiling_required",
            "The owner must set a maximum estimated spend for this run before any request is sent.",
        )
    return None


def _unavailable(preview: dict[str, Any], code: str, reason: str) -> dict[str, Any]:
    preview = dict(preview)
    preview["available"] = False
    preview["code"] = code
    preview["reason"] = reason
    return preview


def build_preview(
    provider_id: str,
    model_id: str,
    probes: list[str],
    revision: int,
    *,
    owner: Any = None,
    now: datetime | None = None,
    price_lookup: Callable[[str, str], dict[str, Any] | None] | None = None,
) -> dict[str, Any]:
    """Render the full preview. Sends nothing, persists nothing.

    Always returns the complete preview shape — including when unavailable —
    so the console can show the owner exactly why a run is blocked. Each
    refusal names only the gate that refused it: a missing protocol entry is
    never reported as a missing price, and a published tariff is rendered
    even when the protocol gate blocks the run. Gate order is unchanged:
    target/selection, protocol, plan bounds, tariff freshness, owner
    selection, ceiling. The tariff lookup runs before the protocol gate
    because it is a local catalogue read — zero requests, zero cost — and
    the price must be honest in every refusal. An unusable ceiling (NaN,
    ±inf, a bool or an unrepresentable integer) also fails closed, below
    the protocol/tariff text.
    """
    _validate_target(provider_id, model_id)
    selected = _validate_probes(probes)
    base = _base_preview(provider_id, model_id, selected, revision)

    now = now or datetime.now(timezone.utc)
    lookup = price_lookup if price_lookup is not None else catalog_price_for
    price = lookup(provider_id, model_id)
    status = price_status(price, now)

    protocol = protocol_for(provider_id, model_id)
    if protocol is None:
        # Registry refusal. The price is *not* the blocker, so it is reported
        # exactly as the catalogue holds it and the reason says only what is
        # actually missing: the reviewed protocol entry.
        base["price"] = _tariff_price_block(price, status)
        return _unavailable(
            base,
            "no_probe_protocol",
            "This exact provider/model pair has no reviewed probe protocol registered, so the paid "
            "probe is blocked; probe registration is an opt-in allowlist, not a catalogue-wide "
            "setting. The published tariff below is shown for reference and is not the blocker.",
        )

    result = dict(base)
    result.update(
        {
            "available": False,
            "protocol_version": protocol.protocol_version,
            "protocol_dialect": protocol.dialect,
            "connect_timeout_seconds": protocol.connect_timeout_seconds,
            "run_deadline_seconds": protocol.run_deadline_seconds,
            "max_request_bytes": protocol.max_request_bytes,
            "max_image_bytes": protocol.max_image_bytes,
            "hard_ceiling_available": False,
            "estimate_is_not_a_bill_guarantee": True,
            "base_revision": revision,
            "code": "ready",
            "reason": "",
        }
    )
    result["price"] = _tariff_price_block(price, status)

    try:
        plan = plan_requests(protocol, probes)
    except ProbeRefused as exc:
        return _unavailable(result, exc.code, exc.message)

    result["maximum_requests"] = len(plan)
    result["max_output_tokens"] = sum(output_tokens_for(protocol, item) for item in plan)
    result["request_bytes_upper_bound"] = sum(item.byte_length for item in plan)

    tariff = price if status == "known" else None
    if tariff is None:
        result["estimate"] = {
            "currency": "USD",
            "total_usd": None,
            "output_component_usd": None,
            "input_component_usd": None,
            "hard_ceiling_available": False,
            "price_source": price.get("source") if isinstance(price, dict) else None,
            "price_as_of": price.get("observed_at") if isinstance(price, dict) else None,
        }
        if status == "stale" and isinstance(price, dict):
            return _unavailable(
                result,
                "no_trusted_price",
                f"The published OpenCode Go tariff for this exact model was observed on "
                f"{price.get('observed_at')} and is stale; paid probes are blocked until it is refreshed.",
            )
        return _unavailable(
            result,
            "no_trusted_price",
            "No published, fresh OpenCode Go tariff is available for this exact provider/model; paid probes are blocked.",
        )

    estimates = [request_estimate(protocol, tariff, item) for item in plan]
    total = sum(entry["total_usd"] for entry in estimates)
    result["estimate"] = {
        **{key: value for key, value in estimates[0].items() if key not in {"total_usd", "output_tokens_max"}},
        "output_tokens_max": sum(entry["output_tokens_max"] for entry in estimates),
        "output_component_usd": sum(entry["output_component_usd"] for entry in estimates),
        "input_component_usd": sum(entry["input_component_usd"] for entry in estimates),
        "input_tokens_upper_bound": sum(entry["input_tokens_upper_bound"] for entry in estimates),
        "total_usd": total,
    }
    result["price"] = {
        "status": "known",
        "amount": total,
        "currency": "USD",
        "source": tariff.get("source"),
        "as_of": tariff.get("observed_at"),
    }

    refusal = _owner_gate(owner)
    if refusal is not None:
        return _unavailable(result, refusal[0], refusal[1])
    ceiling = _ceiling_usd(owner.get("max_spend_estimate_usd"))
    if ceiling is None or ceiling < total:
        # Defence in depth: the gate above already refused an unusable ceiling;
        # if this comparison is ever reached with one (NaN, ±inf or an integer
        # too large to represent), it counts as exceeded, never as open-ended.
        shown = "an unusable value" if ceiling is None else f"{ceiling:.8f} USD"
        return _unavailable(
            result,
            "spend_ceiling_exceeded",
            f"The worst-case estimate ({total:.8f} USD) exceeds the owner's maximum estimated spend "
            f"({shown}) for this run.",
        )

    result["available"] = True
    result["code"] = "ready"
    result["reason"] = (
        f"{len(plan)} bounded synthetic request(s) are ready for explicit confirmation: no request has been "
        "sent, no retry or fallback will occur, and the estimate is not a bill guarantee."
    )
    return result


def mark_unavailable(preview: dict[str, Any], code: str, reason: str) -> dict[str, Any]:
    """Flip an otherwise-ready preview to unavailable (target-level gates)."""
    return _unavailable(preview, code, reason)


# --- confirmations ---------------------------------------------------------

class ConfirmationError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def preview_binding(preview: dict[str, Any], owner: dict[str, Any]) -> str:
    """A digest of everything the owner actually confirmed."""
    material = {
        "provider_id": preview.get("provider_id"),
        "model_id": preview.get("model_id"),
        "probes": preview.get("probes"),
        "suite_version": preview.get("suite_version"),
        "protocol_version": preview.get("protocol_version"),
        "maximum_requests": preview.get("maximum_requests"),
        "max_output_tokens": preview.get("max_output_tokens"),
        "base_revision": preview.get("base_revision"),
        "estimate": (preview.get("estimate") or {}).get("total_usd"),
        "price_as_of": (preview.get("estimate") or {}).get("price_as_of"),
        "authorize_billing_route": owner.get("authorize_billing_route"),
        "spend_policy": owner.get("spend_policy"),
        "max_spend_estimate_usd": owner.get("max_spend_estimate_usd"),
    }
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class ConfirmationStore:
    """Short-lived, single-use, server-bound run confirmations.

    A confirmation is issued by a preview and consumed by exactly one run.
    It carries no authority of its own: the run re-checks every gate and
    re-derives the binding, so a confirmation can never widen what the
    preview showed.
    """

    def __init__(self, ttl: int = CONFIRMATION_TTL_SECONDS, max_entries: int = MAX_CONFIRMATIONS) -> None:
        self._ttl = ttl
        self._max = max_entries
        self._lock = threading.Lock()
        self._entries: dict[str, tuple[str, float]] = {}

    def issue(self, binding: str, *, now: float | None = None) -> tuple[str, float]:
        moment = time.time() if now is None else now
        token = secrets.token_urlsafe(24)
        with self._lock:
            self._prune(moment)
            self._entries[token] = (binding, moment + self._ttl)
            while len(self._entries) > self._max:
                self._entries.pop(next(iter(self._entries)))
        return token, moment + self._ttl

    def consume(self, token: Any, binding: str, *, now: float | None = None) -> None:
        moment = time.time() if now is None else now
        if not isinstance(token, str) or not token:
            raise ConfirmationError("confirmation_required", "This run requires the confirmation issued by its preview.")
        with self._lock:
            self._prune(moment)
            entry = self._entries.pop(token, None)
        if entry is None:
            raise ConfirmationError(
                "confirmation_invalid",
                "The run confirmation is unknown, already used, or expired; preview again to run these probes.",
            )
        stored_binding, expires_at = entry
        if moment > expires_at:
            raise ConfirmationError("confirmation_expired", "The run confirmation has expired; preview again.")
        if not secrets.compare_digest(stored_binding, binding):
            raise ConfirmationError(
                "confirmation_mismatch",
                "The run does not match the preview that issued its confirmation; preview again.",
            )

    def _prune(self, moment: float) -> None:
        for token, (_binding, expires_at) in list(self._entries.items()):
            if moment > expires_at:
                self._entries.pop(token, None)


class RunGate:
    """Concurrency 1, plus owner cancellation of the run in flight."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: dict[tuple[str, str], threading.Event] = {}

    def acquire(self, provider_id: str, model_id: str) -> threading.Event:
        with self._lock:
            if self._active:
                raise ProbeRefused(
                    "probe_run_in_progress",
                    "Another capability probe run is already in flight; concurrency is limited to one.",
                    status=409,
                )
            event = threading.Event()
            self._active[(provider_id, model_id)] = event
            return event

    def release(self, provider_id: str, model_id: str) -> None:
        with self._lock:
            self._active.pop((provider_id, model_id), None)

    def cancel(self, provider_id: str, model_id: str) -> bool:
        with self._lock:
            event = self._active.get((provider_id, model_id))
            if event is None:
                return False
            event.set()
            return True

    @property
    def active_targets(self) -> list[tuple[str, str]]:
        with self._lock:
            return list(self._active)


# --- response parsing and classification -----------------------------------

def _choice(payload: Any) -> dict[str, Any] | None:
    """The first Chat Completions choice — ``finish_reason`` lives here."""
    if not isinstance(payload, dict):
        return None
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    first = choices[0]
    return first if isinstance(first, dict) else None


def _choice_message(payload: Any) -> dict[str, Any] | None:
    choice = _choice(payload)
    if choice is None or not isinstance(choice.get("message"), dict):
        return None
    return choice["message"]


def _finish_reason(payload: Any) -> Any:
    choice = _choice(payload)
    return choice.get("finish_reason") if choice is not None else None


def _message_text(message: dict[str, Any]) -> str | None:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
                parts.append(part["text"])
        return "".join(parts) if parts else None
    return None


def _clean(message: str) -> str:
    """Bound and sanitize a gateway-authored evidence summary."""
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", message)
    return text[:_MAX_SUMMARY].strip()


def classify_text(payload: Any, nonce: str) -> tuple[str, str]:
    """Request 1 evidence: the nonce must come back byte-for-byte."""
    message = _choice_message(payload)
    if message is None:
        return "inconclusive", "the response held no well-formed choice"
    text = _message_text(message)
    if text is None:
        return "inconclusive", "the response held no plain-text completion"
    if _finish_reason(payload) != "stop":
        return "inconclusive", "the completion did not finish with a clean stop"
    if text != nonce:
        return "inconclusive", "the completed response did not echo the run nonce"
    return "observed_supported", "exact nonce match on a completed response"


def image_text_matches(text: str) -> bool:
    """A narrow, deterministic reading of a plain-text visual answer."""
    normalized = re.sub(r"[^a-z0-9]+", " ", text.lower())
    has_count = re.search(r"\b(2|two)\b", normalized) is not None
    return (
        has_count
        and "blue" in normalized
        and "circle" in normalized
        and "red" in normalized
        and "square" in normalized
    )


def _validate_tool_answer(message: dict[str, Any]) -> tuple[bool, bool]:
    """Inspect — never execute — a returned tool call.

    Returns ``(schema_valid, values_match_image)``. Anything malformed is
    ``False``: an unusable call is inconclusive, never evidence against the
    capability.
    """
    calls = message.get("tool_calls")
    if not isinstance(calls, list):
        return False, False
    target = None
    for call in calls:
        if isinstance(call, dict) and isinstance(call.get("function"), dict):
            if call["function"].get("name") == TOOL_NAME:
                target = call["function"]
                break
    if target is None:
        return False, False
    arguments = target.get("arguments")
    if not isinstance(arguments, str):
        return False, False
    try:
        parsed = json.loads(arguments)
    except ValueError:
        return False, False
    if not isinstance(parsed, dict) or set(parsed) != {"blue_circle_count", "red_square_present"}:
        return False, False
    count = parsed["blue_circle_count"]
    present = parsed["red_square_present"]
    if type(count) is not int or type(present) is not bool:
        return False, False
    return True, count == BLUE_CIRCLE_COUNT and present is RED_SQUARE_PRESENT


def classify_visual(payload: Any, *, image_probe: bool, tool_probe: bool) -> dict[str, str]:
    """Request 2 evidence: image understanding and tool transport, apart.

    Each capability is judged on its own predicate. A correct answer in
    plain text proves image understanding only; a schema-valid call proves
    tool transport only, and proves image understanding only when its facts
    match the image. A wrong, missing or malformed call is inconclusive.
    """
    message = _choice_message(payload)
    if message is None:
        return {
            capability: "inconclusive"
            for capability, wanted in (("image_input", image_probe), ("function_tools", tool_probe))
            if wanted
        }

    finish_ok = _finish_reason(payload) in {"stop", "tool_calls"}
    text = _message_text(message)
    schema_valid, values_match = _validate_tool_answer(message)
    outcomes: dict[str, str] = {}

    if image_probe:
        if not finish_ok:
            outcomes["image_input"] = "inconclusive"
        elif schema_valid and values_match:
            outcomes["image_input"] = "observed_supported"
        elif text is not None and image_text_matches(text):
            outcomes["image_input"] = "observed_supported"
        elif schema_valid:
            outcomes["image_input"] = "inconclusive"
        else:
            outcomes["image_input"] = "inconclusive"

    if tool_probe:
        if not finish_ok:
            outcomes["function_tools"] = "inconclusive"
        elif schema_valid:
            outcomes["function_tools"] = "observed_supported"
        else:
            outcomes["function_tools"] = "inconclusive"

    return outcomes


def evidence_summary_for(capability: str, status: str, payload: Any) -> str:
    """A bounded, gateway-authored summary. Never provider or model text."""
    message = _choice_message(payload)
    if status == "not_tested":
        return "not sent"
    if capability == "text_generation":
        if message is None:
            return "the response held no well-formed choice"
        text = _message_text(message)
        if text is None:
            return "the response held no plain-text completion"
        if _finish_reason(payload) != "stop":
            return "the completion did not finish with a clean stop"
        return "exact nonce match on a completed response" if status == "observed_supported" else "the completed response did not echo the run nonce"
    schema_valid, values_match = _validate_tool_answer(message) if message is not None else (False, False)
    if capability == "image_input":
        if status == "observed_supported":
            if schema_valid and values_match:
                return "schema-valid tool answer matched the synthetic image facts"
            return "natural-language answer matched the synthetic image facts"
        if schema_valid and values_match:
            return "the answer did not match the synthetic image facts"
        return "the answer did not match the synthetic image facts"
    if status == "observed_supported":
        return f"tool_calls named {TOOL_NAME} with schema-valid arguments"
    if message is not None and isinstance(message.get("tool_calls"), list):
        return "returned tool arguments did not validate against the closed schema"
    return f"no {TOOL_NAME} tool call was returned"


def _usage_tokens(usage: Any) -> dict[str, int | bool | None]:
    """The token counters a run body may report, or None where they were refused.

    Provider-controlled: a counter above
    :data:`gateway.bridge.streams.MAX_TOKEN_COUNTER` reads as unknown here
    rather than serialising into the run body as a JSON number the console's
    ``JSON.parse`` turns into ``Infinity``. A *reported* counter the bound
    refuses is recorded, never erased: ``invalid_usage: true`` travels beside
    the nulls so the evidence can tell "the provider gave a number this
    gateway will not carry" from "the provider reported no usage at all"
    (SEC-R66-C information loss). The marker is a bool — it cannot itself
    become a number the console reads as Infinity.
    """
    if not isinstance(usage, dict):
        return {"input_tokens": None, "output_tokens": None}
    tokens: dict[str, int | bool | None] = {
        "input_tokens": usable_token_count(usage.get("prompt_tokens")),
        "output_tokens": usable_token_count(usage.get("completion_tokens")),
    }
    if reported_counters_unusable(usage):
        tokens[INVALID_USAGE_MARKER] = True
    return tokens


def _usable_cost(cost: Any) -> float | None:
    """Return ``cost`` as a usable non-negative finite float, else ``None``.

    A provider's ``usage.cost`` arrives from JSON, where an overflowing
    literal (``1e400``) parses to ``inf`` and a bare ``NaN`` token is
    accepted by ``json.loads``: neither is a usable cost, and neither may
    reach a persisted evidence claim or a response body. An integer too
    large to represent as a float is likewise unusable.
    """
    if isinstance(cost, bool) or not isinstance(cost, (int, float)):
        return None
    try:
        value = float(cost)
    except (OverflowError, ValueError):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return value


def _accumulate_cost(total: float | None, added: float) -> float | None:
    """Add ``added`` to the running total, or ``None`` if the sum is unusable.

    Every operand already passed :func:`_usable_cost`, but that bounds the
    *operands*, not the sum: two finite values past half the float range
    (``1e308 + 1e308``, ``-1e308 + -1e308``) add to ``±inf``, and a total
    that is not a finite float has no RFC-8259 spelling. The caller must
    therefore fail closed when this returns ``None`` rather than carry a
    value ``json_response`` would emit as the non-standard ``Infinity``
    token — the same escape the value boundary exists to stop (SEC-R66-A).
    """
    candidate = (0.0 if total is None else total) + added
    if not math.isfinite(candidate):
        return None
    return candidate


def _downgrade_claimed_results(pending: dict[str, dict[str, Any]], summary: str) -> None:
    """Replace every claimed result of a run with ``inconclusive``.

    Used when the run's accumulated cost stops being representable: a run
    whose total cannot be stated must not claim any capability, so every
    ``observed_*`` row is withdrawn. Probes the runner never sent keep their
    honest ``not_tested`` — an unrun probe is not an inconclusive one.

    The row is edited in place rather than rebuilt. A rebuild would drop
    ``reported_usage``, ``spend_estimate`` and the row's own
    ``request_count``, so a refused counter recorded by an earlier response
    would vanish with it and an overflow run would read exactly like a run
    where the provider reported no usage at all — the SEC-R66-C
    information-loss shape, in the branch where the cost-overflow and the
    invalid-usage probes intersect (SEC-1). A reported refusal is recorded,
    never erased; nothing raw travels either way, because ``reported_usage``
    only ever holds bounded counters or the boolean marker.
    """
    for row in pending.values():
        if row.get("status") == "not_tested":
            continue
        row["status"] = "inconclusive"
        row["evidence_summary"] = _clean(summary)
        row["error_class"] = None


def cost_accounting_ok(usage: Any) -> bool:
    """Whether the response carries cost accounting the policy requires.

    OpenCode's inference API documents ``usage.cost``. Without it the run
    has no evidence that the request was accounted for, so the scope's rule
    applies: stop before the next request and leave the unrun capabilities
    ``not_tested``. The billing-source *vocabulary* is not documented, so
    only its structure is checked — never a guessed meaning. A non-finite
    cost (``NaN`` or ±inf) is not cost accounting: it is unusable, so the
    same inconclusive rule applies rather than a claim.
    """
    if not isinstance(usage, dict):
        return False
    if _usable_cost(usage.get("cost")) is None:
        return False
    if "opencode" in usage:
        inner = usage.get("opencode")
        if not isinstance(inner, dict) or "billing_source" in inner and not isinstance(inner.get("billing_source"), str):
            return False
    return True


def _reported_cost(usage: Any) -> float | None:
    if not isinstance(usage, dict):
        return None
    return _usable_cost(usage.get("cost"))


def evidence_error_class(error: AdapterError) -> str:
    """Map a classified adapter failure onto the evidence vocabulary."""
    mapping = {
        ERROR_AUTH: "auth",
        ERROR_POLICY: "policy",
        ERROR_RATE_LIMITED: "rate_limited",
        ERROR_MALFORMED: "malformed",
        ERROR_EXHAUSTED: "billing",
        ERROR_ROUTE_GONE: "route_gone",
        ERROR_UNKNOWN: "unknown",
    }
    if error.error_class in mapping:
        return mapping[error.error_class]
    if error.error_class == ERROR_TRANSIENT:
        # Messages here are gateway-authored constants, so this reads a
        # known literal rather than provider text.
        return "timeout" if "timeout" in (error.message or "") else "transport"
    return "unknown"


# --- execution -------------------------------------------------------------

@dataclass
class ProbeOutcome:
    """What one run observed. Evidence only — no content, no credentials."""

    results: list[dict[str, Any]] = field(default_factory=list)
    request_count: int = 0
    blocked_reason: str | None = None
    cancelled: bool = False
    session_id: str = ""
    reported_cost_usd: float | None = None


def _result(
    capability: str,
    status: str,
    summary: str,
    *,
    error_class: str | None = None,
    request_count: int = 0,
    usage: dict[str, int | bool | None] | None = None,
    spend: dict[str, Any] | None = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "capability": capability,
        "status": status,
        "evidence_summary": _clean(summary),
        "error_class": error_class,
        "request_count": request_count,
    }
    if usage is not None:
        entry["reported_usage"] = usage
    if spend is not None:
        entry["spend_estimate"] = spend
    return entry


def run_suite(
    protocol: ProbeProtocol,
    probes: list[str],
    transport: Callable[..., ProviderResponse],
    *,
    price: dict[str, Any] | None = None,
    cancel_event: threading.Event | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    plan: list[PlannedRequest] | None = None,
    session_id: str | None = None,
) -> ProbeOutcome:
    """Execute the bounded suite: one shot per request, no retry, no fallback.

    The runner stops — and marks the remaining capabilities ``not_tested`` —
    on the first transport failure, on an unusable response, on absent cost
    accounting, on cancellation, and at the run deadline. When the running
    total of the reported costs stops being a finite float (two usable
    operands can still add to ``inf``), the run stops the same way and
    withdraws every claim it made: ``reported_cost_usd`` stays ``None``,
    so no response body can carry ``Infinity``. A returned tool
    call is read as data and never invoked.
    """
    selected = _validate_probes(probes)
    prepared = plan if plan is not None else plan_requests(protocol, selected)
    started = monotonic()
    session = session_id or secrets.token_hex(16)
    outcome = ProbeOutcome(session_id=session)

    pending: dict[str, dict[str, Any]] = {
        capability: _result(capability, "not_tested", "not sent: the run has not reached this probe")
        for capability in selected
    }

    for item in prepared:
        if cancel_event is not None and cancel_event.is_set():
            outcome.cancelled = True
            outcome.blocked_reason = "cancelled"
            break
        if monotonic() - started > protocol.run_deadline_seconds:
            outcome.blocked_reason = "deadline"
            break

        outcome.request_count += 1
        try:
            transport_kwargs = {
                "timeout": protocol.request_timeout_seconds,
                "connect_timeout": protocol.connect_timeout_seconds,
                "session_id": session,
            }
            parameters = inspect.signature(transport).parameters
            if "deadline" in parameters:
                transport_kwargs["deadline"] = started + protocol.run_deadline_seconds
                transport_kwargs["cancel_event"] = cancel_event
                transport_kwargs["max_response_bytes"] = protocol.max_response_bytes
            response = transport(item.payload, **transport_kwargs)
        except AdapterError as exc:
            if cancel_event is not None and cancel_event.is_set():
                outcome.cancelled = True
                outcome.blocked_reason = "cancelled"
            elif monotonic() - started >= protocol.run_deadline_seconds:
                outcome.blocked_reason = "deadline"
            failure = evidence_error_class(exc)
            for capability in item.capabilities:
                pending[capability] = _result(
                    capability,
                    "inconclusive",
                    f"the upstream request failed ({failure}); a failure is never evidence of an unsupported capability",
                    error_class=failure,
                    request_count=outcome.request_count,
                )
            if not outcome.cancelled and outcome.blocked_reason != "deadline":
                outcome.blocked_reason = "prior_failure"
            break
        except Exception:  # noqa: BLE001 - fail closed on anything unexpected
            # Nothing the transport raises may escape the runner. An
            # unhandled error here reaches the owner API as an unclassified
            # 500 with no run recorded - the confirmation consumed and the
            # evidence trail empty (QA re-gate F3). Whatever escaped, the
            # honest outcome is the one every transport failure gets:
            # inconclusive evidence, an honest stop reason, and no claim
            # about the capability.
            if cancel_event is not None and cancel_event.is_set():
                outcome.cancelled = True
                outcome.blocked_reason = "cancelled"
            elif monotonic() - started >= protocol.run_deadline_seconds:
                outcome.blocked_reason = "deadline"
            for capability in item.capabilities:
                pending[capability] = _result(
                    capability,
                    "inconclusive",
                    "the upstream request failed (transport); a failure is never evidence of an unsupported capability",
                    error_class="transport",
                    request_count=outcome.request_count,
                )
            if not outcome.cancelled and outcome.blocked_reason != "deadline":
                outcome.blocked_reason = "prior_failure"
            break

        payload: Any = None
        if response.body is not None and len(response.body) > protocol.max_response_bytes:
            for capability in item.capabilities:
                pending[capability] = _result(
                    capability, "inconclusive", "the upstream response exceeded the configured byte limit",
                    error_class="response_too_large", request_count=outcome.request_count,
                )
            outcome.blocked_reason = "prior_failure"
            break
        try:
            if response.body:
                payload = json.loads(response.body.decode("utf-8", "replace"))
        except ValueError:
            payload = None
        if not isinstance(payload, dict):
            for capability in item.capabilities:
                pending[capability] = _result(
                    capability,
                    "inconclusive",
                    "the response was not a well-formed object",
                    error_class="malformed",
                    request_count=outcome.request_count,
                )
            outcome.blocked_reason = "prior_failure"
            break

        # Support evidence is only attributable when the provider echoes the
        # exact reviewed model. Missing or different IDs are inconclusive.
        if payload.get("model") != protocol.model_id:
            for capability in item.capabilities:
                pending[capability] = _result(
                    capability, "inconclusive", "the response did not identify the exact reviewed model",
                    error_class="model_mismatch", request_count=outcome.request_count,
                )
            outcome.blocked_reason = "prior_failure"
            break

        usage = payload.get("usage")
        usage_meta = _usage_tokens(usage)
        accounted = cost_accounting_ok(usage)
        reported = _reported_cost(usage)
        cost_total_overflow = False
        if reported is not None:
            total = _accumulate_cost(outcome.reported_cost_usd, reported)
            if total is None:
                # Every operand was a usable cost, but their running sum is
                # not representable (SEC-R66-A). The value is dropped, and
                # the run-level rule below withdraws every claim it would
                # otherwise make: a body must never carry ``Infinity``.
                cost_total_overflow = True
                outcome.reported_cost_usd = None
            else:
                outcome.reported_cost_usd = total
        spend = request_estimate(protocol, price, item) if price is not None else None
        if spend is not None:
            spend = {
                "amount": spend["total_usd"],
                "currency": "USD",
                "source": spend["price_source"],
                "as_of": spend["price_as_of"],
            }

        if item.group == "text":
            status, summary = classify_text(payload, item.nonce or "")
            statuses = {"text_generation": (status, summary)}
        else:
            classified = classify_visual(
                payload,
                image_probe="image_input" in item.capabilities,
                tool_probe="function_tools" in item.capabilities,
            )
            statuses = {
                capability: (classified[capability], evidence_summary_for(capability, classified[capability], payload))
                for capability in item.capabilities
            }

        for capability, (status, summary) in statuses.items():
            if not accounted:
                # Cost accounting is absent or unusable: the scope's rule is
                # to downgrade rather than claim a result we cannot account
                # for, and to stop before the next request.
                pending[capability] = _result(
                    capability,
                    "inconclusive",
                    "the response carried no usable cost accounting, so the outcome is inconclusive",
                    error_class=None,
                    request_count=outcome.request_count,
                    usage=usage_meta,
                    spend=spend,
                )
            else:
                pending[capability] = _result(
                    capability,
                    status,
                    summary,
                    error_class=None,
                    request_count=outcome.request_count,
                    usage=usage_meta,
                    spend=spend,
                )

        if cost_total_overflow:
            # Fail closed at the run level: the total this run's accounting
            # would report cannot be represented, so no claim from this run
            # survives and the stop reason names the accounting — never a
            # success state, never a 5xx (the run already spent and
            # persisted; the body must stay parseable either way).
            _downgrade_claimed_results(
                pending,
                "the run's accumulated cost total is unrepresentable, so no capability is claimed",
            )
            outcome.blocked_reason = "cost_accounting"
            break

        if not accounted:
            outcome.blocked_reason = "cost_accounting"
            break

    outcome.results = [pending[capability] for capability in selected]
    return outcome
