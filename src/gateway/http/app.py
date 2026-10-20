"""Single-listener trusted-LAN console and OpenAI-compatible API."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import time
from pathlib import Path
from typing import Any, Callable

from gateway.adapters.base import AdapterError
from gateway.adapters.chatgpt_codex import (
    ANTI_PHISHING,
    PREREQUISITE,
    ChatGPTCodexAdapter,
)
from gateway.auth import ProfileAuth, access_mode_description, profile_allows
from gateway.bridge.buffered import chat_body_to_responses, responses_body_to_chat
from gateway.bridge.model import ConversionError
from gateway.bridge.streams import convert_stream, sse_done
from gateway.config import (

    FALLBACK_TRIGGERS,
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
from gateway.core.capabilities import DIALECT_OPENAI_CHAT, DIALECT_OPENAI_RESPONSES
from gateway.core.routing import RoutingError, Router
from gateway.credentials import CredentialStore
from gateway.http.core import Request, Response, Router as HttpRouter, StreamResponse, json_response
from gateway.http.security import SecurityError, SecurityPolicy, check_forwarded_headers, check_host, check_origin
from gateway.identifiers import ValidationError, validate_alias_name, validate_profile_id
from gateway.logging import RequestLogger
from gateway.routes import validate_alias_routes
from gateway.observability import (
    CONFIDENCE_MEDIUM,
    CONFIDENCE_NONE,
    LimitSnapshot,
    LimitStore,
    RequestLedger,
    SOURCE_PUBLISHED_ESTIMATE,
    SOURCE_UNKNOWN,
)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

SURFACE_OWNER = "owner"



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
        self.credentials = credentials
        self.router = router
        self.registry = registry

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
        r.add(["GET"], "/favicon.ico", self.handle_favicon)

        r.add(["GET"], "/v1/models", self.handle_models)
        r.add(["POST"], "/v1/chat/completions", self.handle_chat_completions)
        r.add(["POST"], "/v1/responses", self.handle_responses)

        for prefix in self.mounts:
            r.add(["GET"], prefix + "/", self.handle_console)
            r.add(["GET"], prefix + "/index.html", self.handle_console)
            r.add(["GET"], prefix + "/app.js", self.handle_static_js)
            r.add(["GET"], prefix + "/style.css", self.handle_static_css)
            r.add(["GET"], prefix + "/api/status", self.handle_public_status)
            r.add(["GET"], prefix + "/api/state", self.handle_state)

            r.add(["POST"], prefix + "/api/provider", self.handle_provider_action)
            r.add(["POST"], prefix + "/api/model", self.handle_model_action)
            r.add(["POST"], prefix + "/api/alias", self.handle_alias_action)
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
            result = json_response(exc.to_dict(), exc.status)
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

    def handle_chat_completions(self, request: Request) -> Any:
        return self._handle_inference(request, DIALECT_OPENAI_CHAT)

    def handle_responses(self, request: Request) -> Any:
        return self._handle_inference(request, DIALECT_OPENAI_RESPONSES)

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

        stream = bool(payload.get("stream"))
        request_id = f"req_{int(time.time() * 1000):x}"
        session_hint = self._session_hint(request)

        routed = self.router.invoke(
            alias.name,
            payload,
            source_dialect=dialect,
            stream=stream,
            request_id=request_id,
            session_hint=session_hint,
            profile_id=profile.profile_id,
            client_ip=request.client_address[0],
        )

        # The attribution is recorded on the request so the access log can
        # name the alias and the provider that served it.
        request.context["attribution"] = routed.attribution.to_dict()
        request.context["streamed"] = stream

        if not stream:
            body = routed.response.body or b""
            # A converted request must receive a converted response. The
            # upstream dialect and the caller's dialect are the same for a
            # passthrough, and different when the bridge converted the
            # request.
            upstream_dialect = routed.request.dialect or dialect
            if upstream_dialect != dialect and body:
                body = _convert_buffered(body, upstream_dialect, dialect)
            return Response(
                routed.response.status,
                [("Content-Type", routed.response.content_type or "application/json")],
                body,
            )

        upstream_dialect = routed.request.dialect or dialect
        upstream_stream = routed.response.stream or iter(())

        def frames():
            if upstream_dialect == dialect:
                yield from upstream_stream
                return
            for frame in convert_stream(upstream_stream, upstream_dialect, dialect, routed.request.model):
                yield frame
            if dialect == DIALECT_OPENAI_CHAT:
                yield sse_done()

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
            aliases.append(
                {
                    "name": alias.name,
                    "enabled": alias.enabled,
                    "primary": alias.primary.to_dict() if alias.primary else None,
                    "fallbacks": [ref.to_dict() for ref in alias.fallbacks],
                    "fallback_triggers": list(alias.fallback_triggers),
                    "fallback_authorized": alias.fallback_authorized,
                    "fallback_confirmed_providers": list(alias.fallback_confirmed_providers),
                    "fallback_error_classes": list(alias.fallback_error_classes),
                    "cooldown_seconds": alias.cooldown_seconds,
                }
            )

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

        return json_response(
            {
                "initialized": config.initialized,
                "revision": config.revision,
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
                "ledger": self.ledger.recent(limit=25) if self.ledger else [],
                "totals": self.ledger.totals() if self.ledger else {},
                "cooldowns": self.router.cooldown.snapshot(),
                "release": self._release_identity() if self._release_identity else {},
                "base_url": self.base_url,
                "fallback_triggers": list(FALLBACK_TRIGGERS),
                "device_code": {
                    "prerequisite": PREREQUISITE,
                    "anti_phishing": ANTI_PHISHING,
                },
                "checklist": self._checklist(config),
            }
        )

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
                        retention_class=info.retention_class,
                        monthly_limit_usd=info.monthly_limit_usd,
                        limits_source=info.limits_source,
                        limits_observed_at=info.limits_observed_at,
                    )
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
                dialect = payload.get("dialect")
                provider.models[model_id] = ModelEntry(
                    model_id=model_id,
                    enabled=existing.enabled if existing else bool(payload.get("enabled", True)),
                    dialect=dialect if isinstance(dialect, str) else (existing.dialect if existing else None),
                    source="manual",
                    observed_at=utc_now_iso(),
                    notes="entered manually",
                    retention_class=existing.retention_class if existing else "unknown",
                    limits_source="unknown",
                )
            else:
                return json_response(
                    {"error": {"message": f"unknown action '{action}'", "type": "unknown_action"}}, 400
                )
        return json_response({"ok": True})

    # --- alias actions ---------------------------------------------------

    def handle_alias_action(self, request: Request) -> Response:
        denied = self._require_mutation(request)
        if denied is not None:
            return denied
        try:
            payload = request.json() or {}
        except ValueError:
            payload = {}
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
            except ValidationError as exc:
                return json_response(
                    {"error": {"message": exc.message, "type": "invalid_alias", "field": exc.field}},
                    400,
                )
            triggers = [t for t in payload.get("fallback_triggers") or [] if t in FALLBACK_TRIGGERS]
            confirmed = [p for p in payload.get("fallback_confirmed_providers") or [] if isinstance(p, str)]
            safe_classes = [
                c for c in payload.get("fallback_error_classes") or []
                if c in ("auth", "policy", "malformed")
            ]
            cooldown = payload.get("cooldown_seconds")
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
                )
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
        config = self.config.snapshot()
        alias = config.aliases.get(name)
        if alias is None:
            return json_response({"error": {"message": "unknown alias", "type": "unknown_alias"}}, 404)
        if alias.primary is None:
            return json_response(
                {"error": {"message": "the alias has no primary route", "type": "alias_incomplete"}}, 409
            )
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
                request_id="route_test",
            )
        except (RoutingError, AdapterError, ConversionError) as exc:
            message = getattr(exc, "message", str(exc))
            code = getattr(exc, "code", "route_test_failed")
            return json_response({"error": {"message": message, "type": code}}, 502)
        return json_response(
            {
                "ok": True,
                "serving_provider": routed.attribution.serving_provider,
                "serving_model": routed.attribution.serving_model,
                "fallback_used": routed.attribution.fallback_used,
                "fallback_reason": routed.attribution.fallback_reason,
            }
        )

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
    """Convert a buffered response body between the two dialects.

    If the body cannot be converted it is returned unchanged rather than
    mangled: a caller can then see the upstream's own object, which is
    better than a silently wrong one.
    """
    try:
        payload = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return body
    if not isinstance(payload, dict):
        return body
    try:
        if source == DIALECT_OPENAI_RESPONSES and target == DIALECT_OPENAI_CHAT:
            converted = responses_body_to_chat(payload)
        elif source == DIALECT_OPENAI_CHAT and target == DIALECT_OPENAI_RESPONSES:
            converted = chat_body_to_responses(payload)
        else:
            return body
    except Exception:  # pragma: no cover - defensive
        return body
    return json.dumps(converted, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


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
