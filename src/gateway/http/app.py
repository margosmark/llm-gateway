"""Single-listener trusted-LAN console and OpenAI-compatible API."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import secrets
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from gateway.adapters.base import AdapterError
from gateway.adapters.chatgpt_codex import (
    ANTI_PHISHING,
    PREREQUISITE,
    ChatGPTCodexAdapter,
)
from gateway.adapters.local_openai import LocalOpenAIAdapter
from gateway.adapters.local_policy import LocalEndpointError, normalize_base_url
from gateway.auth import ProfileAuth, access_mode_description, profile_allows
from gateway.bridge.anthropic import aggregate_stream_any, convert_buffered_bytes, convert_stream_bytes
from gateway.bridge.model import ConversionError
from gateway.bridge.streams import sse_done
from gateway.backup import export_config, import_config, snapshot_interval_seconds
from gateway.buildinfo import MCP_CONTRACT_VERSION
from gateway.budget import budget_status, over_budget
from gateway.circuits import (
    EXHAUSTED_BASE_SECONDS,
    PROBE_TIMEOUT_SECONDS,
    TRANSIENT_FAILURE_THRESHOLD,
)
from gateway.costs import (
    aggregate_rollup_rows,
    aggregate_usage,
    annotate_events,
    published_prices,
    response_token_counters,
    stream_token_counters,
)
from gateway.ledger_persistence import ROLLUP_SCHEMA
from gateway.leaderboard import build_leaderboard
from gateway.owner_auth import (
    COOKIE_NAME,
    SESSION_TTL_SECONDS,
    MachineKeyStore,
    OwnerAuthError,
    OwnerAuthStore,
)
from gateway.statistics import aggregate_statistics
from gateway.provider_health import provider_health
from gateway.stores import atomic_write_json
from gateway.capability_discovery import (
    APPROVABLE_METADATA_FIELDS,
    EVIDENCE_CLASSIFICATIONS,
    DiscoveryStore,
    proposal_expired,
)
from gateway.capability_probe import (
    EVIDENCE_METHOD,
    catalog_price_for,
    ConfirmationError,
    ConfirmationStore,
    ProbeRefused,
    RunGate,
    build_preview,
    mark_unavailable,
    plan_requests,
    preview_binding,
    protocol_for,
    price_status,
    registry_coverage,
    run_suite,
)
from gateway.config import (

    FALLBACK_TRIGGERS,
    PHASES,
    STATE_CONFIGURED_UNTESTED,
    STATE_CONNECTED,
    STATE_FAILED,
    STATE_NOT_CONFIGURED,
    ConfigStore,
    GatewayConfig,
    ModelEntry,
    RouteRef,
    utc_now_iso,
)
from gateway.core.capabilities import (
    DIALECT_OPENAI_CHAT,
    DIALECT_OPENAI_RESPONSES,
    alias_context_limit,
)
from gateway.core.routing import RoutingError, Router
from gateway.credentials import CredentialStore
from gateway.fallback_reasons import fallback_reason_class
from gateway.http import messages as messages_surface
from gateway.http.core import Request, Response, Router as HttpRouter, StreamResponse, json_response
from gateway.http.security import (
    MUTATING_METHODS,
    SecurityError,
    SecurityPolicy,
    bearer_authorization,
    check_forwarded_headers,
    check_host,
    check_origin,
)
from gateway.http.state_cache import StateCache
from gateway.identifiers import ValidationError, validate_alias_name, validate_identifier, validate_profile_id
from gateway.logging import RequestLogger
from gateway.ledger_export import DEFAULT_EXPORT_ROWS, MAX_EXPORT_ROWS, export_records, render_csv, render_json
from gateway.metrics import render_metrics
from gateway.routes import validate_alias_routes, validate_phase_routing
from gateway.provider_concurrency import CappedAdapterRegistry, ProviderConcurrency, check_limit, effective_limit
from gateway.observability import (
    CONFIDENCE_MEDIUM,
    CONFIDENCE_NONE,
    LimitSnapshot,
    LimitStore,
    RequestLedger,
    SOURCE_PUBLISHED_ESTIMATE,
    SOURCE_UNKNOWN,
    new_request_id,
)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

SURFACE_OWNER = "owner"

# --- analytics: the durable daily rollup, surfaced ----------------------

#: The analytics window the console asks for by default, and the hard cap.
#: The rollup retains exactly the ledger's 30-day window, so a longer window
#: would be a claim the data cannot support.
ANALYTICS_DEFAULT_DAYS = 7
ANALYTICS_MAX_DAYS = 30

#: A recorded fallback reason is a full sentence that changes by the second
#: ("... the provider chatgpt_codex is in a local cooldown for 771s (...)"),
#: so counting raw strings would open a new bucket for every request. The
#: rules live in :mod:`gateway.fallback_reasons`, shared with the rollup
#: store that folds reasons *at ingest* - one module, one ordered rule set,
#: one closed vocabulary (``cooldown`` / ``route_ineligible`` /
#: ``upstream_failure`` / ``other`` / ``unknown``), so the reader and the
#: store cannot drift: the rules are ordered, first match wins, and anything
#: unmatched is reported as ``other``, never dropped and never guessed at.
#: The console shows the label; the API reports the key. The reader-side
#: :func:`fallback_reason_class` call below is the belt to that braces: it
#: re-classifies whatever an older sidecar or a hand-written payload
#: carries, and it is idempotent on labels the store already folded.


def analytics_days(request: Request) -> tuple[int, int | None]:
    """The requested analytics window: ``?days=N``, default 7, clamped to 1..30.

    An unparseable value falls back to the default; a value outside the
    clamp is applied clamped and reported back in ``requested_days`` so the
    console can say it did not get what it asked for.
    """
    raw = request.query_one("days")
    if raw is None:
        return ANALYTICS_DEFAULT_DAYS, None
    try:
        requested = int(raw)
    except (TypeError, ValueError):
        return ANALYTICS_DEFAULT_DAYS, None
    applied = max(1, min(ANALYTICS_MAX_DAYS, requested))
    return applied, (requested if requested != applied else None)


def _nonnegative(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _rate(fallbacks: int | None, requests: int | None) -> float | None:
    """Fallbacks / requests, or None when either side is unknown or empty.

    A rate with no requests is unknown - never ``0.0``, which would read as
    "no fallbacks happened" rather than "nothing was measured". The same
    applies to a fallback count the payload did not carry: an absent count
    is not a count of zero.
    """
    if not requests or fallbacks is None:
        return None
    return round(fallbacks / requests, 6)


def _reason_breakdown(counts: Any) -> list[dict[str, Any]] | None:
    """The bounded, sorted by-reason view of a raw ``fallback_reasons`` map."""
    if not isinstance(counts, dict) or not counts:
        return [] if isinstance(counts, dict) else None
    totals: dict[str, int] = {}
    for reason, count in counts.items():
        if not isinstance(count, int) or isinstance(count, bool) or count <= 0:
            continue
        label = fallback_reason_class(reason)
        totals[label] = totals.get(label, 0) + count
    total = sum(totals.values())
    return [
        {"reason": label, "count": count, "share": round(count / total, 6) if total else None}
        for label, count in sorted(totals.items(), key=lambda item: (-item[1], item[0]))
    ]


def _fallback_group(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Requests, fallbacks, the rate and the reasons for one group of rows."""
    requests = 0
    fallbacks = 0
    reasons: dict[str, int] = {}
    for row in rows:
        requests += _nonnegative(row.get("requests")) or 0
        fallbacks += _nonnegative(row.get("fallbacks")) or 0
        breakdown = _reason_breakdown(row.get("fallback_reasons")) or []
        for entry in breakdown:
            reasons[entry["reason"]] = reasons.get(entry["reason"], 0) + entry["count"]
    total = sum(reasons.values())
    top = [
        {"reason": label, "count": count, "share": round(count / total, 6) if total else None}
        for label, count in sorted(reasons.items(), key=lambda item: (-item[1], item[0]))
    ]
    return {
        "requests": requests,
        "fallbacks": fallbacks,
        "fallback_rate": _rate(fallbacks, requests),
        "fallback_reasons": top,
    }


def _fallback_groups(rows: list[dict[str, Any]], dimension: str) -> dict[str, Any]:
    """``_fallback_group`` for every distinct value of one rollup dimension."""
    buckets: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        key = row.get(dimension)
        if isinstance(key, str):
            buckets.setdefault(key, []).append(row)
    return {key: _fallback_group(buckets[key]) for key in sorted(buckets)}


def _empty_series_entry(day: str, status: str) -> dict[str, Any]:
    return {"day": day, "status": status, "requests": None, "input_tokens": None,
            "output_tokens": None, "cached_tokens": None, "fallbacks": None,
            "errors": None, "fallback_rate": None, "fallback_reasons": None}


def build_analytics(
    rollup: dict[str, Any] | None,
    *,
    days: int = ANALYTICS_DEFAULT_DAYS,
    requested_days: int | None = None,
    prices: dict[str, dict[str, Any]] | None = None,
    today: datetime | None = None,
) -> dict[str, Any]:
    """Assemble the owner state's analytics block from the durable rollup.

    This runs on every ``/owner/api/state`` poll, so it reads only the
    in-memory rollup the ledger already maintains - it never opens the
    ledger file. The block keeps the rollup's own ``coverage`` contract: a
    day without rows is reported with its status and null values, never as
    a zero, and a schema this build does not recognise is refused rather
    than interpreted.

    ``prices`` is the caller's price snapshot, so the catalogue is loaded
    once per state request no matter how many days are aggregated.
    """
    moment = today if today is not None else datetime.now(UTC)
    window = [(moment - timedelta(days=offset)).strftime("%Y-%m-%d") for offset in range(days - 1, -1, -1)]
    today_text = window[-1]
    schema = rollup.get("schema") if isinstance(rollup, dict) else None
    # A sidecar whose schema this build does not recognise is refused rather
    # than interpreted: a missing field would otherwise read as a zero.
    data: dict[str, Any] | None = rollup if isinstance(rollup, dict) and schema == ROLLUP_SCHEMA else None
    available = data is not None
    reason = None if available else ("durable_rollup_disabled" if rollup is None else "unrecognised_rollup_schema")

    raw_coverage = data.get("coverage") if data is not None else None
    coverage = raw_coverage if isinstance(raw_coverage, dict) else None
    first_row = coverage.get("first_row_ts") if coverage else None
    first_day = first_row[:10] if isinstance(first_row, str) and len(first_row) >= 10 else None

    rows: list[dict[str, Any]] = []
    daily_map: dict[str, dict[str, Any]] = {}
    if data is not None:
        for row in data.get("rows") or []:
            if isinstance(row, dict) and isinstance(row.get("day"), str) and window[0] <= row["day"] <= today_text:
                rows.append(row)
        for entry in data.get("daily") or []:
            if isinstance(entry, dict) and isinstance(entry.get("day"), str):
                daily_map[entry["day"]] = entry

    series: list[dict[str, Any]] = []
    rows_by_day: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        rows_by_day.setdefault(row["day"], []).append(row)
    for day in window:
        entry = daily_map.get(day)
        if not available:
            series.append(_empty_series_entry(day, "unknown"))
            continue
        if entry is not None:
            series.append(_series_entry(day, entry, "open" if day == today_text else "ok"))
            continue
        if day == today_text:
            # The day has not finished; no rows yet is expected, not a hole.
            series.append(_empty_series_entry(day, "no_rows"))
        elif first_day is None or day < first_day:
            # History starts later than this day: nothing was ever recorded
            # here, so this is not a gap in recorded data.
            series.append(_empty_series_entry(day, "outside_coverage"))
        else:
            series.append(_empty_series_entry(day, "gap"))

    usage = {day: (aggregate_rollup_rows(rows_by_day[day], _prices=prices)
                   if available and day in rows_by_day else None)
             for day in window}

    payload: dict[str, Any] = {
        "available": available,
        "unavailable_reason": reason,
        "source": data.get("source") if data is not None else None,
        "schema": data.get("schema") if data is not None else None,
        "generated_at": data.get("generated_at") if data is not None else None,
        "coverage": coverage,
        "days": days,
        "window": {"from": window[0], "to": today_text},
        "gaps": [entry["day"] for entry in series if entry["status"] == "gap"],
        "fallback": {
            "overall": _fallback_group(rows),
            "alias": _fallback_groups(rows, "alias"),
            "provider": _fallback_groups(rows, "provider"),
            "model": _fallback_groups(rows, "model"),
        },
        "series": series,
        "usage": usage,
    }
    if requested_days is not None:
        payload["requested_days"] = requested_days
    return payload


def _series_entry(day: str, entry: dict[str, Any], status: str) -> dict[str, Any]:
    requests = _nonnegative(entry.get("requests"))
    fallbacks = _nonnegative(entry.get("fallbacks"))
    errors = entry.get("errors")
    error_total = (sum(count for count in errors.values() if isinstance(count, int) and not isinstance(count, bool))
                   if isinstance(errors, dict) else None)
    return {
        "day": day,
        "status": status,
        "requests": requests,
        "input_tokens": _nonnegative(entry.get("input_tokens")),
        "output_tokens": _nonnegative(entry.get("output_tokens")),
        "cached_tokens": _nonnegative(entry.get("cached_tokens")),
        "fallbacks": fallbacks,
        "errors": error_total,
        "fallback_rate": _rate(fallbacks, requests),
        "fallback_reasons": _reason_breakdown(entry.get("fallback_reasons")),
    }



#: The owner API surface a machine key may reach. **Deny by default**:
#: anything not listed stays human-session-only, which is what keeps the
#: console shell, every ``/api/auth/*`` endpoint, backup export/import,
#: the request preview and the profile token mint out of an automation
#: credential's hands. Paths are mount-relative (``/api/...``), so they
#: hold for every console mount.
MACHINE_KEY_READ_PATHS: frozenset[str] = frozenset(
    {
        "/api/status",
        "/api/state",
        "/api/leaderboard",
        "/api/capabilities",
        "/api/ledger/export",
    }
)
MACHINE_KEY_WRITE_PATHS: frozenset[str] = frozenset(
    {
        "/api/alias",
        "/api/alias-group",
        "/api/model",
        "/api/provider",
        "/api/settings",
        "/api/capabilities/preview",
        "/api/capabilities/run",
        "/api/capabilities/decision",
        "/api/capabilities/probe/cancel",
    }
)

#: Provider actions a machine key may never invoke, even with a write
#: scope: they enrol or acquire a *credential* (a provider API key, a
#: device-authorisation flow). Credential minting stays human-session-
#: only, exactly like the machine-key endpoints themselves.
MACHINE_KEY_BLOCKED_PROVIDER_ACTIONS: frozenset[str] = frozenset(
    {"enroll_key", "device_start", "device_poll"}
)


class GatewayApp:
    """The HTTP application."""

    def __init__(
        self,
        config: ConfigStore,
        credentials: CredentialStore,
        router: Router,
        registry,
        profile_auth: ProfileAuth,
        policy: SecurityPolicy,
        ledger: RequestLedger | None = None,
        limits: LimitStore | None = None,
        release_identity: Callable[[], dict[str, Any]] | None = None,
        base_url: str = "",
        mounts: tuple[str, ...] = ("/owner",),
        logger: RequestLogger | None = None,
        owner_auth: OwnerAuthStore | None = None,
        machine_keys: MachineKeyStore | None = None,
        cookie_secure: bool | None = None,
    ) -> None:
        self.config = config
        self.discovery = DiscoveryStore(config.path.parent / "capability_discovery.json")
        # Bounded probe state: single-use confirmations and a one-run gate.
        self.probe_confirmations = ConfirmationStore()
        self.probe_gate = RunGate()
        #: The tariff lookup. Overridable so a test can prove that unknown
        #: or stale pricing fails closed without touching the catalogue.
        self.probe_price_lookup = catalog_price_for
        self.credentials = credentials
        self.router = router
        self.registry = registry
        # The cap is per provider and the owner can override it per
        # provider, so the guard reads the config on every acquire: the
        # resolver returns the saved override (or None) and the effective
        # limit - cloud default, local floor, owner override - is derived
        # in gateway.provider_concurrency.
        self.router._registry = CappedAdapterRegistry(
            registry,
            ProviderConcurrency(resolver=self._provider_max_in_flight),
        )

        self.profile_auth = profile_auth
        self.policy = policy
        #: Owner authentication: the account, its sessions and the
        #: optional model client key. Built from the data directory when a
        #: caller does not inject one, so every construction path (CLI,
        #: fixtures) gets the same boundary.
        self.owner_auth = owner_auth if owner_auth is not None else OwnerAuthStore(config.path.parent)
        #: Machine keys: the third credential, in its own owner-only file
        #: next to the account store. Same data directory, same
        #: ``0600``-inside-``0700`` rule, separate record on purpose.
        self.machine_keys = machine_keys if machine_keys is not None else MachineKeyStore(config.path.parent)
        #: The one-time setup code, generated here so it exists before the
        #: first request. Only ``None`` when an account already exists or
        #: the code was already issued; the plaintext is printed once at
        #: startup (server-side) and kept in an owner-only host file.
        self.setup_code_created = self.owner_auth.ensure_setup_code()
        #: ``Secure`` on the session cookie when the deployment is served
        #: over TLS. The gateway itself speaks plain HTTP on the private
        #: LAN, so the attribute is derived from an explicitly https
        #: canonical origin or ``GATEWAY_COOKIE_SECURE=1`` - never guessed
        #: from a request header a client controls.
        self.cookie_secure = (
            bool(cookie_secure)
            if cookie_secure is not None
            else any(origin.lower().startswith("https://") for origin in policy.canonical_origins)
            or os.environ.get("GATEWAY_COOKIE_SECURE", "") == "1"
        )
        self.ledger = ledger
        self.limits = limits or LimitStore()
        self._release_identity = release_identity
        self.base_url = base_url
        #: Which browser surfaces this listener serves. The LAN listener
        #: serves "/owner"; the loopback listener serves "/setup". They are
        #: separate listeners because a wildcard bind is forbidden, so
        #: 127.0.0.1 is only reachable if something binds it explicitly.
        self.mounts = mounts
        #: A per-process secret used to derive upstream conversation
        #: identifiers. It is regenerated on every start, which is what
        #: bounds the lifetime of a derived session.
        self._session_secret = secrets.token_bytes(32)
        #: One metadata-only line per request. Never carries a body.
        self.logger = logger if logger is not None else RequestLogger()
        #: Device authorisations in flight, keyed by their device auth id.
        #: Held in memory only; nothing about them is persisted.
        self._pending_device: dict[str, Any] = {}
        self._routes = HttpRouter()
        #: The rendered console snapshot, keyed by a digest of every input
        #: ``_render_state`` reads that can change while the process is up -
        #: see ``gateway.http.state_cache`` for how each input is detected
        #: changing.
        self._state_cache = StateCache(self, self._render_state)
        self._build_routes()

    # --- routing table ---------------------------------------------------

    def _build_routes(self) -> None:
        r = self._routes
        r.add(["GET"], "/version", self.handle_version)
        r.add(["GET"], "/health", self.handle_health)
        r.add(["GET"], "/metrics", self.handle_metrics)
        r.add(["GET"], "/favicon.ico", self.handle_favicon)

        r.add(["GET"], "/v1/models", self.handle_models)
        r.add(["GET"], "/v1/alias-catalog", self.handle_alias_catalog)
        r.add(["GET"], "/v1/systemone/models", self.handle_systemone_models)
        r.add(["POST"], "/v1/systemone", self.handle_systemone)
        r.add(["POST"], "/v1/chat/completions", self.handle_chat_completions)
        r.add(["POST"], "/v1/responses", self.handle_responses)
        # ANCHOR t_52fe959a "anthropic-messages-route": the Anthropic
        # Messages surface, added after the OpenAI inference routes.
        # Additive only - the routes above and below are not reordered.
        r.add(["POST"], "/v1/messages", self.handle_messages)

        for prefix in self.mounts:
            r.add(["GET"], prefix + "/", self.handle_console)
            r.add(["GET"], prefix + "/index.html", self.handle_console)
            r.add(["GET"], prefix + "/app.js", self.handle_static_js)
            r.add(["GET"], prefix + "/style.css", self.handle_static_css)
            r.add(["GET"], prefix + "/api/status", self.handle_public_status)
            r.add(["GET"], prefix + "/api/state", self.handle_state)
            # The leaderboard is a separate read-only endpoint on purpose:
            # its ranked rows and date x model matrix are a second O(rows)
            # pass, and folding it into /owner/api/state would put the
            # console poll's mandatory sub-2s bar (R56) back at risk. Same
            # listener, same mounts, same Host/Origin/forwarded checks as
            # every other owner route - no new exposure.
            r.add(["GET"], prefix + "/api/leaderboard", self.handle_leaderboard)
            r.add(["GET"], prefix + "/api/ledger/export", self.handle_ledger_export)
            r.add(["GET"], prefix + "/api/backup/export", self.handle_backup_export)
            r.add(["POST"], prefix + "/api/backup/import", self.handle_backup_import)
            r.add(["POST"], prefix + "/api/preview", self.handle_preview)

            r.add(["GET"], prefix + "/api/capabilities", self.handle_capability_read)
            r.add(["POST"], prefix + "/api/capabilities/preview", self.handle_capability_preview)
            r.add(["POST"], prefix + "/api/capabilities/run", self.handle_capability_run)
            r.add(["POST"], prefix + "/api/capabilities/decision", self.handle_capability_decision)
            r.add(["POST"], prefix + "/api/capabilities/probe/cancel", self.handle_capability_probe_cancel)
            r.add(["POST"], prefix + "/api/provider", self.handle_provider_action)
            r.add(["POST"], prefix + "/api/model", self.handle_model_action)
            r.add(["POST"], prefix + "/api/alias", self.handle_alias_action)
            r.add(["POST"], prefix + "/api/alias-group", self.handle_alias_group_action)
            r.add(["POST"], prefix + "/api/profile", self.handle_profile_action)
            r.add(["POST"], prefix + "/api/settings", self.handle_settings_action)

            # Owner authentication. The first three are reachable without a
            # session because they are how a session is obtained (first-run
            # setup, sign-in, and the pre-auth probe the console uses to
            # decide which of those two to render); everything else on this
            # mount requires a valid session.
            r.add(["GET"], prefix + "/api/auth/session", self.handle_auth_session)
            r.add(["POST"], prefix + "/api/auth/setup", self.handle_auth_setup)
            r.add(["POST"], prefix + "/api/auth/login", self.handle_auth_login)
            r.add(["POST"], prefix + "/api/auth/logout", self.handle_auth_logout)
            r.add(["POST"], prefix + "/api/auth/password", self.handle_auth_password)
            r.add(["POST"], prefix + "/api/auth/client-key", self.handle_auth_client_key)

            # Machine keys (MCP/automation). Both routes are human-session
            # only by construction: the list is owner console data and
            # create/rotate/revoke mint or destroy a credential, so a
            # machine key must never reach them (see _authorize).
            r.add(["GET"], prefix + "/api/machine-keys", self.handle_machine_keys_list)
            r.add(["POST"], prefix + "/api/machine-keys", self.handle_machine_keys_action)

        #: Owner paths that stay reachable without a session: the console
        #: shell and its assets (the login view is rendered by them) plus
        #: the three authentication endpoints above. Everything else under
        #: a mount is owner surface and is gated.
        self._public_owner_paths: frozenset[str] = frozenset(
            {
                path
                for prefix in self.mounts
                for path in (
                    prefix,
                    prefix + "/",
                    prefix + "/index.html",
                    prefix + "/app.js",
                    prefix + "/style.css",
                    prefix + "/api/auth/session",
                    prefix + "/api/auth/setup",
                    prefix + "/api/auth/login",
                )
            }
        )

    def handle(self, request: Request) -> Any:
        """Entry point. Applies the security policy, then dispatches."""
        try:
            check_forwarded_headers(request)
            check_host(request, self.policy)
            check_origin(request, self.policy)

        except SecurityError as exc:
            return exc.to_response()

        started = time.monotonic()
        route, params, path_matched = self._routes.match(request.method, request.path)
        if route is None:
            if path_matched:
                allowed = self._routes.allowed_methods(request.path)
                result = json_response(
                    {"error": {"message": "method not allowed", "type": "method_not_allowed", "allowed": allowed}},
                    405,
                    [("Allow", ", ".join(allowed))],
                )
            else:
                result = json_response({"error": {"message": "not found", "type": "not_found"}}, 404)
            self._log_request(request, result, started)
            return result

        request.params = params
        request.context["surface"] = SURFACE_OWNER

        # Access control runs after routing and before any handler: a
        # refused request never reaches the code it would have exercised,
        # so a streaming endpoint cannot emit a frame before it is
        # authenticated and a mutation cannot half-apply.
        try:
            denied = self._authorize(request)
        except OwnerAuthError as exc:  # pragma: no cover - defensive
            denied = self._owner_auth_error(exc)
        if denied is not None:
            self._log_request(request, denied, started)
            return denied

        # CSRF applies to the cookie-authenticated browser surfaces only.
        # The API authenticates with bearer tokens, which a browser never
        # sends ambiently, so it is not CSRF-relevant there.
        try:
            result = route.handler(request)
        except SecurityError as exc:
            result = exc.to_response()
            self._log_request(request, result, started)
            return result
        except OwnerAuthError as exc:
            result = self._owner_auth_error(exc)
            self._log_request(request, result, started)
            return result
        except RoutingError as exc:
            headers = ([("Retry-After", str(exc.retry_after_seconds))]
                       if exc.status == 503 and exc.retry_after_seconds is not None else [])
            result = json_response(exc.to_dict(), exc.status, headers)
            self._log_request(request, result, started)
            return result
        except ConversionError as exc:
            result = json_response(
                {"error": {"message": exc.message, "type": "conversion_refused", "capability": exc.capability}},
                422,
            )
            self._log_request(request, result, started)
            return result
        except AdapterError as exc:
            result = json_response({"error": {"message": exc.message, "type": exc.error_class}}, 502)
            self._log_request(request, result, started)
            return result
        except Exception:  # pragma: no cover - defensive
            result = json_response({"error": {"message": "internal error", "type": "gateway_error"}}, 500)
            self._log_request(request, result, started)
            return result
        self._log_request(request, result, started)
        return result

    def _log_request(self, request: Request, result: Any, started: float) -> None:
        """Record one request outcome.

        The path is logged without its query string, and the body is never
        logged, so a prompt, a response or a credential cannot reach the
        log.
        """
        status = getattr(result, "status", 0)
        error_class = None
        alias = provider = model = None
        action = request.context.get("action")
        body = getattr(result, "body", None)
        if isinstance(body, (bytes, bytearray)):
            try:
                payload = json.loads(bytes(body).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                payload = None
            if isinstance(payload, dict):
                error = payload.get("error")
                if isinstance(error, dict) and isinstance(error.get("type"), str):
                    error_class = error["type"]
        attribution = request.context.get("attribution")
        if isinstance(attribution, dict):
            alias = attribution.get("requested_alias")
            provider = attribution.get("serving_provider")
            model = attribution.get("serving_model")
        machine_key_id: str | None = None
        if request.context.get("machine_key_id") and request.method in MUTATING_METHODS:
            # Audit rule for machine keys: every mutation - allowed or
            # refused - is logged with the display id of the key that
            # attempted it and the action. The handler names the action
            # when it has one; otherwise method + path identifies it.
            # The key value is in no field of this record, so no log line
            # can carry it.
            machine_key_id = str(request.context["machine_key_id"])
            action = action or f"{request.method} {request.path}"
        # The *target* of a machine-key lifecycle action (create, rotate,
        # revoke): the display id the store acted on, set by the handler
        # after the store call. It keeps a revocation attributable once
        # the record is deleted. It is a display id and nothing else -
        # the key value never enters this context.
        target = request.context.get("machine_key_target")
        machine_key_target = target if isinstance(target, str) and target else None
        self.logger.log(
            method=request.method,
            path=request.path,
            status=status,
            duration_ms=int((time.monotonic() - started) * 1000),
            alias=alias,
            provider=provider,
            model=model,
            error_class=error_class,
            streamed=bool(request.context.get("streamed")),
            action=action,
            machine_key_id=machine_key_id,
            machine_key_target=machine_key_target,
        )

    # --- surface helpers -------------------------------------------------

    def _surface(self, request: Request) -> str:
        return request.context.get("surface", SURFACE_OWNER)



    def _can_mutate(self, request: Request) -> bool:
        """Whether this request may change configuration.

        Owner mutations require a signed-in owner session, or a
        **write-scoped** machine key on the machine-writable surface
        (``MACHINE_KEY_WRITE_PATHS``). A read-scoped key never reaches
        this: ``_authorize`` refuses it with ``scope_required`` before
        the handler runs.
        """
        if request.context.get("owner_authenticated"):
            return True
        return bool(request.context.get("machine_key_authenticated")) and (
            request.context.get("machine_key_scope") == "write"
        )

    def _can_enroll_credentials(self, request: Request) -> bool:
        """Enrolling a provider credential is an owner action, so it needs
        the same signed-in session as any other mutation.

        Deliberately *not* widened to machine keys: a machine key may
        change configuration but may never mint or store a credential.
        It also keeps this verdict true only for sessions, which is what
        separates a machine key's ``/owner/api/state`` cache entry from a
        session's (see ``gateway.http.state_cache``: the token includes
        both verdicts).
        """
        return bool(request.context.get("owner_authenticated"))

    def _require_mutation(self, request: Request) -> Any:
        if not self._can_mutate(request):
            return json_response(
                {"error": {"message": "owner sign-in required", "type": "authentication_required"}},
                401,
                [("Cache-Control", "no-store")],
            )
        return None

    # --- access control -------------------------------------------------

    def _session_token(self, request: Request) -> str | None:
        """Read the owner session cookie. Nothing else authenticates an owner."""
        raw = request.headers.get("cookie")
        if not raw:
            return None
        for part in raw.split(";"):
            name, _, value = part.strip().partition("=")
            if name == COOKIE_NAME:
                return value or None
        return None

    def _session_cookie(self, token: str, max_age: int) -> str:
        attributes = [
            f"{COOKIE_NAME}={token}",
            "Path=/",
            f"Max-Age={max_age}",
            "HttpOnly",
            "SameSite=Strict",
        ]
        if self.cookie_secure:
            # TLS-aware: only a deployment served over https (an https
            # canonical origin, or GATEWAY_COOKIE_SECURE=1 in front of a
            # TLS terminator) marks the cookie Secure, because a browser
            # would otherwise drop it on the plain-HTTP LAN listener.
            attributes.append("Secure")
        return "; ".join(attributes)

    def _owner_auth_error(self, exc: OwnerAuthError) -> Response:
        headers: list[tuple[str, str]] = [("Cache-Control", "no-store")]
        if exc.retry_after is not None:
            headers.append(("Retry-After", str(int(math.ceil(exc.retry_after)))))
        return json_response(exc.payload(), exc.status, headers)

    def _authorize(self, request: Request) -> Response | None:
        """The access boundaries, kept apart on purpose.

        ``/v1/...`` is the **model client** boundary: bearer key only when
        the owner has configured one (off by default), and never a machine
        key. Anything under a console mount is the **owner** boundary:
        a request carrying ``Authorization: Bearer`` is a machine request
        and goes to :meth:`_authorize_machine_key` (which decides by
        surface and scope, and never falls back to the cookie); every
        other request needs the session cookie, always - or nothing at
        all for the pre-auth console paths.
        """
        path = request.path
        if path == "/v1" or path.startswith("/v1/"):
            return self._require_client_key(request)
        for prefix in self.mounts:
            if path == prefix or path.startswith(prefix + "/"):
                suffix = path[len(prefix):] or "/"
                bearer = bearer_authorization(request)
                if bearer is not None:
                    return self._authorize_machine_key(request, suffix, bearer)
                if path in self._public_owner_paths:
                    return None
                return self._require_owner_session(request)
        return None

    def _machine_denied(self, code: str, message: str) -> Response:
        return json_response(
            {"error": {"message": message, "type": code}},
            403,
            [("Cache-Control", "no-store")],
        )

    def _authorize_machine_key(self, request: Request, suffix: str, bearer: str) -> Response | None:
        """Authorise a machine-key request: credential, then shape, then scope.

        Order matters. The key is resolved **first**, so every refusal
        below - a session cookie, an endpoint outside the machine surface,
        a read scope on a mutation - is attributable to the key that
        attempted it in the access log, and no refusal path can fall
        through to a session check. Nothing here is cached: the store
        re-reads its file when it changed, so a revoke or a rotate is in
        effect on the very next request.
        """
        record = self.machine_keys.authenticate(bearer)
        if record is None:
            return json_response(
                {
                    "error": {
                        "message": "a valid machine key is required for this request",
                        "type": "authentication_required",
                    }
                },
                401,
                [
                    ("Cache-Control", "no-store"),
                    ("WWW-Authenticate", 'Bearer realm="personal-llm-gateway", error="invalid_token"'),
                ],
            )
        request.context["machine_key_authenticated"] = True
        request.context["machine_key_id"] = str(record["display_id"])
        request.context["machine_key_scope"] = str(record["scope"])
        # A machine principal never rides a browser session: a request
        # that carries both is refused rather than "resolved" either way.
        if request.headers.get("cookie"):
            return self._machine_denied(
                "cookie_rejected",
                "machine-key requests must not carry a session cookie",
            )
        mutating = request.method in MUTATING_METHODS
        allowed = MACHINE_KEY_WRITE_PATHS if mutating else MACHINE_KEY_READ_PATHS
        if suffix not in allowed:
            return self._machine_denied(
                "machine_key_forbidden",
                "machine keys cannot reach this endpoint; sign in to the owner console instead",
            )
        if mutating and str(record["scope"]) != "write":
            return self._machine_denied(
                "scope_required",
                "this machine key is read-only; a write-scoped machine key is required for this action",
            )
        return None

    def _require_signed_in_owner(self, request: Request) -> Response | None:
        """Owner-console-only endpoints: a machine key must never satisfy this.

        The route-level authoriser already refuses a machine key on these
        paths; this is the second, handler-level gate so a future route
        wired to the wrong handler still cannot serve credential or
        console operations to an automation client.
        """
        if request.context.get("owner_authenticated"):
            return None
        return json_response(
            {"error": {"message": "owner sign-in required", "type": "authentication_required"}},
            401,
            [("Cache-Control", "no-store")],
        )

    def _require_owner_session(self, request: Request) -> Response | None:
        token = self._session_token(request)
        info = self.owner_auth.session_info(token)
        if info is None:
            return json_response(
                {
                    "error": {
                        "message": "owner sign-in required",
                        "type": "authentication_required",
                        "setup_required": self.owner_auth.setup_required,
                    }
                },
                401,
                [("Cache-Control", "no-store")],
            )
        request.context["owner_authenticated"] = True
        request.context["owner_username"] = info["username"]
        request.context["owner_session"] = token
        request.context["owner_expires_at"] = info["expires_at"]
        return None

    def _require_client_key(self, request: Request) -> Response | None:
        """Bearer check for the model-facing inventory.

        Refusal happens before the handler, so every endpoint - including
        the streaming ones - answers with the same JSON error and never
        opens a stream, emits a frame, or calls a provider.
        """
        authorization = request.headers.get("authorization")
        machine = bearer_authorization(request)
        if machine is not None and self.machine_keys.authenticate(machine, touch=False) is not None:
            # A machine key is an owner-API credential and never a model
            # key - not even on the keyless default, where anything else
            # is accepted. Two credentials, two surfaces, no crossover.
            return json_response(
                {
                    "error": {
                        "message": "machine keys authorize the owner API only; send them to /owner, not /v1",
                        "type": "invalid_api_key",
                    }
                },
                401,
                [
                    ("WWW-Authenticate", 'Bearer realm="personal-llm-gateway", error="invalid_token"'),
                    ("Cache-Control", "no-store"),
                ],
            )
        if not self.owner_auth.client_key_enabled:
            return None
        if self.owner_auth.check_client_key(authorization):
            return None
        return json_response(
            {
                "error": {
                    "message": (
                        "a client API key is configured for this gateway; "
                        "send Authorization: Bearer <key> on model requests"
                    ),
                    "type": "invalid_api_key",
                }
            },
            401,
            [
                ("WWW-Authenticate", 'Bearer realm="personal-llm-gateway", error="invalid_token"'),
                ("Cache-Control", "no-store"),
            ],
        )

    # --- owner authentication endpoints ---------------------------------

    def handle_auth_session(self, request: Request) -> Response:
        """Pre-auth probe: is there a session, and is first-run setup due?

        Deliberately carries nothing but those facts before sign-in; the
        client-key state and the username only appear with a session.
        """
        token = self._session_token(request)
        info = self.owner_auth.session_info(token)
        payload: dict[str, Any] = {
            "authenticated": info is not None,
            "setup_required": self.owner_auth.setup_required,
            "username": None,
        }
        if info is not None:
            payload["username"] = info.get("username")
            payload["expires_at"] = info.get("expires_at")
            payload["client_key"] = self.owner_auth.client_key_status()
        return json_response(payload, 200, [("Cache-Control", "no-store")])

    def handle_auth_setup(self, request: Request) -> Response:
        """First-run account creation, gated by the one-time setup code."""
        try:
            payload = request.json() or {}
        except ValueError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        token = self.owner_auth.complete_setup(
            payload.get("username"),
            payload.get("password"),
            payload.get("setup_code"),
            client=request.client_address[0] if request.client_address else "",
        )
        return json_response(
            {"ok": True, "username": payload.get("username"), "setup_required": False},
            200,
            [
                ("Set-Cookie", self._session_cookie(token, SESSION_TTL_SECONDS)),
                ("Cache-Control", "no-store"),
            ],
        )

    def handle_auth_login(self, request: Request) -> Response:
        try:
            payload = request.json() or {}
        except ValueError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        token = self.owner_auth.login(
            payload.get("username"),
            payload.get("password"),
            client=request.client_address[0] if request.client_address else "",
        )
        return json_response(
            {"ok": True, "username": payload.get("username")},
            200,
            [
                ("Set-Cookie", self._session_cookie(token, SESSION_TTL_SECONDS)),
                ("Cache-Control", "no-store"),
            ],
        )

    def handle_auth_logout(self, request: Request) -> Response:
        token = self._session_token(request)
        self.owner_auth.revoke_session(token)
        # Max-Age=0 makes the browser drop the cookie immediately; the
        # server-side revocation is what actually ends the session.
        return json_response(
            {"ok": True},
            200,
            [
                ("Set-Cookie", self._session_cookie("", 0)),
                ("Cache-Control", "no-store"),
            ],
        )

    def handle_auth_password(self, request: Request) -> Response:
        try:
            payload = request.json() or {}
        except ValueError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        result = self.owner_auth.change_password(
            request.context.get("owner_session"),
            payload.get("current_password"),
            payload.get("new_password"),
        )
        return json_response({"ok": True, **result}, 200, [("Cache-Control", "no-store")])

    def handle_auth_client_key(self, request: Request) -> Response:
        """Set, rotate or clear the optional model client key.

        The response never contains the key - only a random display id and
        the transition - and it states the client-visible impact in words,
        because a key change silently breaking agents is exactly the
        failure this endpoint exists to make loud.
        """
        try:
            payload = request.json() or {}
        except ValueError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        action = str(payload.get("action") or "")
        if action == "set":
            status = self.owner_auth.set_client_key(payload.get("key"))
            impact = (
                "Model API access now requires Authorization: Bearer <key> on every /v1 request. "
                "Clients without the header are refused until you update them; the gateway does "
                "not modify any client configuration for you."
            )
        elif action == "rotate":
            status = self.owner_auth.rotate_client_key(payload.get("key"))
            grace = status.get("previous_key_valid_for_seconds")
            impact = (
                "The new key is required immediately; the previous key keeps working for "
                f"{grace} more seconds so clients can be updated without downtime. The gateway "
                "does not modify any client configuration for you."
            )
        elif action == "clear":
            status = self.owner_auth.clear_client_key()
            impact = (
                "Model API access is keyless again: every client works without an "
                "Authorization header, exactly as before a key was set."
            )
        else:
            return json_response(
                {"error": {"message": f"unknown action '{action}'", "type": "unknown_action"}}, 400
            )
        return json_response(
            {"ok": True, "action": action, "client_key": status, "impact": impact},
            200,
            [("Cache-Control", "no-store")],
        )

    # --- machine keys (MCP / automation) --------------------------------

    def handle_machine_keys_list(self, request: Request) -> Response:
        """The console's machine-key list: display id, name, scope, last used.

        Exactly those four fields - the store holds a hash and nothing
        that could be handed back, and no other record (name aside) is
        part of the contract. Session-only: a machine key cannot
        enumerate itself (see the route table).
        """
        denied = self._require_signed_in_owner(request)
        if denied is not None:
            return denied
        rows = self.machine_keys.list()
        return json_response(
            {"machine_keys": rows, "count": len(rows)},
            200,
            [("Cache-Control", "no-store")],
        )

    @staticmethod
    def _record_machine_key_target(request: Request, record: Any) -> None:
        """Name the key a lifecycle action acted on, for the access log.

        The display id only, taken from the store's own return value -
        so the logged id is one the store actually acted on, never a
        value invented by the caller. Called *after* the store call on
        purpose: revocation deletes the record outright, and this is
        what keeps a revoked key attributable afterwards. The plaintext
        key travels in the same response dict and is never copied into
        the request context, so no log line can carry it.
        """
        target = record.get("display_id") if isinstance(record, dict) else None
        if isinstance(target, str) and target:
            request.context["machine_key_target"] = target

    def handle_machine_keys_action(self, request: Request) -> Response:
        """Create, rotate or revoke a machine key. Human session only.

        The plaintext key is in this response and nowhere else: it is
        never stored, never logged, and no later request can read it
        back. Rotation replaces the secret immediately (same display id,
        previous value dead on the next request) and revocation deletes
        the record outright, so nothing can keep honouring it.
        """
        denied = self._require_signed_in_owner(request)
        if denied is not None:
            return denied
        try:
            payload = request.json() or {}
        except ValueError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        action = str(payload.get("action") or "")
        # The access log names which machine-key action ran, keyed by the
        # console operator's session - never by a key, which is not here.
        request.context["action"] = f"machine_key.{action}" if action else "machine_key"
        if action == "create":
            created = self.machine_keys.create(payload.get("name"), payload.get("scope"))
            self._record_machine_key_target(request, created)
            return json_response(
                {
                    "ok": True,
                    "action": "create",
                    "shown_once": True,
                    "machine_key": created,
                    "notice": (
                        "Copy this key now: it is shown exactly once and cannot be "
                        "retrieved again. Store it wherever the automation runs."
                    ),
                },
                200,
                [("Cache-Control", "no-store")],
            )
        if action == "rotate":
            rotated = self.machine_keys.rotate(payload.get("display_id"))
            self._record_machine_key_target(request, rotated)
            return json_response(
                {
                    "ok": True,
                    "action": "rotate",
                    "shown_once": True,
                    "machine_key": rotated,
                    "notice": (
                        "Copy this key now: it is shown exactly once. The previous "
                        "value stopped working the moment it was issued."
                    ),
                },
                200,
                [("Cache-Control", "no-store")],
            )
        if action == "revoke":
            result = self.machine_keys.revoke(payload.get("display_id"))
            self._record_machine_key_target(request, result)
            return json_response(
                {"ok": True, "action": "revoke", **result},
                200,
                [("Cache-Control", "no-store")],
            )
        return json_response(
            {"error": {"message": f"unknown action '{action}'", "type": "unknown_action"}}, 400
        )

    # --- metadata --------------------------------------------------------

    def handle_version(self, request: Request) -> Response:
        identity = self._release_identity() if self._release_identity else {
            "service": "personal-llm-gateway",
            "source_revision": "unknown",
            "source_tree": "unknown",
            "image_revision": "unknown",
            # The contract version is code, not build provenance: it is
            # known even when the git context is not, and it must be
            # published on every path out of /version. Same constant, no
            # second literal.
            "mcp_contract_version": MCP_CONTRACT_VERSION,
        }
        return json_response(identity)

    def handle_health(self, request: Request) -> Response:
        return json_response({"status": "ok"})

    def handle_metrics(self, request: Request) -> Response:
        cooldowns = self.router.circuits.snapshot()
        body = render_metrics(self.ledger, cooldowns).encode("utf-8")
        return Response(200, [("Content-Type", "text/plain; version=0.0.4; charset=utf-8"), ("Cache-Control", "no-store")], body)

    def handle_favicon(self, request: Request) -> Response:
        """No icon, but no 404 noise in the browser console either."""
        return Response(204, [("Cache-Control", "max-age=86400")], b"")

    # --- API surface -----------------------------------------------------

    def _bearer_token(self, request: Request) -> str | None:
        header = request.headers.get("authorization")
        if not header:
            return None
        parts = header.split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            return parts[1].strip()
        return None

    def _resolve_profile(self, request: Request):
        profile, reason = self.profile_auth.resolve(self._bearer_token(request))
        if profile is None:
            raise RoutingError(
                "this request is not authorised: " + reason,
                code="unauthorised",
                status=401,
            )
        return profile

    def handle_models(self, request: Request) -> Response:
        profile = self._resolve_profile(request)
        config = self.config.snapshot()
        published = sorted(config.published_aliases(), key=lambda a: a.name)
        allowed = [alias for alias in published if profile_allows(profile, alias.name)]
        created = int(time.time())
        data = [
            {"id": alias.name, "object": "model", "created": created, "owned_by": "personal-llm-gateway"}
            for alias in allowed
        ]
        payload: dict[str, Any] = {"object": "list", "data": data}
        if not data:
            payload["notice"] = (
                "No models are published yet. Connect a provider, enable a model, and publish an "
                "alias in Models & Routes. Until then this catalogue is empty by design."
            )
        return json_response(payload)

    def handle_alias_catalog(self, request: Request) -> Response:
        """Published alias metadata for clients that need capabilities.

        Unlike owner state, this contains no provider inventory, ledger,
        credentials or unpublished routes. It makes no upstream calls.
        """
        profile = self._resolve_profile(request)
        config = self.config.snapshot()
        aliases = []
        for alias in sorted(config.published_aliases(), key=lambda item: item.name):
            if not profile_allows(profile, alias.name):
                continue
            routes = []
            for route in alias.all_routes():
                provider = config.providers.get(route.provider)
                entry = provider.models.get(route.model) if provider else None
                routes.append((route.label(), entry.context_limit if entry else None))
            primary = alias.primary
            provider = config.providers.get(primary.provider) if primary else None
            model = provider.models.get(primary.model) if provider and primary else None
            aliases.append({
                "id": alias.name,
                "context": alias_context_limit(routes),
                "primary_reasoning_efforts": list(model.reasoning_efforts) if model else [],
                "supports_function_tools": all(
                    config.providers.get(route.provider) is not None
                    and (entry := config.providers[route.provider].models.get(route.model)) is not None
                    and entry.supports_function_tools
                    for route in alias.all_routes()
                ),
                # An alias is image-capable when **any** route that may serve
                # it accepts image input. This is deliberately not the
                # all-routes rule the tools flag uses: the router enforces
                # image capability per request, so an image is routed to a
                # route that can see it and a text-only route is excluded for
                # that request (recording why), never silently served the
                # image. If no image-capable route remains eligible the
                # request fails closed at the gateway.
                "supports_image_input": any(
                    config.providers.get(route.provider) is not None
                    and (entry := config.providers[route.provider].models.get(route.model)) is not None
                    and entry.supports_image_input
                    for route in alias.all_routes()
                ),
            })
        return json_response({"object": "list", "data": aliases})

    def handle_systemone_models(self, request: Request) -> Response:
        """Return explicitly refreshed Jev models, without an upstream read."""
        self._resolve_profile(request)
        provider = self.config.snapshot().providers.get("opencode_console")
        models = [] if provider is None else [
            {"name": entry.model_id, "description": entry.notes or "", "release_date": "unknown"}
            for entry in sorted(provider.enabled_models(), key=lambda item: item.model_id)
        ]
        return json_response({"models": models})

    def handle_systemone(self, request: Request) -> Response:
        """Pass a validated native Jev System One request through unchanged.

        This is deliberately separate from aliases and the chat router: callers
        knowingly supply the state and questions to OpenCode Console, and no completion
        request is inspected or forwarded here.
        """
        self._resolve_profile(request)
        try:
            payload = request.json()
        except ValueError:
            raise RoutingError("request body is not valid JSON", code="malformed_request", status=400)
        error = _validate_systemone_request(payload)
        if error:
            raise RoutingError(error, code="malformed_request", status=400)
        assert isinstance(payload, dict)
        adapter = self.registry.get("opencode_console")
        if adapter is None or not hasattr(adapter, "invoke_systemone"):
            raise RoutingError("OpenCode Console Jev is not configured", code="provider_unavailable", status=503)
        config = self.config.snapshot()
        provider = config.providers.get("opencode_console")
        model = payload["model"]
        if provider is None or model not in provider.models or not provider.models[model].enabled:
            raise RoutingError(f"System One model '{model}' is not enabled by this gateway", code="unknown_model", status=404)
        request.context["action"] = "systemone"
        request.context["attribution"] = {
            "requested_alias": None, "serving_provider": "opencode_console", "serving_model": model,
        }
        return Response(200, [("Content-Type", "application/json")], adapter.invoke_systemone(payload))

    def handle_preview(self, request: Request) -> Response:
        """Show the primary upstream request without invoking a provider."""
        from gateway.preview import preview_request

        profile = self._resolve_profile(request)
        try:
            payload = request.json()
        except ValueError:
            raise RoutingError("request body is not valid JSON", code="malformed_request", status=400)
        if not isinstance(payload, dict):
            raise RoutingError("request body is not valid JSON", code="malformed_request", status=400)
        alias_name = payload.get("model")
        if not isinstance(alias_name, str) or not alias_name:
            raise RoutingError("request is missing a model", code="malformed_request", status=400)
        alias = self.config.snapshot().aliases.get(alias_name)
        if alias is None or not alias.enabled:
            raise RoutingError(
                f"model '{alias_name}' is not published by this gateway",
                code="unknown_model", status=404,
            )
        if not profile_allows(profile, alias.name):
            raise RoutingError(
                f"this client profile may not use model '{alias_name}'",
                code="model_not_allowed", status=403,
            )
        preview = preview_request(
            self.router, alias_name, payload,
            source_dialect=DIALECT_OPENAI_CHAT,
            profile_id=profile.profile_id,
            client_ip=request.client_address[0],
            session_hint=self._session_hint(request),
        )
        return json_response(preview, headers=[("Cache-Control", "no-store")])

    def handle_chat_completions(self, request: Request) -> Any:
        return self._handle_inference(request, DIALECT_OPENAI_CHAT)

    def handle_responses(self, request: Request) -> Any:
        return self._handle_inference(request, DIALECT_OPENAI_RESPONSES)

    def handle_messages(self, request: Request) -> Any:
        """Serve the Anthropic Messages surface, ``POST /v1/messages``.

        The Anthropic contract (parse and validate) is exclusive to
        :mod:`gateway.http.messages`; the request then runs through the
        same inference flow as the OpenAI surfaces with the Anthropic
        Messages dialect as the caller's, so route selection and the
        bridge render Anthropic-shaped buffered and streamed responses.
        """
        return messages_surface.handle_messages(self, request)

    def _session_hint(self, request: Request) -> str:
        """The client-supplied conversation name, if it sent one.

        This is only a *hint*. The gateway derives the real upstream
        identifier itself, in the router, so that every path that reaches a
        provider carries one.
        """
        for header in ("x-opencode-session", "x-session-id"):
            candidate = request.headers.get(header)
            if candidate:
                return candidate
        return ""

    def _handle_inference(self, request: Request, dialect: str) -> Any:
        profile = self._resolve_profile(request)
        try:
            payload = request.json()
        except ValueError:
            raise RoutingError("request body is not valid JSON", code="malformed_request", status=400)
        if not isinstance(payload, dict):
            raise RoutingError("request body is not valid JSON", code="malformed_request", status=400)

        model = payload.get("model")
        if not isinstance(model, str) or not model:
            raise RoutingError("request is missing a model", code="malformed_request", status=400)

        config = self.config.snapshot()
        alias = config.aliases.get(model)
        if alias is None or not alias.enabled:
            raise RoutingError(
                f"model '{model}' is not published by this gateway",
                code="unknown_model",
                status=404,
            )
        if not profile_allows(profile, alias.name):
            raise RoutingError(
                f"this client profile may not use model '{model}'",
                code="model_not_allowed",
                status=403,
            )

        if self.ledger and alias.budget is not None:
            refusal = over_budget(self.ledger.recent(limit=10_000), alias.name, alias.budget, history_complete=self.ledger.evicted == 0)
            if refusal is not None:
                raise RoutingError(
                    f"monthly budget ${alias.budget:.2f} reached; recorded spend is ${refusal['spend_usd']:.2f}; resets at {refusal['reset_at']} UTC",
                    code="budget_exceeded",
                    status=429,
                )

        stream = bool(payload.get("stream"))
        request_id = new_request_id()
        session_hint = self._session_hint(request)
        # Read and strip the gateway-owned phase hint before the payload is
        # converted or forwarded: no upstream provider ever sees it.
        caller_phase = _extract_caller_phase(payload)

        routed = self.router.invoke(
            alias.name,
            payload,
            source_dialect=dialect,
            stream=stream,
            request_id=request_id,
            session_hint=session_hint,
            profile_id=profile.profile_id,
            client_ip=request.client_address[0],
            caller_phase=caller_phase,
        )

        # The attribution is recorded on the request so the access log can
        # name the alias and the provider that served it.
        request.context["attribution"] = routed.attribution.to_dict()
        request.context["streamed"] = stream
        request.context["request_id"] = request_id
        request.context["serving_model"] = routed.attribution.serving_model

        if not stream:
            upstream_dialect = routed.request.dialect or dialect
            if routed.response.stream is not None:
                # The upstream is streaming only. Consume it and present the
                # result as the single object the client asked for.
                body = json.dumps(
                    aggregate_stream_any(list(routed.response.stream), upstream_dialect),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            else:
                body = routed.response.body or b""
            # A converted request must receive a converted response. The
            # upstream dialect and the caller's dialect are the same for a
            # passthrough, and different when the bridge converted the
            # request.
            if upstream_dialect != dialect and body:
                body = _convert_buffered(body, upstream_dialect, dialect)
            if self.ledger:
                self.ledger.amend(request_id, token_counters=response_token_counters(body, dialect))
            return Response(
                routed.response.status,
                [("Content-Type", routed.response.content_type or "application/json")],
                body,
            )

        upstream_dialect = routed.request.dialect or dialect
        upstream_stream = routed.response.stream or iter(())

        def frames():
            observed: dict[str, int] = {}
            converted = upstream_stream if upstream_dialect == dialect else convert_stream_bytes(
                upstream_stream, upstream_dialect, dialect, routed.request.model
            )
            for frame in converted:
                observed.update(stream_token_counters([frame], dialect) or {})
                yield frame
            if dialect != upstream_dialect and dialect == DIALECT_OPENAI_CHAT:
                yield sse_done()
            if self.ledger:
                self.ledger.amend(request_id, token_counters=observed or None)

        return StreamResponse(
            frames(),
            status=routed.response.status,
            headers=[
                ("Content-Type", "text/event-stream; charset=utf-8"),
                ("Cache-Control", "no-cache"),
                ("X-Accel-Buffering", "no"),
            ],
        )

    # --- console ---------------------------------------------------------

    def handle_console(self, request: Request) -> Response:
        html = _read_static("console.html")
        if html is None:
            return json_response({"error": {"message": "console assets are missing", "type": "asset_error"}}, 500)
        # The console is served from two mounts; tell the page which one it
        # is talking to so its API calls go to the right listener.
        base = self._mount_for(request.path)
        html = html.replace(b"__GATEWAY_BASE__", base.encode("utf-8"))
        return Response(200, [("Content-Type", "text/html; charset=utf-8"), ("Cache-Control", "no-store")], html)

    def _mount_for(self, path: str) -> str:
        for prefix in self.mounts:
            if path.startswith(prefix):
                return prefix
        return self.mounts[0] if self.mounts else ""

    def handle_static_js(self, request: Request) -> Response:
        body = _read_static("console.js")
        if body is None:
            return json_response({"error": {"message": "console assets are missing", "type": "asset_error"}}, 500)
        return Response(200, [("Content-Type", "text/javascript; charset=utf-8"), ("Cache-Control", "no-store")], body)

    def handle_static_css(self, request: Request) -> Response:
        body = _read_static("console.css")
        if body is None:
            return json_response({"error": {"message": "console assets are missing", "type": "asset_error"}}, 500)
        return Response(200, [("Content-Type", "text/css; charset=utf-8"), ("Cache-Control", "no-store")], body)


    # --- state -----------------------------------------------------------

    def handle_ledger_export(self, request: Request) -> Response:
        """Export recent ledger metadata; this read-only route records no event."""
        raw_limit = request.query_one("limit")
        try:
            limit = int(raw_limit) if raw_limit is not None else DEFAULT_EXPORT_ROWS
        except ValueError:
            return json_response({"error": {"message": "limit must be an integer", "type": "invalid_export_limit"}}, 400)
        if limit < 1 or limit > MAX_EXPORT_ROWS:
            return json_response({"error": {"message": f"limit must be between 1 and {MAX_EXPORT_ROWS}", "type": "invalid_export_limit"}}, 400)
        events = self.ledger.recent(limit=limit) if self.ledger is not None else []
        # Validate format even when the ledger is unavailable.
        format_name = request.query_one("format", "csv")
        if format_name not in ("csv", "json"):
            return json_response({"error": {"message": "format must be csv or json", "type": "invalid_export_format"}}, 400)
        records = export_records(events)
        body = render_csv(records) if format_name == "csv" else render_json(records)
        content_type = "text/csv; charset=utf-8" if format_name == "csv" else "application/json; charset=utf-8"
        filename = "ledger.csv" if format_name == "csv" else "ledger.json"
        return Response(200, [("Content-Type", content_type), ("Content-Disposition", f'attachment; filename="{filename}"'), ("Cache-Control", "no-store")], body)

    def handle_public_status(self, request: Request) -> Response:
        """Safe status: no secrets, no disclosure class.

        It is served behind the owner session like every other ``/owner``
        endpoint (the pre-auth console only needs
        ``/owner/api/auth/session``), and it still deliberately excludes
        profile records, routing detail, the request ledger and the
        credential store state.
        """
        config = self.config.snapshot()
        providers = [
            {
                "provider_id": provider.provider_id,
                "state": provider.state,
                "enabled_models": sorted(
                    entry.model_id for entry in provider.enabled_models()
                ),
            }
            for provider in sorted(config.providers.values(), key=lambda p: p.provider_id)
        ]
        published = sorted(config.published_aliases(), key=lambda a: a.name)
        return json_response(
            {
                "service": "personal-llm-gateway",
                "initialized": config.initialized,
                "access_mode": config.access_mode,
                "access_mode_description": access_mode_description(config.access_mode),
                "providers": providers,
                "published_models": [alias.name for alias in published],
                "release": self._release_identity() if self._release_identity else {},
                "checklist": self._checklist(config),
            }
        )

    def handle_leaderboard(self, request: Request) -> Response:
        """The private model leaderboard: ranked models + date x model matrix.

        Deliberately a separate endpoint from ``/owner/api/state`` (see the
        route table), so the console poll keeps its sub-2s bar. The rollup is
        the in-memory one the ledger already maintains - this path never opens
        the ledger file - and the published-price catalogue is read exactly
        once here, then shared by every model priced below (the R56 rule: one
        materialisation per request, not per consumer). The payload holds
        aggregates only: no request identifier, prompt, response or
        credential can reach it.
        """
        days, requested_days = analytics_days(request)
        payload = build_leaderboard(
            self.ledger.rollup() if self.ledger else None,
            days=days,
            prices=published_prices(),
        )
        if requested_days is not None:
            payload["requested_days"] = requested_days
        return json_response(payload)

    def handle_state(self, request: Request) -> Response:
        """The console snapshot, served from the token-keyed cache.

        A read whose inputs all match the cached render returns those exact
        bytes; any change - a config write, a credential enrolment, a ledger
        record or amendment, a circuit transition (including an expiry), a
        limit observation - changes the token and this read re-renders
        synchronously, so a mutation is visible on the immediately following
        read. Wall-clock drift (usage windows ageing out, budget month
        rollover) is bounded two ways: a body older than ``refresh_after``
        is re-rendered in the background after it is served, and a body
        older than ``hard_max`` is never served at all - that read
        re-renders synchronously. Neither can put the render back on the
        path of a steady reader. See ``gateway.http.state_cache``.
        """
        return self._state_cache.serve(request)

    def _render_state(self, request: Request) -> Response:
        config = self.config.snapshot()
        may_mutate = self._can_mutate(request)

        # One request, one snapshot. The bounded ledger ring and the
        # published-price catalogue are each materialised exactly once here
        # and shared by every consumer below (per-alias budget, the three
        # statistics windows, provider health, usage costs, ledger
        # annotations). Re-pulling either per alias or per window is what
        # took this path from 0.3s to 46.9s in production (wave-2B rollback
        # 2026-10-29): ~20 ring scans and, far worse, >100k catalogue file
        # reads per console poll. No consumer below may re-pull the ledger.
        ledger_snapshot = self.ledger.recent(limit=10_000) if self.ledger else []
        ledger_history_complete = bool(self.ledger and self.ledger.evicted == 0)
        ledger_evicted = self.ledger.evicted if self.ledger else 0
        price_snapshot = published_prices()

        providers = []
        for provider_id in sorted(config.providers):
            provider = config.providers[provider_id]
            models = []
            for model_id in sorted(provider.models):
                entry = provider.models[model_id]
                models.append(
                    {
                        "model_id": entry.model_id,
                        "enabled": entry.enabled,
                        "dialect": entry.dialect,
                        "source": entry.source,
                        "observed_at": entry.observed_at,
                        "retention_class": entry.retention_class,
                        "context_window": entry.context_window,
                        "context_limit": entry.context_limit,
                        "max_output_tokens": entry.max_output_tokens,
                        "monthly_limit_usd": entry.monthly_limit_usd,
                        "limits_source": entry.limits_source,
                        "notes": entry.notes,
                        "reasoning_efforts": entry.reasoning_efforts,
                    }
                )
            providers.append(
                {
                    "provider_id": provider.provider_id,
                    "kind": provider.kind,
                    "state": provider.state,
                    "last_test_at": provider.last_test_at,
                    "last_error": provider.last_error,
                    "catalog_observed_at": provider.catalog_observed_at,
                    # Owner state only: this is the destination the owner
                    # typed. It is deliberately absent from the unauthenticated
                    # status payload, the access log, the ledger and errors.
                    "base_url": provider.base_url,
                    # Owner state only: the saved per-provider cap and the
                    # number that actually applies to it right now (cloud
                    # default, local floor, or the override above). Same
                    # owner-session boundary as base_url.
                    "max_in_flight": provider.max_in_flight,
                    "max_in_flight_effective": effective_limit(
                        provider.provider_id, provider.max_in_flight
                    ),
                    "credential_present": self.credentials.describe(provider.provider_id)["present"],
                    "models": models,
                }
            )

        aliases = []
        for name in sorted(config.aliases):
            alias = config.aliases[name]
            route_limits: list[tuple[str, int | None]] = []
            for ref in alias.all_routes():
                provider = config.providers.get(ref.provider)
                entry = provider.models.get(ref.model) if provider else None
                route_limits.append((ref.label(), entry.context_limit if entry else None))
            alias_payload = alias.to_dict()
            alias_payload["context_limit"] = alias_context_limit(route_limits)
            alias_payload["budget_status"] = budget_status(ledger_snapshot, alias.name, alias.budget, history_complete=ledger_history_complete, _prices=price_snapshot)
            aliases.append(alias_payload)

        profiles = []
        for profile_id in sorted(config.profiles):
            profile = config.profiles[profile_id]
            profiles.append(
                {
                    "profile_id": profile.profile_id,
                    "name": profile.name,
                    "enabled": profile.enabled,
                    "allowed_aliases": list(profile.allowed_aliases),
                    "all_aliases": profile.all_aliases,
                    "has_token": profile.has_token,
                    "token_created_at": profile.token_created_at,
                    "token_rotated_at": profile.token_rotated_at,
                    "anonymous": profile.anonymous,
                }
            )

        # Fail visibly when fallback policy contains silent coverage gaps.
        # A confirmed cross-provider route can be reached after any of these
        # triggers; if omitted, the router correctly refuses the switch, but
        # the owner needs to know the missing checkbox before callers see 503s.
        trigger_coverage_warnings: list[str] = []
        globally_enabled = {
            trigger for alias in config.aliases.values() for trigger in alias.fallback_triggers
        }
        for trigger in FALLBACK_TRIGGERS:
            if trigger not in globally_enabled:
                trigger_coverage_warnings.append(
                    f"No alias enables the '{trigger}' fallback trigger; routes held by this condition will be refused."
                )
        for alias in config.aliases.values():
            if not alias.fallbacks:
                continue
            cross_provider = any(
                route.provider != alias.primary.provider
                for route in alias.fallbacks
                if alias.primary is not None
            )
            if not cross_provider or not alias.fallback_authorized:
                continue
            for trigger in FALLBACK_TRIGGERS:
                if trigger not in alias.fallback_triggers:
                    trigger_coverage_warnings.append(
                        f"Alias '{alias.name}' confirms a cross-provider fallback but does not enable '{trigger}'; that skip/failure will be refused."
                    )

        # The 25-row console excerpt is the head of the same snapshot: the
        # ring pull returns newest-first, so the first 25 rows are exactly
        # what a separate recent(limit=25) pull would have produced.
        recent_ledger = ledger_snapshot[:25]
        # Analytics come from the durable rollup the ledger keeps in memory;
        # this path never opens the ledger file, because it runs on every
        # console poll. The catalogue is loaded once above and reused here.
        days, requested_days = analytics_days(request)
        analytics = build_analytics(
            self.ledger.rollup() if self.ledger else None,
            days=days,
            requested_days=requested_days,
            prices=price_snapshot,
        )
        return json_response(
            {
                "initialized": config.initialized,
                "revision": config.revision,
                "snapshot_interval_seconds": snapshot_interval_seconds(config),
                "access_mode": config.access_mode,
                "access_mode_description": access_mode_description(config.access_mode),
                "surface": self._surface(request),
                "can_mutate": may_mutate,
                "can_enroll_credentials": self._can_enroll_credentials(request),
                "authenticated": bool(request.context.get("owner_authenticated")),
                # The signed-in owner and the client-key state (presence and
                # transition only - never a hash, never the value).
                "owner": {"username": request.context.get("owner_username")},
                "access": {"client_key": self.owner_auth.client_key_status()},
                "providers": providers,
                "aliases": aliases,
                "profiles": profiles,
                "published_models": [alias.name for alias in sorted(config.published_aliases(), key=lambda a: a.name)],
                "limits": self.limits.all(),
                "ledger": annotate_events(recent_ledger, _prices=price_snapshot),
                "totals": self.ledger.totals() if self.ledger else {},
                "usage_costs": aggregate_usage(ledger_snapshot, window_hours=24, evicted_events=ledger_evicted, _prices=price_snapshot) if self.ledger else {"window_hours": 24, "coverage": {"source": "bounded_recent_events", "events_considered": 0, "incomplete": False}, "alias": {}, "provider": {}, "model": {}},
                "analytics": analytics,
                "request_statistics": {window: aggregate_statistics(ledger_snapshot, window, _prices=price_snapshot) for window in ("hour", "day", "month")} if self.ledger else {},
                "provider_health": provider_health(providers, self.router.circuits.details(), ledger_snapshot),
                "ledger_durability": self.ledger.durability() if self.ledger else {"enabled": False, "retention_days": 30, "billing_system": False},
                "cooldowns": self.router.circuits.snapshot(),
                "cooldown_details": self.router.circuits.details(),
                "cooldown_policy": {
                    "exhausted_base_seconds": EXHAUSTED_BASE_SECONDS,
                    "exhausted_max_seconds": self.router.circuits.exhausted_max_seconds,
                    "rate_limited_max_seconds": self.router.circuits.rate_limited_max_seconds,
                    "transient_max_seconds": self.router.circuits.transient_max_seconds,
                    "probe_timeout_seconds": PROBE_TIMEOUT_SECONDS,
                    "transient_failure_threshold": TRANSIENT_FAILURE_THRESHOLD,
                },
                "release": self._release_identity() if self._release_identity else {},
                "base_url": self.base_url,
                "fallback_triggers": list(FALLBACK_TRIGGERS),
                "fallback_policy_warnings": trigger_coverage_warnings,
                "device_code": {
                    "prerequisite": PREREQUISITE,
                    "anti_phishing": ANTI_PHISHING,
                },
                "checklist": self._checklist(config),
            }
        )

    def handle_backup_export(self, request: Request) -> Response:
        config = self.config.snapshot()
        return json_response({"snapshot_interval_seconds": snapshot_interval_seconds(config),
                              "export": export_config(config)})

    def handle_backup_import(self, request: Request) -> Response:
        denied = self._require_mutation(request)
        if denied is not None:
            return denied
        try:
            payload = request.json()
            imported = import_config(payload, self.config.snapshot(), self.registry)
            self.config.replace_validated(imported)
        except ValidationError as exc:
            return json_response({"error": {"message": exc.message, "type": "invalid_import", "field": exc.field}}, 400)
        except (ValueError, TypeError) as exc:
            return json_response({"error": {"message": str(exc), "type": "invalid_import"}}, 400)
        return json_response({"ok": True, "revision": self.config.snapshot().revision})

    def _checklist(self, config: GatewayConfig) -> list[dict[str, Any]]:
        """The get-started checklist. Every step states whether it is done."""
        connected = any(p.state == STATE_CONNECTED for p in config.providers.values())
        tested = any(p.last_test_at for p in config.providers.values())
        discovered = any(p.models for p in config.providers.values())
        enabled = any(p.enabled_models() for p in config.providers.values())
        aliased = any(a.primary is not None for a in config.aliases.values())
        published = bool(config.published_aliases())
        return [
            {"step": 1, "title": "Connect a provider", "done": connected},
            {"step": 2, "title": "Test the provider connection", "done": tested},
            {"step": 3, "title": "Discover or enter model IDs", "done": discovered},
            {"step": 4, "title": "Choose models to expose", "done": enabled},
            {"step": 5, "title": "Create an alias and route policy", "done": aliased},
            {"step": 6, "title": "Test the alias", "done": published},
            {"step": 7, "title": "Use it from a LAN client", "done": published},
        ]

    # --- capability discovery (opt-in, metadata-only) -------------------

    def handle_capability_read(self, request: Request) -> Response:
        provider_id = request.query_one("provider_id") or ""
        model_id = request.query_one("model_id") or ""
        payload = self.discovery.read(provider_id, model_id)
        # Probe-registry coverage rides along so the console can tell the
        # owner *before* any preview that probing is an opt-in allowlist
        # (currently 1 registered pair), not a broken feature.
        config = self.config.snapshot()
        staged = sum(len(provider.models) for provider in config.providers.values())
        payload["probe_registry"] = {
            **registry_coverage(staged),
            "this_pair_registered": protocol_for(provider_id, model_id) is not None,
        }
        return json_response(payload)

    def _probe_readiness(self, provider_id: str, config: GatewayConfig) -> tuple[str, str] | None:
        """Why this provider cannot host a paid probe right now, or ``None``.

        Structural only: no connection test, no catalogue refresh and no
        provider request happens on this path.
        """
        provider = config.providers.get(provider_id)
        if provider is None or provider.state != STATE_CONNECTED:
            return (
                "provider_not_configured",
                "The provider connection has not been confirmed as connected, so no paid probe may run against it.",
            )
        adapter = self.registry.get(provider_id)
        if adapter is None or not callable(getattr(adapter, "capability_probe_request", None)):
            return (
                "adapter_not_probe_capable",
                "The registered adapter for this provider does not implement the reviewed bounded probe transport.",
            )
        readiness = getattr(adapter, "probe_readiness", None)
        if not callable(readiness):
            return (
                "adapter_not_probe_capable",
                "The registered adapter for this provider does not report probe readiness.",
            )
        reported = readiness()
        if not isinstance(reported, tuple) or len(reported) != 2:
            return (
                "adapter_not_probe_capable",
                "The registered adapter for this provider reports probe readiness in an unusable form.",
            )
        ready, reason = reported
        if not ready:
            return (
                "provider_not_configured",
                f"The adapter reports this provider is not ready for a probe ({reason}).",
            )
        return None

    def handle_capability_preview(self, request: Request) -> Response:
        """Full preview for a probe run. Sends nothing and costs nothing."""
        denied = self._require_mutation(request)
        if denied is not None:
            return denied
        try:
            payload = request.json()
            provider_id, model_id, probes = payload["provider_id"], payload["model_id"], payload["probes"]
            if not isinstance(provider_id, str) or not isinstance(model_id, str) or not isinstance(probes, list):
                raise ValueError
            owner = payload.get("owner")
            config = self.config.snapshot()
            provider = config.providers.get(provider_id)
            if provider is None or model_id not in provider.models:
                return json_response({"error": {"message": "Exact provider/model is not staged", "type": "unknown_model"}}, 404)
            result = build_preview(
                provider_id,
                model_id,
                probes,
                config.revision,
                owner=owner,
                price_lookup=self.probe_price_lookup,
            )
            if result.get("protocol_version") is not None:
                readiness = self._probe_readiness(provider_id, config)
                if readiness is not None:
                    result = mark_unavailable(result, *readiness)
                elif self.probe_gate.active_targets:
                    result = mark_unavailable(
                        result,
                        "probe_run_in_progress",
                        "Another capability probe run is in flight; preview again when it finishes.",
                    )
                elif result["available"]:
                    # A run is only possible from this exact preview: issue the
                    # single-use confirmation that binds the owner's selection.
                    token, expires_at = self.probe_confirmations.issue(
                        preview_binding(result, owner if isinstance(owner, dict) else {})
                    )
                    result = {**result, "confirmation": token, "confirmation_expires_at": expires_at}
            return json_response(result, 200 if result["available"] else 422)
        except ProbeRefused as exc:
            return json_response({"error": {"message": exc.message, "type": exc.code}}, exc.status)
        except (ValueError, TypeError, KeyError):
            return json_response({"error": {"message": "Invalid capability preview request", "type": "invalid_request"}}, 400)

    def handle_capability_run(self, request: Request) -> Response:
        """Execute one confirmed, bounded, paid probe run — or refuse it.

        Every gate is checked before a transport is touched, and each
        failure names the gate that refused it. Nothing here writes
        configuration: the run records evidence and leaves an unselected
        proposal for the owner to decide.
        """
        denied = self._require_mutation(request)
        if denied is not None:
            return denied
        try:
            payload = request.json()
            provider_id, model_id, probes = payload["provider_id"], payload["model_id"], payload["probes"]
            if not isinstance(provider_id, str) or not isinstance(model_id, str) or not isinstance(probes, list):
                raise ValueError
            owner = payload.get("owner")
            revision = payload.get("base_revision")
            if not isinstance(revision, int) or isinstance(revision, bool):
                raise ValueError
            config = self.config.snapshot()
            provider = config.providers.get(provider_id)
            if provider is None or model_id not in provider.models:
                return json_response({"error": {"message": "Exact provider/model is not staged", "type": "unknown_model"}}, 404)
            protocol = protocol_for(provider_id, model_id)
            if protocol is None:
                return json_response({"error": {"message": "No reviewed probe protocol is registered for this exact provider/model pair; no upstream request was sent", "type": "no_probe_protocol"}}, 422)
            readiness = self._probe_readiness(provider_id, config)
            if readiness is not None:
                code, reason = readiness
                return json_response({"error": {"message": reason, "type": code}}, 409)
            # Recompute the preview: the run must match what the owner saw,
            # including the tariff, the bounds and the selected route.
            preview = build_preview(
                provider_id,
                model_id,
                probes,
                config.revision,
                owner=owner,
                price_lookup=self.probe_price_lookup,
            )
            if not preview["available"]:
                return json_response({"error": {"message": preview["reason"], "type": preview["code"]}}, 422)
            if config.revision != revision:
                return json_response({"error": {"message": "The staged configuration changed after this preview; preview again.", "type": "stale_revision"}}, 409)
            binding = preview_binding(preview, owner if isinstance(owner, dict) else {})
            try:
                self.probe_confirmations.consume(payload.get("confirmation"), binding)
            except ConfirmationError as exc:
                return json_response({"error": {"message": exc.message, "type": exc.code}}, 422)
            price = self.probe_price_lookup(provider_id, model_id)
            if price_status(price, datetime.now(UTC)) != "known":
                price = None
            try:
                plan = plan_requests(protocol, probes)
            except ProbeRefused as exc:
                return json_response({"error": {"message": exc.message, "type": exc.code}}, exc.status)
            adapter = self.registry.get(provider_id)
            try:
                cancel_event = self.probe_gate.acquire(provider_id, model_id)
            except ProbeRefused as exc:
                return json_response({"error": {"message": exc.message, "type": exc.code}}, exc.status)
            session_id = secrets.token_hex(16)
            started_at = utc_now_iso()
            try:
                outcome = run_suite(
                    protocol,
                    probes,
                    adapter.capability_probe_request,
                    price=price,
                    cancel_event=cancel_event,
                    plan=plan,
                    session_id=session_id,
                )
            finally:
                self.probe_gate.release(provider_id, model_id)
            row = config.providers[provider_id].models[model_id].to_dict()
            run_id, proposal_id = self.discovery.record_run(
                provider_id,
                model_id,
                outcome.results,
                row,
                config.revision,
                evidence_method=EVIDENCE_METHOD,
                request_count=outcome.request_count,
                protocol_version=protocol.protocol_version,
                session_id=session_id,
            )
            return json_response({
                "run_id": run_id,
                "proposal_id": proposal_id,
                "proposal_decision": "pending",
                "session_id": session_id,
                "protocol_version": protocol.protocol_version,
                "evidence_method": EVIDENCE_METHOD,
                "request_count": outcome.request_count,
                "blocked_reason": outcome.blocked_reason,
                "cancelled": outcome.cancelled,
                "reported_cost_usd": outcome.reported_cost_usd,
                "started_at": started_at,
                "finished_at": utc_now_iso(),
                "results": outcome.results,
            })
        except ProbeRefused as exc:
            return json_response({"error": {"message": exc.message, "type": exc.code}}, exc.status)
        except (ValueError, TypeError, KeyError):
            return json_response({"error": {"message": "Invalid capability run request", "type": "invalid_request"}}, 400)

    def handle_capability_probe_cancel(self, request: Request) -> Response:
        """Cancel the run in flight. Reports honestly when nothing is running."""
        denied = self._require_mutation(request)
        if denied is not None:
            return denied
        try:
            payload = request.json()
            provider_id, model_id = payload["provider_id"], payload["model_id"]
            if not isinstance(provider_id, str) or not isinstance(model_id, str):
                raise ValueError
        except (ValueError, TypeError, KeyError):
            return json_response({"error": {"message": "Invalid probe cancel request", "type": "invalid_request"}}, 400)
        return json_response({"cancelled": self.probe_gate.cancel(provider_id, model_id)})

    def handle_capability_decision(self, request: Request) -> Response:
        denied = self._require_mutation(request)
        if denied is not None:
            return denied
        try:
            payload = request.json()
            proposal_id = payload["proposal_id"]
            decision_id = payload["decision_id"]
            decisions = payload["decisions"]
            revision = payload["expected_revision"]
            fingerprint = payload["expected_fingerprint"]
            if (not isinstance(proposal_id, str) or not isinstance(decision_id, str)
                    or not isinstance(decisions, dict) or not isinstance(revision, int)
                    or isinstance(revision, bool) or not isinstance(fingerprint, str)):
                raise ValueError
            # decide() bounds both ids as well. It used to be the ONLY place
            # that did, so an over-long id was refused after the config
            # transaction had committed (revision bump + marker for a decision
            # that never applied). Every decide() input refusal must be a
            # no-write refusal, so validate the ids here, before anything is
            # read, compared or written. Stored decisions can never carry an
            # out-of-bound id (decide() rejects them before recording), so
            # this cannot hide a legitimate replay.
            if not proposal_id or len(proposal_id) > 64 or not decision_id or len(decision_id) > 64:
                raise ValueError("invalid decision id")
            # Exact HTTP retries are idempotent even after their config revision
            # has advanced. This must precede snapshot CAS and any config mutation.
            replay = self.discovery.decision_replay(proposal_id, decision_id, decisions)
            if replay is not None:
                # A prior sidecar commit may have failed while checkpointing
                # the remaining partial proposal. Repair that checkpoint from
                # the config snapshot before returning replay success.
                if replay.get("status") == "partially_approved":
                    proposal = self.discovery.proposal(proposal_id)
                    if proposal is not None:
                        config = self.config.snapshot()
                        row = config.providers[proposal["provider_id"]].models[proposal["model_id"]]
                        fingerprint_now = hashlib.sha256(
                            json.dumps(row.to_dict(), sort_keys=True, separators=(",", ":")).encode()
                        ).hexdigest()
                        self.discovery.rebase_partial(proposal_id, config.revision, fingerprint_now)
                return json_response(replay)
            proposal = self.discovery.proposal(proposal_id)
            if proposal is None:
                return json_response({"error": {"message": "Unknown capability proposal", "type": "unknown_proposal"}}, 404)
            provider_id, model_id = proposal["provider_id"], proposal["model_id"]
            # If a previous request durably committed config but failed before
            # the evidence sidecar commit (or the process stopped there), finish
            # that exact transaction from config's durable decision marker.
            committed = self.config.snapshot()
            decision_marker = committed.settings.get("capability_decisions", {}).get(proposal_id)
            if isinstance(decision_marker, dict) and decision_marker.get("decision_id") == decision_id and decision_marker.get("decisions") == decisions:
                result = self.discovery.decide(proposal_id, decision_id, decisions, revision, fingerprint, recover_committed=True)
                result["revision"] = committed.revision
                if result["status"] == "partially_approved":
                    row_now = committed.providers[provider_id].models[model_id]
                    updated_fingerprint = hashlib.sha256(
                        json.dumps(row_now.to_dict(), sort_keys=True, separators=(",", ":")).encode()
                    ).hexdigest()
                    self.discovery.rebase_partial(proposal_id, committed.revision, updated_fingerprint)
                return json_response(result)
            # CAP-105 / CAP-201: an expired proposal cannot be approved by a
            # NEW decision, and that refusal must carry NO state change.
            # decide() checks expiry only after the config transaction has
            # committed, so an expired proposal used to be refused with config
            # written anyway -- revision bump, capability applied, provenance +
            # decision marker -- and because the refusal had armed the exact
            # config marker, the retry then "recovered" into a 200. Gate it
            # here, before any config mutation, right after the ONE path
            # allowed past expiry: the exact config-committed marker above
            # (recover_committed=True, which completes a transaction config
            # already durably wrote). The sidecar is untrusted input, so a
            # missing or unparseable expires_at refuses here too -- expiry is
            # never read as "still valid". None of these refusals touch config
            # or the sidecar: no revision bump, no provenance, no marker, no
            # decision row, and the proposal is left exactly as found.
            if proposal_expired(proposal):
                raise RuntimeError("proposal expired")
            with self.config.mutate() as config:
                provider = config.providers.get(provider_id)
                row = provider.models.get(model_id) if provider else None
                if row is None:
                    return json_response({"error": {"message": "Proposal target no longer exists", "type": "unknown_model"}}, 404)
                current = row.to_dict()
                actual_fingerprint = hashlib.sha256(json.dumps(current, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                if config.revision != revision or actual_fingerprint != fingerprint:
                    self.discovery.mark_conflicted(proposal_id)
                    raise RuntimeError("stale config")
                if proposal["base_revision"] != revision or proposal["base_model_fingerprint"] != fingerprint:
                    self.discovery.mark_conflicted(proposal_id)
                    raise RuntimeError("stale proposal")
                # The proposal is untrusted sidecar input: its loader checks
                # only schema and bucket types, never field names or value
                # types. Apply the shared capability vocabulary and require a
                # boolean before any config or evidence write, so this endpoint
                # can never become an arbitrary metadata patch (spec: decision
                # endpoint). Every key the apply loop later reads -- including
                # `classification` -- is validated here and the approved writes
                # are precomputed, so these checks raise before decide()/setattr:
                # a refused request records nothing, leaves config unchanged and
                # never burns the proposal (a post-decide() refusal would leave
                # the 0600 sidecar claiming an approval that never applied).
                field_records = {f.get("field"): f for f in proposal.get("fields", []) if isinstance(f, dict)}
                if not set(decisions) <= APPROVABLE_METADATA_FIELDS:
                    raise ValueError("field outside capability vocabulary")
                # The decision VALUES are spec vocabulary too (approved|
                # rejected), and an empty map applies nothing while still
                # burning the proposal and bumping the revision. decide()
                # checked this only AFTER the config commit, so a value typo
                # used to be refused with a marker + revision bump for a
                # decision that never applied (and a mixed valid+typo map was
                # refused with its valid field APPLIED). Raise inside the
                # mutating block: it commits only when no exception escapes,
                # so this is a guaranteed no-write refusal.
                if not decisions or not set(decisions.values()) <= {"approved", "rejected"}:
                    raise ValueError("invalid decision values")
                approved_writes = []
                for field, decision in decisions.items():
                    field_record = field_records.get(field)
                    if field_record is None:
                        raise ValueError("unknown proposal field")
                    if not isinstance(field_record.get("proposed_value"), bool):
                        raise ValueError("non-boolean capability value")
                    if field_record.get("classification") not in EVIDENCE_CLASSIFICATIONS:
                        raise ValueError("unknown evidence classification")
                    if decision == "approved":
                        approved_writes.append(
                            (field, field_record["proposed_value"], field_record["classification"]))
                # Persist the effective config before recording the decision in
                # the evidence sidecar. If config persistence fails, the
                # proposal remains pending and the same decision can be retried.
                result = {"proposal_id": proposal_id, "status": "approved", "decisions": decisions}
                approvals = config.settings.setdefault("capability_approvals", {})
                target = approvals.setdefault(provider_id, {}).setdefault(model_id, {})
                for field, value, classification in approved_writes:
                    setattr(row, field, value)
                    target[field] = {"value": value, "source": "capability_discovery_observation",
                                     "proposal_id": proposal_id, "decision_id": decision_id,
                                     "approved_at": utc_now_iso(), "classification": classification}
                if not target:
                    approvals.get(provider_id, {}).pop(model_id, None)
                decision_markers = config.settings.setdefault("capability_decisions", {})
                decision_markers[proposal_id] = {"decision_id": decision_id, "decisions": decisions}
                # Keep recovery metadata bounded to the proposal-store window.
                for stale_id in list(decision_markers)[:-500]:
                    decision_markers.pop(stale_id, None)
                result["revision"] = config.revision + 1
                remaining = [
                    field for field in proposal.get("fields", [])
                    if isinstance(field, dict) and field.get("field") not in decisions
                    and field.get("decision") == "pending"
                ]
                result["status"] = "partially_approved" if remaining else "approved"
                result["revision"] = config.revision + 1
            # Config is now durable. Commit the audit/decision sidecar second;
            # if it fails the request reports failure, but retry must repair it
            # from the provenance written atomically with config.
            committed_config = self.config.snapshot()
            row_now = committed_config.providers[provider_id].models[model_id]
            committed_approvals = committed_config.settings.get("capability_approvals", {}).get(provider_id, {}).get(model_id, {})
            if all(committed_approvals.get(field, {}).get("decision_id") == decision_id for field in decisions if decisions[field] == "approved"):
                result = self.discovery.decide(proposal_id, decision_id, decisions, revision, fingerprint)
                result["revision"] = committed_config.revision
                if result["status"] == "partially_approved":
                    updated_fingerprint = hashlib.sha256(
                        json.dumps(row_now.to_dict(), sort_keys=True, separators=(",", ":")).encode()
                    ).hexdigest()
                    self.discovery.rebase_partial(proposal_id, committed_config.revision, updated_fingerprint)
            return json_response(result)
        except (ValueError, TypeError, KeyError):
            return json_response({"error": {"message": "Invalid capability decision", "type": "invalid_request"}}, 400)
        except RuntimeError as exc:
            return json_response({"error": {"message": "Capability proposal conflict or resolved", "type": "conflict"}}, 409)
        except Exception:
            return json_response({"error": {"message": "Capability decision could not be applied", "type": "invalid_request"}}, 400)

    # --- provider actions ------------------------------------------------

    def _provider_max_in_flight(self, provider_id: str) -> int | None:
        """The owner's saved override for this provider, or ``None``.

        Read from the config snapshot on every acquire, so an override
        saved from the console applies to the next request with no
        restart and no re-wrapping of the registry.
        """
        provider = self.config.snapshot().providers.get(provider_id)
        return provider.max_in_flight if provider is not None else None

    def handle_provider_action(self, request: Request) -> Response:
        denied = self._require_mutation(request)
        if denied is not None:
            return denied
        try:
            payload = request.json() or {}
        except ValueError:
            payload = {}
        action = str(payload.get("action") or "")
        provider_id = str(payload.get("provider_id") or "")
        # The access log must say *which* owner action was performed: four
        # different provider actions share one path, and without this a
        # failure cannot be told apart from the others.
        request.context["action"] = action
        request.context["provider_id"] = provider_id
        # Credential enrolment and device flows stay human-session-only
        # even for a write-scoped machine key: an automation credential
        # must not be able to store a provider secret or run an
        # authorisation flow. Checked here, after the action is known and
        # before anything is contacted or written.
        if request.context.get("machine_key_authenticated") and (
            action in MACHINE_KEY_BLOCKED_PROVIDER_ACTIONS
        ):
            return self._machine_denied(
                "machine_key_forbidden",
                "machine keys cannot enrol or acquire credentials; sign in to the owner console instead",
            )
        # The cooldown-clear action is dispatched before the provider
        # lookup: its explicit all scope is not addressed to one provider
        # at all. Every other scope still requires a known provider, so the
        # unknown-provider refusal below is unchanged for the other three
        # actions and for the provider and route scopes.
        if action == "clear_cooldowns":
            return self._handle_clear_cooldowns(payload, provider_id)
        adapter = self.registry.get(provider_id)
        if adapter is None:
            return json_response(
                {"error": {"message": f"unknown provider '{provider_id}'", "type": "unknown_provider"}}, 404
            )

        if action == "test":
            result = adapter.test_connection()
            with self.config.mutate() as config:
                provider = config.providers.get(provider_id)
                if provider is not None:
                    provider.state = STATE_CONNECTED if result.ok else STATE_FAILED
                    provider.last_test_at = utc_now_iso()
                    provider.last_error = None if result.ok else result.message
            return json_response(result.to_dict())

        if action == "refresh":
            models = adapter.list_models()
            now = utc_now_iso()
            with self.config.mutate() as config:
                provider = config.providers.get(provider_id)
                if provider is None:
                    provider = _new_provider_state(provider_id, adapter.kind)
                    config.providers[provider_id] = provider
                provider.catalog_observed_at = now
                for info in models:
                    existing = provider.models.get(info.model_id)
                    entry = ModelEntry(
                        model_id=info.model_id,
                        enabled=existing.enabled if existing else False,
                        dialect=info.dialect or (existing.dialect if existing else None),
                        source="discovered",
                        observed_at=now,
                        limits_known=info.monthly_limit_usd is not None,
                        notes=info.notes,
                        reasoning_efforts=list(info.reasoning_efforts),
                        supports_streaming=info.supports_streaming,
                        supports_function_tools=info.supports_function_tools,
                        supports_builtin_tools=info.supports_builtin_tools,
                        supports_image_input=info.supports_image_input,
                        context_window=info.context_window,
                        context_limit=info.context_limit,
                        max_output_tokens=info.max_output_tokens,
                        retention_class=info.retention_class,
                        monthly_limit_usd=info.monthly_limit_usd,
                        limits_source=info.limits_source,
                        limits_observed_at=info.limits_observed_at,
                    )
                    # Explicitly approved discovery overrides survive catalogue refresh.
                    approved = config.settings.get("capability_approvals", {})
                    override_fields = approved.get(provider_id, {}).get(info.model_id, {}) if isinstance(approved, dict) else {}
                    for field_name, record in override_fields.items():
                        if field_name in APPROVABLE_METADATA_FIELDS and isinstance(record, dict):
                            value = record.get("value")
                            if isinstance(value, bool):
                                setattr(entry, field_name, value)
                    provider.models[info.model_id] = entry
                if provider.state == STATE_NOT_CONFIGURED:
                    provider.state = STATE_CONFIGURED_UNTESTED
            # Publish the published limits as snapshots, so the Status view
            # can show them with their source and an as-of time.
            self._publish_limits(provider_id, now)
            return json_response({"ok": True, "model_count": len(models), "observed_at": now})

        if action == "set_base_url":
            # The one action where the owner supplies a destination the
            # gateway will connect to. It is refused unless it is a plain
            # http(s) URL addressing a private/loopback address, it is
            # re-checked against the socket's actual peer before any request
            # byte is written, and it is stored on the provider row rather
            # than in a credential - so it can never carry a secret.
            if not isinstance(adapter, LocalOpenAIAdapter):
                return json_response(
                    {"error": {"message": "this provider has no configurable endpoint", "type": "unsupported_action"}},
                    400,
                )
            raw_url = payload.get("base_url")
            if raw_url is None or (isinstance(raw_url, str) and not raw_url.strip()):
                # An empty value clears the endpoint. Discovered rows came
                # from the server being removed, so they go with it.
                normalized = None
            elif isinstance(raw_url, str):
                try:
                    normalized = normalize_base_url(raw_url)
                except LocalEndpointError as exc:
                    # Fixed reason, never the value that was refused: a URL
                    # can carry a password-shaped string.
                    return json_response(
                        {"error": {"message": exc.reason, "type": "invalid_endpoint"}}, 400
                    )
            else:
                return json_response(
                    {"error": {"message": "the endpoint must be a string", "type": "invalid_endpoint"}}, 400
                )
            with self.config.mutate() as config:
                provider = config.providers.get(provider_id)
                if provider is None:
                    provider = _new_provider_state(provider_id, adapter.kind)
                    config.providers[provider_id] = provider
                changed = (provider.base_url or None) != normalized
                provider.base_url = normalized
                if changed:
                    # Capability truth: a row discovered from the previous
                    # server is not evidence about this one. Manual entries
                    # are the owner's own and survive.
                    provider.models = {
                        key: entry
                        for key, entry in provider.models.items()
                        if entry.source != "discovered"
                    }
                    provider.catalog_observed_at = None
                    if normalized is None:
                        provider.state = STATE_NOT_CONFIGURED
                    else:
                        provider.state = STATE_CONFIGURED_UNTESTED
                    provider.last_error = None
            return json_response({"ok": True, "base_url": normalized})

        if action == "set_max_in_flight":
            # The per-provider in-flight cap. A positive integer sets the
            # override, null/empty clears it back to the default (cloud
            # providers take the global default, the local adapters stay
            # at their low floor). Non-secret, owner-shaped like every
            # other provider setting: same session gate, same mutation
            # check, and the access log records the action, never the
            # value. A non-integer, a bool, zero or a negative value is
            # refused with 400 and writes nothing.
            raw_limit = payload.get("max_in_flight")
            if raw_limit is None or (isinstance(raw_limit, str) and not raw_limit.strip()):
                normalized_limit = None
            else:
                try:
                    normalized_limit = check_limit(raw_limit, "max_in_flight")
                except ValueError:
                    return json_response(
                        {
                            "error": {
                                "message": "max_in_flight must be a positive integer",
                                "type": "invalid_max_in_flight",
                            }
                        },
                        400,
                    )
            with self.config.mutate() as config:
                provider = config.providers.get(provider_id)
                if provider is None:
                    provider = _new_provider_state(provider_id, adapter.kind)
                    config.providers[provider_id] = provider
                provider.max_in_flight = normalized_limit
            return json_response(
                {
                    "ok": True,
                    "max_in_flight": normalized_limit,
                    "max_in_flight_effective": effective_limit(provider_id, normalized_limit),
                }
            )

        if action == "disconnect":
            handle = self.credentials.provider_handle(provider_id)
            handle.clear()
            with self.config.mutate() as config:
                provider = config.providers.get(provider_id)
                if provider is not None:
                    provider.state = STATE_NOT_CONFIGURED
                    provider.last_error = None
            return json_response({"ok": True})

        if action == "device_start":
            if not isinstance(adapter, ChatGPTCodexAdapter):
                return json_response(
                    {"error": {"message": "this provider has no device-code flow", "type": "unsupported_action"}},
                    400,
                )
            self._prune_pending_device()
            authorization = adapter.start_device_authorization()
            # Keyed by the opaque poll handle, never by the provider's own
            # device auth id, which does not leave this process.
            self._pending_device[authorization.poll_handle] = authorization
            return json_response(authorization.to_dict())

        if action == "device_poll":
            if not isinstance(adapter, ChatGPTCodexAdapter):
                return json_response(
                    {"error": {"message": "this provider has no device-code flow", "type": "unsupported_action"}},
                    400,
                )
            self._prune_pending_device()
            poll_handle = str(payload.get("poll_handle") or "")
            authorization = self._pending_device.get(poll_handle)
            if authorization is None:
                return json_response(
                    {"error": {"message": "that authorisation is no longer pending", "type": "unknown_authorization"}},
                    404,
                )
            authorization = adapter.poll_device_authorization(authorization)
            if authorization.state in ("approved", "denied", "expired", "cancelled"):
                self._pending_device.pop(poll_handle, None)
                if authorization.state == "approved":
                    with self.config.mutate() as config:
                        provider = config.providers.get(provider_id)
                        if provider is None:
                            provider = _new_provider_state(provider_id, adapter.kind)
                            config.providers[provider_id] = provider
                        provider.state = STATE_CONFIGURED_UNTESTED
            # The status payload never repeats the one-time code.
            return json_response(authorization.to_status_dict())

        if action == "enroll_key":
            api_key = payload.get("api_key")
            if not isinstance(api_key, str) or not api_key.strip():
                return json_response(
                    {"error": {"message": "an API key is required", "type": "malformed_request"}}, 400
                )
            self.credentials.provider_handle(provider_id).write({"api_key": api_key.strip()})
            with self.config.mutate() as config:
                provider = config.providers.get(provider_id)
                if provider is None:
                    provider = _new_provider_state(provider_id, adapter.kind)
                    config.providers[provider_id] = provider
                provider.state = STATE_CONFIGURED_UNTESTED
            # The key is never echoed back.
            return json_response({"ok": True, "stored": True})

        return json_response(
            {"error": {"message": f"unknown action '{action}'", "type": "unknown_action"}}, 400
        )

    def _handle_clear_cooldowns(self, payload: dict[str, Any], provider_id: str) -> Response:
        """The deliberate recovery action, at a scope the owner names.

        Three scopes, and the widest one is never a default:

        * no ``scope`` (or ``scope="provider"``) - exactly the provider
          named by ``provider_id`` and its routes. This is the original
          behaviour, so the console's provider-card button is unchanged;
        * ``scope="route"``, or a ``model`` (``route``) with no scope -
          the single provider/model circuit;
        * ``scope="all"`` - every circuit on the gateway, and only when
          the payload spells ``all`` out. A defaulted field can never
          reach ``clear(provider=None)``, so no accidental mass clear is
          possible.

        The response is metadata only: the removed entries and their
        keys, never a credential or request content. No provider is
        called unless the owner also asks for one explicit probe by
        naming its alias.
        """
        scope_raw = payload.get("scope", None)
        model_raw = payload.get("model", None)
        route_raw = payload.get("route", None)
        if (
            (scope_raw is not None and not isinstance(scope_raw, str))
            or (model_raw is not None and not isinstance(model_raw, str))
            or (route_raw is not None and not isinstance(route_raw, str))
        ):
            return json_response(
                {"error": {"message": "Invalid cooldown clear request", "type": "invalid_request"}}, 400
            )
        scope = scope_raw or ""
        model = model_raw or route_raw or ""
        if model_raw and route_raw and model_raw != route_raw:
            return json_response(
                {"error": {"message": "model and route name different circuits", "type": "invalid_request"}}, 400
            )
        if scope not in ("", "provider", "route", "all"):
            return json_response(
                {"error": {"message": "scope must be one of provider, route or all", "type": "invalid_scope"}}, 400
            )
        if scope == "provider" and model:
            return json_response(
                {"error": {"message": "the provider scope cannot name a route", "type": "invalid_scope"}}, 400
            )
        if scope == "all" and model:
            return json_response(
                {"error": {"message": "the all scope cannot name a route", "type": "invalid_scope"}}, 400
            )
        if scope == "route" and not model:
            return json_response(
                {"error": {"message": "a route clear must name the model", "type": "invalid_scope"}}, 400
            )
        # Everything except an unnamed-provider all clear addresses one
        # provider, so the unknown-provider refusal applies exactly as it
        # did before this action had a scope.
        if scope != "all" or provider_id:
            if self.registry.get(provider_id) is None:
                return json_response(
                    {"error": {"message": f"unknown provider '{provider_id}'", "type": "unknown_provider"}}, 404
                )
        if scope == "all":
            resolved = "all"
            cleared = self.router.circuits.clear()
        elif model:
            resolved = "route"
            cleared = self.router.circuits.clear(provider=provider_id, model=model)
        else:
            resolved = "provider"
            cleared = self.router.circuits.clear(provider=provider_id)
        probe_alias = str(payload.get("probe_alias") or "")
        probe = self._run_alias_probe(probe_alias) if probe_alias else None
        return json_response(
            {
                "ok": True,
                "scope": resolved,
                "provider": None if resolved == "all" else provider_id,
                "model": model if resolved == "route" else None,
                "cleared": cleared,
                "cleared_count": len(cleared),
                "keys": [entry.get("key") for entry in cleared],
                "probe": probe,
            }
        )

    def _publish_limits(self, provider_id: str, observed_at: str) -> None:
        """Record the published limits as limit snapshots.

        Every snapshot carries a source and a confidence. A model with no
        published allowance is recorded as **unknown** - never as zero, and
        never as unlimited.
        """
        config = self.config.snapshot()
        provider = config.providers.get(provider_id)
        if provider is None:
            return
        for entry in provider.models.values():
            if entry.monthly_limit_usd is not None:
                self.limits.put(
                    LimitSnapshot(
                        provider=provider_id,
                        window=f"monthly:{entry.model_id}",
                        value=entry.monthly_limit_usd,
                        limit=entry.monthly_limit_usd,
                        observed_at=observed_at,
                        source=SOURCE_PUBLISHED_ESTIMATE,
                        confidence=CONFIDENCE_MEDIUM,
                        note="published monthly allowance for this model",
                    )
                )
            else:
                self.limits.put(
                    LimitSnapshot(
                        provider=provider_id,
                        window=f"monthly:{entry.model_id}",
                        value=None,
                        limit=None,
                        observed_at=observed_at,
                        source=SOURCE_UNKNOWN,
                        confidence=CONFIDENCE_NONE,
                        note="no published allowance for this model",
                    )
                )

    def _prune_pending_device(self) -> None:
        """Drop pending device authorisations that can no longer complete.

        The pending state is in memory only. It never outlives the process,
        and an entry that has expired is removed rather than left to answer
        a poll with a stale code.
        """
        now = time.monotonic()
        for handle in [
            handle
            for handle, authorization in self._pending_device.items()
            if authorization.expires_at <= now
        ]:
            self._pending_device.pop(handle, None)

    # --- model actions ---------------------------------------------------

    def handle_model_action(self, request: Request) -> Response:
        denied = self._require_mutation(request)
        if denied is not None:
            return denied
        try:
            payload = request.json() or {}
        except ValueError:
            payload = {}
        action = str(payload.get("action") or "")
        provider_id = str(payload.get("provider_id") or "")
        model_id = str(payload.get("model_id") or "")

        with self.config.mutate() as config:
            provider = config.providers.get(provider_id)
            if action in ("enable", "disable"):
                if provider is None:
                    return json_response({"error": {"message": "unknown provider", "type": "unknown_provider"}}, 404)
                entry = provider.models.get(model_id)
                if entry is None:
                    return json_response({"error": {"message": "unknown model", "type": "unknown_model"}}, 404)
                entry.enabled = action == "enable"
            elif action == "manual":
                if provider is None:
                    provider = _new_provider_state(provider_id, "manual")
                    config.providers[provider_id] = provider
                existing = provider.models.get(model_id)
                # A manual entry **merges**. It changes only the fields the
                # owner actually supplied, and preserves the published
                # metadata it has no opinion about. Rebuilding the entry
                # would silently blank a discovered model's context limit,
                # its reasoning levels and its capability flags - and the
                # README sends the owner here to fix a missing dialect.
                if existing is None:
                    entry = ModelEntry(
                        model_id=model_id,
                        enabled=bool(payload.get("enabled", True)),
                        notes="entered manually",
                    )
                else:
                    entry = existing
                    if isinstance(payload.get("enabled"), bool):
                        entry.enabled = payload["enabled"]
                dialect = payload.get("dialect")
                if isinstance(dialect, str) and dialect:
                    entry.dialect = dialect
                if isinstance(payload.get("notes"), str) and payload["notes"]:
                    entry.notes = payload["notes"]
                entry.source = "manual"
                entry.observed_at = utc_now_iso()
                provider.models[model_id] = entry
            else:
                return json_response(
                    {"error": {"message": f"unknown action '{action}'", "type": "unknown_action"}}, 400
                )
        return json_response({"ok": True})

    # --- alias groups ---------------------------------------------------

    def handle_alias_group_action(self, request: Request) -> Response:
        denied = self._require_mutation(request)
        if denied is not None:
            return denied
        try:
            payload = request.json() or {}
        except ValueError:
            payload = {}
        if not isinstance(payload, dict):
            return json_response({"error": {"message": "request body must be an object", "type": "invalid_request"}}, 400)
        from gateway.alias_groups import set_group_enabled

        action = payload.get("action")
        if action not in ("publish", "disable"):
            return json_response({"error": {"message": "action must be publish or disable", "type": "invalid_group_action"}}, 400)
        names: list[str] = []
        try:
            with self.config.mutate() as config:
                names = set_group_enabled(config, payload.get("tag"), action == "publish")
            return json_response({"ok": True, "tag": payload.get("tag"), "enabled": action == "publish", "aliases": names})
        except ValidationError as exc:
            return json_response({"error": {"message": exc.message, "type": "invalid_alias_group", "field": exc.field}}, 400)

    # --- alias actions ---------------------------------------------------

    def handle_alias_action(self, request: Request) -> Response:
        denied = self._require_mutation(request)
        if denied is not None:
            return denied
        try:
            payload = request.json() or {}
        except ValueError:
            payload = {}
        if not isinstance(payload, dict):
            return json_response({"error": {"message": "request body must be an object", "type": "invalid_request"}}, 400)
        action = str(payload.get("action") or "")
        name = str(payload.get("name") or "")

        if action == "save":
            # Everything is validated before a configuration transaction
            # opens, so a bad alias is refused rather than published.
            try:
                alias_name = validate_alias_name(payload.get("name"))
                primary, fallbacks = validate_alias_routes(
                    self.config, self.registry, payload.get("primary"), payload.get("fallbacks") or []
                )
                phase_routing = validate_phase_routing(
                    self.config, self.registry, payload.get("phase_routing")
                )
            except ValidationError as exc:
                return json_response(
                    {"error": {"message": exc.message, "type": "invalid_alias", "field": exc.field}},
                    400,
                )
            raw_tag = payload.get("tag", "")
            try:
                tag = validate_identifier(raw_tag, "alias tag") if raw_tag else ""
            except ValidationError as exc:
                return json_response(
                    {"error": {"message": exc.message, "type": "invalid_alias", "field": exc.field}}, 400
                )
            triggers = [t for t in payload.get("fallback_triggers") or [] if t in FALLBACK_TRIGGERS]
            confirmed = [p for p in payload.get("fallback_confirmed_providers") or [] if isinstance(p, str)]
            safe_classes = [
                c for c in payload.get("fallback_error_classes") or []
                if c in ("auth", "policy", "malformed")
            ]
            cooldown = payload.get("cooldown_seconds")
            budget = payload.get("budget")
            if budget is not None and (not isinstance(budget, (int, float)) or isinstance(budget, bool) or not math.isfinite(budget) or not 0 < budget <= 1_000_000):
                return json_response({"error": {"message": "budget must be a finite USD amount greater than 0 and at most 1000000, or null", "type": "invalid_budget"}}, 400)
            with self.config.mutate() as config:
                existing = config.aliases.get(alias_name)
                config.aliases[alias_name] = _alias_from_payload(
                    alias_name,
                    primary,
                    fallbacks,
                    triggers,
                    bool(payload.get("fallback_authorized", False)),
                    confirmed,
                    safe_classes,
                    cooldown if isinstance(cooldown, int) else 300,
                    enabled=bool(payload.get("enabled", existing.enabled if existing else False)),
                    phase_routing=phase_routing,
                    tag=tag,
                )
                config.aliases[alias_name].budget = budget
            return json_response({"ok": True})

        if action == "publish":
            # Publishing is the last chance to refuse an alias that cannot
            # be served, so the route is checked again here.
            with self.config.mutate() as config:
                alias = config.aliases.get(name)
                if alias is None:
                    return json_response({"error": {"message": "unknown alias", "type": "unknown_alias"}}, 404)
                if alias.primary is None:
                    return json_response(
                        {"error": {"message": "the alias has no primary route", "type": "alias_incomplete"}}, 409
                    )
                try:
                    validate_alias_routes(
                        self.config, self.registry, alias.primary.to_dict(), [r.to_dict() for r in alias.fallbacks]
                    )
                except ValidationError as exc:
                    return json_response(
                        {"error": {"message": exc.message, "type": "invalid_alias", "field": exc.field}}, 400
                    )
                alias.enabled = bool(payload.get("enabled", True))
            return json_response({"ok": True, "enabled": alias.enabled})

        if action == "delete":
            with self.config.mutate() as config:
                config.aliases.pop(name, None)
            return json_response({"ok": True})

        if action == "test":
            return self._test_alias(name, payload)

        return json_response({"error": {"message": f"unknown action '{action}'", "type": "unknown_action"}}, 400)

    def _test_alias(self, name: str, payload: dict[str, Any]) -> Response:
        """Run an explicit route test.

        The test sends a tiny prompt to the alias and reports the provider
        that served it. It does not store the prompt or the response.
        """
        result = self._run_alias_probe(name)
        error = result.get("error")
        if error is not None:
            status = {"unknown_alias": 404, "alias_incomplete": 409}.get(error["type"], 502)
            refusal = error.get("status")
            if isinstance(refusal, int) and not isinstance(refusal, bool):
                status = refusal
            return json_response({"error": error}, status)
        return json_response(result)

    def _run_alias_probe(self, name: str) -> dict[str, Any]:
        """Send one tiny explicit probe through an alias.

        Returns a metadata-only result: the provider and model that
        served it, whether a fallback was used, or the error class and
        message. Never a prompt, a response body, or a credential. This
        runs only as part of an explicit owner action - never on a page
        load or a configuration read.
        """
        config = self.config.snapshot()
        alias = config.aliases.get(name)
        if alias is None:
            return {"ok": False, "error": {"message": "unknown alias", "type": "unknown_alias"}}
        if alias.primary is None:
            return {
                "ok": False,
                "error": {"message": "the alias has no primary route", "type": "alias_incomplete"},
            }
        try:
            routed = self.router.invoke(
                name,
                {
                    "model": name,
                    "messages": [{"role": "user", "content": "Reply with the single word: ok"}],
                    "max_tokens": 16,
                    "stream": False,
                },
                source_dialect=DIALECT_OPENAI_CHAT,
                stream=False,
                request_id=new_request_id(),
            )
        except (RoutingError, AdapterError, ConversionError) as exc:
            message = getattr(exc, "message", str(exc))
            code = getattr(exc, "code", "route_test_failed")
            error: dict[str, Any] = {"message": message, "type": code}
            if code == "rate_limited" and getattr(exc, "status", None) == 429:
                # The gateway's own capacity refusal, not an upstream one:
                # report it here exactly as the inference path does.
                error["status"] = 429
            return {"ok": False, "error": error}
        return {
            "ok": True,
            "serving_provider": routed.attribution.serving_provider,
            "serving_model": routed.attribution.serving_model,
            "fallback_used": routed.attribution.fallback_used,
            "fallback_reason": routed.attribution.fallback_reason,
        }

    # --- profile actions -------------------------------------------------

    def handle_profile_action(self, request: Request) -> Response:
        denied = self._require_mutation(request)
        if denied is not None:
            return denied
        try:
            payload = request.json() or {}
        except ValueError:
            payload = {}
        action = str(payload.get("action") or "")
        profile_id = str(payload.get("profile_id") or "")

        if action == "save":
            try:
                profile_id = validate_profile_id(payload.get("profile_id"))
                name = str(payload.get("name") or "").strip()
                if not name:
                    raise ValidationError("a profile name is required", "name")
                allowed = []
                for alias in payload.get("allowed_aliases") or []:
                    allowed.append(validate_alias_name(alias))
            except ValidationError as exc:
                return json_response(
                    {"error": {"message": exc.message, "type": "invalid_profile", "field": exc.field}}, 400
                )
            # A new profile denies by default. Access to every published
            # alias has to be granted explicitly.
            all_aliases = bool(payload.get("all_aliases", False))
            with self.config.mutate() as config:
                existing = config.profiles.get(profile_id)
                if existing is None:
                    from gateway.config import Profile

                    existing = Profile(profile_id=profile_id, name=name)
                    config.profiles[profile_id] = existing
                existing.name = name
                existing.enabled = bool(payload.get("enabled", True))
                existing.allowed_aliases = allowed
                existing.all_aliases = all_aliases
            return json_response({"ok": True, "profile_id": profile_id})

        if action == "delete":
            with self.config.mutate() as config:
                config.profiles.pop(profile_id, None)
            self.profile_auth.revoke_token(profile_id)
            return json_response({"ok": True})

        if action == "issue_token":
            config = self.config.snapshot()
            profile = config.profiles.get(profile_id)
            if profile is None:
                return json_response({"error": {"message": "unknown profile", "type": "unknown_profile"}}, 404)
            token = self.profile_auth.issue_token(profile)
            with self.config.mutate() as cfg:
                target = cfg.profiles.get(profile_id)
                if target is not None:
                    target.has_token = True
                    target.token_created_at = utc_now_iso()
                    target.token_rotated_at = utc_now_iso()
            # The token is shown once, as the direct result of this action.
            return json_response({"ok": True, "token": token, "shown_once": True})

        if action == "revoke_token":
            self.profile_auth.revoke_token(profile_id)
            with self.config.mutate() as cfg:
                target = cfg.profiles.get(profile_id)
                if target is not None:
                    target.has_token = False
            return json_response({"ok": True})

        return json_response({"error": {"message": f"unknown action '{action}'", "type": "unknown_action"}}, 400)

    # --- settings --------------------------------------------------------

    def handle_settings_action(self, request: Request) -> Response:
        denied = self._require_mutation(request)
        if denied is not None:
            return denied
        try:
            payload = request.json() or {}
        except ValueError:
            payload = {}
        action = str(payload.get("action") or "")
        if action == "snapshot_interval":
            interval = payload.get("seconds")
            if not isinstance(interval, int) or isinstance(interval, bool) or interval not in (0, 3600, 21600, 86400):
                return json_response({"error": {"message": "snapshot interval must be 0, 3600, 21600, or 86400 seconds", "type": "invalid_setting"}}, 400)
            with self.config.mutate() as config:
                config.settings["snapshot_interval_seconds"] = interval
            if interval > 0:
                snapshot_dir = self.config.path.parent / "snapshots"
                stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
                atomic_write_json(snapshot_dir / f"config-{stamp}.json", self.config.snapshot().to_dict())
            return json_response({"ok": True, "snapshot_interval_seconds": interval})
        if action == "access_mode":
            return json_response(
                {"error": {"message": "trusted LAN mode is anonymous and cannot be changed to authenticated access", "type": "unsupported_access_mode"}},
                400,
            )
        if action == "initialize":
            with self.config.mutate() as config:
                config.initialized = True
            return json_response({"ok": True})
        return json_response({"error": {"message": f"unknown action '{action}'", "type": "unknown_action"}}, 400)


def _convert_buffered(body: bytes, source: str, target: str) -> bytes:
    """Convert a buffered response body between any two served dialects.

    The conversion itself lives in the bridge (:mod:`gateway.bridge.anthropic`,
    which also covers the OpenAI-family pairs). If the body cannot be
    converted it is returned unchanged rather than mangled: a caller can then
    see the upstream's own object, which is better than a silently wrong one.
    """
    return convert_buffered_bytes(body, source, target)


def _extract_caller_phase(payload: dict[str, Any]) -> str | None:
    """Read and **strip** the gateway-only phase hint from a request.

    The hint lives under ``metadata.personal_gateway.phase``. That namespace
    is gateway-owned, so the whole ``personal_gateway`` block is removed from
    the payload before the request is converted or forwarded: no upstream
    provider ever sees any of it, now or when a future field is added there.
    An invalid or unknown value is ignored (returns ``None``) rather than
    refused, so a client that sends a stale phase still gets the ordinary
    route.
    """
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        return None
    inner = metadata.get("personal_gateway")
    # Strip the whole gateway-owned namespace regardless of validity: none
    # of it may ever reach a provider.
    metadata.pop("personal_gateway", None)
    if not metadata:
        payload.pop("metadata", None)
    if not isinstance(inner, dict):
        return None
    phase = inner.get("phase")
    return phase if isinstance(phase, str) and phase in PHASES else None


def _validate_systemone_request(payload: Any) -> str | None:
    """Validate the JSON-only safe subset of OpenCode Console's native contract.

    Image assets and box questions are outside this JSON-only initial gateway
    boundary.  The submitted state/questions stay only in the
    request body and are never written to config, logs, or the ledger.
    """
    if not isinstance(payload, dict):
        return "request body is not valid JSON"
    model = payload.get("model")
    if not isinstance(model, str) or not model:
        return "request is missing a System One model"
    if "state" not in payload or not isinstance(payload["state"], (str, dict, list)):
        return "System One state must be a string, object, or array"
    if payload.get("assets") is not None:
        return "System One image assets are not supported by this gateway"
    questions = payload.get("questions")
    if not isinstance(questions, dict) or not questions:
        return "System One questions must be a non-empty object"
    for name, question in questions.items():
        if not isinstance(name, str) or not name or not isinstance(question, dict):
            return "each System One question must have a non-empty name and object value"
        kind = question.get("type")
        if kind not in ("noul", "choice", "score"):
            return "System One questions support only noul, choice, and score"
        criteria = question.get("criteria")
        if kind == "choice" and (not isinstance(criteria, dict) or not criteria):
            return "a System One choice question requires non-empty criteria"
        if kind == "score" and (not isinstance(criteria, list) or not criteria or len(criteria) > 10):
            return "a System One score question requires one to ten criteria"
    return None


def _alias_from_payload(
    name: str,
    primary: RouteRef | None,
    fallbacks: list[RouteRef],
    triggers: list[str],
    authorized: bool,
    confirmed: list[str],
    safe_classes: list[str],
    cooldown: int,
    enabled: bool,
    phase_routing=None,
    tag: str = "",
):
    from gateway.config import Alias

    return Alias(
        name=name,
        enabled=enabled,
        primary=primary,
        fallbacks=fallbacks,
        fallback_triggers=triggers,
        fallback_authorized=authorized,
        fallback_confirmed_providers=confirmed,
        fallback_error_classes=safe_classes,
        cooldown_seconds=cooldown,
        phase_routing=phase_routing,
        tag=tag,
    )


def _new_provider_state(provider_id: str, kind: str):
    from gateway.config import ProviderState

    return ProviderState(provider_id=provider_id, kind=kind)


def _read_static(name: str) -> bytes | None:
    path = STATIC_DIR / name
    try:
        return path.read_bytes()
    except OSError:
        return None
