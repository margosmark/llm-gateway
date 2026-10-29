"""Single-listener trusted-LAN console and OpenAI-compatible API."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
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
from gateway.auth import ProfileAuth, access_mode_description, profile_allows
from gateway.bridge.anthropic import aggregate_stream_any, convert_buffered_bytes, convert_stream_bytes
from gateway.bridge.model import ConversionError
from gateway.bridge.streams import sse_done
from gateway.backup import export_config, import_config, snapshot_interval_seconds
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
from gateway.statistics import aggregate_statistics
from gateway.provider_health import provider_health
from gateway.stores import atomic_write_json
from gateway.capability_discovery import (
    APPROVABLE_METADATA_FIELDS,
    EVIDENCE_CLASSIFICATIONS,
    DiscoveryStore,
    preview_for,
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
from gateway.http.security import SecurityError, SecurityPolicy, check_forwarded_headers, check_host, check_origin
from gateway.identifiers import ValidationError, validate_alias_name, validate_identifier, validate_profile_id
from gateway.logging import RequestLogger
from gateway.ledger_export import DEFAULT_EXPORT_ROWS, MAX_EXPORT_ROWS, export_records, render_csv, render_json
from gateway.metrics import render_metrics
from gateway.routes import validate_alias_routes, validate_phase_routing
from gateway.provider_concurrency import CappedAdapterRegistry, ProviderConcurrency
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
        release_identity: Callable[[], dict[str, str]] | None = None,
        base_url: str = "",
        mounts: tuple[str, ...] = ("/owner",),
        logger: RequestLogger | None = None,
    ) -> None:
        self.config = config
        self.discovery = DiscoveryStore(config.path.parent / "capability_discovery.json")
        self.credentials = credentials
        self.router = router
        self.registry = registry
        self.router._registry = CappedAdapterRegistry(registry, ProviderConcurrency())

        self.profile_auth = profile_auth
        self.policy = policy
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
            r.add(["GET"], prefix + "/api/ledger/export", self.handle_ledger_export)
            r.add(["GET"], prefix + "/api/backup/export", self.handle_backup_export)
            r.add(["POST"], prefix + "/api/backup/import", self.handle_backup_import)
            r.add(["POST"], prefix + "/api/preview", self.handle_preview)

            r.add(["GET"], prefix + "/api/capabilities", self.handle_capability_read)
            r.add(["POST"], prefix + "/api/capabilities/preview", self.handle_capability_preview)
            r.add(["POST"], prefix + "/api/capabilities/run", self.handle_capability_run)
            r.add(["POST"], prefix + "/api/capabilities/decision", self.handle_capability_decision)
            r.add(["POST"], prefix + "/api/provider", self.handle_provider_action)
            r.add(["POST"], prefix + "/api/model", self.handle_model_action)
            r.add(["POST"], prefix + "/api/alias", self.handle_alias_action)
            r.add(["POST"], prefix + "/api/alias-group", self.handle_alias_group_action)
            r.add(["POST"], prefix + "/api/profile", self.handle_profile_action)
            r.add(["POST"], prefix + "/api/settings", self.handle_settings_action)

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

        # CSRF applies to the cookie-authenticated browser surfaces only.
        # The API authenticates with bearer tokens, which a browser never
        # sends ambiently, so it is not CSRF-relevant there.
        try:
            result = route.handler(request)
        except SecurityError as exc:
            result = exc.to_response()
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
        )

    # --- surface helpers -------------------------------------------------

    def _surface(self, request: Request) -> str:
        return request.context.get("surface", SURFACE_OWNER)



    def _can_mutate(self, request: Request) -> bool:
        """Whether this request may change configuration."""
        return True

    def _can_enroll_credentials(self, request: Request) -> bool:
        """Every reachable trusted-LAN peer is an administrator."""
        return True

    def _require_mutation(self, request: Request) -> Any:
        return None

    # --- metadata --------------------------------------------------------

    def handle_version(self, request: Request) -> Response:
        identity = self._release_identity() if self._release_identity else {
            "service": "personal-llm-gateway",
            "source_revision": "unknown",
            "source_tree": "unknown",
            "image_revision": "unknown",
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
        """Safe, unauthenticated status.

        This is the only console data reachable without a session. It
        deliberately excludes profile records, the configuration revision,
        routing detail, the request ledger and the credential store state,
        because the live deployment's disclosure class was exactly that.
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

    def handle_state(self, request: Request) -> Response:
        config = self.config.snapshot()
        may_mutate = self._can_mutate(request)

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
            alias_payload["budget_status"] = budget_status(self.ledger.recent(limit=10_000) if self.ledger else [], alias.name, alias.budget, history_complete=bool(self.ledger and self.ledger.evicted == 0))
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

        price_snapshot = published_prices()
        recent_ledger = self.ledger.recent(limit=25) if self.ledger else []
        usage_ledger = self.ledger.recent(limit=10_000) if self.ledger else []
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
                "authenticated": False,
                "providers": providers,
                "aliases": aliases,
                "profiles": profiles,
                "published_models": [alias.name for alias in sorted(config.published_aliases(), key=lambda a: a.name)],
                "limits": self.limits.all(),
                "ledger": annotate_events(recent_ledger, _prices=price_snapshot),
                "totals": self.ledger.totals() if self.ledger else {},
                "usage_costs": aggregate_usage(usage_ledger, window_hours=24, evicted_events=self.ledger.evicted, _prices=price_snapshot) if self.ledger else {"window_hours": 24, "coverage": {"source": "bounded_recent_events", "events_considered": 0, "incomplete": False}, "alias": {}, "provider": {}, "model": {}},
                "analytics": analytics,
                "request_statistics": {window: aggregate_statistics(self.ledger.recent(limit=10_000), window) for window in ("hour", "day", "month")} if self.ledger else {},
                "provider_health": provider_health(providers, self.router.circuits.details(), self.ledger.recent(limit=10_000) if self.ledger else []),
                "ledger_durability": self.ledger.durability() if self.ledger else {"enabled": False, "retention_days": 30, "billing_system": False},
                "cooldowns": self.router.circuits.snapshot(),
                "cooldown_details": self.router.circuits.details(),
                "cooldown_policy": {
                    "exhausted_base_seconds": EXHAUSTED_BASE_SECONDS,
                    "exhausted_max_seconds": self.router.circuits.exhausted_max_seconds,
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
        return json_response(self.discovery.read(provider_id, model_id))

    def handle_capability_preview(self, request: Request) -> Response:
        denied = self._require_mutation(request)
        if denied is not None:
            return denied
        try:
            payload = request.json()
            provider_id, model_id, probes = payload["provider_id"], payload["model_id"], payload["probes"]
            if not isinstance(provider_id, str) or not isinstance(model_id, str) or not isinstance(probes, list):
                raise ValueError
            config = self.config.snapshot()
            provider = config.providers.get(provider_id)
            if provider is None or model_id not in provider.models:
                return json_response({"error": {"message": "Exact provider/model is not staged", "type": "unknown_model"}}, 404)
            result = preview_for(provider_id, model_id, probes, config.revision)
            return json_response(result, 200 if result["available"] else 422)
        except (ValueError, TypeError, KeyError):
            return json_response({"error": {"message": "Invalid capability preview request", "type": "invalid_request"}}, 400)

    def handle_capability_run(self, request: Request) -> Response:
        denied = self._require_mutation(request)
        if denied is not None:
            return denied
        # Real provider transports intentionally remain disabled until a reviewed,
        # bounded and priced adapter contract is registered. Never treat a client
        # confirmation as permission to call arbitrary provider transports.
        return json_response({"error": {"message": "No provider probe protocol is approved; no upstream request was sent", "type": "probe_unavailable"}}, 422)

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
            proposal = self.discovery.proposal(proposal_id)
            if proposal is None:
                return json_response({"error": {"message": "Unknown capability proposal", "type": "unknown_proposal"}}, 404)
            provider_id, model_id = proposal["provider_id"], proposal["model_id"]
            with self.config.mutate() as config:
                provider = config.providers.get(provider_id)
                row = provider.models.get(model_id) if provider else None
                if row is None:
                    return json_response({"error": {"message": "Proposal target no longer exists", "type": "unknown_model"}}, 404)
                current = row.to_dict()
                actual_fingerprint = hashlib.sha256(json.dumps(current, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                if config.revision != revision or actual_fingerprint != fingerprint:
                    raise RuntimeError("stale config")
                if proposal["base_revision"] != revision or proposal["base_model_fingerprint"] != fingerprint:
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
                result = self.discovery.decide(proposal_id, decision_id, decisions, revision, fingerprint)
                approvals = config.settings.setdefault("capability_approvals", {})
                target = approvals.setdefault(provider_id, {}).setdefault(model_id, {})
                for field, value, classification in approved_writes:
                    setattr(row, field, value)
                    target[field] = {"value": value, "source": "capability_discovery_observation",
                                     "proposal_id": proposal_id, "decision_id": decision_id,
                                     "approved_at": utc_now_iso(), "classification": classification}
                if not target:
                    approvals.get(provider_id, {}).pop(model_id, None)
                result["revision"] = config.revision + 1
            return json_response(result)
        except (ValueError, TypeError, KeyError):
            return json_response({"error": {"message": "Invalid capability decision", "type": "invalid_request"}}, 400)
        except RuntimeError as exc:
            return json_response({"error": {"message": "Capability proposal conflict or resolved", "type": "conflict"}}, 409)
        except Exception:
            return json_response({"error": {"message": "Capability decision could not be applied", "type": "invalid_request"}}, 400)

    # --- provider actions ------------------------------------------------

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

        if action == "clear_cooldowns":
            # The deliberate recovery action. The owner has confirmed the
            # subscription/reset window is open, so the local circuits for
            # exactly this provider are cleared. It never touches another
            # provider's circuits, and it returns only metadata: the
            # removed circuit entries, never a credential or request
            # content. No provider is called unless the owner also asks
            # for one explicit probe by naming its alias.
            cleared = self.router.circuits.clear(provider=provider_id)
            probe_alias = str(payload.get("probe_alias") or "")
            probe = self._run_alias_probe(probe_alias) if probe_alias else None
            return json_response(
                {
                    "ok": True,
                    "provider": provider_id,
                    "cleared": cleared,
                    "cleared_count": len(cleared),
                    "probe": probe,
                }
            )

        return json_response(
            {"error": {"message": f"unknown action '{action}'", "type": "unknown_action"}}, 400
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
            return {"ok": False, "error": {"message": message, "type": code}}
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
