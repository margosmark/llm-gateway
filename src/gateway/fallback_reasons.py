"""The closed fallback-reason vocabulary, shared by every layer that counts.

A recorded fallback reason is a full sentence that changes by the second
("... the provider chatgpt_codex is in a local cooldown for 771s ..."), and
it embeds a per-second remaining counter, a climbing failure count and a
provider-supplied ``retry_after`` - so raw strings are effectively unique per
request. Counting them verbatim would open a new bucket for every request
(and, once persisted, a sidecar row that only ever grows).

Both layers that see a reason therefore fold it onto the *same* closed
vocabulary: the rollup store at ingest (``gateway.ledger_persistence``), and
the owner analytics reader (``gateway.http.app``). They import the rules from
here precisely so the two cannot drift: a reason the store buckets is the
reason the reader reports. The vocabulary is closed - the rules are ordered,
first match wins, and anything unmatched is reported as ``other``, never
dropped and never guessed at. Folding is idempotent, so a label that is
already classified is returned unchanged.

The console shows a label; the API reports the key. Counts are preserved
exactly: folding changes which key a count lands under, never how many
counts there are.
"""
from __future__ import annotations

from typing import Any

#: Ordered rules: (label, marker). First match wins, case-insensitively.
FALLBACK_REASON_RULES: tuple[tuple[str, str], ...] = (
    ("cooldown", "in a local cooldown"),
    ("route_ineligible", "not eligible"),
    ("route_ineligible", "does not support"),
    ("route_ineligible", "is not enabled"),
    ("route_ineligible", "is not configured"),
    ("route_ineligible", "is not known"),
    ("route_ineligible", "no adapter is registered"),
    ("route_ineligible", "no model at this provider"),
    ("route_ineligible", "not documented"),
    ("upstream_failure", "trigger is enabled"),
    ("upstream_failure", "safe fallback policy"),
)

#: The whole vocabulary: every bucket a reason can be reported under, and
#: the only values :func:`fallback_reason_class` ever returns. ``other`` is
#: the unmatched overflow and ``unknown`` the explicit absence of a reason.
FALLBACK_LABELS: frozenset[str] = frozenset({
    "cooldown",
    "route_ineligible",
    "upstream_failure",
    "other",
    "unknown",
})


def fallback_reason_class(reason: Any) -> str:
    """Map a recorded fallback reason onto the closed label vocabulary.

    Idempotent: a value that is already a label comes back unchanged, so the
    reader can safely re-classify what the store folded at ingest.
    """
    if not isinstance(reason, str) or not reason.strip():
        return "unknown"
    text = reason.strip().lower()
    if text in FALLBACK_LABELS:
        return text
    lowered = reason.lower()
    for label, marker in FALLBACK_REASON_RULES:
        if marker in lowered:
            return label
    return "other"
