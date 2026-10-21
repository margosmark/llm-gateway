"""Deterministic weighted route selection, sticky to a conversation."""

from __future__ import annotations

import hashlib
from typing import Sequence, TypeVar

T = TypeVar("T")


def weighted_index(weights: Sequence[int], conversation: str, alias: str) -> int:
    """Choose a weighted slot deterministically without retaining conversation data."""
    if not weights or any(isinstance(weight, bool) or not isinstance(weight, int) or weight <= 0 for weight in weights):
        raise ValueError("weights must be a non-empty sequence of positive integers")
    total = sum(weights)
    digest = hashlib.sha256(f"{alias}\0{conversation}".encode("utf-8")).digest()
    slot = int.from_bytes(digest[:8], "big") % total
    for index, weight in enumerate(weights):
        if slot < weight:
            return index
        slot -= weight
    raise AssertionError("weighted slot was outside the pool")


def select_weighted(routes: Sequence[T], weights: Sequence[int], conversation: str, alias: str) -> tuple[T, list[T]]:
    """Return the sticky primary and remaining pool entries as failure candidates."""
    if len(routes) != len(weights):
        raise ValueError("routes and weights must have equal lengths")
    index = weighted_index(weights, conversation, alias)
    return routes[index], [route for i, route in enumerate(routes) if i != index]
