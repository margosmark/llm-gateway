"""The OpenCode Go adapter.

OpenCode Go serves **four** wire protocols under one provider, so the
dialect is a property of the *model*, not of the provider:

============================  ==========================================
dialect                       models
============================  ==========================================
``openai_responses``          grok-4.7, gpt-6-luna, gpt-5.6-luna,
                              muse-spark-1.2/1.3-contributor
``openai_chat``               the GLM, Kimi, MiMo, DeepSeek, LongCat,
                              MiniMax, Step, Beam, Hy models
``anthropic_messages``        claude-haiku-5-5, minimax-m3,
                              qwen3.8-max/flash, qwen3.7-plus
``google_v1beta``             gemini-4-argon-contributor
============================  ==========================================

The mapping comes from a dated snapshot of the provider's published endpoint
table (:mod:`scripts.build_go_catalog_snapshot`), not from a guess. Ids the
provider serves but does not document are recorded with an unknown dialect
and are refused rather than guessed at.

Two request-hygiene rules from the provider's own documentation are applied
on every call: a **self-identifying user agent** and a **stable
``x-opencode-session``** header for the conversation.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from gateway.adapters.base import (
    AdapterError,
    ProviderRequest,
    ProviderResponse,
    TestResult,
    error_from_response,
)
from gateway.adapters.http_client import UpstreamClient, parse_json_body
from gateway.config import ERROR_POLICY, STATE_CONNECTED, STATE_FAILED
from gateway.core.capabilities import (
    DIALECT_ANTHROPIC_MESSAGES,
    DIALECT_GOOGLE_V1BETA,
    DIALECT_OPENAI_CHAT,
    DIALECT_OPENAI_RESPONSES,
    ImageEvidence,
    ModelInfo,
)

PROVIDER_ID = "opencode_go"
KIND = "opencode_go"

DEFAULT_BASE_URL = "https://opencode.ai/inference/go"
CATALOG_PATH = "/v1/models"

#: The gateway identifies itself rather than presenting a generic SDK name.
#: The provider routes and caches on this header.
USER_AGENT = "personal-llm-gateway/2.0"

#: Dialects the gateway can serve through its OpenAI-compatible surface.
SERVABLE_DIALECTS = frozenset({DIALECT_OPENAI_RESPONSES, DIALECT_OPENAI_CHAT})

SNAPSHOT_FILENAME = "go_catalog.json"


def _default_snapshot_path() -> Path:
    """Locate the catalogue snapshot.

    The layout differs between the source tree (``<repo>/data/``) and the
    container (``/app/data/`` beside ``/app/src/``), so both are checked,
    along with an explicit override.
    """
    override = os.environ.get("GATEWAY_GO_CATALOG")
    if override:
        return Path(override)
    package_dir = Path(__file__).resolve().parent
    candidates = (
        package_dir.parent.parent.parent / "data" / SNAPSHOT_FILENAME,
        package_dir.parent.parent / "data" / SNAPSHOT_FILENAME,
        package_dir.parent.parent.parent.parent / "data" / SNAPSHOT_FILENAME,
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0]


class OpenCodeGoAdapter:
    """Adapter for the OpenCode Go provider."""

    provider_id = PROVIDER_ID
    kind = KIND

    def __init__(
        self,
        credentials,
        client: UpstreamClient | None = None,
        base_url: str = DEFAULT_BASE_URL,
        snapshot_path: Path | None = None,
    ) -> None:
        self._credentials = credentials
        self._client = client or UpstreamClient(user_agent=USER_AGENT)
        self._base_url = base_url.rstrip("/")
        self._snapshot_path = snapshot_path or _default_snapshot_path()
        self._snapshot: dict[str, Any] | None = None

    # --- catalogue ------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """The dated snapshot of the provider's published catalogue."""
        if self._snapshot is None:
            try:
                raw = self._snapshot_path.read_text(encoding="utf-8")
                data = json.loads(raw)
            except (OSError, ValueError):
                data = {}
            self._snapshot = data if isinstance(data, dict) else {}
        return self._snapshot

    def _snapshot_models(self) -> dict[str, Any]:
        models = self.snapshot().get("models")
        return models if isinstance(models, dict) else {}

    def image_input_evidence(self) -> dict[str, ImageEvidence]:
        """The image verdicts the dated catalogue transcribed, per exact id.

        The catalogue is the capability authority for Go routes, so its rows
        are what the gateway reconciles into stored state at boot. A row
        counts only when it carries both a verdict and the source URL that
        was read for it: a verdict with no source is not evidence, and a
        source URL alone is never turned into a claim.
        """
        evidence: dict[str, ImageEvidence] = {}
        for model_id, row in self._snapshot_models().items():
            if not isinstance(model_id, str) or not isinstance(row, dict):
                continue
            verdict = row.get("supports_image_input")
            source = row.get("image_input_source")
            if not isinstance(verdict, bool) or not isinstance(source, str) or not source:
                continue
            observed_at = row.get("image_input_observed_at")
            evidence[model_id] = ImageEvidence(
                supports_image_input=verdict,
                source=source,
                observed_at=observed_at if isinstance(observed_at, str) else None,
            )
        return evidence

    def _api_key(self) -> str | None:
        payload = self._credentials.read()
        if not payload:
            return None
        key = payload.get("api_key")
        return key if isinstance(key, str) and key else None

    def _headers(self, session_id: str | None = None, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        key = self._api_key()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        if session_id:
            # A stable session id per conversation: the provider uses it for
            # routing and prompt caching.
            headers["x-opencode-session"] = session_id
        if extra:
            headers.update(extra)
        return headers

    def list_models(self) -> list[ModelInfo]:
        """Fetch the live model list. Never called implicitly."""
        response = self._client.request(
            "GET",
            f"{self._base_url}{CATALOG_PATH}",
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
                operation="the OpenCode Go catalogue request",
            )

        payload = parse_json_body(response.body)
        entries = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(entries, list):
            return []

        snapshot = self._snapshot_models()
        observed_at = self.snapshot().get("observed_at")
        # Image support is a claim only when the catalogue also carries the
        # source that was read for it; a verdict with no source never reaches
        # a route.
        evidence = self.image_input_evidence()
        models: list[ModelInfo] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            model_id = entry.get("id")
            if not isinstance(model_id, str) or not model_id:
                continue
            published = snapshot.get(model_id) if isinstance(snapshot.get(model_id), dict) else {}
            dialect = published.get("dialect")
            if not isinstance(dialect, str):
                dialect = ""
            models.append(
                ModelInfo(
                    model_id=model_id,
                    dialect=dialect,
                    context_window=published.get("context_window")
                    if isinstance(published.get("context_window"), int)
                    else None,
                    context_limit=published.get("context_limit")
                    if isinstance(published.get("context_limit"), int)
                    else None,
                    max_output_tokens=published.get("max_output_tokens")
                    if isinstance(published.get("max_output_tokens"), int)
                    else None,
                    reasoning_efforts=tuple(published.get("reasoning_efforts") or ()),
                    supports_streaming=bool(published.get("supports_streaming", True)),
                    supports_function_tools=bool(published.get("supports_function_tools", True)),
                    supports_builtin_tools=bool(published.get("supports_builtin_tools", False)),
                    # The dated, source-backed catalogue is the capability
                    # authority for Go routes. Do not invent vision support
                    # from a model name or its wire dialect, and do not carry
                    # a verdict the catalogue gives no source for.
                    supports_image_input=bool(
                        (verdict := evidence.get(model_id)) is not None
                        and verdict.supports_image_input
                    ),
                    retention_class=str(published.get("retention_class") or "unknown"),
                    monthly_limit_usd=published.get("monthly_limit_usd")
                    if isinstance(published.get("monthly_limit_usd"), (int, float))
                    else None,
                    limits_source=str(published.get("limits_source") or "unknown"),
                    limits_observed_at=published.get("observed_at") if isinstance(published.get("observed_at"), str) else observed_at,
                    source="discovered",
                    notes=published.get("notes") if isinstance(published.get("notes"), str) else None,
                )
            )
        return models

    def test_connection(self) -> TestResult:
        """Run an explicit connection test against the catalogue endpoint."""
        if not self._api_key():
            return TestResult(
                ok=False,
                state=STATE_FAILED,
                message="No API key is stored for OpenCode Go.",
                detail={"reason": "not_configured"},
            )
        try:
            models = self.list_models()
        except AdapterError as exc:
            return TestResult(ok=False, state=STATE_FAILED, message=exc.message, detail=exc.to_dict())
        return TestResult(
            ok=True,
            state=STATE_CONNECTED,
            message=f"OpenCode Go responded; {len(models)} models available.",
            detail={"model_count": len(models)},
        )

    # --- invocation -----------------------------------------------------

    def endpoint_for(self, model: str) -> tuple[str, str]:
        """Return ``(dialect, path)`` for *model*.

        A model with no published endpoint row is refused. The gateway does
        not guess which protocol an undocumented model speaks.
        """
        published = self._snapshot_models().get(model)
        if not isinstance(published, dict):
            raise AdapterError(
                ERROR_POLICY,
                f"the OpenCode Go snapshot has no published endpoint for model '{model}'; "
                "refresh the catalogue or enter the model manually",
                provider_id=PROVIDER_ID,
            )
        dialect = published.get("dialect")
        path = published.get("endpoint_path")
        if not isinstance(dialect, str) or not isinstance(path, str) or not dialect:
            raise AdapterError(
                ERROR_POLICY,
                f"the dialect for model '{model}' is not documented by the provider",
                provider_id=PROVIDER_ID,
            )
        return dialect, path

    def invoke(self, request: ProviderRequest) -> ProviderResponse:
        """Send a request upstream in the model's own dialect."""
        dialect, path = self.endpoint_for(request.model)
        if dialect not in SERVABLE_DIALECTS:
            raise AdapterError(
                ERROR_POLICY,
                f"model '{request.model}' is served over the {dialect} dialect, "
                "which this gateway does not translate; publish an OpenAI-dialect route instead",
                provider_id=PROVIDER_ID,
            )

        body = json.dumps(request.payload, ensure_ascii=False).encode("utf-8")
        headers = self._headers(session_id=request.session_id)
        headers["Accept"] = "text/event-stream" if request.stream else "application/json"

        response = self._client.request(
            "POST",
            f"{self._base_url}{path}",
            headers=headers,
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
                operation="the OpenCode Go inference request",
                headers=response.headers,
            )

        return ProviderResponse(
            status=response.status,
            headers=response.headers,
            body=response.body,
            stream=response.stream,
            content_type=response.content_type,
            dialect=dialect,
        )
