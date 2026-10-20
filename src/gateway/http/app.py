"""The gateway application.

One application, two browser surfaces, one API surface.

* ``/owner/`` - the owner console, bound to the LAN address.
* ``/setup/`` - the setup UI, bound to loopback only. This is the only
  surface where a credential may be enrolled.
* ``/v1/...`` - the OpenAI-compatible API.
* ``/version``, ``/health`` - anonymous metadata.

Both browser surfaces are served the same application. The difference is a
capability set: the setup surface may enroll credentials, and the owner
surface may do so only when an owner session is present. The console is
never *casually reachable* into a credential form.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable

from gateway.adapters.base import AdapterError
from gateway.adapters.chatgpt_codex import (
    ANTI_PHISHING,
    PREREQUISITE,
    ChatGPTCodexAdapter,
)
from gateway.auth import (
    ANONYMOUS_PROFILE_ID,
    OwnerAuth,
    ProfileAuth,
    access_mode_description,
    parse_cookies,
    profile_allows,
)
from gateway.bridge.model import ConversionError
from gateway.bridge.streams import convert_stream, sse_done
from gateway.config import (
    ACCESS_AUTHENTICATED,
    ACCESS_LAN_NOAUTH,
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
from gateway.http.security import (
    CSRF_COOKIE,
    MUTATING_METHODS,
    SESSION_COOKIE,
    SecurityError,
    SecurityPolicy,
    check_csrf,
    check_forwarded_headers,
    check_host,
    check_origin,
    require_loopback,
)
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
SURFACE_SETUP = "setup"


class GatewayApp:
    """The HTTP application."""

    def __init__(
        self,
        config: ConfigStore,
        credentials: CredentialStore,
        router: Router,
        registry,
        owner_auth: OwnerAuth,
        profile_auth: ProfileAuth,
        policy: SecurityPolicy,
        ledger: RequestLedger | None = None,
        limits: LimitStore | None = None,
        release_identity: Callable[[], dict[str, str]] | None = None,
        base_url: str = "",
        mounts: tuple[str, ...] = ("/owner",),
    ) -> None:
        self.config = config
        self.credentials = credentials
        self.router = router
        self.registry = registry
        self.owner_auth = owner_auth
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
        self._routes = HttpRouter()
        self._build_routes()

    # --- routing table ---------------------------------------------------

    def _build_routes(self) -> None:
        r = self._routes
        r.add(["GET"], "/version", self.handle_version)
        r.add(["GET"], "/health", self.handle_health)

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
            r.add(["GET"], prefix + "/api/session", self.handle_session_info)
            r.add(["POST"], prefix + "/api/session", self.handle_login)
            r.add(["DELETE"], prefix + "/api/session", self.handle_logout)
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
            require_loopback(request, self.policy)
        except SecurityError as exc:
            return exc.to_response()

        route, params, path_matched = self._routes.match(request.method, request.path)
        if route is None:
            if path_matched:
                allowed = self._routes.allowed_methods(request.path)
                return json_response(
                    {"error": {"message": "method not allowed", "type": "method_not_allowed", "allowed": allowed}},
                    405,
                    [("Allow", ", ".join(allowed))],
                )
            return json_response({"error": {"message": "not found", "type": "not_found"}}, 404)

        request.params = params
        # The two listeners are separate applications: the LAN listener
        # serves "/owner", the loopback listener serves "/setup".
        request.context["surface"] = SURFACE_SETUP if "/setup" in self.mounts else SURFACE_OWNER

        # CSRF applies to the cookie-authenticated browser surfaces only.
        # The API authenticates with bearer tokens, which a browser never
        # sends ambiently, so it is not CSRF-relevant there.
        if (
            self.policy.is_browser_surface(request.path)
            and request.method in MUTATING_METHODS
            and not self._is_login_endpoint(request)
        ):
            session = self._session(request)
            if session is None:
                return json_response(
                    {"error": {"message": "an owner session is required", "type": "owner_session_required"}},
                    401,
                )
            try:
                check_csrf(request, session.csrf)
            except SecurityError as exc:
                return exc.to_response()

        try:
            return route.handler(request)
        except SecurityError as exc:
            return exc.to_response()
        except RoutingError as exc:
            return json_response(exc.to_dict(), exc.status)
        except ConversionError as exc:
            return json_response(
                {"error": {"message": exc.message, "type": "conversion_refused", "capability": exc.capability}},
                422,
            )
        except AdapterError as exc:
            return json_response({"error": {"message": exc.message, "type": exc.error_class}}, 502)
        except Exception:  # pragma: no cover - defensive
            return json_response({"error": {"message": "internal error", "type": "gateway_error"}}, 500)

    # --- surface helpers -------------------------------------------------

    def _surface(self, request: Request) -> str:
        return request.context.get("surface", SURFACE_OWNER)

    def _is_login_endpoint(self, request: Request) -> bool:
        """The login endpoint establishes the session, so it cannot require one.

        It is protected by the owner passphrase instead. A cross-site login
        request cannot do anything the attacker could not do by guessing the
        passphrase, and the passphrase is the only secret it carries.
        """
        return request.method == "POST" and request.path.endswith("/api/session")

    def _session(self, request: Request):
        cookies = parse_cookies(request.headers.get("cookie"))
        return self.owner_auth.session_from_cookies(cookies)

    def _expected_csrf(self, request: Request) -> str | None:
        """The CSRF token the browser should have presented, if any."""
        if request.method not in MUTATING_METHODS:
            return None
        if not self.policy.is_browser_surface(request.path):
            return None
        session = self._session(request)
        return session.csrf if session else None

    def _require_owner_session(self, request: Request) -> Any:
        """Return an error response if there is no owner session."""
        if self._session(request) is None:
            return json_response(
                {"error": {"message": "an owner session is required", "type": "owner_session_required"}},
                401,
            )
        return None

    def _can_mutate(self, request: Request) -> bool:
        """Whether this request may change configuration."""
        if self._surface(request) == SURFACE_SETUP:
            return self._session(request) is not None
        config = self.config.snapshot()
        if config.access_mode == ACCESS_AUTHENTICATED:
            return self._session(request) is not None
        # LAN no-auth mode: the console may not change anything from the LAN.
        return False

    def _can_enroll_credentials(self, request: Request) -> bool:
        """Credential enrollment is loopback-only, always."""
        return self._surface(request) == SURFACE_SETUP and self._session(request) is not None

    def _require_mutation(self, request: Request) -> Any:
        if self._can_mutate(request):
            return None
        return json_response(
            {
                "error": {
                    "message": (
                        "this action requires the setup UI, which is reachable from the host itself "
                        "or through an SSH local tunnel"
                    ),
                    "type": "mutation_not_permitted",
                }
            },
            403,
        )

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
        session_id = request.headers.get("x-session-id") or request.headers.get("x-opencode-session")

        routed = self.router.invoke(
            alias.name,
            payload,
            source_dialect=dialect,
            stream=stream,
            request_id=request_id,
            session_id=session_id,
            profile_id=profile.profile_id,
            client_ip=request.client_address[0],
        )

        if not stream:
            body = routed.response.body or b""
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

    def handle_session_info(self, request: Request) -> Response:
        config = self.config.snapshot()
        session = self._session(request)
        return json_response(
            {
                "authenticated": session is not None,
                "access_mode": config.access_mode,
                "access_mode_description": access_mode_description(config.access_mode),
                "passphrase_set": self.owner_auth.passphrase_is_set(),
                "surface": self._surface(request),
                "can_mutate": self._can_mutate(request),
                "can_enroll_credentials": self._can_enroll_credentials(request),
            }
        )

    def handle_login(self, request: Request) -> Response:
        try:
            payload = request.json() or {}
        except ValueError:
            payload = {}
        passphrase = payload.get("passphrase")
        if not isinstance(passphrase, str) or not passphrase:
            return json_response(
                {"error": {"message": "a passphrase is required", "type": "malformed_request"}}, 400
            )
        if not self.owner_auth.passphrase_is_set():
            # First run: the owner chooses the passphrase. This is only
            # possible from the setup UI. Otherwise the first host to reach
            # the LAN console could claim the gateway.
            if self._surface(request) != SURFACE_SETUP:
                return json_response(
                    {
                        "error": {
                            "message": (
                                "the owner passphrase is created in the setup UI, which is reachable "
                                "from the host itself or through an SSH local tunnel"
                            ),
                            "type": "passphrase_creation_forbidden",
                        }
                    },
                    403,
                )
            self.owner_auth.set_passphrase(passphrase)
            session = self.owner_auth.create_session()
            return Response(
                200,
                [("Content-Type", "application/json; charset=utf-8"), *self.owner_auth.session_cookies(session)],
                json.dumps({"ok": True, "created": True, "csrf": session.csrf}).encode("utf-8"),
            )
        if not self.owner_auth.check_passphrase(passphrase):
            return json_response(
                {"error": {"message": "the passphrase was not accepted", "type": "unauthorised"}}, 401
            )
        session = self.owner_auth.create_session()
        return Response(
            200,
            [("Content-Type", "application/json; charset=utf-8"), *self.owner_auth.session_cookies(session)],
            json.dumps({"ok": True, "created": False, "csrf": session.csrf}).encode("utf-8"),
        )

    def handle_logout(self, request: Request) -> Response:
        session = self._session(request)
        if session is not None:
            self.owner_auth.destroy_session(session.token)
        return Response(
            200,
            [
                ("Content-Type", "application/json; charset=utf-8"),
                ("Set-Cookie", f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"),
                ("Set-Cookie", f"{CSRF_COOKIE}=; Path=/; SameSite=Strict; Max-Age=0"),
            ],
            b'{"ok":true}',
        )

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
        session = self._session(request)
        if session is None:
            return json_response(
                {"error": {"message": "an owner session is required", "type": "owner_session_required"}},
                401,
            )
        config = self.config.snapshot()
        session = self._session(request)
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
                "authenticated": session is not None,
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
            authorization = adapter.start_device_authorization()
            self._pending_device[authorization.device_auth_id] = authorization
            return json_response(authorization.to_dict())

        if action == "device_poll":
            if not isinstance(adapter, ChatGPTCodexAdapter):
                return json_response(
                    {"error": {"message": "this provider has no device-code flow", "type": "unsupported_action"}},
                    400,
                )
            device_auth_id = str(payload.get("device_auth_id") or "")
            authorization = self._pending_device.get(device_auth_id)
            if authorization is None:
                return json_response(
                    {"error": {"message": "that authorisation is no longer pending", "type": "unknown_authorization"}},
                    404,
                )
            authorization = adapter.poll_device_authorization(authorization)
            if authorization.state in ("approved", "denied", "expired"):
                self._pending_device.pop(device_auth_id, None)
                if authorization.state == "approved":
                    with self.config.mutate() as config:
                        provider = config.providers.get(provider_id)
                        if provider is None:
                            provider = _new_provider_state(provider_id, adapter.kind)
                            config.providers[provider_id] = provider
                        provider.state = STATE_CONFIGURED_UNTESTED
            result = authorization.to_dict()
            # The one-time code is only echoed while the flow is pending.
            if authorization.state != "pending":
                result.pop("user_code", None)
            return json_response(result)

        if action == "enroll_key":
            if not self._can_enroll_credentials(request):
                return json_response(
                    {
                        "error": {
                            "message": "credentials may only be enrolled from the setup UI on the host itself",
                            "type": "credential_enroll_forbidden",
                        }
                    },
                    403,
                )
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

    _pending_device: dict[str, Any] = {}

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
            primary = RouteRef.from_dict(payload.get("primary"))
            fallbacks = []
            for item in payload.get("fallbacks") or []:
                ref = RouteRef.from_dict(item)
                if ref is not None:
                    fallbacks.append(ref)
            triggers = [t for t in payload.get("fallback_triggers") or [] if t in FALLBACK_TRIGGERS]
            confirmed = [p for p in payload.get("fallback_confirmed_providers") or [] if isinstance(p, str)]
            safe_classes = [
                c for c in payload.get("fallback_error_classes") or []
                if c in ("auth", "policy", "malformed")
            ]
            cooldown = payload.get("cooldown_seconds")
            with self.config.mutate() as config:
                existing = config.aliases.get(name)
                config.aliases[name] = _alias_from_payload(
                    name,
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
            with self.config.mutate() as config:
                alias = config.aliases.get(name)
                if alias is None:
                    return json_response({"error": {"message": "unknown alias", "type": "unknown_alias"}}, 404)
                if alias.primary is None:
                    return json_response(
                        {"error": {"message": "the alias has no primary route", "type": "alias_incomplete"}}, 409
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
            name = str(payload.get("name") or "").strip()
            if not name:
                return json_response({"error": {"message": "a profile name is required", "type": "malformed_request"}}, 400)
            allowed = [a for a in payload.get("allowed_aliases") or [] if isinstance(a, str)]
            with self.config.mutate() as config:
                existing = config.profiles.get(profile_id)
                if existing is None:
                    from gateway.config import Profile

                    existing = Profile(profile_id=profile_id, name=name)
                    config.profiles[profile_id] = existing
                existing.name = name
                existing.enabled = bool(payload.get("enabled", True))
                existing.allowed_aliases = allowed
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
            mode = str(payload.get("access_mode") or "")
            if mode not in (ACCESS_LAN_NOAUTH, ACCESS_AUTHENTICATED):
                return json_response(
                    {"error": {"message": "unknown access mode", "type": "malformed_request"}}, 400
                )
            with self.config.mutate() as config:
                config.access_mode = mode
                if mode == ACCESS_AUTHENTICATED and ANONYMOUS_PROFILE_ID not in config.profiles:
                    from gateway.config import Profile

                    config.profiles[ANONYMOUS_PROFILE_ID] = Profile(
                        profile_id=ANONYMOUS_PROFILE_ID,
                        name="Anonymous LAN profile",
                        enabled=True,
                        anonymous=True,
                    )
            return json_response({"ok": True, "access_mode": mode})
        if action == "initialize":
            with self.config.mutate() as config:
                config.initialized = True
            return json_response({"ok": True})
        return json_response({"error": {"message": f"unknown action '{action}'", "type": "unknown_action"}}, 400)


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
