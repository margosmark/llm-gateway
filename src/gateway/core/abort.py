"""The upstream-abort seam (T11).

When a client disconnects in the middle of a streamed answer, the upstream
request must be stopped. An abandoned stream that keeps running keeps
consuming the provider's allowance, and the bytes it produces are thrown
away.

This module is the seam between the HTTP layer, which notices the
disconnect, and the adapter, which owns the upstream request. It contains
**no provider-specific branch**: an adapter is told to cancel through the
ordinary duck-typed seam, exactly as every other adapter behaviour is
discovered. Nothing here knows what OpenCode Go, ChatGPT or anything else
is.

Three rules govern the seam:

1. **Aborting is idempotent.** :func:`abort_upstream` may be called any
   number of times, from any thread. The first call signals the adapter;
   later calls report that the request was already aborted and do not
   signal it again.
2. **An aborted request is not a provider failure.** The provider did
   nothing wrong: the caller left. The route context records the abort, so
   a provider error that arrives *after* the abort is classified as a
   client abort rather than as an upstream failure.
3. **The outcome is metadata.** What was aborted, why, and whether the
   adapter was told to cancel. Never the request or the response.

The routing core creates one :class:`RouteContext` per upstream request and
hands it over with the response. The HTTP layer aborts it when the client
disconnects. The context is also what a corrector - the ledger, for
instance - listens to, so the outcome is corrected where it was recorded
instead of in the routing core.
"""

from __future__ import annotations

import inspect
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

#: The error class of a request the client abandoned. It is deliberately
#: not one of the provider failure classes: the upstream did not fail.
CLIENT_ABORT = "client_abort"

#: The default reason recorded for an abort.
CLIENT_ABORT_REASON = "client_abort"

#: Route contexts by request id. This registry owns the context until the
#: request's HTTP lifecycle calls :func:`release_route_context`; a weak
#: registry would lose the context as soon as the application handler
#: returns its StreamResponse.
_CONTEXTS: dict[str, RouteContext] = {}
_CONTEXTS_LOCK = threading.Lock()

#: How many route contexts the registry retains. Nothing releases a
#: context at the end of a request yet, so without a bound the registry
#: would pin every request's upstream payload forever on a gateway that
#: runs for months. The limit only has to cover the requests actually in
#: flight - the server serves far fewer concurrently than this - so an
#: evicted context is always one whose request is already over.
CONTEXT_REGISTRY_LIMIT = 1024

#: Observers of every abort, whoever caused it. A listener that has to
#: correct the recorded outcome of an aborted request registers here
#: instead of editing the routing core.
_ABORT_OBSERVERS: list[Callable[["RouteContext", "AbortOutcome"], None]] = []
_ABORT_OBSERVERS_LOCK = threading.Lock()


@dataclass
class AbortOutcome:
    """What an abort did. Metadata only, and safe to log."""

    #: The reason the request was aborted.
    reason: str = CLIENT_ABORT_REASON
    #: The route context is in the aborted state after the call.
    aborted: bool = False
    #: The request had already been aborted before this call.
    already_aborted: bool = False
    #: A cancellation path was available: the adapter offered a cancel
    #: hook, or the upstream stream could be closed.
    cancellation_supported: bool = False
    #: The cancellation path was invoked.
    cancellation_attempted: bool = False
    #: The cancellation path completed without raising.
    cancelled: bool = False
    #: The provider that served the request, when it is known.
    provider: str | None = None
    #: The upstream model that served the request, when it is known.
    model: str | None = None
    #: The request identifier, so a caller can correct the ledger record.
    request_id: str = ""
    #: A short, fixed description of what was signalled.
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        """The outcome as plain metadata. Never the request or the response."""
        return {
            "reason": self.reason,
            "aborted": self.aborted,
            "already_aborted": self.already_aborted,
            "cancellation_supported": self.cancellation_supported,
            "cancellation_attempted": self.cancellation_attempted,
            "cancelled": self.cancelled,
            "provider": self.provider,
            "model": self.model,
            "request_id": self.request_id,
            "detail": self.detail,
        }


@dataclass(eq=False)
class RouteContext:
    """The handle on one upstream request.

    The routing core creates one for every route it reaches and hands it
    to the HTTP layer with the response. The HTTP layer calls
    :func:`abort_upstream` on it when the client disconnects.

    The context is also the place where the abort is *remembered*, so a
    provider error that arrives after the abort is not mistaken for a
    provider failure.
    """

    #: The request identifier, so a caller can correct the ledger record.
    request_id: str = ""
    #: The provider that served the request.
    provider: str | None = None
    #: The upstream model that served the request. Never the alias.
    model: str | None = None
    #: The adapter that owns the upstream request.
    adapter: Any = None
    #: The request that was sent upstream.
    request: Any = None
    #: The upstream stream, when the answer is streamed. Closing it stops
    #: the upstream read and closes the upstream connection.
    stream: Iterator[bytes] | None = None
    #: An explicit cancellation callback, when the owner of the request
    #: has one - an asyncio task, for instance.
    cancel: Callable[[], Any] | None = None
    #: The request ledger, when the routing core recorded this request. A
    #: corrector uses it to amend the record that was already written.
    ledger: Any = None
    #: The state of the request: True once it has been aborted.
    aborted: bool = False
    #: Why the request was aborted.
    abort_reason: str | None = None
    #: Observers of this request, notified once, when it is aborted.
    observers: list[Callable[["RouteContext", "AbortOutcome"], None]] = field(default_factory=list)
    #: The outcome of the abort, once it has happened.
    _outcome: "AbortOutcome | None" = field(default=None, repr=False, compare=False)
    #: The lock that makes the abort idempotent under concurrency.
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)
    #: Set once the first abort has finished signalling the adapter. A
    #: concurrent second caller waits for it, so every caller sees the
    #: same outcome.
    _signalled: threading.Event = field(default_factory=threading.Event, repr=False, compare=False)

    def add_observer(self, observer: Callable[["RouteContext", "AbortOutcome"], None]) -> None:
        """Register a callback notified once, when the request is aborted.

        An observer registered after the abort has already happened is
        notified immediately, so a late listener never misses the event.
        """
        with self._lock:
            notify = self.aborted
            if not notify:
                self.observers.append(observer)
        if notify:
            observer(self, self.outcome())

    def outcome(self) -> AbortOutcome:
        """Describe the current state of the request."""
        with self._lock:
            if self._outcome is not None:
                return self._outcome
            return AbortOutcome(
                reason=self.abort_reason or CLIENT_ABORT_REASON,
                aborted=self.aborted,
                already_aborted=self.aborted,
                provider=self.provider,
                model=self.model,
                request_id=self.request_id,
                detail="the request is still running",
            )

    def abort(self, reason: str = CLIENT_ABORT_REASON) -> AbortOutcome:
        """Abort the upstream request. Idempotent.

        The first call marks the request aborted and signals the
        adapter's cancellation path. Later calls change nothing and report
        that the request was already aborted.
        """
        observers: list[Callable[["RouteContext", "AbortOutcome"], None]] = []
        with self._lock:
            if self.aborted:
                already = True
            else:
                already = False
                self.aborted = True
                self.abort_reason = reason
                observers = list(self.observers)

        if already:
            # Another caller is aborting, or has already aborted. Wait for
            # its outcome so both callers report the same thing, and never
            # signal the adapter a second time.
            self._signalled.wait(timeout=5)
            stored = self.outcome()
            return AbortOutcome(
                reason=stored.reason or reason,
                aborted=True,
                already_aborted=True,
                cancellation_supported=stored.cancellation_supported,
                cancellation_attempted=False,
                cancelled=stored.cancelled,
                provider=stored.provider,
                model=stored.model,
                request_id=stored.request_id,
                detail="the request was already aborted",
            )

        supported, attempted, cancelled, detail = self._signal_cancellation()
        outcome = AbortOutcome(
            reason=reason,
            aborted=True,
            already_aborted=False,
            cancellation_supported=supported,
            cancellation_attempted=attempted,
            cancelled=cancelled,
            provider=self.provider,
            model=self.model,
            request_id=self.request_id,
            detail=detail,
        )
        with self._lock:
            self._outcome = outcome
        self._signalled.set()
        for observer in observers:
            _notify(observer, self, outcome)
        _notify_global(self, outcome)
        return outcome

    def _signal_cancellation(self) -> tuple[bool, bool, bool, str]:
        """Tell the adapter to stop.

        Returns ``(supported, attempted, cancelled, detail)``.

        The cancellation path is discovered, not assumed: an explicit
        cancellation callback is called when there is one, an adapter that
        offers a ``cancel`` hook is called, and the upstream stream is
        closed. Closing the stream is what stops a streaming upstream: the
        generator's own cleanup closes the provider connection.
        """
        signalled: list[str] = []
        failed: list[str] = []
        supported = False
        attempted = False

        explicit = self.cancel
        if callable(explicit):
            supported = True
            attempted = True
            if _invoke_cancel(explicit, self.request):
                signalled.append("the cancellation callback ran")
            else:
                failed.append("the cancellation callback did not complete")

        adapter_cancel = getattr(self.adapter, "cancel", None)
        if callable(adapter_cancel) and adapter_cancel is not explicit:
            supported = True
            attempted = True
            if _invoke_cancel(adapter_cancel, self.request):
                signalled.append("the adapter's cancel hook ran")
            else:
                failed.append("the adapter's cancel hook did not complete")

        stream = self.stream
        closer = getattr(stream, "close", None)
        if callable(closer):
            supported = True
            attempted = True
            try:
                closer()
                signalled.append("the upstream stream was closed")
            except Exception:  # noqa: BLE001 - closing must never raise.
                failed.append("the upstream stream could not be closed")

        if not signalled:
            detail = "; ".join(failed) if failed else "there was nothing to cancel"
            return supported, attempted, False, detail
        detail = "; ".join(signalled)
        if failed:
            detail = f"{detail}; {'; '.join(failed)}"
        return supported, attempted, True, detail


def _notify(
    observer: Callable[["RouteContext", "AbortOutcome"], None],
    context: RouteContext,
    outcome: AbortOutcome,
) -> None:
    """Call one observer. A failing observer never breaks the abort."""
    try:
        observer(context, outcome)
    except Exception:  # noqa: BLE001 - an observer must not break the abort.
        return


def _notify_global(context: RouteContext, outcome: AbortOutcome) -> None:
    """Call every registered global observer of an abort."""
    with _ABORT_OBSERVERS_LOCK:
        observers = list(_ABORT_OBSERVERS)
    for observer in observers:
        _notify(observer, context, outcome)


def _invoke_cancel(func: Callable[..., Any], request: Any) -> bool:
    """Call a cancellation hook, with the request when it accepts one."""
    try:
        parameters = inspect.signature(func).parameters
    except (TypeError, ValueError):  # pragma: no cover - builtins without a signature.
        parameters = {}
    positional = [
        parameter
        for parameter in parameters.values()
        if parameter.kind
        in (parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD)
    ]
    try:
        if positional:
            func(request)
        else:
            func()
    except Exception:  # noqa: BLE001 - a cancellation hook must never raise.
        return False
    return True


def abort_upstream(route_ctx: Any, reason: str = CLIENT_ABORT_REASON) -> AbortOutcome:
    """Abort the upstream request behind *route_ctx*.

    *route_ctx* is a :class:`RouteContext`, or anything that carries one -
    a routed response, for instance. ``None`` is accepted and does
    nothing, so a caller that has no context does not have to branch.

    The call is idempotent: the first call signals the adapter's
    cancellation path, and every later call reports that the request was
    already aborted without signalling it again. The returned
    :class:`AbortOutcome` says what was done, so the caller can log or
    report it.
    """
    context = resolve_route_context(route_ctx)
    if context is None:
        return AbortOutcome(
            reason=reason,
            aborted=False,
            already_aborted=False,
            cancellation_supported=False,
            cancellation_attempted=False,
            cancelled=False,
            detail="there is no upstream request to abort",
        )
    return context.abort(reason)


def abort_hook(route_ctx: Any, reason: str = CLIENT_ABORT_REASON) -> Callable[[], AbortOutcome]:
    """A zero-argument callable that aborts the upstream request.

    The HTTP layer hands this to the disconnect path, so the path that
    notices a broken pipe does not have to know what a route context is.
    """
    context = resolve_route_context(route_ctx)

    def _abort() -> AbortOutcome:
        return abort_upstream(context, reason)

    return _abort


def resolve_route_context(route_ctx: Any) -> RouteContext | None:
    """Return the :class:`RouteContext` behind *route_ctx*, if there is one."""
    if route_ctx is None:
        return None
    if isinstance(route_ctx, RouteContext):
        return route_ctx
    candidate = getattr(route_ctx, "route_context", None)
    if isinstance(candidate, RouteContext):
        return candidate
    return None


def is_client_abort(route_ctx: Any) -> bool:
    """True when the request behind *route_ctx* was aborted by the client."""
    context = resolve_route_context(route_ctx)
    return bool(context and context.aborted)


def classify_late_error(route_ctx: Any, error: Any) -> str:
    """The error class to record for a provider error that arrived late.

    A provider error raised *after* the client disconnected is a
    consequence of the abort, not a provider failure: recording it as one
    would corrupt the ledger's error classes and could bench a healthy
    route. The provider's own class is returned when the request was not
    aborted.
    """
    if is_client_abort(route_ctx):
        return CLIENT_ABORT
    error_class = getattr(error, "error_class", None)
    return error_class if isinstance(error_class, str) else "unknown"


def register_route_context(context: RouteContext) -> None:
    """Remember *context* so it can be found by request id."""
    if not context.request_id:
        return
    with _CONTEXTS_LOCK:
        try:
            _CONTEXTS[context.request_id] = context
        except TypeError:  # pragma: no cover - a context that cannot be referenced.
            return
        while len(_CONTEXTS) > CONTEXT_REGISTRY_LIMIT:
            # A dictionary keeps insertion order, so the first key is the
            # oldest registered context - the one whose request has been
            # over the longest, and the only one worth forgetting.
            _CONTEXTS.pop(next(iter(_CONTEXTS)), None)


def release_route_context(context: RouteContext) -> None:
    """Forget *context*. Called when the request is over."""
    if not context.request_id:
        return
    with _CONTEXTS_LOCK:
        if _CONTEXTS.get(context.request_id) is context:
            _CONTEXTS.pop(context.request_id, None)


def find_route_context(request_id: str) -> RouteContext | None:
    """The route context of a request, when it is still reachable.

    The registry holds the most recent contexts, bounded by
    :data:`CONTEXT_REGISTRY_LIMIT`, and forgets an id once the request's
    lifecycle calls :func:`release_route_context` - so this returns
    ``None`` for a request that is over and has been evicted, or that
    never registered at all.
    """
    if not request_id:
        return None
    with _CONTEXTS_LOCK:
        return _CONTEXTS.get(request_id)


def abort_request(request_id: str, reason: str = CLIENT_ABORT_REASON) -> AbortOutcome:
    """Abort the upstream request behind *request_id*.

    The HTTP layer knows the request it is serving, not the route
    context behind it, so it aborts by request id. When the request is
    unknown - it already finished, or it never reached a provider - the
    outcome says so and nothing is cancelled.
    """
    return abort_upstream(find_route_context(request_id), reason)


def register_abort_observer(observer: Callable[["RouteContext", "AbortOutcome"], None]) -> None:
    """Register an observer of every abort.

    A listener that has to correct the recorded outcome of an aborted
    request - to record it as a client abort rather than a provider
    failure - registers here, and never has to edit the routing core.
    """
    with _ABORT_OBSERVERS_LOCK:
        if observer not in _ABORT_OBSERVERS:
            _ABORT_OBSERVERS.append(observer)


def unregister_abort_observer(observer: Callable[["RouteContext", "AbortOutcome"], None]) -> None:
    """Remove an observer registered with :func:`register_abort_observer`."""
    with _ABORT_OBSERVERS_LOCK:
        if observer in _ABORT_OBSERVERS:
            _ABORT_OBSERVERS.remove(observer)
