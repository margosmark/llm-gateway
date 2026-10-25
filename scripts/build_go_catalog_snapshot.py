#!/usr/bin/env python3
"""Regenerate the OpenCode Go catalog snapshot.

The snapshot is the gateway's knowledge of what the OpenCode Go provider
offers: the model id, the wire dialect it is served over, the published
monthly allowance, the token prices, and the retention class.

It is *published* information, transcribed from the provider's own
documentation. It is **not** probed at runtime and it is **not** a guess.
Every row carries the source URL and the date the table was read.

Regenerate with:

    python3 scripts/build_go_catalog_snapshot.py

The live catalog endpoint (``GET https://opencode.ai/inference/go/v1/models``)
lists more ids than the published table does. Ids that appear live but not in
the published table are recorded with ``dialect: null`` and
``limits_source: "unknown"``, so the gateway reports them as unknown rather
than guessing an endpoint or an allowance.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SOURCE_URL = "https://opencode.ai/docs/go/"
CATALOG_URL = "https://opencode.ai/inference/go/v1/models"
OBSERVED_AT = "2026-10-22"

# --- dialects, per the published "Endpoints" table -------------------------
#
# The published table lists these models. The eight ids in LIVE_ONLY below
# are served by the provider but have no published endpoint row.
ENDPOINTS: dict[str, tuple[str, str]] = {
    # model id -> (dialect key, published path)
    "grok-4.7": ("openai_responses", "/openai/v1/responses"),
    "gpt-6-luna": ("openai_responses", "/openai/v1/responses"),
    "gpt-5.6-luna": ("openai_responses", "/openai/v1/responses"),
    "muse-spark-1.3-contributor": ("openai_responses", "/openai/v1/responses"),
    "muse-spark-1.2-contributor": ("openai_responses", "/openai/v1/responses"),
    "claude-haiku-5-5": ("anthropic_messages", "/anthropic/v1/messages"),
    "minimax-m3": ("anthropic_messages", "/anthropic/v1/messages"),
    "qwen3.8-max": ("anthropic_messages", "/anthropic/v1/messages"),
    "qwen3.8-flash": ("anthropic_messages", "/anthropic/v1/messages"),
    "qwen3.7-plus": ("anthropic_messages", "/anthropic/v1/messages"),
    "gemini-4-argon-contributor": ("google_v1beta", "/google/v1beta/models/gemini-4-argon-contributor"),
    "glm-5.3-flash": ("openai_chat", "/openai/v1/chat/completions"),
    "glm-5.3": ("openai_chat", "/openai/v1/chat/completions"),
    "glm-5.2": ("openai_chat", "/openai/v1/chat/completions"),
    "kimi-k3": ("openai_chat", "/openai/v1/chat/completions"),
    "kimi-k2.7-code": ("openai_chat", "/openai/v1/chat/completions"),
    "longcat-2.0": ("openai_chat", "/openai/v1/chat/completions"),
    "deepseek-v4.1-flash": ("openai_chat", "/openai/v1/chat/completions"),
    "deepseek-v4-pro": ("openai_chat", "/openai/v1/chat/completions"),
    "mimo-v2.6-flash": ("openai_chat", "/openai/v1/chat/completions"),
    "mimo-v2.6-pro": ("openai_chat", "/openai/v1/chat/completions"),
    "mimo-v2.5": ("openai_chat", "/openai/v1/chat/completions"),
    "mimo-v2.5-pro": ("openai_chat", "/openai/v1/chat/completions"),
    "minimax-m3.1-flash": ("openai_chat", "/openai/v1/chat/completions"),
    "longcat-2.5-preview-free": ("openai_chat", "/openai/v1/chat/completions"),
    "step-5-preview": ("openai_chat", "/openai/v1/chat/completions"),
    "beam-free": ("openai_chat", "/openai/v1/chat/completions"),
    "hy4-preview": ("openai_chat", "/openai/v1/chat/completions"),
    "hy3": ("openai_chat", "/openai/v1/chat/completions"),
}

#: Ids the live catalog returned that the published table does not document.
LIVE_ONLY = (
    "glm-5.1",
    "grok-4.6",
    "omen-alpha",
    "kimi-k2.6",
    "minimax-m2.5",
    "minimax-m2.7",
    "qwen3.6-plus",
    "qwen3.7-max",
)

# --- monthly allowances (Go plan) ----------------------------------------
MONTHLY_LIMIT_GO = {
    "glm-5.3": 15.0,
    "kimi-k3": 15.0,
    "mimo-v2.6-pro": 15.0,
    "qwen3.8-max": 15.0,
    "grok-4.7": 15.0,
    "gpt-6-luna": 15.0,
    "gpt-5.6-luna": 15.0,
    "claude-haiku-5-5": 15.0,
    "deepseek-v4-pro": 15.0,
    "qwen3.8-flash": 30.0,
    "hy4-preview": 30.0,
    "glm-5.3-flash": 60.0,
    "glm-5.2": 60.0,
    "kimi-k2.7-code": 60.0,
    "mimo-v2.6-flash": 60.0,
    "mimo-v2.5": 60.0,
    "minimax-m3.1-flash": 20.0,
    "minimax-m3": 60.0,
    "muse-spark-1.2-contributor": 60.0,
    "muse-spark-1.3-contributor": 60.0,
    "longcat-2.0": 60.0,
    "hy3": 60.0,
    "step-5-preview": 60.0,
    "qwen3.7-plus": 60.0,
    "gemini-4-argon-contributor": 60.0,
    "deepseek-v4.1-flash": 80.0,
}

#: Free models remain usable after the included allowance is exhausted.
UNLIMITED = ("longcat-2.5-preview-free", "beam-free")

# --- retention, from the published privacy table --------------------------
RETENTION_30_DAYS = (
    "gpt-6-luna",
    "gpt-5.6-luna",
    "grok-4.7",
    "claude-haiku-5-5",
    "beam-free",
)
RETENTION_TRAINING = (
    "gemini-4-argon-contributor",
    "muse-spark-1.2-contributor",
    "muse-spark-1.3-contributor",
)

#: Models the provider has deprecated or is about to.
DEPRECATED = {
    "mimo-v2.5": "deprecated 2026-10-21 10:00 CST; auto-routed to V2.6 since 2026-10-14",
    "mimo-v2.5-pro": "deprecated 2026-10-21 10:00 CST; auto-routed to V2.6 since 2026-10-14",
}

#: Token prices per 1M tokens on the Go plan. ``None`` means "free".
PRICES: dict[str, tuple[float | None, float | None, float | None]] = {
    "glm-5.3-flash": (0.15, 0.50, 0.03),
    "glm-5.3": (1.40, 4.40, 0.26),
    "glm-5.2": (1.40, 4.40, 0.26),
    "kimi-k3": (3.00, 15.00, 0.30),
    "kimi-k2.7-code": (0.95, 4.00, 0.19),
    "longcat-2.0": (0.30, 1.20, 0.006),
    "longcat-2.5-preview-free": (None, None, None),
    "step-5-preview": (1.00, 2.70, 0.10),
    "beam-free": (None, None, None),
    "mimo-v2.6-flash": (0.14, 0.28, 0.0028),
    "mimo-v2.6-pro": (0.435, 0.87, 0.003625),
    "mimo-v2.5": (0.14, 0.28, 0.0028),
    "mimo-v2.5-pro": (0.435, 0.87, 0.003625),
    "minimax-m3.1-flash": (0.15, 0.60, 0.015),
    "minimax-m3": (0.30, 1.20, 0.06),
    "muse-spark-1.3-contributor": (0.10, 0.20, 0.002),
    "muse-spark-1.2-contributor": (0.10, 0.20, 0.002),
    "qwen3.8-max": (2.00, 6.00, 0.25),
    "qwen3.8-flash": (0.15, 0.47, 0.016),
    "qwen3.7-plus": (0.40, 1.60, 0.04),
    "deepseek-v4.1-flash": (0.15, 0.60, 0.003),
    "deepseek-v4-pro": (0.66, 1.98, 0.022),
    "hy4-preview": (0.834, 2.501, 0.042),
    "hy3": (0.14, 0.58, 0.035),
    "grok-4.7": (2.00, 6.00, 0.50),
    "gpt-6-luna": (0.10, 0.50, 0.01),
    "gpt-5.6-luna": (0.20, 1.20, 0.02),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
    "gemini-4-argon-contributor": (4.00, 20.00, 0.20),
}

#: Context-tier notes, where the published prices change with context size.
CONTEXT_TIERS = {
    "gpt-6-luna": "prices double above 272K input tokens",
    "gpt-5.6-luna": "prices double above 272K input tokens",
    "claude-haiku-5-5": "prices increase above 100K input tokens",
    "qwen3.7-plus": "prices increase above 256K input tokens",
    "grok-4.7": "prices increase above 200K input tokens",
    "deepseek-v4.1-flash": "off-peak and peak prices apply by hour",
    "deepseek-v4-pro": "off-peak and peak prices apply by hour",
}

#: Context information from the *model providers'* official documentation.
#:
#: OpenCode Go documents that these exact model IDs are served over the listed
#: endpoints, but it does not separately impose or publish a smaller service
#: context cap.  These are therefore model windows, not an invented Go quota.
#: The URLs stay with the snapshot so an operator can review this conclusion.
MODEL_LIMITS: dict[str, dict[str, Any]] = {
    "mimo-v2.6-flash": {
        "context_window": 1_048_576,
        "context_limit": 1_048_576,
        "max_output_tokens": 131_072,
        "source": "https://platform.xiaomimimo.com/docs/en-US/quick-start/model",
        "note": (
            "MiMo documents a 1M context window and a 131072-token maximum completion; "
            "thinking defaults to enabled and is a binary thinking.type control, not an effort level."
        ),
    },
    "minimax-m3.1-flash": {
        "context_window": 1_000_000,
        "context_limit": 1_000_000,
        "max_output_tokens": None,
        "source": "https://platform.minimax.io/docs/guides/text-generation",
        "note": (
            "MiniMax documents a 1,000,000-token context window and the low/medium/high/xhigh/max "
            "reasoning-effort vocabulary; it does not publish a maximum output on this page."
        ),
    },
}

#: Reasoning-effort support, where the model provider documents it.
REASONING_EFFORTS = {
    "gpt-6-luna": ("none", "low", "medium", "high", "xhigh", "max"),
    "gpt-5.6-luna": ("none", "low", "medium", "high", "xhigh", "max"),
    "minimax-m3.1-flash": ("low", "medium", "high", "xhigh", "max"),
}

#: Image-input support is a hard capability, so every verdict below is
#: transcribed per **exact upstream model ID** from the source named in its
#: own row - never inferred from a provider name, a model family, a tier
#: name, or the wire dialect. A model absent from this table remains false
#: (unknown to the router) and cannot be advertised for image use.
#:
#: A row carries the verdict *and* the page it was read from, because the
#: two together are what makes the claim reviewable: a verdict with no
#: source is not evidence, and a source URL on its own is not a claim. The
#: ``observed_at`` date is the date that page was read, which is not always
#: the catalogue-wide ``OBSERVED_AT`` above.
#:
#: Sibling slugs disagree, which is exactly why this is keyed by exact ID:
#: ``mimo-v2.5-pro`` publishes Inputs: Text only while its sibling
#: ``mimo-v2.5`` publishes Text, Image, Audio and Video. Never copy a
#: verdict from one MiMo row to another.
#:
#: Sources and what they say (all read 2026-10-24 unless noted):
#:
#: * ``minimax-m3.1-flash`` - MiniMax documents that MiniMax-M3.1-Flash and
#:   MiniMax-M3 accept OpenAI ``image_url`` parts (read 2026-10-23);
#:   OpenCode Go's published endpoint table establishes that the exact
#:   ``minimax-m3.1-flash`` id is served over that compatibility endpoint.
#: * the MiMo rows - OpenCode's per-model data pages for models served by
#:   the same platform as the catalogue endpoint above. The live
#:   ``/v1/models`` response publishes no modalities field (only ``id``,
#:   ``object``, ``created``, ``owned_by``), so the data page is the
#:   citation. ``mimo-v2.6-flash``, ``mimo-v2.6-pro`` and ``mimo-v2.5``
#:   list Inputs "Text, Image, Audio, and Video"; ``mimo-v2.5-pro`` lists
#:   Inputs "Text".
#:
#: Only the **image** bit is carried into the capability model. Those pages
#: also list Audio and Video inputs; the gateway has no capability flag the
#: router could act on for them, so inventing one would be a claim nothing
#: checks.
IMAGE_INPUT_EVIDENCE: dict[str, dict[str, Any]] = {
    "minimax-m3.1-flash": {
        "supports_image_input": True,
        "source": "https://platform.minimax.io/docs/api-reference/text-openai-api",
        "observed_at": "2026-10-23",
    },
    "mimo-v2.6-flash": {
        "supports_image_input": True,
        "source": "https://opencode.ai/data/xiaomi/mimo-v2-6-flash",
        "observed_at": "2026-10-24",
    },
    "mimo-v2.6-pro": {
        "supports_image_input": True,
        "source": "https://opencode.ai/data/xiaomi/mimo-v2-6-pro",
        "observed_at": "2026-10-24",
    },
    "mimo-v2.5": {
        "supports_image_input": True,
        "source": "https://opencode.ai/data/xiaomi/mimo-v2-5",
        "observed_at": "2026-10-24",
    },
    "mimo-v2.5-pro": {
        "supports_image_input": False,
        "source": "https://opencode.ai/data/xiaomi/mimo-v2-5-pro",
        "observed_at": "2026-10-24",
    },
}


def retention_for(model_id: str) -> str:
    if model_id in RETENTION_TRAINING:
        return "training_permitted"
    if model_id in RETENTION_30_DAYS:
        return "30_days"
    if model_id in ENDPOINTS:
        return "0_days"
    return "unknown"


def build() -> dict[str, Any]:
    models: dict[str, Any] = {}
    for model_id in sorted(set(ENDPOINTS) | set(LIVE_ONLY)):
        dialect, path = ENDPOINTS.get(model_id, (None, None))
        monthly = MONTHLY_LIMIT_GO.get(model_id)
        limits_source = "published_estimate"
        if model_id in UNLIMITED:
            monthly = None
            limits_source = "published_estimate"
        elif monthly is None:
            limits_source = "unknown"

        price = PRICES.get(model_id, (None, None, None))
        notes: list[str] = []
        if model_id in DEPRECATED:
            notes.append(DEPRECATED[model_id])
        if model_id in CONTEXT_TIERS:
            notes.append(CONTEXT_TIERS[model_id])
        if model_id in UNLIMITED:
            notes.append("free model; remains usable after the included allowance is exhausted")
        if dialect is None:
            notes.append("served by the provider but absent from the published endpoint table")

        image_input = IMAGE_INPUT_EVIDENCE.get(model_id, {})
        model_limits = MODEL_LIMITS.get(model_id, {})
        model_note = model_limits.get("note")
        if isinstance(model_note, str):
            notes.append(model_note)
        models[model_id] = {
            "model_id": model_id,
            "dialect": dialect,
            "endpoint_path": path,
            "monthly_limit_usd": monthly,
            "limits_source": model_limits.get("source") or limits_source,
            "price_input_per_m": price[0],
            "price_output_per_m": price[1],
            "price_cached_read_per_m": price[2],
            "retention_class": retention_for(model_id),
            "reasoning_efforts": list(REASONING_EFFORTS.get(model_id, ())),
            "context_window": model_limits.get("context_window"),
            "context_limit": model_limits.get("context_limit"),
            "max_output_tokens": model_limits.get("max_output_tokens"),
            "context_source": model_limits.get("source"),
            "supports_streaming": True,
            "supports_function_tools": True,
            "supports_builtin_tools": False,
            "supports_image_input": bool(image_input.get("supports_image_input", False)),
            "image_input_source": image_input.get("source"),
            "image_input_observed_at": image_input.get("observed_at"),
            "deprecated": model_id in DEPRECATED,
            "notes": "; ".join(notes) if notes else None,
            "source": SOURCE_URL,
            "observed_at": OBSERVED_AT,
        }

    return {
        "provider": "opencode_go",
        "source": SOURCE_URL,
        "catalog_endpoint": CATALOG_URL,
        "observed_at": OBSERVED_AT,
        "plan": {
            "name": "Go",
            "price_usd_month": 10.0,
            "plus_price_usd_month": 40.0,
            "windows": {"5h": 0.20, "weekly": 0.50, "monthly": 1.00},
        },
        "usage_window_note": (
            "The 5-hour window is 20% of the monthly limit, the weekly window "
            "is 50%, and the monthly window is 100%."
        ),
        "model_count": len(models),
        "models": models,
    }


def main() -> None:
    target = Path(__file__).resolve().parent.parent / "data" / "go_catalog.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = build()
    target.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {target} ({payload['model_count']} models)")


if __name__ == "__main__":
    main()
