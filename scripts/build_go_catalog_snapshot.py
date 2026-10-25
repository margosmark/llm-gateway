#!/usr/bin/env python3
"""Regenerate the OpenCode Go catalog snapshot.

The snapshot is the gateway's knowledge of what the OpenCode Go provider
offers: the model id, the wire dialect it is served over, the published
monthly allowance, the token prices, the context and output windows, and the
image-input verdict.

It is *published* information, transcribed from pages that were read on a
dated crawl. It is **not** probed at runtime and it is **not** a guess.
Every row carries the source URL and the date that page was read.

Three sources are transcribed, each keeping its own URL and read date in the
row that carries its facts:

* **The published endpoint/price/limit table** — ``SOURCE_URL``, read
  2026-10-25. It establishes the dialect and endpoint path of a model, its
  monthly allowance (``limits_source``), its retention class and its
  ``observed_at`` when no per-slug page applies. It is named at the top level
  of the snapshot as ``source``.
* **The live catalogue** — ``CATALOG_URL``, read 2026-10-25. It establishes
  which ids the provider currently serves (the serving drift below).
* **The per-model data pages** — ``https://opencode.ai/data/<vendor>/<id>``,
  read 2026-10-25 by a 389-page crawl. They establish the context window,
  the maximum output, the token prices and the image-input verdict, per exact
  slug. Their URL is the row's ``source``/``context_source`` and the prices'
  provenance, so ``gateway.costs`` reports the page and date it priced from.

Regenerate with:

    python3 scripts/build_go_catalog_snapshot.py

The live catalog endpoint lists more ids than the published table does. Ids
that appear live but not in the published table are recorded with
``dialect: null`` and ``limits_source: "unknown"``, so the gateway reports
them as unknown rather than guessing an endpoint or an allowance.

Serving drift observed 2026-10-25: ``kimi-k3.1-pro`` and ``gemini-4-argon``
are served and documented but were absent from this snapshot (they are
added); ``iceman-contributor-free`` is served with neither a data page nor a
published row (added, fail-closed: no dialect, no prices, no image verdict);
``gemini-4-argon-contributor`` and ``longcat-2.5-preview-free`` are
catalogued but no longer served (kept, marked deprecated).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SOURCE_URL = "https://opencode.ai/docs/go/"
CATALOG_URL = "https://opencode.ai/inference/go/v1/models"
#: The date every source below was read: the published table, the live
#: catalogue and the per-model data pages.
OBSERVED_AT = "2026-10-25"

# --- dialects, per the published "Endpoints" table -------------------------
#
# Read 2026-10-25. The table publishes an endpoint for these ids.
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
    "gemini-4-argon": ("google_v1beta", "/google/v1beta/models/gemini-4-argon"),
    "glm-5.3-flash": ("openai_chat", "/openai/v1/chat/completions"),
    "glm-5.3": ("openai_chat", "/openai/v1/chat/completions"),
    "glm-5.2": ("openai_chat", "/openai/v1/chat/completions"),
    "kimi-k3.1-pro": ("openai_chat", "/openai/v1/chat/completions"),
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

#: Endpoint rows that existed on 2026-10-22 and were removed from the
#: published table by 2026-10-25, together with the live catalogue. The row
#: keeps the dialect the provider last published for it - and nothing else
#: claims it is current: its ``observed_at`` stays at the read date when the
#: table still carried it, and ``deprecated`` records why it stopped.
RETIRED_ENDPOINTS: dict[str, tuple[str, str]] = {
    "gemini-4-argon-contributor": (
        "google_v1beta",
        "/google/v1beta/models/gemini-4-argon-contributor",
    ),
}

#: Ids the live catalog returned that the published table does not document.
#: Read 2026-10-25: nine ids, including the newly served
#: ``iceman-contributor-free``.
LIVE_ONLY = (
    "glm-5.1",
    "grok-4.6",
    "iceman-contributor-free",
    "kimi-k2.6",
    "minimax-m2.5",
    "minimax-m2.7",
    "omen-alpha",
    "qwen3.6-plus",
    "qwen3.7-max",
)

# --- per-slug rows transcribed from the OpenCode data pages ---------------
#
# Read 2026-10-25 (a 389-page crawl of https://opencode.ai/data/, zero
# failures). model id -> (data page URL, context window, maximum output).
# The URL is the row's ``source`` and therefore also the provenance of its
# prices; a ``None`` window is a page that carries no model payload.
DATASET_ROWS: dict[str, tuple[str, int | None, int | None]] = {
    "beam-free": ("https://opencode.ai/data/xai/beam-free", 1048576, 131072),
    "claude-haiku-5-5": ("https://opencode.ai/data/anthropic/claude-haiku-5-5", 1000000, 128000),
    "deepseek-v4-pro": ("https://opencode.ai/data/deepseek/deepseek-v4-pro", 1000000, 384000),
    "deepseek-v4.1-flash": ("https://opencode.ai/data/deepseek/deepseek-v4-1-flash", 1000000, 384000),
    "gemini-4-argon": ("https://opencode.ai/data/google/gemini-4-argon", 1048576, 65536),
    "glm-5.1": ("https://opencode.ai/data/zhipuai/glm-5-1", 200000, 131072),
    "glm-5.2": ("https://opencode.ai/data/zhipuai/glm-5-2", 1000000, 131072),
    "glm-5.3": ("https://opencode.ai/data/zhipuai/glm-5-3", 1000000, 131072),
    "glm-5.3-flash": ("https://opencode.ai/data/zhipuai/glm-5-3-flash", 1000000, 131072),
    "gpt-5.6-luna": ("https://opencode.ai/data/openai/gpt-5-6-luna", 1050000, 128000),
    "gpt-6-luna": ("https://opencode.ai/data/openai/gpt-6-luna", 1050000, 128000),
    "grok-4.6": ("https://opencode.ai/data/xai/grok-4-6", 500000, 500000),
    "grok-4.7": ("https://opencode.ai/data/xai/grok-4-7", 500000, 500000),
    "hy3": ("https://opencode.ai/data/tencent/hy3", 256000, 128000),
    "hy4-preview": ("https://opencode.ai/data/tencent/hy4-preview", 1024000, 64000),
    "kimi-k2.6": ("https://opencode.ai/data/moonshotai/kimi-k2-6", 262144, 262144),
    "kimi-k2.7-code": ("https://opencode.ai/data/moonshotai/kimi-k2-7-code", 262144, 262144),
    "kimi-k3": ("https://opencode.ai/data/moonshotai/kimi-k3", 1048576, 131072),
    "kimi-k3.1-pro": ("https://opencode.ai/data/moonshotai/kimi-k3-1-pro", 1048576, 131072),
    "longcat-2.0": ("https://opencode.ai/data/meituan/longcat-2-0", 1000000, 131072),
    "mimo-v2.5": ("https://opencode.ai/data/xiaomi/mimo-v2-5", 1048576, 131072),
    "mimo-v2.5-pro": ("https://opencode.ai/data/xiaomi/mimo-v2-5-pro", 1048576, 131072),
    "mimo-v2.6-flash": ("https://opencode.ai/data/xiaomi/mimo-v2-6-flash", 1048576, 131072),
    "mimo-v2.6-pro": ("https://opencode.ai/data/xiaomi/mimo-v2-6-pro", 1048576, 131072),
    "minimax-m2.5": ("https://opencode.ai/data/minimax/minimax-m2-5", 204800, 131072),
    "minimax-m2.7": ("https://opencode.ai/data/minimax/minimax-m2-7", 204800, 131072),
    "minimax-m3": ("https://opencode.ai/data/minimax/minimax-m3", 1048576, 512000),
    "minimax-m3.1-flash": ("https://opencode.ai/data/minimax/minimax-m3-1-flash", 1000000, 512000),
    "muse-spark-1.2-contributor": ("https://opencode.ai/data/meta/muse-spark-1-2-contributor", 1048576, 131072),
    "muse-spark-1.3-contributor": ("https://opencode.ai/data/meta/muse-spark-1-3-contributor", 1048576, 131072),
    "omen-alpha": ("https://opencode.ai/data/unknown/omen-alpha", None, None),
    "qwen3.6-plus": ("https://opencode.ai/data/alibaba/qwen3-6-plus", 1000000, 65536),
    "qwen3.7-max": ("https://opencode.ai/data/alibaba/qwen3-7-max", 1000000, 131072),
    "qwen3.7-plus": ("https://opencode.ai/data/alibaba/qwen3-7-plus", 1000000, 64000),
    "qwen3.8-flash": ("https://opencode.ai/data/alibaba/qwen3-8-flash", 1000000, 131072),
    "qwen3.8-max": ("https://opencode.ai/data/alibaba/qwen3-8-max", 1000000, 131072),
    "step-5-preview": ("https://opencode.ai/data/stepfun/step-5-preview", 1000000, 1000000),
}

# --- monthly allowances (Go plan) ------------------------------------------
#
# The published "Usage limits" table, read 2026-10-25. Two values moved
# since 2026-10-22: ``step-5-preview`` $60 -> $15 and ``mimo-v2.5-pro`` is
# now published at $15 (it was absent). ``gemini-4-argon-contributor`` keeps
# the $60 its row last published on 2026-10-22; it is no longer listed.
MONTHLY_LIMIT_GO = {
    "glm-5.3": 15.0,
    "kimi-k3": 15.0,
    "kimi-k3.1-pro": 15.0,
    "mimo-v2.6-pro": 15.0,
    "mimo-v2.5-pro": 15.0,
    "qwen3.8-max": 15.0,
    "grok-4.7": 15.0,
    "gpt-6-luna": 15.0,
    "gpt-5.6-luna": 15.0,
    "claude-haiku-5-5": 15.0,
    "deepseek-v4-pro": 15.0,
    "qwen3.8-flash": 30.0,
    "hy4-preview": 30.0,
    "gemini-4-argon": 30.0,
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
    "step-5-preview": 15.0,
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

#: Models the provider has deprecated, is about to, or has stopped serving.
DEPRECATED = {
    "mimo-v2.5": "deprecated 2026-10-21 10:00 CST; auto-routed to V2.6 since 2026-10-14",
    "mimo-v2.5-pro": "deprecated 2026-10-21 10:00 CST; auto-routed to V2.6 since 2026-10-14",
    "gemini-4-argon-contributor": (
        "no longer served and no longer published (observed 2026-10-25); "
        "superseded by gemini-4-argon"
    ),
    "longcat-2.5-preview-free": (
        "no longer returned by the live catalogue (observed 2026-10-25); "
        "still published as free for a limited time"
    ),
}

#: Token prices per 1M tokens, transcribed from each model's own data page
#: (read 2026-10-25). These are **list-price estimates**, not the Go plan's
#: rate: the Go plan bills its own discounted amount - and serves
#: ``beam-free`` free of charge - so a cost computed from these numbers is a
#: list-price estimate, never a bill. ``None`` means the page publishes no
#: price for that field. The two rows without a data page carry the published
#: table's own figures, with the read date in their row.
PRICES: dict[str, tuple[float | None, float | None, float | None]] = {
    "beam-free": (0.3, 1.0, 0.04),
    "claude-haiku-5-5": (0.1, 0.5, 0.01),
    "deepseek-v4-pro": (0.435, 0.87, None),
    "deepseek-v4.1-flash": (0.3, 1.2, 0.006),
    "gemini-4-argon": (2.0, 10.0, 0.1),
    "glm-5.1": (0.89637, 3.58548, 0.19421),
    "glm-5.2": (1.19516, 4.18306, 0.29879),
    "glm-5.3": (1.19516, 4.18306, 0.29879),
    "glm-5.3-flash": (0.11952, 0.41831, 0.03436),
    "gpt-5.6-luna": (0.2, 1.2, 0.02),
    "gpt-6-luna": (0.1, 0.5, 0.01),
    "grok-4.6": (2.0, 6.0, 0.5),
    "grok-4.7": (2.0, 6.0, 0.5),
    "hy3": (0.18, 0.59, 0.059),
    "hy4-preview": (0.834, 2.501, 0.042),
    "kimi-k2.6": (0.95, 4.0, 0.16),
    "kimi-k2.7-code": (0.95, 4.0, 0.19),
    "kimi-k3": (3.0, 15.0, 0.3),
    "kimi-k3.1-pro": (2.0, 10.0, 0.2),
    "longcat-2.0": (0.3, 1.2, 0.006),
    "mimo-v2.5": (0.14, 0.28, 0.0028),
    "mimo-v2.5-pro": (0.435, 0.87, 0.0036),
    "mimo-v2.6-flash": (0.14, 0.28, 0.0028),
    "mimo-v2.6-pro": (0.43, 0.87, 0.0036),
    "minimax-m2.5": (0.3, 1.2, 0.03),
    "minimax-m2.7": (0.3, 1.2, 0.06),
    "minimax-m3": (0.3, 1.2, 0.06),
    "minimax-m3.1-flash": (0.15, 0.6, 0.015),
    "muse-spark-1.2-contributor": (0.1, 0.2, 0.002),
    "muse-spark-1.3-contributor": (0.1, 0.2, 0.002),
    "qwen3.6-plus": (0.5, 3.0, 0.05),
    "qwen3.7-max": (2.5, 7.5, 0.5),
    "qwen3.7-plus": (0.4, 1.6, 0.08),
    "qwen3.8-flash": (0.15, 0.47, 0.016),
    "qwen3.8-max": (2.0, 6.0, 0.25),
    "step-5-preview": (1.0, 2.7, 0.1),
    "gemini-4-argon-contributor": (4.00, 20.00, 0.20),
    "longcat-2.5-preview-free": (None, None, None),
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

#: Context facts that come from the *model provider's* own documentation
#: rather than from the OpenCode data page. They win over the transcribed
#: page where they state a value, and they are the only rows with a
#: ``context_limit``: OpenCode Go publishes no service-side context cap, so
#: a limit is recorded only where the model provider's page establishes the
#: window for an id the endpoint table serves.
MODEL_LIMITS: dict[str, dict[str, Any]] = {
    "mimo-v2.6-flash": {
        "context_window": 1_048_576,
        "context_limit": 1_048_576,
        "max_output_tokens": 131_072,
        "source": "https://platform.xiaomimimo.com/docs/en-US/quick-start/model",
        "limits_source": "https://platform.xiaomimimo.com/docs/en-US/quick-start/model",
        "note": (
            "MiMo documents a 1M context window and a 131072-token maximum completion; "
            "thinking defaults to enabled and is a binary thinking.type control, not an effort level."
        ),
    },
    "minimax-m3.1-flash": {
        "context_limit": 1_000_000,
        "limits_source": "https://platform.minimax.io/docs/guides/text-generation",
        "note": (
            "MiniMax documents a 1,000,000-token context window and the low/medium/high/xhigh/max "
            "reasoning-effort vocabulary; that page publishes no maximum output, so the "
            "512,000-token maximum is transcribed from the OpenCode data page (read 2026-10-25)."
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
#: name, or the wire dialect. A model absent from this table has had no page
#: read for it, stays false (unknown to the router) and cannot be advertised
#: for image use.
#:
#: A row carries the verdict *and* the page it was read from, because the
#: two together are what makes the claim reviewable: a verdict with no
#: source is not evidence, and a source URL on its own is not a claim. The
#: ``observed_at`` date is the date that page was read.
#:
#: Read 2026-10-25 from each model's own data page, except the five rows
#: transcribed on 2026-10-23/24 from the pages named in their entries - the
#: verdicts are unchanged from that read, so the earlier date stands.
#:
#: Sibling slugs disagree, which is exactly why this is keyed by exact ID:
#: ``mimo-v2.5-pro`` publishes Inputs: Text only while its sibling
#: ``mimo-v2.5`` publishes Text, Image, Audio and Video. Never copy a
#: verdict from one MiMo row to another.
#:
#: Four catalogue rows carry no entry, because no page was read for them:
#: ``omen-alpha`` (its data page carries no model payload), and
#: ``iceman-contributor-free``, ``gemini-4-argon-contributor`` and
#: ``longcat-2.5-preview-free`` (no data page at all). They stay false with
#: no source - the fail-closed default, never an invented URL.
#:
#: Only the **image** bit is carried into the capability model. Those pages
#: also list Audio and Video inputs; the gateway has no capability flag the
#: router could act on for them, so inventing one would be a claim nothing
#: checks.
IMAGE_INPUT_EVIDENCE: dict[str, dict[str, Any]] = {
    "beam-free": {"supports_image_input": False, "source": "https://opencode.ai/data/xai/beam-free", "observed_at": "2026-10-25"},
    "claude-haiku-5-5": {"supports_image_input": True, "source": "https://opencode.ai/data/anthropic/claude-haiku-5-5", "observed_at": "2026-10-25"},
    "deepseek-v4-pro": {"supports_image_input": False, "source": "https://opencode.ai/data/deepseek/deepseek-v4-pro", "observed_at": "2026-10-25"},
    "deepseek-v4.1-flash": {"supports_image_input": True, "source": "https://opencode.ai/data/deepseek/deepseek-v4-1-flash", "observed_at": "2026-10-25"},
    "gemini-4-argon": {"supports_image_input": True, "source": "https://opencode.ai/data/google/gemini-4-argon", "observed_at": "2026-10-25"},
    "glm-5.1": {"supports_image_input": False, "source": "https://opencode.ai/data/zhipuai/glm-5-1", "observed_at": "2026-10-25"},
    "glm-5.2": {"supports_image_input": False, "source": "https://opencode.ai/data/zhipuai/glm-5-2", "observed_at": "2026-10-25"},
    "glm-5.3": {"supports_image_input": False, "source": "https://opencode.ai/data/zhipuai/glm-5-3", "observed_at": "2026-10-25"},
    "glm-5.3-flash": {"supports_image_input": True, "source": "https://opencode.ai/data/zhipuai/glm-5-3-flash", "observed_at": "2026-10-25"},
    "gpt-5.6-luna": {"supports_image_input": True, "source": "https://opencode.ai/data/openai/gpt-5-6-luna", "observed_at": "2026-10-25"},
    "gpt-6-luna": {"supports_image_input": True, "source": "https://opencode.ai/data/openai/gpt-6-luna", "observed_at": "2026-10-25"},
    "grok-4.6": {"supports_image_input": True, "source": "https://opencode.ai/data/xai/grok-4-6", "observed_at": "2026-10-25"},
    "grok-4.7": {"supports_image_input": True, "source": "https://opencode.ai/data/xai/grok-4-7", "observed_at": "2026-10-25"},
    "hy3": {"supports_image_input": False, "source": "https://opencode.ai/data/tencent/hy3", "observed_at": "2026-10-25"},
    "hy4-preview": {"supports_image_input": False, "source": "https://opencode.ai/data/tencent/hy4-preview", "observed_at": "2026-10-25"},
    "kimi-k2.6": {"supports_image_input": True, "source": "https://opencode.ai/data/moonshotai/kimi-k2-6", "observed_at": "2026-10-25"},
    "kimi-k2.7-code": {"supports_image_input": True, "source": "https://opencode.ai/data/moonshotai/kimi-k2-7-code", "observed_at": "2026-10-25"},
    "kimi-k3": {"supports_image_input": True, "source": "https://opencode.ai/data/moonshotai/kimi-k3", "observed_at": "2026-10-25"},
    "kimi-k3.1-pro": {"supports_image_input": True, "source": "https://opencode.ai/data/moonshotai/kimi-k3-1-pro", "observed_at": "2026-10-25"},
    "longcat-2.0": {"supports_image_input": False, "source": "https://opencode.ai/data/meituan/longcat-2-0", "observed_at": "2026-10-25"},
    "mimo-v2.5": {"supports_image_input": True, "source": "https://opencode.ai/data/xiaomi/mimo-v2-5", "observed_at": "2026-10-24"},
    "mimo-v2.5-pro": {"supports_image_input": False, "source": "https://opencode.ai/data/xiaomi/mimo-v2-5-pro", "observed_at": "2026-10-24"},
    "mimo-v2.6-flash": {"supports_image_input": True, "source": "https://opencode.ai/data/xiaomi/mimo-v2-6-flash", "observed_at": "2026-10-24"},
    "mimo-v2.6-pro": {"supports_image_input": True, "source": "https://opencode.ai/data/xiaomi/mimo-v2-6-pro", "observed_at": "2026-10-24"},
    "minimax-m2.5": {"supports_image_input": False, "source": "https://opencode.ai/data/minimax/minimax-m2-5", "observed_at": "2026-10-25"},
    "minimax-m2.7": {"supports_image_input": False, "source": "https://opencode.ai/data/minimax/minimax-m2-7", "observed_at": "2026-10-25"},
    "minimax-m3": {"supports_image_input": True, "source": "https://opencode.ai/data/minimax/minimax-m3", "observed_at": "2026-10-25"},
    "minimax-m3.1-flash": {"supports_image_input": True, "source": "https://platform.minimax.io/docs/api-reference/text-openai-api", "observed_at": "2026-10-23"},
    "muse-spark-1.2-contributor": {"supports_image_input": True, "source": "https://opencode.ai/data/meta/muse-spark-1-2-contributor", "observed_at": "2026-10-25"},
    "muse-spark-1.3-contributor": {"supports_image_input": True, "source": "https://opencode.ai/data/meta/muse-spark-1-3-contributor", "observed_at": "2026-10-25"},
    "qwen3.6-plus": {"supports_image_input": True, "source": "https://opencode.ai/data/alibaba/qwen3-6-plus", "observed_at": "2026-10-25"},
    "qwen3.7-max": {"supports_image_input": False, "source": "https://opencode.ai/data/alibaba/qwen3-7-max", "observed_at": "2026-10-25"},
    "qwen3.7-plus": {"supports_image_input": True, "source": "https://opencode.ai/data/alibaba/qwen3-7-plus", "observed_at": "2026-10-25"},
    "qwen3.8-flash": {"supports_image_input": True, "source": "https://opencode.ai/data/alibaba/qwen3-8-flash", "observed_at": "2026-10-25"},
    "qwen3.8-max": {"supports_image_input": True, "source": "https://opencode.ai/data/alibaba/qwen3-8-max", "observed_at": "2026-10-25"},
    "step-5-preview": {"supports_image_input": True, "source": "https://opencode.ai/data/stepfun/step-5-preview", "observed_at": "2026-10-25"},
}


def retention_for(model_id: str) -> str:
    if model_id in RETENTION_TRAINING:
        return "training_permitted"
    if model_id in RETENTION_30_DAYS:
        return "30_days"
    if model_id in ENDPOINTS:
        return "0_days"
    return "unknown"


def _row_source(model_id: str) -> tuple[str, str]:
    """The page this row's own facts were read from, and when.

    A model with a data page is attributed to that page (which is also what
    ``gateway.costs`` reports as the price provenance). The two rows that
    have no data page are attributed to where they were actually observed:
    the live catalogue for a served id, the published table for a row whose
    last published read was 2026-10-22.
    """
    if model_id in RETIRED_ENDPOINTS:
        # Last published read: the table no longer carries this row.
        return SOURCE_URL, "2026-10-22"
    row = DATASET_ROWS.get(model_id)
    if row is not None:
        return row[0], OBSERVED_AT
    if model_id in LIVE_ONLY:
        # Served, but with no data page to read (iceman-contributor-free).
        return CATALOG_URL, OBSERVED_AT
    return SOURCE_URL, OBSERVED_AT


def build() -> dict[str, Any]:
    models: dict[str, Any] = {}
    for model_id in sorted(set(ENDPOINTS) | set(RETIRED_ENDPOINTS) | set(LIVE_ONLY)):
        dialect, path = ENDPOINTS.get(model_id) or RETIRED_ENDPOINTS.get(model_id) or (None, None)
        monthly = MONTHLY_LIMIT_GO.get(model_id)
        limits_source = "published_estimate"
        if model_id in UNLIMITED:
            monthly = None
        elif monthly is None:
            limits_source = "unknown"

        price = PRICES.get(model_id, (None, None, None))
        notes: list[str] = []
        if model_id in DEPRECATED:
            notes.append(DEPRECATED[model_id])
        if model_id in CONTEXT_TIERS:
            notes.append(CONTEXT_TIERS[model_id])
        if model_id in UNLIMITED:
            notes.append(
                "monthly allowance is unlimited; the token prices shown are "
                "list-price estimates, not the rate the Go plan charges"
            )
        if dialect is None and model_id in LIVE_ONLY:
            notes.append("served by the provider but absent from the published endpoint table")
        if model_id == "deepseek-v4-pro":
            # Kept deliberate: the data page publishes no cached-read price,
            # so a cached-token cost stays Unknown rather than guessing.
            notes.append(
                "the data page publishes no cached-read price, so a request "
                "that reports cached tokens is priced as Unknown"
            )

        image_input = IMAGE_INPUT_EVIDENCE.get(model_id, {})
        model_limits = MODEL_LIMITS.get(model_id, {})
        data_row = DATASET_ROWS.get(model_id)

        context_window = data_row[1] if data_row else None
        max_output_tokens = data_row[2] if data_row else None
        context_source = data_row[0] if data_row else None
        context_limit = model_limits.get("context_limit")
        if "context_window" in model_limits:
            context_window = model_limits["context_window"]
        if "max_output_tokens" in model_limits:
            max_output_tokens = model_limits["max_output_tokens"]
        if model_limits.get("source"):
            context_source = model_limits["source"]
        if model_limits.get("limits_source"):
            limits_source = model_limits["limits_source"]
        model_note = model_limits.get("note")
        if isinstance(model_note, str):
            notes.append(model_note)

        source, observed_at = _row_source(model_id)
        models[model_id] = {
            "model_id": model_id,
            "dialect": dialect,
            "endpoint_path": path,
            "monthly_limit_usd": monthly,
            "limits_source": limits_source,
            "price_input_per_m": price[0],
            "price_output_per_m": price[1],
            "price_cached_read_per_m": price[2],
            "retention_class": retention_for(model_id),
            "reasoning_efforts": list(REASONING_EFFORTS.get(model_id, ())),
            "context_window": context_window,
            "context_limit": context_limit,
            "max_output_tokens": max_output_tokens,
            "context_source": context_source,
            "supports_streaming": True,
            "supports_function_tools": True,
            "supports_builtin_tools": False,
            "supports_image_input": bool(image_input.get("supports_image_input", False)),
            "image_input_source": image_input.get("source"),
            "image_input_observed_at": image_input.get("observed_at"),
            "deprecated": model_id in DEPRECATED,
            "notes": "; ".join(notes) if notes else None,
            "source": source,
            "observed_at": observed_at,
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
        "price_note": (
            "price_*_per_m are list-price estimates transcribed from the data "
            "page named in the row's source (read 2026-10-25), not the Go "
            "plan's rate; monthly_limit_usd is a published estimate from the "
            "published table named in source, and an absent price means the "
            "page publishes none."
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
