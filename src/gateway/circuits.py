"""Durable circuit breakers, one per route.

A route that fails in a way the owner has enabled a trigger for is put
into a **local cooldown**: it is skipped for a while, and the skip is
recorded with its reason, so a cooldown can never look like a silent
fallback.

This module is that cooldown, done properly. The earlier version was a
plain in-memory timer keyed by provider, and it had four weaknesses that
all showed up in the same situation - a provider that is out of
allowance:

* **It was lost on a restart.** A gateway that came back up during an
  outage immediately called the exhausted provider again, which is exactly
  the moment a cooldown is worth most.
* **It was flat.** Every qualifying failure produced the same window,
  however many had already happened, and a success reset nothing.
* **It treated a blip like an outage.** A single transient error - a 520
  from an edge - benched the provider for the same five minutes as a
  confirmed exhaustion of the allowance.
* **It was per provider.** One route's failure benched every model at
  that provider, including models that were working.

What this store does instead:

* the state is **persisted**, so a restart during an outage does not
  defeat it;
* the window **escalates** with consecutive failures and a success resets
  it;
* **distinct failure classes get distinct treatment** - a confirmed
  exhaustion is sticky and opens for a long time, a transient blip needs
  a pattern before it opens anything;
* the state is keyed by **route** - provider *and* model - with a
  provider-wide entry for the failures that are account-scoped;
* when a window ends the circuit is **half-open**: exactly one request is
  let through to find out whether the provider recovered.

Two rules this store never breaks:

* A circuit is a **local retry policy, never evidence that the provider
  reset**. There are no background probes and no health checks.
* The stored state is **metadata only** - a provider, a model, a failure
  class, counters and timestamps. No prompt, no completion, no content.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from gateway.config import ERROR_EXHAUSTED, ERROR_RATE_LIMITED, ERROR_TRANSIENT
from gateway.stores import atomic_write_json, read_json

#: The circuit's states. A circuit is ``open`` while its window runs, and
#: ``half_open`` once the window has passed and a single probe is allowed.
STATE_OPEN = "open"
STATE_HALF_OPEN = "half_open"

#: A confirmed exhaustion is a sticky condition: the allowance is gone, and
#: minutes cannot bring it back. This is the floor for that window.
EXHAUSTED_BASE_SECONDS = 3600

#: The longest window any circuit may hold a route, however many failures
#: have happened.
MAX_BACKOFF_SECONDS = 86400

#: How many *consecutive* transient failures are needed before a route is
#: benched. One 520 from an edge is a blip, not an outage.
TRANSIENT_FAILURE_THRESHOLD = 2

#: How long a stored circuit is kept after its window has passed. A
#: circuit that has not been touched for this long is forgotten.
MAX_ENTRY_AGE_SECONDS = 7 * 86400

#: The most circuits that are stored. The oldest are dropped first.
MAX_ENTRIES = 512

#: How long a claimed recovery probe may stay in flight before another one
#: may be claimed. A probe that never completes must not block the route
#: for ever.
PROBE_TIMEOUT_SECONDS = 300

#: The key used for a circuit that covers a whole provider.
PROVIDER_WILDCARD = "*"


@dataclass
class CircuitEntry:
    """One open circuit."""

    provider: str
    #: The model this circuit covers. ``None`` for a provider-wide
    #: circuit.
    model: str | None
    #: Whether the circuit covers every route at the provider. This is
    #: kept apart from *model*, because a provider-wide circuit and a
    #: route circuit must never share an identity.
    provider_wide: bool
    #: Whether this entry actually holds the route. An entry that only
    #: counts failures is **not** a circuit: it must never block a route,
    #: and it must never be probed.
    armed: bool
    error_class: str
    #: How many consecutive failures opened this circuit.
    failures: int
    opened_at: float
    #: When the window ends, in epoch seconds.
    until: float
    #: Why the circuit was opened, in the owner's language.
    reason: str
    #: Whether a recovery probe has been claimed. **In memory only**: a
    #: restart must not leave a route blocked by a probe that no longer
    #: exists.
    probe_in_flight: bool = False
    probe_started_at: float = 0.0
    #: The token of the request that claimed the probe, so that only the
    #: claim's owner may release it. **In memory only**, like the flag.
    probe_owner: str | None = None

    @property
    def key(self) -> str:
        if self.provider_wide:
            return f"{self.provider}/{PROVIDER_WILDCARD}"
        return f"{self.provider}/{self.model or ''}"

    @property
    def scope(self) -> str:
        return "provider" if self.provider_wide else "route"

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "provider_wide": self.provider_wide,
            "armed": self.armed,
            "error_class": self.error_class,
            "failures": self.failures,
            "opened_at": self.opened_at,
            "until": self.until,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "CircuitEntry | None":
        if not isinstance(data, dict):
            return None
        provider = data.get("provider")
        if not isinstance(provider, str) or not provider:
            return None
        model = data.get("model")
        if model is not None and not isinstance(model, str):
            return None
        provider_wide = data.get("provider_wide")
        if not isinstance(provider_wide, bool):
            # A file written before the provider-wide flag existed is
            # inferred from the model: no model means the whole provider.
            provider_wide = model is None
        armed = data.get("armed")
        if not isinstance(armed, bool):
            # An entry written before the flag existed is a circuit.
            armed = True
        until = data.get("until")
        if not isinstance(until, (int, float)):
            return None
        failures = data.get("failures")
        opened_at = data.get("opened_at")
        error_class = data.get("error_class")
        reason = data.get("reason")
        return cls(
            provider=provider,
            model=model,
            provider_wide=provider_wide,
            armed=armed,
            error_class=error_class if isinstance(error_class, str) else "",
            failures=failures if isinstance(failures, int) and failures > 0 else 1,
            opened_at=opened_at if isinstance(opened_at, (int, float)) else 0.0,
            until=float(until),
            reason=reason if isinstance(reason, str) else "",
        )


class CircuitStore:
    """The open circuits, and the file they are remembered in."""

    def __init__(self, path: Path | None = None, clock: Callable[[], float] = time.time) -> None:
        self._path = path
        #: The clock, so a test can move time without sleeping.
        self._clock = clock
        self._entries: dict[str, CircuitEntry] = {}
        self._lock = threading.RLock()
        if path is not None:
            self._load()

    def _now(self) -> float:
        return self._clock()

    # --- keys ------------------------------------------------------------

    @staticmethod
    def _provider_key(provider: str) -> str:
        return f"{provider}/{PROVIDER_WILDCARD}"

    @staticmethod
    def _route_key(provider: str, model: str) -> str:
        return f"{provider}/{model}"

    # --- queries ---------------------------------------------------------

    def preview(self, provider: str, model: str) -> tuple[bool, str]:
        """Is this route usable right now, without claiming anything?

        This is the check for **enumeration**. It never mutates the store,
        because a candidate that is assembled but never invoked must not
        hold a recovery probe: a claim that is never used would keep the
        route on its fallback until the probe timed out.
        """
        return self._check(provider, model, claim=False, owner=None)

    def check(self, provider: str, model: str) -> tuple[bool, str]:
        """May this route be used, claiming a half-open recovery probe?

        This is the **claim**, kept under its historical name for callers
        that only need the verdict. An invocation path should use
        :meth:`claim` instead, because only the claim's owner can release
        it.
        """
        allowed, reason, _owner = self.claim(provider, model)
        return allowed, reason

    def claim(self, provider: str, model: str) -> tuple[bool, str, str]:
        """Atomically check eligibility and claim a half-open probe.

        Returns ``(allowed, reason, owner)``. The owner token identifies
        this claim, so that only the request that made it can release it:
        a request that gives up must never clear a probe that another
        request is still running.
        """
        owner = uuid.uuid4().hex
        allowed, reason = self._check(provider, model, claim=True, owner=owner)
        return allowed, reason, owner

    def _check(
        self, provider: str, model: str, *, claim: bool, owner: str | None
    ) -> tuple[bool, str]:
        """Return route eligibility; only an invocation path may claim."""
        with self._lock:
            now = self._now()
            self._prune(now)
            entries = [
                entry
                for entry in (self._entries.get(self._provider_key(provider)),
                              self._entries.get(self._route_key(provider, model)))
                if entry is not None and entry.armed
            ]
            for entry in entries:
                if now < entry.until:
                    return False, self._refusal(entry, now)
                # The window has passed: the circuit is half-open.
                if self._probe_is_live(entry, now):
                    return False, (
                        f"a recovery probe is already in flight for "
                        f"{self._label(entry)}, so this route is not used yet"
                    )
            # Nothing is holding the route. Claim the probe, if there is a
            # half-open circuit to probe.
            if claim:
                for entry in entries:
                    entry.probe_in_flight = True
                    entry.probe_started_at = now
                    entry.probe_owner = owner
            return True, ""

    def snapshot(self) -> dict[str, int]:
        """The remaining window of every open circuit."""
        with self._lock:
            now = self._now()
            self._prune(now)
            return {
                key: max(0, int(entry.until - now))
                for key, entry in self._entries.items()
                if entry.armed and entry.until > now
            }

    def details(self) -> dict[str, dict[str, Any]]:
        """Every circuit, with its reason and its state."""
        with self._lock:
            now = self._now()
            self._prune(now)
            return {
                key: {
                    "provider": entry.provider,
                    "model": entry.model,
                    "scope": entry.scope,
                    "error_class": entry.error_class,
                    "failures": entry.failures,
                    "remaining_seconds": max(0, int(entry.until - now)),
                    "reason": entry.reason,
                    "state": (
                        "counting"
                        if not entry.armed
                        else (STATE_OPEN if now < entry.until else STATE_HALF_OPEN)
                    ),
                }
                for key, entry in self._entries.items()
                if entry.armed or entry.failures
            }

    def remaining(self, provider: str, model: str | None = None) -> int:
        """The remaining window of a route's circuit, in seconds."""
        with self._lock:
            now = self._now()
            key = self._route_key(provider, model) if model else self._provider_key(provider)
            entry = self._entries.get(key)
            if entry is None:
                return 0
            return max(0, int(entry.until - now))

    def reason(self, provider: str, model: str | None = None) -> str:
        """Why a circuit is open."""
        with self._lock:
            key = self._route_key(provider, model) if model else self._provider_key(provider)
            entry = self._entries.get(key)
            return entry.reason if entry is not None else ""

    # --- changes ---------------------------------------------------------

    def set(
        self,
        provider: str,
        seconds: int,
        reason: str | None = None,
        model: str | None = None,
        error_class: str = "",
    ) -> None:
        """Open a circuit by hand.

        With no *model*, the circuit covers the whole provider; with one,
        it covers that route. This is the operator's lever, and the
        console uses it to clear or force a cooldown.
        """
        with self._lock:
            now = self._now()
            key = self._route_key(provider, model) if model else self._provider_key(provider)
            entry = CircuitEntry(
                provider=provider,
                model=model,
                provider_wide=False,
                armed=True,
                error_class=error_class,
                failures=1,
                opened_at=now,
                until=now + max(0, seconds),
                reason=reason or "the operator opened this circuit",
            )
            self._entries[key] = entry
            self._prune(now)
            self._persist()

    def record_failure(
        self,
        provider: str,
        model: str,
        error_class: str,
        base_seconds: int,
        retry_after: int | None = None,
    ) -> None:
        """Record a failure and open the circuit it deserves.

        The class decides the treatment:

        * ``exhausted`` is a confirmed exhaustion of the allowance. It is
          sticky, so it opens for at least an hour, and it covers the
          whole provider, because the account's allowance is gone rather
          than one route's share of it.
        * ``rate_limited`` opens the route for at least as long as the
          provider asked for, when it said.
        * ``transient`` is a blip. It opens the route only after a pattern
          of consecutive failures, and it covers that route alone.
        """
        with self._lock:
            now = self._now()
            route_key = self._route_key(provider, model)
            previous = self._entries.get(route_key)
            failures = (previous.failures + 1) if previous is not None else 1

            if error_class == ERROR_TRANSIENT:
                if failures < TRANSIENT_FAILURE_THRESHOLD:
                    # A pattern has not formed yet. Remember the failure
                    # so the next one counts, but do not bench the route.
                    entry = previous or CircuitEntry(
                        provider=provider,
                        model=model,
                        provider_wide=False,
                        armed=False,
                        error_class=error_class,
                        failures=failures,
                        opened_at=now,
                        until=now,
                        reason="a transient failure was seen",
                    )
                    entry.failures = failures
                    entry.probe_in_flight = False
                    self._entries[route_key] = entry
                    self._persist()
                    return
                base = base_seconds
                account_scoped = False
            elif error_class == ERROR_RATE_LIMITED:
                base = max(base_seconds, retry_after or 0)
                account_scoped = False
            elif error_class == ERROR_EXHAUSTED:
                base = max(base_seconds, EXHAUSTED_BASE_SECONDS)
                account_scoped = True
            else:
                base = base_seconds
                account_scoped = False

            window = min(base * (2 ** (failures - 1)), MAX_BACKOFF_SECONDS)
            reason = self._reason_for(error_class, window, failures, retry_after, base)
            # The route's own circuit always opens.
            self._entries[self._route_key(provider, model)] = CircuitEntry(
                provider=provider,
                model=model,
                provider_wide=False,
                armed=True,
                error_class=error_class,
                failures=failures,
                opened_at=now,
                until=now + window,
                reason=reason,
            )
            if account_scoped:
                # The failure is account-scoped, so it covers the provider
                # as well: the account's allowance is gone, not one route's
                # share of it.
                self._entries[self._provider_key(provider)] = CircuitEntry(
                    provider=provider,
                    model=None,
                    provider_wide=True,
                    armed=True,
                    error_class=error_class,
                    failures=failures,
                    opened_at=now,
                    until=now + window,
                    reason=reason,
                )
            self._prune(now)
            self._persist()

    def release_probe(self, provider: str, model: str, owner: str | None = None) -> None:
        """Release a claimed probe when the invocation did not happen.

        With an ``owner`` token, only the claim's own holder may release
        it: a request that gives up must never clear a probe that another
        request is still running.
        """
        with self._lock:
            for key in (self._provider_key(provider), self._route_key(provider, model)):
                entry = self._entries.get(key)
                if entry is None:
                    continue
                if owner is not None and entry.probe_owner != owner:
                    continue
                entry.probe_in_flight = False
                entry.probe_started_at = 0.0
                entry.probe_owner = None

    def record_success(self, provider: str, model: str) -> None:
        """Record a success, which closes the route's circuit.

        A provider-wide circuit is only closed by the probe that was
        allowed through it - one healthy route does not prove that an
        account's allowance came back.
        """
        with self._lock:
            provider_key = self._provider_key(provider)
            entry = self._entries.get(provider_key)
            if entry is not None and entry.probe_in_flight:
                self._entries.pop(provider_key, None)
            self._entries.pop(self._route_key(provider, model), None)
            self._persist()

    def clear(self, provider: str | None = None, model: str | None = None) -> None:
        """Forget a circuit, or all of them."""
        with self._lock:
            if provider is None:
                self._entries.clear()
            elif model is None:
                self._entries.pop(self._provider_key(provider), None)
                for key in [k for k in self._entries if k.startswith(f"{provider}/")]:
                    self._entries.pop(key, None)
            else:
                self._entries.pop(self._route_key(provider, model), None)
            self._persist()

    # --- helpers ---------------------------------------------------------

    @staticmethod
    def _label(entry: CircuitEntry) -> str:
        if entry.model is None:
            return f"the provider {entry.provider}"
        return f"{entry.provider} → {entry.model}"

    @staticmethod
    def _reason_for(
        error_class: str,
        window: int,
        failures: int,
        retry_after: int | None,
        base: int,
    ) -> str:
        if error_class == ERROR_EXHAUSTED:
            cause = "the provider reported that this account's allowance is exhausted"
        elif error_class == ERROR_RATE_LIMITED:
            cause = "the provider rate-limited this route"
        elif error_class == ERROR_TRANSIENT:
            cause = "the provider failed transiently"
        else:
            cause = "the provider failed"
        if failures > 1:
            cause += f" ({failures} consecutive failures)"
        # Say which of the two answers set the window, so a skip can be
        # read without guessing.
        if retry_after and retry_after >= base:
            cause += f" and the provider asked us to wait {retry_after}s"
        elif base:
            cause += f" and the local cooldown policy is {base}s"
        return f"{cause}; this route is held for {window}s"

    @staticmethod
    def _refusal(entry: CircuitEntry, now: float) -> str:
        remaining = max(0, int(entry.until - now))
        if entry.model is None:
            return f"the provider {entry.provider} is in a local cooldown for {remaining}s ({entry.reason})"
        return f"this route is in a local cooldown for {remaining}s ({entry.reason})"

    @staticmethod
    def _probe_is_live(entry: CircuitEntry, now: float) -> bool:
        if not entry.probe_in_flight:
            return False
        if now - entry.probe_started_at > PROBE_TIMEOUT_SECONDS:
            # The probe was abandoned. It must not block the route for ever.
            entry.probe_in_flight = False
            entry.probe_owner = None
            return False
        return True

    def _prune(self, now: float) -> None:
        """Forget circuits that are stale, and keep the store bounded."""
        for key, entry in list(self._entries.items()):
            if now - entry.until > MAX_ENTRY_AGE_SECONDS:
                self._entries.pop(key, None)
        if len(self._entries) <= MAX_ENTRIES:
            return
        oldest = sorted(self._entries.items(), key=lambda item: item[1].until)
        for key, _entry in oldest[: len(self._entries) - MAX_ENTRIES]:
            self._entries.pop(key, None)

    # --- persistence -----------------------------------------------------

    def _load(self) -> None:
        assert self._path is not None
        payload = read_json(self._path, {})
        if not isinstance(payload, dict):
            return
        raw = payload.get("circuits")
        if not isinstance(raw, dict):
            return
        now = self._now()
        for value in raw.values():
            entry = CircuitEntry.from_dict(value)
            if entry is None:
                continue
            if now - entry.until > MAX_ENTRY_AGE_SECONDS:
                continue
            # A probe is never remembered: a restart must not leave a route
            # blocked by a probe that no longer exists.
            entry.probe_in_flight = False
            self._entries[entry.key] = entry

    def _persist(self) -> None:
        if self._path is None:
            return
        now = self._now()
        payload = {
            "version": 1,
            "circuits": {
                key: entry.to_dict()
                for key, entry in sorted(self._entries.items())
                if now - entry.until <= MAX_ENTRY_AGE_SECONDS
            },
        }
        try:
            atomic_write_json(self._path, payload)
        except OSError:
            # A circuit that cannot be written down is still a circuit for
            # this process. Losing it must never break a request.
            pass
