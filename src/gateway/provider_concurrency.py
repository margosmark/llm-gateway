"""Process-local limits on concurrent calls to each provider."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from gateway.adapters.base import AdapterError, ProviderResponse
from gateway.config import ERROR_RATE_LIMITED, MAX_IN_FLIGHT_CEILING

#: The process-wide default for a provider the owner has not overridden.
#: Cloud providers get this.
DEFAULT_PROVIDER_CONCURRENCY = 32

#: The default for the local adapters (``ollama`` / ``llama_cpp`` /
#: ``lm_studio``). A local model is one machine's memory: 32 concurrent
#: streams against a 27B model is an OOM hazard, not a throughput win, so
#: the local adapters stay low unless the owner raises that provider
#: explicitly (``max_in_flight``, set from the owner console).
LOCAL_PROVIDER_CONCURRENCY = 4

#: The adapters that run a model on this LAN's own hardware. The split is
#: by provider id, which is exactly how the adapters are registered.
LOCAL_PROVIDER_IDS = frozenset({"llama_cpp", "lm_studio", "ollama"})


def check_limit(value: Any, field: str = "provider concurrency limit") -> int:
    """Validate a concurrency limit: a positive ``int``, never a ``bool``.

    ``bool`` is rejected because it is an ``int`` subclass, so ``True``
    would otherwise silently mean a limit of one. ``MAX_IN_FLIGHT_CEILING``
    bounds the other end: a limit no request could ever reach is the cap
    switched off, not a limit, so it is refused rather than stored.
    """
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field} must be a positive integer")
    if value > MAX_IN_FLIGHT_CEILING:
        raise ValueError(f"{field} must be a positive integer no greater than {MAX_IN_FLIGHT_CEILING}")
    return value


def default_limit_for(provider_id: str) -> int:
    """The default cap for a provider the owner has not overridden."""
    if provider_id in LOCAL_PROVIDER_IDS:
        return LOCAL_PROVIDER_CONCURRENCY
    return DEFAULT_PROVIDER_CONCURRENCY


def effective_limit(
    provider_id: str, override: int | None, *, default: int = DEFAULT_PROVIDER_CONCURRENCY
) -> int:
    """Owner override, else the local/cloud default for this provider."""
    if override is None:
        if provider_id in LOCAL_PROVIDER_IDS:
            return LOCAL_PROVIDER_CONCURRENCY
        return default
    return check_limit(override, "max_in_flight")


class ProviderConcurrencyLimit(AdapterError):
    #: Over the cap is a *local* capacity answer, not an upstream one: it
    #: is reported as 429 with the same body on every path - a fast
    #: refusal, no queueing. Every other adapter error keeps the ordinary
    #: upstream-failure status.
    http_status = 429

    def __init__(self, provider_id: str) -> None:
        super().__init__(ERROR_RATE_LIMITED, "local concurrency limit reached for this provider; retry later", provider_id=provider_id)


class ProviderConcurrency:
    """Bound active calls independently per provider in this process.

    ``resolver`` returns the owner's override for a provider (``None``
    when there is none) and is consulted on every acquire, so an override
    the owner saves applies to the next request without a restart. With
    no resolver the single ``limit`` applies to every provider, which is
    the unit-level seam (``ProviderConcurrency(1)``).
    """

    def __init__(
        self,
        limit: int = DEFAULT_PROVIDER_CONCURRENCY,
        resolver: Callable[[str], int | None] | None = None,
    ) -> None:
        self.limit = check_limit(limit)
        self._resolver = resolver
        self._lock = threading.Lock()
        self._active: dict[str, int] = {}

    def limit_for(self, provider_id: str) -> int:
        """The cap that applies to ``provider_id`` right now."""
        if self._resolver is None:
            return self.limit
        return effective_limit(provider_id, self._resolver(provider_id), default=self.limit)

    def acquire(self, provider_id: str) -> None:
        limit = self.limit_for(provider_id)
        with self._lock:
            active = self._active.get(provider_id, 0)
            if active >= limit:
                raise ProviderConcurrencyLimit(provider_id)
            self._active[provider_id] = active + 1

    def release(self, provider_id: str) -> None:
        with self._lock:
            active = self._active.get(provider_id, 0)
            if active <= 1:
                self._active.pop(provider_id, None)
            else:
                self._active[provider_id] = active - 1


class _StreamLease:
    """The in-flight slot held by one streaming provider call.

    The slot is released **exactly once**, whichever exit path reaches it
    first:

    * the stream generator's ``finally`` - exhaustion, an upstream error,
      or ``close()`` once the stream has been entered, and
    * the request handler's own ``finally`` through
      ``release_if_unstarted`` - the path that has to exist because
      closing a generator that was never started runs no code at all. A
      client that resets the connection after the request body is read
      but before the response headers leave the process (or a ``HEAD``
      request) abandons the stream before its first ``next()``, and
      without this handler-side release the slot stayed held until the
      gateway restarted: ``4`` such resets kill a local provider, ``32``
      a cloud one (finding F1 of security gate `t_3fd0925e`,
      ``docs/REVIEW.md`` R73).

    ``release_if_unstarted`` deliberately does nothing once the stream has
    started: while a stream is running the slot is genuinely held, and
    the stream's own ``finally`` releases it when the stream actually
    ends. Releasing it at handler exit instead would hand the slot to the
    next request while the abandoned stream was still being read.
    """

    __slots__ = ("_limits", "_lock", "_provider_id", "_released", "_started")

    def __init__(self, limits: ProviderConcurrency, provider_id: str) -> None:
        self._limits = limits
        self._provider_id = provider_id
        self._started = False
        self._released = False
        self._lock = threading.Lock()

    def mark_started(self) -> None:
        with self._lock:
            self._started = True

    def release(self) -> None:
        with self._lock:
            if self._released:
                return
            self._released = True
        self._limits.release(self._provider_id)

    def release_if_unstarted(self) -> None:
        with self._lock:
            if self._started or self._released:
                return
            self._released = True
        self._limits.release(self._provider_id)


class _LeasedStream:
    """A provider's stream with its in-flight slot lease attached.

    ``ProviderResponse.stream`` is wrapped and converted on its way to the
    HTTP response, but this object is what ``invoke`` hands back, so the
    request handler can always find the lease through
    ``release_if_unstarted`` - even when the stream itself is never
    entered and no line of the stream generator ever runs.
    """

    __slots__ = ("_lease", "_stream")

    def __init__(self, stream: Any, lease: _StreamLease) -> None:
        self._stream = stream
        self._lease = lease

    def __iter__(self) -> _LeasedStream:
        return self

    def __next__(self) -> Any:
        return next(self._stream)

    def close(self) -> None:
        """Abandon the stream and give the slot back (idempotent)."""
        try:
            self._stream.close()
        finally:
            self._lease.release()

    def release_if_unstarted(self) -> None:
        """Give the slot back only if the stream was never entered."""
        self._lease.release_if_unstarted()


class _CappedAdapter:
    def __init__(self, adapter: Any, limits: ProviderConcurrency) -> None:
        self._adapter = adapter
        self._limits = limits

    def __getattr__(self, name: str) -> Any:
        return getattr(self._adapter, name)

    def invoke(self, request: Any) -> ProviderResponse:
        provider_id = self._adapter.provider_id
        self._limits.acquire(provider_id)
        try:
            response = self._adapter.invoke(request)
        except BaseException:
            self._limits.release(provider_id)
            raise
        if response.stream is None:
            self._limits.release(provider_id)
            return response

        lease = _StreamLease(self._limits, provider_id)

        def held_stream():
            lease.mark_started()
            try:
                yield from response.stream
            finally:
                lease.release()

        return ProviderResponse(
            status=response.status,
            headers=response.headers,
            body=response.body,
            stream=_LeasedStream(held_stream(), lease),
            content_type=response.content_type,
            dialect=response.dialect,
        )


class CappedAdapterRegistry:
    """Registry facade that caps only inference calls, not provider actions."""

    def __init__(self, registry: Any, limits: ProviderConcurrency) -> None:
        self._registry = registry
        self._limits = limits
        self._wrapped: dict[str, _CappedAdapter] = {}

    def get(self, provider_id: str) -> Any:
        adapter = self._registry.get(provider_id)
        if adapter is None:
            return None
        wrapped = self._wrapped.get(provider_id)
        if wrapped is None or wrapped._adapter is not adapter:
            wrapped = _CappedAdapter(adapter, self._limits)
            self._wrapped[provider_id] = wrapped
        return wrapped

    def __getattr__(self, name: str) -> Any:
        return getattr(self._registry, name)
