"""One OpenAI-compatible adapter for the three local server flavours.

Ollama, llama.cpp's ``llama-server`` and LM Studio all publish the same
``/v1`` surface - ``GET /v1/models`` for the catalogue and
``POST /v1/chat/completions`` for inference - so a single adapter covers all
three and the flavour owns only the payload differences. The provider ids are
fixed (``ollama``, ``llama_cpp``, ``lm_studio``); the credential store stays
a closed allowlist and the routing core never learns a provider name.

What the adapter guarantees, and why:

* **The stored endpoint is the only destination.** It is re-read from the
  config store on every request (a saved URL takes effect without a restart)
  and every connection is peer-checked by :mod:`gateway.adapters.local_policy`
  before a byte is written. No URL is ever taken from a request.
* **No credential leaves this provider.** The adapter reads only its own
  handle. With no key enrolled there is no ``Authorization`` header at all -
  all three servers are keyless by default - and a caller's own bearer token
  never reaches this code path.
* **The payload is rewritten to what all three document.** The bridge renders
  ``max_completion_tokens``; none of the three documents it (llama-server
  reads it only from PR #19831, merged 2026-02-24), so it becomes
  ``max_tokens``, which all three document. Fields no local flavour documents
  (``tool_choice``, ``n``, ``user``, ``reasoning_effort``) are dropped rather
  than forwarded.
* **Capabilities are fail-closed.** ``/v1/models`` publishes no capability
  evidence on any of the three, so tools and image input start ``False`` and
  move only on an owner approval or, for Ollama, on its documented
  ``POST /api/show`` evidence. Cost is never a number: there is no tariff.
* **Cold loads get a real budget.** All three servers can take longer than
  the default 10 s connect budget to produce a first byte (on-demand model
  loading, idle unload, JIT loading), so this adapter builds its own client
  with a 60 s connect/stream-head budget.
"""

from __future__ import annotations

import json
import os
from typing import Any

from gateway.adapters.base import (
    AdapterError,
    ProviderRequest,
    ProviderResponse,
    TestResult,
    error_from_response,
)
from gateway.adapters.http_client import UpstreamClient, parse_json_body
from gateway.adapters.local_policy import make_peer_check
from gateway.config import STATE_CONNECTED, STATE_FAILED, ERROR_POLICY
from gateway.core.capabilities import DIALECT_OPENAI_CHAT, ModelInfo

#: The three fixed provider ids. Registration, the credential allowlist and
#: the console all key off exactly these.
FLAVORS = ("ollama", "llama_cpp", "lm_studio")

USER_AGENT = "personal-llm-gateway/2.0"

#: The connect/stream-head budget for a local server. Every one of the three
#: can exceed the global 10 s default before its first byte - a model that is
#: loaded on demand, unloaded when idle, or JIT-loaded - and a cold load is
#: not a stall. Configurable because "how long may I wait for my own server"
#: is an operator decision, not a library constant.
DEFAULT_CONNECT_TIMEOUT = 60.0
MIN_CONNECT_TIMEOUT = 10.0
MAX_CONNECT_TIMEOUT = 300.0
CONNECT_TIMEOUT_ENV = "GATEWAY_LOCAL_CONNECT_TIMEOUT"

#: The read budget is the gateway's normal one: a generation may legitimately
#: take minutes, and a progressing stream is never cut.
DEFAULT_READ_TIMEOUT = 120.0

#: Fields no local flavour documents. The bridge never sends them either;
#: they are dropped here so a future caller cannot smuggle an OpenAI-only
#: parameter into a server that answers 400 on unknown fields.
UNSUPPORTED_FIELDS = ("tool_choice", "n", "user", "reasoning_effort")

#: Human labels for owner-facing messages.
LABELS = {
    "ollama": "Ollama",
    "llama_cpp": "llama.cpp server",
    "lm_studio": "LM Studio",
}


def connect_timeout_from_env(environ: Any = None) -> float:
    """The configured cold-load budget, clamped to the documented range."""
    source = os.environ if environ is None else environ
    raw = source.get(CONNECT_TIMEOUT_ENV)
    if not isinstance(raw, str) or not raw.strip():
        return DEFAULT_CONNECT_TIMEOUT
    try:
        value = float(raw.strip())
    except ValueError:
        return DEFAULT_CONNECT_TIMEOUT
    if value != value:  # NaN
        return DEFAULT_CONNECT_TIMEOUT
    return max(MIN_CONNECT_TIMEOUT, min(MAX_CONNECT_TIMEOUT, value))


class LocalOpenAIAdapter:
    """The OpenAI-compatible adapter shared by ``ollama``, ``llama_cpp`` and ``lm_studio``."""

    def __init__(
        self,
        provider_id: str,
        credentials: Any,
        config: Any,
        flavor: str | None = None,
        client: UpstreamClient | None = None,
        connect_timeout: float | None = None,
    ) -> None:
        if provider_id not in FLAVORS:
            raise ValueError(f"unknown local provider id '{provider_id}'")
        resolved_flavor = flavor or provider_id
        if resolved_flavor not in FLAVORS:
            raise ValueError(f"unknown local flavour '{resolved_flavor}'")
        self.provider_id = provider_id
        self.kind = provider_id
        self._flavor = resolved_flavor
        self._label = LABELS[resolved_flavor]
        self._credentials = credentials
        self._config = config
        self._client = client or UpstreamClient(
            user_agent=USER_AGENT,
            timeout=DEFAULT_READ_TIMEOUT,
            connect_timeout=(
                connect_timeout
                if connect_timeout is not None
                else connect_timeout_from_env()
            ),
            peer_check=make_peer_check(provider_id),
        )

    # --- destination ---------------------------------------------------

    def _base_url(self) -> str:
        """The saved endpoint, or the policy refusal when there is none."""
        provider = self._config.snapshot().providers.get(self.provider_id)
        url = getattr(provider, "base_url", None) if provider is not None else None
        if not isinstance(url, str) or not url.strip():
            raise AdapterError(
                ERROR_POLICY,
                "no local endpoint is configured for this provider",
                provider_id=self.provider_id,
            )
        return url.rstrip("/")

    def _configured(self) -> bool:
        provider = self._config.snapshot().providers.get(self.provider_id)
        url = getattr(provider, "base_url", None) if provider is not None else None
        return isinstance(url, str) and bool(url.strip())

    def _endpoint(self, suffix: str) -> str:
        """Join the stored root with an OpenAI path (``/models`` → ``/v1/models``)."""
        return f"{self._base_url()}/v1{suffix}"

    # --- headers -------------------------------------------------------

    def _api_key(self) -> str | None:
        payload = self._credentials.read()
        if not payload:
            return None
        key = payload.get("api_key")
        return key if isinstance(key, str) and key else None

    def _headers(self, stream: bool = False, json_body: bool = False) -> dict[str, str]:
        headers = {
            "Accept": "text/event-stream" if stream else "application/json",
            "User-Agent": USER_AGENT,
        }
        if json_body:
            headers["Content-Type"] = "application/json"
        key = self._api_key()
        if key:
            # Present only when the owner enrolled a token. Keyless is the
            # default for all three servers, and no other provider's
            # credential is reachable from here.
            headers["Authorization"] = f"Bearer {key}"
        return headers

    # --- catalogue -----------------------------------------------------

    def list_models(self) -> list[ModelInfo]:
        """Fetch the server's model list explicitly. Never called implicitly."""
        response = self._client.request(
            "GET",
            self._endpoint("/models"),
            headers=self._headers(),
            provider_id=self.provider_id,
            # A catalogue read may be repeated freely.
            retry_safe=True,
        )
        self._raise_for_status(response, "the local model catalogue request")

        payload = parse_json_body(response.body)
        entries = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(entries, list):
            # A body that is not an OpenAI catalogue is an empty catalogue,
            # not an exception: the owner sees zero models and refreshes.
            return []
        models: list[ModelInfo] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            model_id = entry.get("id")
            if not isinstance(model_id, str) or not model_id:
                continue
            info = ModelInfo(
                model_id=model_id,
                dialect=DIALECT_OPENAI_CHAT,
                # Fail-closed: /v1/models publishes no capability evidence on
                # any of the three servers, so tools and image input stay
                # false until an owner approval (or, for Ollama, the
                # documented /api/show evidence) says otherwise.
                supports_streaming=True,
                supports_function_tools=False,
                supports_builtin_tools=False,
                supports_image_input=False,
                reasoning_efforts=(),
                context_window=None,
                context_limit=None,
                retention_class="unknown",
                monthly_limit_usd=None,
                limits_source="unknown",
                source="discovered",
                notes=f"{self._label} local model; capabilities and limits unknown until evidenced",
            )
            if self._flavor == "ollama":
                self._enrich_from_show(info)
            models.append(info)
        return models

    def _enrich_from_show(self, info: ModelInfo) -> None:
        """Apply Ollama's documented ``POST /api/show`` evidence, if it answers.

        Only two fields are read, both documented by Ollama: ``capabilities``
        (a list of feature names such as ``vision``) and ``model_info`` (which
        carries ``*.context_length``). Anything else in the body is ignored,
        and a server that does not answer - which is every non-Ollama server,
        and older Ollama builds - simply leaves the row as it was. This runs
        only inside an explicit refresh; boot and inference never call it.
        """
        try:
            body = json.dumps({"model": info.model_id}).encode("utf-8")
            response = self._client.request(
                "POST",
                f"{self._base_url()}/api/show",
                headers=self._headers(json_body=True),
                body=body,
                provider_id=self.provider_id,
                retry_safe=True,
            )
        except AdapterError:
            return
        if response.status != 200:
            return
        payload = parse_json_body(response.body)
        if not isinstance(payload, dict):
            return
        capabilities = payload.get("capabilities")
        if isinstance(capabilities, list) and "vision" in capabilities:
            # Documented evidence for image input. Nothing else in the list
            # is mapped: Ollama publishes no capability value for tool use,
            # so tools stay false until the owner approves them.
            info.supports_image_input = True
        model_info = payload.get("model_info")
        if isinstance(model_info, dict):
            for key, value in model_info.items():
                if isinstance(key, str) and key.endswith(".context_length") and isinstance(value, int):
                    if value > 0:
                        info.context_window = value
                        info.context_limit = value
                    break

    # --- connection test ------------------------------------------------

    def test_connection(self) -> TestResult:
        if not self._configured():
            return TestResult(
                ok=False,
                state=STATE_FAILED,
                message=f"No local endpoint is configured for {self._label}.",
                detail={"reason": "not_configured"},
            )
        try:
            models = self.list_models()
        except AdapterError as exc:
            return TestResult(ok=False, state=STATE_FAILED, message=exc.message, detail=exc.to_dict())
        return TestResult(
            ok=True,
            state=STATE_CONNECTED,
            message=f"{self._label} responded; {len(models)} models available.",
            detail={"model_count": len(models)},
        )

    # --- inference -------------------------------------------------------

    def invoke(self, request: ProviderRequest) -> ProviderResponse:
        if request.dialect and request.dialect != DIALECT_OPENAI_CHAT:
            raise AdapterError(
                ERROR_POLICY,
                f"{self._label} serves the OpenAI Chat dialect only",
                provider_id=self.provider_id,
            )
        payload = self._rewrite(request)
        headers = self._headers(stream=bool(request.stream), json_body=True)
        response = self._client.request(
            "POST",
            self._endpoint("/chat/completions"),
            headers=headers,
            body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            stream=bool(request.stream),
            provider_id=self.provider_id,
            # Inference is never retried: a repeat could bill twice or
            # duplicate a tool side effect after an ambiguous delivery.
            retry_safe=False,
        )
        self._raise_for_status(response, "the local inference request")
        return ProviderResponse(
            status=response.status,
            headers=response.headers,
            body=response.body,
            stream=response.stream,
            content_type=response.content_type,
            dialect=DIALECT_OPENAI_CHAT,
        )

    def _rewrite(self, request: ProviderRequest) -> dict[str, Any]:
        """Render the bridge's Chat payload as the shape all three document."""
        payload = dict(request.payload)
        # The alias never reaches the provider; the route's own model id does.
        payload["model"] = request.model
        # The response is read as streamed or buffered exactly as asked, so
        # the wire flag has to match the caller rather than the payload's.
        payload["stream"] = bool(request.stream)
        if not request.stream:
            payload.pop("stream_options", None)
        # Rewrite, never drop: the caller's output cap must survive. None of
        # the three documents max_completion_tokens, so the documented
        # spelling of the same request is max_tokens.
        if "max_completion_tokens" in payload:
            payload["max_tokens"] = payload.pop("max_completion_tokens")
        for field in UNSUPPORTED_FIELDS:
            payload.pop(field, None)
        return payload

    # --- helpers ---------------------------------------------------------

    def _raise_for_status(self, response: Any, operation: str) -> None:
        status = response.status
        if 300 <= status < 400:
            # The shared client already refuses to follow it and drops the
            # connection. A private endpoint that redirects is a
            # misconfiguration, never a destination to chase.
            raise AdapterError(
                ERROR_POLICY,
                "the configured local endpoint answered with a redirect; a local endpoint must respond directly",
                provider_id=self.provider_id,
                status=status,
            )
        if status >= 400:
            raise error_from_response(
                status,
                response.body,
                self.provider_id,
                parse_json_body(response.body),
                operation=operation,
                headers=response.headers,
            )


__all__ = [
    "FLAVORS",
    "LocalOpenAIAdapter",
    "DEFAULT_CONNECT_TIMEOUT",
    "connect_timeout_from_env",
]
