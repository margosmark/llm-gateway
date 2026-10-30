"""Opt-in capability discovery primitives. Real provider transports are deliberately unregistered.

The current MVP exposes preview/read APIs but rejects execution until an adapter
has an approved bounded request and pricing contract. The fake protocol exists
only for deterministic isolated tests.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from gateway.stores import atomic_write_json, read_json

PROBES = {"text_generation", "image_input", "function_tools"}
CAPABILITY_FIELDS = {"image_input": "supports_image_input", "function_tools": "supports_function_tools"}
# The complete vocabulary an approval decision may ever write. The proposal
# sidecar is untrusted input (the loader checks only schema and bucket types),
# so the decision path must consult this shared list rather than the proposal's
# own field names. The catalogue-refresh path reuses the same constant.
APPROVABLE_METADATA_FIELDS = frozenset(CAPABILITY_FIELDS.values())
#: The evidence classification a proposal field record may carry (spec:
#: `"classification": "documented|observed"`). The sidecar loader validates
#: only schema and bucket types, never field keys, so the decision path must
#: consult this shared list before any config or evidence write -- otherwise a
#: deleted or bogus classification key fails only inside the apply loop, after
#: decide() has already persisted the decision and burned the proposal.
EVIDENCE_CLASSIFICATIONS = frozenset({"documented", "observed"})
MAX_RUNS = 500
PREVIEW_TTL_SECONDS = 300


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def row_fingerprint(row: dict[str, Any]) -> str:
    encoded = json.dumps(row, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def proposal_expired(proposal: dict[str, Any]) -> bool:
    """True when the proposal's decision window has closed.

    One definition of "expired" for both call sites: the HTTP handler gates
    NEW decisions on it *before* the config transaction commits (spec CAP-105
    / CAP-201: an expired proposal is refused with no state change), and the
    store re-checks it as defense in depth. Keeping them identical is what
    stops the two layers from disagreeing about a refusal.

    The proposal is untrusted sidecar input: a missing or unparseable
    `expires_at` raises here, which the caller turns into a refusal -- expiry
    is never silently treated as "not expired".
    """
    return datetime.fromisoformat(proposal["expires_at"].replace("Z", "+00:00")) <= _now()


def preview_for(provider_id: str, model_id: str, probes: list[Any], revision: int) -> dict[str, Any]:
    if not provider_id or len(provider_id) > 64 or not model_id or len(model_id) > 256:
        raise ValueError("invalid target")
    if not probes or len(probes) > 3 or any(not isinstance(p, str) or p not in PROBES for p in probes) or len(set(probes)) != len(probes):
        raise ValueError("invalid probe selection")
    return {
        "available": False,
        "provider_id": provider_id,
        "model_id": model_id,
        "probes": probes,
        "suite_version": "mvp-1",
        "maximum_requests": len(probes),
        "max_output_tokens": 128,
        "timeout_seconds": 15,
        "concurrency": 1,
        "retries": 0,
        "streaming": False,
        "price": {"status": "unknown", "amount": None, "currency": "USD", "source": None},
        "reason": "No approved provider probe protocol and trustworthy price are registered; paid probe is blocked.",
        "base_revision": revision,
        "estimate_is_not_a_bill_guarantee": True,
    }


class DiscoveryStore:
    """Private atomic evidence store; never contains probe content or credentials."""
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        raw = read_json(path, {})
        valid = isinstance(raw, dict) and raw.get("schema") == 1 and all(
            isinstance(raw.get(key), list) for key in ("runs", "proposals", "approvals", "decisions")
        )
        self._data = raw if valid else {"schema": 1, "runs": [], "proposals": [], "approvals": [], "decisions": []}

    def read(self, provider_id: str, model_id: str) -> dict[str, Any]:
        if len(provider_id) > 64 or len(model_id) > 256:
            return {"runs": [], "proposals": [], "approvals": []}
        with self._lock:
            return {key: [row for row in self._data[key] if row.get("provider_id") == provider_id and row.get("model_id") == model_id]
                    for key in ("runs", "proposals", "approvals")}

    def record_fake_result(self, provider_id: str, model_id: str, results: list[dict[str, Any]], row: dict[str, Any], revision: int) -> str:
        """Test seam: persist sanitized synthetic evidence and an unselected proposal."""
        if len(results) > 3 or any(
            not isinstance(r, dict)
            or r.get("capability") not in PROBES
            or r.get("status") not in {"observed_supported", "observed_unsupported", "inconclusive"}
            for r in results
        ):
            raise ValueError("invalid result")
        now = _now()
        fields = []
        for result in results:
            field = CAPABILITY_FIELDS.get(result.get("capability"))
            status = result.get("status")
            if field and status in {"observed_supported", "observed_unsupported"}:
                proposed = status == "observed_supported"
                current = row.get(field)
                if isinstance(current, bool) and current != proposed:
                    fields.append({"field": field, "current_value": current, "proposed_value": proposed,
                                   "evidence_refs": [], "classification": "observed", "decision": "pending"})
        run_id, proposal_id = secrets.token_urlsafe(18), secrets.token_urlsafe(18)
        safe_errors = {"timeout", "rate_limited", "auth", "policy", "malformed", "unsupported", "transport", None}
        clean = [{"capability": r["capability"], "status": r["status"], "method": "synthetic_isolated_fake",
                  "error_class": r.get("error_class") if r.get("error_class") in safe_errors else None,
                  "request_count": 1} for r in results]
        run = {"run_id": run_id, "provider_id": provider_id, "model_id": model_id, "started_at": _iso(now),
               "finished_at": _iso(now), "trigger": "owner_console", "suite_version": "mvp-1", "results": clean}
        proposal = {"proposal_id": proposal_id, "provider_id": provider_id, "model_id": model_id,
                    "base_revision": revision, "base_model_fingerprint": row_fingerprint(row),
                    "created_at": _iso(now), "expires_at": _iso(now + timedelta(seconds=PREVIEW_TTL_SECONDS)),
                    "status": "pending", "fields": fields}
        with self._lock:
            self._data["runs"].append(run); self._data["proposals"].append(proposal)
            for key in ("runs", "proposals"):
                self._data[key] = self._data[key][-MAX_RUNS:]
            self._persist()
        return proposal_id

    def proposal(self, proposal_id: str) -> dict[str, Any] | None:
        with self._lock:
            for item in self._data["proposals"]:
                if item.get("proposal_id") == proposal_id:
                    return json.loads(json.dumps(item))
        return None

    def decision_replay(self, proposal_id: str, decision_id: str, decisions: dict[str, str]) -> dict[str, Any] | None:
        """Return a prior exact decision result; reject decision-id reuse."""
        with self._lock:
            for previous in self._data["decisions"]:
                if previous.get("decision_id") == decision_id:
                    if previous.get("proposal_id") != proposal_id or previous.get("decisions") != decisions:
                        raise RuntimeError("decision id conflict")
                    return json.loads(json.dumps(previous["result"]))
        return None

    def rebase_partial(self, proposal_id: str, revision: int, fingerprint: str) -> None:
        """Advance the remaining decision CAS token after our own partial write."""
        with self._lock:
            proposal = next((p for p in self._data["proposals"] if p.get("proposal_id") == proposal_id), None)
            if proposal is not None and proposal.get("status") == "partially_approved":
                proposal["base_revision"] = revision
                proposal["base_model_fingerprint"] = fingerprint
                self._persist()

    def mark_conflicted(self, proposal_id: str) -> None:
        """Record a stale compare-and-swap against the proposal snapshot."""
        with self._lock:
            proposal = next((p for p in self._data["proposals"] if p.get("proposal_id") == proposal_id), None)
            if proposal is not None and proposal.get("status") in {"pending", "partially_approved"}:
                proposal["status"] = "conflicted"
                self._persist()

    def decide(self, proposal_id: str, decision_id: str, decisions: dict[str, str], revision: int, fingerprint: str, *, recover_committed: bool = False) -> dict[str, Any]:
        if not proposal_id or len(proposal_id) > 64 or not decision_id or len(decision_id) > 64:
            raise ValueError("invalid decision id")
        if not decisions or len(decisions) > 3 or any(v not in {"approved", "rejected"} for v in decisions.values()):
            raise ValueError("invalid decisions")
        with self._lock:
            original = self._data
            self._data = json.loads(json.dumps(original))
            try:
                return self._decide_locked(proposal_id, decision_id, decisions, revision, fingerprint, recover_committed=recover_committed)
            except OSError:
                # Persistence failures must not leave an in-memory decision
                # that can be replayed as success. Conflicts intentionally
                # persist their terminal status before raising RuntimeError.
                self._data = original
                raise

    def _decide_locked(self, proposal_id: str, decision_id: str, decisions: dict[str, str], revision: int, fingerprint: str, *, recover_committed: bool = False) -> dict[str, Any]:
            for previous in self._data["decisions"]:
                if previous.get("decision_id") == decision_id:
                    if previous.get("proposal_id") != proposal_id or previous.get("decisions") != decisions:
                        raise RuntimeError("decision id conflict")
                    return json.loads(json.dumps(previous["result"]))
            proposal = next((p for p in self._data["proposals"] if p.get("proposal_id") == proposal_id), None)
            if proposal is None:
                raise KeyError("unknown proposal")
            expired = proposal_expired(proposal)
            # Expiry gates new decisions -- at BOTH layers: the HTTP handler
            # refuses an expired proposal before the config transaction
            # commits (no write at all), and this store-side check is defense
            # in depth for any caller that skips that gate. The exception is
            # completion of the exact audit record for a config transaction
            # already durably committed; the HTTP layer may set
            # recover_committed only after matching config's transaction
            # marker. The decision-id replay check above remains authoritative.
            recoverable_status = {"pending", "partially_approved", "expired"} if recover_committed else {"pending", "partially_approved"}
            if proposal["status"] not in recoverable_status or (expired and not recover_committed):
                if proposal["status"] in {"pending", "partially_approved"}:
                    proposal["status"] = "expired"
                    self._persist()
                raise RuntimeError("proposal expired or resolved")
            # A partial decision is a durable checkpoint, not a terminal state.
            # Keep subsequent field decisions bound to the original model snapshot.
            pending_fields = {f.get("field") for f in proposal.get("fields", [])
                              if isinstance(f, dict) and f.get("decision") == "pending"}
            if set(decisions) - pending_fields:
                raise ValueError("field already decided")
            if revision != proposal["base_revision"] or fingerprint != proposal["base_model_fingerprint"]:
                proposal["status"] = "conflicted"
                self._persist()
                raise RuntimeError("proposal conflict")
            proposal_fields = {f.get("field") for f in proposal.get("fields", []) if isinstance(f, dict)}
            if set(decisions) - APPROVABLE_METADATA_FIELDS:
                # Defense in depth: the HTTP handler checks first, but the
                # store must not record a decision naming a non-capability
                # field even if it is driven by another caller.
                raise ValueError("field outside capability vocabulary")
            if set(decisions) - proposal_fields:
                raise ValueError("unknown proposal field")
            for field in proposal["fields"]:
                if isinstance(field, dict) and field.get("field") in decisions:
                    field["decision"] = decisions[field["field"]]
            decided_fields = [f for f in proposal["fields"] if isinstance(f, dict)]
            proposal["status"] = "approved" if all(f["decision"] != "pending" for f in decided_fields) else "partially_approved"
            result = {"proposal_id": proposal_id, "status": proposal["status"], "decisions": decisions}
            self._data["decisions"].append({"proposal_id": proposal_id, "decision_id": decision_id, "decisions": decisions, "result": result})
            self._data["decisions"] = self._data["decisions"][-MAX_RUNS:]
            self._persist()
            return result

    def _persist(self) -> None:
        atomic_write_json(self.path, self._data, mode=0o600)
