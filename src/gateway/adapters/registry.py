"""The adapter registry.

Adding or removing a provider is a new module plus one registration here.
The routing core never learns a provider's name.
"""

from __future__ import annotations

from typing import Iterable

from gateway.adapters.base import Adapter
from gateway.adapters.http_client import close_all_clients


class AdapterRegistry:
    """Holds the registered adapters, keyed by provider id."""

    def __init__(self) -> None:
        self._adapters: dict[str, Adapter] = {}

    def register(self, adapter: Adapter) -> None:
        self._adapters[adapter.provider_id] = adapter

    def get(self, provider_id: str) -> Adapter | None:
        return self._adapters.get(provider_id)

    def all(self) -> list[Adapter]:
        return [self._adapters[key] for key in sorted(self._adapters)]

    def provider_ids(self) -> list[str]:
        return sorted(self._adapters)

    def close(self) -> None:
        """Release every upstream connection the adapters are holding.

        A provider connection is a process resource, so shutting the
        gateway down has to give it back rather than rely on the operating
        system to notice. The clients are tracked centrally in
        :mod:`gateway.adapters.http_client`, so this reaches every adapter
        that made one - including an adapter added later that defines no
        teardown of its own.
        """
        close_all_clients()

    def __contains__(self, provider_id: object) -> bool:
        return isinstance(provider_id, str) and provider_id in self._adapters

    def __len__(self) -> int:
        return len(self._adapters)

    def extend(self, adapters: Iterable[Adapter]) -> None:
        for adapter in adapters:
            self.register(adapter)
