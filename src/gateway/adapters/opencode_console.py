"""OpenCode Console Jev adapter for the native System One API.

Jev is a structured-decision service, not a text-completion model.  It is
therefore intentionally exposed only through ``/v1/systemone`` and never
through the gateway's chat/Responses router.
"""

from __future__ import annotations

import json
from typing import Any

from gateway.adapters.base import AdapterError, TestResult, error_from_response
from gateway.adapters.http_client import UpstreamClient, parse_json_body
from gateway.config import STATE_CONNECTED, STATE_FAILED
from gateway.core.capabilities import ModelInfo

PROVIDER_ID = "opencode_console"
KIND = "opencode_console"
DEFAULT_BASE_URL = "https://opencode.ai/inference"
MODELS_PATH = "/v1/models"
SYSTEMONE_PATH = "/v1/systemone"
USER_AGENT = "personal-llm-gateway/2.0"


class OpenCodeConsoleAdapter:
    """Native adapter for OpenCode Console's documented System One contract."""

    provider_id = PROVIDER_ID
    kind = KIND

    def __init__(self, credentials, client: UpstreamClient | None = None, base_url: str = DEFAULT_BASE_URL) -> None:
        self._credentials = credentials
        self._client = client or UpstreamClient(user_agent=USER_AGENT)
        self._base_url = base_url.rstrip("/")

    def _api_key(self) -> str | None:
        payload = self._credentials.read()
        if not payload:
            return None
        key = payload.get("api_key")
        return key if isinstance(key, str) and key else None

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
        key = self._api_key()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return headers

    def list_models(self) -> list[ModelInfo]:
        """Fetch the authenticated System One model catalogue explicitly."""
        response = self._client.request(
            "GET", f"{self._base_url}{MODELS_PATH}", headers=self._headers(),
            provider_id=PROVIDER_ID, retry_safe=True,
        )
        if response.status >= 400:
            raise error_from_response(response.status, response.body, PROVIDER_ID, parse_json_body(response.body),
                                      operation="the OpenCode Console Jev catalogue request", headers=response.headers)
        payload = parse_json_body(response.body)
        # OpenCode Console publishes its OpenAI-style catalogue as ``data``.
        # It includes chat models as well, so this adapter deliberately keeps
        # only the documented Jev System One ids rather than guessing a
        # System One dialect for every Console model.
        entries = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(entries, list):
            return []
        models: list[ModelInfo] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            model_id = entry.get("id")
            if not isinstance(model_id, str) or not model_id.startswith("jev-"):
                continue
            models.append(ModelInfo(
                model_id=model_id,
                dialect="systemone",
                supports_streaming=False,
                supports_function_tools=False,
                limits_source="unknown",
                source="discovered",
                notes="OpenCode Console Jev System One model",
            ))
        return models

    def test_connection(self) -> TestResult:
        if not self._api_key():
            return TestResult(False, STATE_FAILED, "No API key is stored for OpenCode Console Jev.", {"reason": "not_configured"})
        try:
            models = self.list_models()
        except AdapterError as exc:
            return TestResult(False, STATE_FAILED, exc.message, exc.to_dict())
        return TestResult(True, STATE_CONNECTED, f"OpenCode Console Jev responded; {len(models)} models available.", {"model_count": len(models)})

    def invoke_systemone(self, payload: dict[str, Any]) -> bytes:
        """Send one already validated native System One request upstream."""
        headers = self._headers()
        headers["Content-Type"] = "application/json"
        response = self._client.request(
            "POST", f"{self._base_url}{SYSTEMONE_PATH}", headers=headers,
            body=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            provider_id=PROVIDER_ID,
        )
        if response.status >= 400:
            raise error_from_response(response.status, response.body, PROVIDER_ID, parse_json_body(response.body),
                                      operation="the OpenCode Console Jev System One request", headers=response.headers)
        return response.body or b""
