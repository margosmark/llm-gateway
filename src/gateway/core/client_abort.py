"""Classify a client disconnect as ``client_abort`` in the request ledger (T11).

A successful route is recorded in the ledger *before* the HTTP layer has
finished consuming the stream. When the client disconnects mid-answer, the
record that was already written says "success" - which is now wrong. The
caller left, so the request did not complete, but the provider did nothing
wrong.

This module corrects that record in place using the ledger's ``amend`` API
(landed by T7), without editing ``observability.py``. It plugs into the
abort seam (``gateway.core.abort``) through that seam's global observer
registry, so it never edits the routing core and never builds a parallel
amendment mechanism.

Two rules govern the classification:

1. **An abort is never a provider failure.** ``client_abort`` is
   deliberately absent from :data:`gateway.config.ERROR_CLASSES`. Correcting
   a record to ``client_abort`` therefore cannot be mistaken for an upstream
   failure, and it never touches the ``rate_limited`` or ``exhausted``
   counters, which are the provider-specific classes.
2. **The correction is idempotent and can never break the abort path.** A
   failing ledger, a missing record, or a repeated abort all resolve to a
   no-op; the upstream is still cancelled and the caller still gets its
   :class:`~gateway.core.abort.AbortOutcome`.

The generic ``ledger.totals()["errors"]`` tally counts *any* truthy
``error_class``, so it does rise by one when a success is corrected to
``client_abort``. That tally means "requests that ended in some error
class", not "provider failures". Use :func:`provider_error_count` for the
provider-failure figure: it counts only outcomes whose class is one of the
provider failure classes, so a client disconnect never inflates it.
"""

from __future__ import annotations

import threading
from typing import Any

from gateway.config import ERROR_CLASSES
from gateway.core.abort import (
    CLIENT_ABORT,
    AbortOutcome,
    RouteContext,
    register_abort_observer,
    unregister_abort_observer,
)

#: The error class recorded for a request the client abandoned. Not a
#: provider failure class - the upstream did not fail, the caller left.
CLIENT_ABORT_ERROR_CLASS = CLIENT_ABORT

#: How many recorded outcomes :func:`provider_error_count` scans. The
#: ledger's in-memory ring is bounded by its capacity (10,000 by default),
#: so this covers every hot record.
_SCAN_LIMIT = 10_000

_INSTALL_LOCK = threading.Lock()
_INSTALLED = False


def is_provider_error_class(error_class: str | None) -> bool:
    """True when *error_class* names a provider failure.

    ``client_abort`` is not one of them: the provider did not fail.
    """
    return bool(error_class) and error_class in ERROR_CLASSES


def provider_error_count(ledger: Any) -> int:
    """How many recorded outcomes are provider failures.

    Counts only outcomes whose ``error_class`` is one of the provider
    failure classes (:data:`gateway.config.ERROR_CLASSES`). A record
    corrected to ``client_abort`` is excluded, so a client disconnect never
    inflates the provider-failure count. Scans the ledger's in-memory ring.
    """
    if ledger is None:
        return 0
    count = 0
    for event in ledger.recent(limit=_SCAN_LIMIT):
        if is_provider_error_class(event.get("error_class")):
            count += 1
    return count


def correct_ledger_for_abort(context: RouteContext, outcome: AbortOutcome) -> bool:
    """Amend the recorded outcome of an aborted request to ``client_abort``.

    The success record was written before the HTTP layer consumed the
    stream. This corrects it in place through the ledger's ``amend`` API
    (T7), so the record reads ``client_abort`` rather than a provider
    failure. Returns True when a record was amended.

    Never raises: a ledger problem must not break the abort path, and the
    upstream is cancelled whether or not the record could be corrected.
    """
    ledger = getattr(context, "ledger", None)
    request_id = getattr(context, "request_id", "")
    if ledger is None or not request_id:
        return False
    try:
        return bool(ledger.amend(request_id, error_class=CLIENT_ABORT_ERROR_CLASS))
    except Exception:  # noqa: BLE001 - a correction must never break the abort.
        return False


def _observer(context: RouteContext, outcome: AbortOutcome) -> None:
    """The registered observer. Discards the helper's boolean result."""
    correct_ledger_for_abort(context, outcome)


def install_client_abort_correction() -> bool:
    """Register the ledger-correction observer. Idempotent.

    Returns True when this call performed the registration, False when it
    was already installed.
    """
    global _INSTALLED
    with _INSTALL_LOCK:
        if _INSTALLED:
            return False
        register_abort_observer(_observer)
        _INSTALLED = True
        return True


def uninstall_client_abort_correction() -> None:
    """Remove the observer. Intended for tests that need a clean slate."""
    global _INSTALLED
    with _INSTALL_LOCK:
        if not _INSTALLED:
            return
        unregister_abort_observer(_observer)
        _INSTALLED = False


def is_installed() -> bool:
    """True when the correction observer is currently registered."""
    with _INSTALL_LOCK:
        return _INSTALLED


# Live as soon as this module is imported - and routing imports it - so a
# disconnect is corrected wherever an abort can happen, with no extra wiring
# at each call site.
install_client_abort_correction()
