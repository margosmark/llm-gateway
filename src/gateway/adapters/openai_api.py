"""The plain OpenAI API-key adapter.

A standard OpenAI platform upstream, reached with a bearer token:

======================================  ====================================
catalogue                               ``GET  /v1/models``
Chat Completions inference              ``POST /v1/chat/completions``
Responses inference                     ``POST /v1/responses``
======================================  ====================================

**Established from** OpenAI's official API reference at
``developers.openai.com/api/reference`` (Create chat completion, Create
response, List models) and from the absence of a machine-readable
capability endpoint (a 2026-09 feature request for
``GET /v1/models/{model}/capabilities`` on the OpenAI community forum).

Two consequences of that contract shape the adapter:

* **The catalogue publishes no dialect, no capability flags, no context
  window, no limit and no retention class.** ``/v1/models`` answers with
  ``id``/``object``/``created``/``owned_by`` only. So a discovered model is
  recorded with an *unknown* dialect and Unknown for every published fact -
  it is refused rather than guessed at until the owner sets the model's
  dialect (the router already excludes an undocumented dialect, and this
  adapter refuses it again at ``invoke`` as defence in depth). The owner's
  dialect edit survives a later refresh, because the provider publishes
  nothing for that field.
* **There is no documented conversation-identifier header.** The derived
  session id in :class:`~gateway.adapters.base.ProviderRequest` is
  therefore not forwarded; nothing caller-controlled reaches the upstream
  under a session name.

Both wire dialects this gateway serves are OpenAI dialects, so a route to
this provider needs no conversion in the common case: a Chat Completions
client reaches ``/v1/chat/completions`` and a Responses client reaches
``/v1/responses``. The bridge still converts in the cross case, on both
the buffered and the streaming path.

Safety rules this module follows:

* No credential, no request body and no upstream body text is ever echoed
  in an error: classification uses the shared vocabulary in
  :mod:`gateway.adapters.base` and a fixed message per class.
* Without a stored key the adapter refuses locally with the shared
  ``auth`` class instead of spending a doomed request.
* ``list_models()`` is only ever reached through an explicit owner action
  (Test or Refresh); nothing in a page load, a status read or a
  configuration save calls it.
"""

from __future__ import annotations

import json

from gateway.adapters.base import (
    AdapterError,
    ProviderRequest,
    ProviderResponse,
    TestResult,
    error_from_response,
)
from gateway.adapters.http_client import UpstreamClient, parse_json_body
from gateway.config import ERROR_AUTH, ERROR_POLICY, STATE_CONNECTED, STATE_FAILED
from gateway.core.capabilities import (
    DIALECT_OPENAI_CHAT,
    DIALECT_OPENAI_RESPONSES,
    ModelInfo,
)

PROVIDER_ID = "openai_api"
KIND = "openai_api"

DEFAULT_BASE_URL = "https://api.openai.com"
MODELS_PATH = "/v1/models"

#: The gateway identifies itself rather than presenting a generic SDK name.
USER_AGENT = "personal-llm-gateway/2.0"

#: Dialect -> upstream path. Only the two OpenAI dialects are served; any
#: other value (including the empty, undocumented dialect) is refused.
SERVABLE_DIALECTS: dict[str, str] = {
    DIALECT_OPENAI_CHAT: "/v1/chat/completions",
    DIALECT_OPENAI_RESPONSES: "/v1/responses",
}


class OpenAIApiAdapter:
    """Adapter for the plain OpenAI API-key provider."""

    provider_id = PROVIDER_ID
    kind = KIND

    def __init__(
        self,
        credentials,
        client: UpstreamClient | None = None,
        base_url: str = DEFAULT_BASE_URL,
    ) -> None:
        self._credentials = credentials
        self._client = client or UpstreamClient(user_agent=USER_AGENT)
        self._base_url = base_url.rstrip("/")

    # --- credential ------------------------------------------------------

    def _api_key(self) -> str | None:
        payload = self._credentials.read()
        if not payload:
            return None
        key = payload.get("api_key")
        return key if isinstance(key, str) and key else None

    def _require_key(self) -> str:
        """Return the stored key, or refuse locally.

        A request with no credential would only buy a guaranteed 401, and
        the shared ``auth`` class is the honest classification for it. The
        refusal carries no upstream round trip and no key material.
        """
        key = self._api_key()
        if not key:
            raise AdapterError(
                ERROR_AUTH,
                "no API key is stored for the OpenAI API provider; "
                "save a key on the provider card before refreshing or invoking",
                provider_id=PROVIDER_ID,
            )
        return key

    def _headers(self, stream: bool = False) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if stream else "application/json",
            "User-Agent": USER_AGENT,
            "Authorization": f"Bearer {self._require_key()}",
        }

    # --- catalogue -------------------------------------------------------

    def list_models(self) -> list[ModelInfo]:
        """Fetch the live model list. Never called implicitly.

        ``/v1/models`` publishes an id and nothing else about a model, so
        every capability, limit and retention field is recorded as unknown
        rather than inferred from the id.
        """
        response = self._client.request(
            "GET",
            f"{self._base_url}{MODELS_PATH}",
            headers=self._headers(),
            provider_id=PROVIDER_ID,
            # A catalogue read may be repeated freely.
            retry_safe=True,
        )
        if response.status >= 400:
            raise error_from_response(
                response.status,
                response.body,
                PROVIDER_ID,
                parse_json_body(response.body),
                operation="the OpenAI model catalogue request",
                headers=response.headers,
            )

        payload = parse_json_body(response.body)
        entries = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(entries, list):
            return []

        models: list[ModelInfo] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            model_id = entry.get("id")
            if not isinstance(model_id, str) or not model_id:
                continue
            models.append(
                ModelInfo(
                    model_id=model_id,
                    # The catalogue publishes no wire dialect. The owner
                    # sets it per model; an empty dialect is excluded by
                    # the router and refused by invoke().
                    dialect="",
                    # Both served endpoints document `stream` and `tools`
                    # as request parameters, so streaming and function
                    # tools are the API-level default. A model that
                    # rejects them answers with a classified upstream
                    # error instead of a silent drop. Image input stays
                    # false: it is a hard per-model capability and this
                    # catalogue publishes no evidence for any model.
                    supports_streaming=True,
                    supports_function_tools=True,
                    supports_builtin_tools=False,
                    supports_image_input=False,
                    reasoning_efforts=(),
                    limits_source="unknown",
                    source="discovered",
                    notes=None,
                )
            )
        return models

    def test_connection(self) -> TestResult:
        """Run an explicit connection test against the catalogue endpoint."""
        if not self._api_key():
            return TestResult(
                ok=False,
                state=STATE_FAILED,
                message="No API key is stored for the OpenAI API provider.",
                detail={"reason": "not_configured"},
            )
        try:
            models = self.list_models()
        except AdapterError as exc:
            return TestResult(ok=False, state=STATE_FAILED, message=exc.message, detail=exc.to_dict())
        return TestResult(
            ok=True,
            state=STATE_CONNECTED,
            message=f"OpenAI responded; {len(models)} models available.",
            detail={"model_count": len(models)},
        )

    # --- invocation ------------------------------------------------------

    def endpoint_for(self, dialect: str) -> str:
        """Return the upstream path for *dialect*, or refuse it.

        The gateway does not guess which protocol an unknown or
        undocumented dialect speaks, and it does not translate the
        Anthropic or Google dialects through an OpenAI upstream.
        """
        path = SERVABLE_DIALECTS.get(dialect)
        if path is None:
            if not dialect:
                raise AdapterError(
                    ERROR_POLICY,
                    "the wire dialect for this model is not documented; "
                    "set the model's dialect on the provider card before routing to it",
                    provider_id=PROVIDER_ID,
                )
            raise AdapterError(
                ERROR_POLICY,
                f"model dialect '{dialect}' is not served by the OpenAI API provider; "
                f"only {DIALECT_OPENAI_CHAT} and {DIALECT_OPENAI_RESPONSES} are",
                provider_id=PROVIDER_ID,
            )
        return path

    def invoke(self, request: ProviderRequest) -> ProviderResponse:
        """Send one request upstream on the model's own dialect."""
        path = self.endpoint_for(request.dialect)
        body = json.dumps(request.payload, ensure_ascii=False).encode("utf-8")
        response = self._client.request(
            "POST",
            f"{self._base_url}{path}",
            headers=self._headers(stream=request.stream),
            body=body,
            stream=request.stream,
            provider_id=PROVIDER_ID,
        )
        if response.status >= 400:
            raise error_from_response(
                response.status,
                response.body,
                PROVIDER_ID,
                parse_json_body(response.body),
                operation="the OpenAI inference request",
                headers=response.headers,
            )

        return ProviderResponse(
            status=response.status,
            headers=response.headers,
            body=response.body,
            stream=response.stream,
            content_type=response.content_type,
            dialect=request.dialect,
        )


__all__ = ["KIND", "PROVIDER_ID", "SERVABLE_DIALECTS", "OpenAIApiAdapter"]
