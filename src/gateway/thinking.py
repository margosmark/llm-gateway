"""The canonical thinking-level vocabulary.

One vocabulary, parsed from every inbound spelling and rendered per
dialect. A *level* is a preference, never a capability (R19): the route
stays eligible, and a level the selected model does not publish is
dropped for that route with the substitution recorded.

This vocabulary is not invented. OpenAI publishes it in full:

    "Supported values are model-dependent and can include ``none``,
    ``minimal``, ``low``, ``medium``, ``high``, ``xhigh``, and ``max``."

    -- *Reasoning models*, OpenAI API documentation, read 2026-10-23:
       https://developers.openai.com/api/docs/guides/reasoning

The same page documents why a vocabulary has to be reconciled against
what a model publishes rather than asserted: "GPT-6 Astra does not
support ``none`` reasoning effort. Setting ``reasoning.effort``
(Responses) or ``reasoning_effort`` (Chat Completions) to ``none``
returns HTTP 400." A level is therefore honoured per model, and a level
no model publishes must not be claimed at all.

The models behind this gateway publish the following, from the dated
OpenCode Go catalogue snapshot (``data/go_catalog.json``, observed
2026-10-22, https://opencode.ai/docs/go/) and, for the MiniMax model,
the provider's own page (https://platform.minimax.io/docs/guides/
text-generation, read 2026-10-23):

====================  ===============  ============================
model                 dialect          published reasoning levels
====================  ===============  ============================
``gpt-5.6-luna``      openai_responses none low medium high xhigh max
``gpt-6-luna``        openai_responses none low medium high xhigh max
``minimax-m3.1-flash`` openai_chat      low medium high xhigh max
every other model     any              none published -- **Unknown**
====================  ===============  ============================

Two decisions this module records, because a vocabulary that claims a
level no provider publishes is an invented capability:

* ``minimal`` **stays**: OpenAI publishes it (citation above).
* ````ultra```` is **not** in the vocabulary: no provider this gateway
  serves publishes it, and OpenAI's published set does not contain it.
  The gateway does not claim it. This decision is recorded rather than
  left implicit: ``gateway.core.capabilities.REASONING_EFFORTS`` *is*
  :data:`CANONICAL_LEVELS`.

**Budgets.** Neither served OpenAI dialect has a budget parameter; an
effort level is the whole control. A token budget is an Anthropic
(``thinking.budget_tokens``) or a Google (``thinkingBudget``) concept,
and neither dialect is served yet (``docs/TODO.md`` T5 and T6). This
gateway therefore publishes **no** budget table and invents no budget:
a budget control that reaches a converted path is refused, never
reduced to an effort level the provider never offered.
"""

from __future__ import annotations

from typing import Any

#: The level that means "do not reason". It is a published level name
#: (OpenAI, and both published Responses models above) and the gateway's
#: canonical spelling of *off*; how "off" is rendered is per dialect, in
#: ``gateway.bridge.requests``.
DISABLED_LEVEL = "none"

#: The canonical levels, weakest first. Every name is published by a
#: provider the gateway serves or fronts; none is invented here.
CANONICAL_LEVELS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")

#: Where each level is published, so a reader can check the vocabulary
#: without trusting this file.
LEVEL_SOURCES: dict[str, str] = {
    "none": (
        "OpenAI API docs, 'Reasoning models' (read 2026-10-23); published by "
        "gpt-5.6-luna and gpt-6-luna in data/go_catalog.json (observed 2026-10-22)"
    ),
    "minimal": "OpenAI API docs, 'Reasoning models' (read 2026-10-23)",
    "low": (
        "OpenAI API docs (read 2026-10-23) and MiniMax-M3.1-Flash "
        "(platform.minimax.io/docs/guides/text-generation, read 2026-10-23)"
    ),
    "medium": (
        "OpenAI API docs (read 2026-10-23) and MiniMax-M3.1-Flash "
        "(platform.minimax.io/docs/guides/text-generation, read 2026-10-23)"
    ),
    "high": (
        "OpenAI API docs (read 2026-10-23) and MiniMax-M3.1-Flash "
        "(platform.minimax.io/docs/guides/text-generation, read 2026-10-23)"
    ),
    "xhigh": (
        "OpenAI API docs (read 2026-10-23) and MiniMax-M3.1-Flash "
        "(platform.minimax.io/docs/guides/text-generation, read 2026-10-23)"
    ),
    "max": (
        "OpenAI API docs (read 2026-10-23) and MiniMax-M3.1-Flash "
        "(platform.minimax.io/docs/guides/text-generation, read 2026-10-23)"
    ),
}


def is_disabled_level(value: Any) -> bool:
    """Whether a level value asks for no reasoning at all."""
    return value == DISABLED_LEVEL


def is_known_level(value: Any) -> bool:
    """Whether a level value is one this gateway publishes.

    An unknown level is still forwarded, never refused: a provider may
    publish a level this gateway has not seen, and the router decides
    per route under the R19 preference rule (drop the level for that
    route, record the substitution). Vocabulary governs what the gateway
    *claims*, not what a client may ask for.
    """
    return isinstance(value, str) and value in CANONICAL_LEVELS
