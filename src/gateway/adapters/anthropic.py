"""Native Anthropic Messages API-key adapter."""
from __future__ import annotations

import json
from typing import Any

from gateway.adapters.base import AdapterError, ProviderRequest, ProviderResponse, TestResult, error_from_response
from gateway.adapters.http_client import UpstreamClient, parse_json_body
from gateway.config import STATE_CONNECTED, STATE_FAILED, ERROR_POLICY
from gateway.core.capabilities import DIALECT_ANTHROPIC_MESSAGES, ModelInfo

PROVIDER_ID = "anthropic"
KIND = PROVIDER_ID
DEFAULT_BASE_URL = "https://api.anthropic.com"
API_VERSION = "2023-06-01"
USER_AGENT = "personal-llm-gateway/2.0"


class AnthropicAdapter:
    provider_id = PROVIDER_ID
    kind = KIND

    def __init__(self, credentials, client: Any | None = None, base_url: str = DEFAULT_BASE_URL) -> None:
        self._credentials = credentials
        self._client = client or UpstreamClient(user_agent=USER_AGENT)
        self._base_url = base_url.rstrip("/")

    def _headers(self, stream: bool = False) -> dict[str, str]:
        payload = self._credentials.read() or {}
        key = payload.get("api_key")
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream" if stream else "application/json", "anthropic-version": API_VERSION}
        if isinstance(key, str) and key:
            headers["x-api-key"] = key
        return headers

    def list_models(self) -> list[ModelInfo]:
        response = self._client.request("GET", f"{self._base_url}/v1/models", headers=self._headers(), provider_id=PROVIDER_ID, retry_safe=True)
        if response.status >= 400:
            raise error_from_response(response.status, response.body, PROVIDER_ID, parse_json_body(response.body), operation="the Anthropic model catalogue request")
        payload = parse_json_body(response.body)
        entries = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(entries, list):
            return []
        return [ModelInfo(model_id=item["id"], dialect=DIALECT_ANTHROPIC_MESSAGES, source="discovered", notes="Anthropic Messages; model capabilities and limits unknown") for item in entries if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"]]

    def test_connection(self) -> TestResult:
        if not self._headers().get("x-api-key"):
            return TestResult(False, STATE_FAILED, "No API key is stored for Anthropic.", {"reason": "not_configured"})
        try:
            models = self.list_models()
        except AdapterError as exc:
            return TestResult(False, STATE_FAILED, exc.message, exc.to_dict())
        return TestResult(True, STATE_CONNECTED, f"Anthropic responded; {len(models)} models available.")

    def invoke(self, request: ProviderRequest) -> ProviderResponse:
        if request.dialect and request.dialect != DIALECT_ANTHROPIC_MESSAGES:
            raise AdapterError(ERROR_POLICY, "Anthropic supports only the native Messages dialect", provider_id=PROVIDER_ID)
        payload = dict(request.payload)
        payload["model"] = request.model
        payload["stream"] = bool(request.stream)
        response = self._client.request("POST", f"{self._base_url}/v1/messages", headers=self._headers(request.stream), body=json.dumps(payload, ensure_ascii=False).encode(), stream=request.stream, provider_id=PROVIDER_ID)
        if response.status >= 400:
            raise error_from_response(response.status, response.body, PROVIDER_ID, parse_json_body(response.body), operation="the Anthropic Messages request")
        return ProviderResponse(response.status, response.headers, response.body, response.stream, response.content_type, DIALECT_ANTHROPIC_MESSAGES)
