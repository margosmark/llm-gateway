"""Process-local limits on concurrent calls to each provider."""

from __future__ import annotations

import threading
from typing import Any

from gateway.adapters.base import AdapterError, ProviderResponse
from gateway.config import ERROR_RATE_LIMITED

DEFAULT_PROVIDER_CONCURRENCY = 4


class ProviderConcurrencyLimit(AdapterError):
    def __init__(self, provider_id: str) -> None:
        super().__init__(ERROR_RATE_LIMITED, "local concurrency limit reached for this provider; retry later", provider_id=provider_id)


class ProviderConcurrency:
    """Bound active calls independently per provider in this process."""

    def __init__(self, limit: int = DEFAULT_PROVIDER_CONCURRENCY) -> None:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("provider concurrency limit must be a positive integer")
        self.limit = limit
        self._lock = threading.Lock()
        self._active: dict[str, int] = {}

    def acquire(self, provider_id: str) -> None:
        with self._lock:
            active = self._active.get(provider_id, 0)
            if active >= self.limit:
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
