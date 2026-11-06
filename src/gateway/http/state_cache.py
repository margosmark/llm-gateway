"""Cached rendering for the owner console's ``/owner/api/state`` poll.

The endpoint materialises the whole console snapshot per call - the
bounded ledger ring, the published-price catalogue, three statistics
windows, per-alias budgets, cooldowns and a ~90KB body - which measured
182ms p50 / 268ms p95 on production data while every other endpoint
answers in under 6ms. The console reads it human-paced, so the rendered
body is cached and served verbatim while its inputs are unchanged.

**Invalidation is keying, not callbacks.** The cache key is a digest of
every input the render reads whose value can change while the process is
up:

====================  ====================================================
input                 change detector
====================  ====================================================
configuration         revision **and** the config file's (inode, mtime,
                      size) - every write path persists atomically before
                      publishing, which catches a backup import that
                      replaces content without bumping revision
credential store      the per-provider ``describe()`` digest (presence and
                      field names, never values)
price catalogue       digest of ``published_prices()``
ledger ring/totals    ``RequestLedger.version`` (bumped by record, amend
                      and clear - the only mutators)
ledger rollup         digest of ``ledger.rollup()`` output, which also
                      covers the asynchronous sidecar write landing after
                      its record
circuits              structural digest of ``details()`` with the two
                      per-second fields (``remaining_seconds``,
                      ``next_probe_at``) removed, plus the ``snapshot()``
                      key set - ``state`` is kept, so an expiry flips
                      ``open`` -> ``half-open`` and the next read
                      re-renders immediately, while a countdown ticking
                      down does not evict the cache every second
limits                digest of ``LimitStore.all()``
request               query string, surface, and the mutation/credential
                      verdicts derived from the request
release identity      the **result** of ``release_identity()`` - a plain
                      four-key dict of strings. The function re-reads
                      ``release.json`` on every call, so a release written
                      after the container was up (``scripts/release.py
                      --build``, ``scripts/write_release_identity.py``
                      against a running volume) must change the key on the
                      next read. The callable *object* is a per-process
                      constant and would catch nothing - that was the
                      original defect
====================  ====================================================

Process constants the render also reads are deliberately not part of the
key: the cooldown-policy literals cannot change after start-up, and
wall-clock fields are handled by ``refresh_after`` / ``hard_max`` below.
``ledger.durability()`` *is* part of the ledger component now: it used to
be two literals, but it reports the store's row count, byte size, oldest
row and last error, so it is mutable state - a store that stops accepting
rows has to reach the owner's status view, not sit stale behind a cached
body.

A mutation therefore changes the key, and the very next read takes the
miss path and renders synchronously: no staleness window can lie about
state the owner just changed. Wall-clock-only drift (usage windows
ageing out, budget month rollover) is bounded two ways: a read whose
cached body is older than ``refresh_after`` serves the body it has and
re-renders it off the request path, so the *next* read is current again
(reads never wait for that refresh), and a body older than ``hard_max``
is never served at all - that read re-renders synchronously, which is
what bounds staleness after a long idle gap.

One render still reads its inputs exactly once, in one pass - this
wrapper sits entirely outside ``_render_state`` and re-introduces no
per-consumer ledger or catalogue re-pull (the wave-2B / R56 rule).
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..costs import published_prices
from .core import Request, Response

#: Fields of ``CircuitStore.details()`` that are derived from wall-clock
#: time on every call. They are excluded from the cache key (otherwise an
#: open cooldown would evict the cache every second) but re-rendered by
#: the background refresh, and the key still carries ``state`` plus the
#: ``snapshot()`` key set, so circuit *transitions* - open, expiry,
#: probe claim - are detected the moment they happen.
_CIRCUIT_TIME_FIELDS = frozenset({"remaining_seconds", "next_probe_at"})

#: A cached body older than this is refreshed in the background by the
#: next read. Serves only wall-clock drift: mutations never wait for it.
DEFAULT_REFRESH_AFTER_SECONDS = 2.0

#: A cached body older than this is not served at all: the read takes the
#: synchronous miss path instead. This bounds staleness after a long idle
#: gap (an hours-old body would still be exact for every mutation, but
#: its usage windows and month budget would have aged), while staying far
#: above any human- or script-paced polling interval, so a steady reader
#: never pays for it.
DEFAULT_HARD_MAX_SECONDS = 300.0

#: Distinct tokens kept alive (``?days=N`` variants and surfaces); the
#: least recently used entry is dropped first.
DEFAULT_MAX_ENTRIES = 8


def _stable(value: Any) -> str:
    """A deterministic serialisation for digesting token components."""
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


@dataclass
class _Entry:
    status: int
    headers: list[tuple[str, str]]
    body: bytes
    rendered_at: float
    request: Request
    token: str


class StateCache:
    """Token-keyed cache of the rendered ``/owner/api/state`` body.

    ``render`` is the untouched full render (``GatewayApp._render_state``);
    every response this returns is byte-for-byte a response that render
    produced for the same inputs.
    """

    def __init__(
        self,
        app: Any,
        render: Callable[[Request], Response],
        *,
        refresh_after: float = DEFAULT_REFRESH_AFTER_SECONDS,
        hard_max: float = DEFAULT_HARD_MAX_SECONDS,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._app = app
        self._render = render
        self.refresh_after = refresh_after
        self.hard_max = hard_max
        self._max_entries = max(1, int(max_entries))
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: dict[str, _Entry] = {}
        self._refreshing = False

    # --- token ----------------------------------------------------------

    def token(self, request: Request) -> str:
        """The cache key: a digest of every input the render reads that
        can change while the process is up (process constants are not
        part of it - see the module docstring)."""
        app = self._app
        config = app.config.snapshot()
        try:
            stat = app.config.path.stat()
            config_stamp: list[Any] | None = [stat.st_ino, stat.st_mtime_ns, stat.st_size]
        except OSError:
            config_stamp = None
        credentials = [
            app.credentials.describe(provider_id)
            for provider_id in sorted(config.providers)
        ]
        circuits = app.router.circuits
        circuit_details = circuits.details()
        components = [
            # The configuration itself: revision covers every normal
            # mutation; the file stamp covers a backup import that writes
            # different content under an unchanged revision.
            ["config", config.revision, config_stamp],
            ["credentials", credentials],
            ["prices", published_prices()],
            ["circuits", sorted(circuits.snapshot()), {
                key: {field: value for field, value in detail.items() if field not in _CIRCUIT_TIME_FIELDS}
                for key, detail in sorted(circuit_details.items())
            }],
            ["limits", app.limits.all()],
            ["request", [request.raw_query, app._surface(request), app._can_mutate(request), app._can_enroll_credentials(request)]],
            # Owner-auth mutations (a client key set/rotated/cleared, a
            # password change, a session issued or revoked) change what the
            # snapshot reports, so the store's revision is part of the key.
            ["access", [app.owner_auth.revision]],
            # The release identity's *result*, not the callable: the
            # callable object str()s to a per-process constant, while
            # release_identity() re-reads release.json on every call, so a
            # release written after start-up has to change this key.
            ["identity", [app.base_url, app._release_identity() if app._release_identity else {}]],
        ]
        if app.ledger is not None:
            components.append(["ledger", [app.ledger.version, app.ledger.evicted, app.ledger.totals(), app.ledger.rollup(), app.ledger.durability()]])
        return hashlib.sha256(_stable(components).encode("utf-8")).hexdigest()

    # --- serving --------------------------------------------------------

    def serve(self, request: Request) -> Response:
        token = self.token(request)
        now = self._clock()
        entry: _Entry | None
        refresh_request: Request | None = None
        with self._lock:
            entry = self._entries.get(token)
            if entry is not None:
                if now - entry.rendered_at >= self.hard_max:
                    # Too old to serve: fall through to the synchronous
                    # render below (a miss), so a long idle gap can never
                    # hand out an aged body.
                    entry = None
                else:
                    # LRU touch: re-insert at the tail of the ordered dict.
                    self._entries.pop(token, None)
                    self._entries[token] = entry
                    if now - entry.rendered_at >= self.refresh_after and not self._refreshing:
                        self._refreshing = True
                        refresh_request = entry.request
        if refresh_request is not None:
            threading.Thread(
                target=self._refresh,
                args=(refresh_request,),
                name="state-cache-refresh",
                daemon=True,
            ).start()
        if entry is not None:
            return Response(entry.status, list(entry.headers), entry.body)
        response = self._render(request)
        self._store_if_unchanged(token, request, response)
        return response

    def _store_if_unchanged(self, token: str, request: Request, response: Response) -> None:
        """Pin ``response`` under ``token`` only if state did not change mid-render.

        The render is not atomic with respect to concurrent mutations
        (it never was), so re-keying afterwards keeps a body from ever
        being served under a token it no longer describes.
        """
        if self.token(request) != token:
            return
        now = self._clock()
        with self._lock:
            self._entries.pop(token, None)
            self._entries[token] = _Entry(
                status=response.status,
                headers=list(response.headers),
                body=response.body,
                rendered_at=now,
                request=request,
                token=token,
            )
            while len(self._entries) > self._max_entries:
                oldest = next(iter(self._entries))
                self._entries.pop(oldest, None)

    def _refresh(self, request: Request) -> None:
        try:
            token = self.token(request)
            response = self._render(request)
            self._store_if_unchanged(token, request, response)
        except Exception:  # noqa: S110, BLE001 - a background refresh must never surface
            # Reads keep serving the last good body; the next read with an
            # aged body simply tries again. Deliberately swallowed: this
            # thread must not raise into the server or a test teardown.
            pass
        finally:
            with self._lock:
                self._refreshing = False

    # --- introspection (tests) ------------------------------------------

    @property
    def entry_count(self) -> int:
        with self._lock:
            return len(self._entries)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
