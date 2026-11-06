"""Process-local limits on concurrent calls to each provider."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from gateway.adapters.base import AdapterError, ProviderResponse
from gateway.config import ERROR_RATE_LIMITED

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
    would otherwise silently mean a limit of one.
    """
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field} must be a positive integer")
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

        def held_stream():
            try:
                yield from response.stream
            finally:
                self._limits.release(provider_id)

        return ProviderResponse(
            status=response.status,
            headers=response.headers,
            body=response.body,
            stream=held_stream(),
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
