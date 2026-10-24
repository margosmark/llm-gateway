"""The ChatGPT / Codex adapter.

Credential path: **user-operated device-code authorisation**, the beta path
that OpenAI documents for headless and remote sign-in.

The flow, as implemented by OpenAI's own Codex client:

1. ``POST {issuer}/api/accounts/deviceauth/usercode`` with the public client
   id. The response carries a ``user_code``, a ``device_auth_id`` and a
   polling ``interval``.
2. The owner opens ``{issuer}/codex/device`` in their own browser and enters
   the code.
3. ``POST {issuer}/api/accounts/deviceauth/token`` is polled. ``403`` and
   ``404`` mean "not yet"; a success carries an ``authorization_code`` plus
   the PKCE verifier and challenge.
4. ``POST {issuer}/oauth/token`` exchanges the authorization code for
   ``access_token``, ``refresh_token`` and ``id_token``.

Inference path: the OpenAI gateway documentation states that a gateway
proxies ``/models`` and ``/responses`` to
``https://chatgpt.com/backend-api/codex``, forwarding the ChatGPT bearer
token and, when present, the ``ChatGPT-Account-ID`` header.

Two things this adapter deliberately does **not** do:

* it never re-displays the device code or any token once stored;
* it never guesses. A ChatGPT subscription is not a guaranteed API or
  custom-gateway entitlement, so the console states that account
  eligibility, model availability and limits depend on the owner's actual
  account.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

from gateway.adapters.base import (
    AdapterError,
    ProviderRequest,
    ProviderResponse,
    TestResult,
    error_from_response,
)
from gateway.adapters.http_client import UpstreamClient, parse_json_body
from gateway.core.capabilities import (
    DIALECT_OPENAI_RESPONSES,
    ImageEvidence,
    ModelInfo,
    effective_context_limit,
    resolve_context_window,
)
from gateway.config import ERROR_AUTH, ERROR_TRANSIENT, STATE_CONNECTED, STATE_FAILED

PROVIDER_ID = "chatgpt_codex"
KIND = "chatgpt_codex"

ISSUER = "https://auth.openai.com"
ACCOUNTS_BASE = f"{ISSUER}/api/accounts"
USER_CODE_URL = f"{ACCOUNTS_BASE}/deviceauth/usercode"
DEVICE_TOKEN_URL = f"{ACCOUNTS_BASE}/deviceauth/token"
OAUTH_TOKEN_URL = f"{ISSUER}/oauth/token"
VERIFICATION_URL = f"{ISSUER}/codex/device"
REDIRECT_URI = f"{ISSUER}/deviceauth/callback"

#: The public OAuth client id used by OpenAI's Codex client for device-code
#: sign-in. It is an identifier, not a secret, and it is overridable so the
#: owner can point the adapter at a different issuer.
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"

#: The documented ChatGPT-authenticated inference base.
CHATGPT_BASE = "https://chatgpt.com/backend-api/codex"
MODELS_PATH = "/models"

#: Image-input support, per exact model slug, from OpenAI's own model page.
#:
#: The Codex catalogue publishes no image or vision field, so this cannot be
#: discovered from the endpoint - it is transcribed per *exact slug* from the
#: model-provider documentation, exactly as the OpenCode Go snapshot does.
#: A slug absent from this map stays false: image support is never inferred
#: from a model family, a tier name, or the Responses dialect, because those
#: inferences are wrong in both directions (GPT-5.6 Terra accepts images;
#: Xiaomi's ``mimo-v2.5-pro`` does not, while its sibling ``mimo-v2.5`` does).
#:
#: ``gpt-5.6-terra``: OpenAI lists "Input modalities: text, image" and
#: ``image_input`` under Supported features (read 2026-10-23).
IMAGE_INPUT_SOURCES = {
    "gpt-5.6-terra": "https://developers.openai.com/api/docs/models/gpt-5.6-terra",
}

#: The date each source above was read. A claim without a date cannot be
#: re-checked later, so the date travels with the URL.
IMAGE_INPUT_OBSERVED_AT = {
    "gpt-5.6-terra": "2026-10-23",
}


#: Request fields the ChatGPT-authenticated Codex backend rejects with a 400.
#:
#: Every field below was confirmed against the live backend, one at a time,
#: each added to an otherwise-accepted body. Each returns
#: ``400 {"detail": [{"msg": "Unsupported parameter: <field>"}]}``, while the
#: identical body without that field returns the account allowance error -
#: which proves validation ran *before* the allowance check, so a 400 here is
#: a genuine shape rejection rather than an exhausted account.
#:
#: The rejection is independent of image input: the same sweep run with an
#: ``input_image`` item present produces the identical set. This is the
#: single most common cause of the "malformed" failure class on this
#: provider, because OpenAI's own Chat Completions dialect accepts all of
#: them, so a client that sets ``temperature`` is doing nothing unusual -
#: the field simply has no place in this dialect and must be dropped, with
#: the substitution recorded, rather than forwarded.
#:
#: Sampling controls have no effect on a reasoning model served through this
#: backend, so dropping them loses nothing the client can observe.
CODEX_UNSUPPORTED_PAYLOAD_FIELDS = frozenset(
    {
        "temperature",
        "top_p",
        "n",
        "stop",
        "stop_sequences",
        "presence_penalty",
        "frequency_penalty",
        "logit_bias",
        "logprobs",
        "top_logprobs",
        "seed",
        "user",
        "metadata",
        "response_format",
        "service_tier",
        "max_tokens",
        "max_completion_tokens",
        "suffix",
    }
)

#: The Codex client appends its own version to the model-list request, and
#: the backend expects it. The gateway identifies itself honestly with its
#: own version rather than impersonating the Codex client.
CHATGPT_CLIENT_VERSION = os.environ.get("GATEWAY_CLIENT_VERSION", "2.0.0")
RESPONSES_PATH = "/responses"

USER_AGENT = "personal-llm-gateway/2.0"

#: Device-code states.
DEVICE_PENDING = "pending"
DEVICE_APPROVED = "approved"
DEVICE_DENIED = "denied"
DEVICE_EXPIRED = "expired"
DEVICE_CANCELLED = "cancelled"

#: The code expires in 15 minutes.
DEVICE_CODE_TTL_SECONDS = 15 * 60

#: The owner must enable this in ChatGPT security settings (personal
#: account) or workspace permissions (workspace admin). The Setup UI says so
#: before the flow starts.
PREREQUISITE = (
    "Device-code sign-in must be enabled for your account: ChatGPT security settings "
    "for a personal account, or workspace permissions for a workspace account."
)

ANTI_PHISHING = (
    "Enter the code only at the official OpenAI URL shown above. "
    "If a website or another person gave you this code, cancel."
)


@dataclass
class DeviceAuthorization:
    """An in-flight device authorisation.

    The ``user_code`` is shown to the owner once, as part of the explicit
    "Connect ChatGPT" action. It is never written to a log, a ledger, or a
    later read.

    ``device_auth_id`` is the provider's own identifier for this
    authorisation. It never leaves this process: the browser is given
    ``poll_handle``, an opaque value the gateway uses to find this record
    again.
    """

    verification_url: str
    user_code: str
    device_auth_id: str
    poll_handle: str = field(default_factory=lambda: secrets.token_urlsafe(24))
    interval: int = 5
    state: str = DEVICE_PENDING
    expires_at: float = field(default_factory=lambda: time.monotonic() + DEVICE_CODE_TTL_SECONDS)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """The payload for the explicit *start* action.

        This is the only response that carries the one-time code. It carries
        the opaque poll handle, never the provider's device auth id.
        """
        return {
            "poll_handle": self.poll_handle,
            "verification_url": self.verification_url,
            "user_code": self.user_code,
            "interval": self.interval,
            "state": self.state,
            "expires_at": self.expires_at,
            "error": self.error,
        }

    def to_status_dict(self) -> dict[str, Any]:
        """The payload for a *poll*.

        The one-time code is not repeated: it was shown once, at the start.
        """
        return {
            "poll_handle": self.poll_handle,
            "interval": self.interval,
            "state": self.state,
            "expires_at": self.expires_at,
            "error": self.error,
        }


def _jwt_claims(token: str) -> dict[str, Any]:
    """Read the claims of a JWT without verifying it.

    The token was issued to us by the provider over TLS; this is used only
    to read the ``chatgpt_account_id`` claim the provider expects us to
    forward. The signature is not checked, and nothing here trusts a claim
    for an access decision.
    """
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1]
    padding = "=" * (-len(payload) % 4)
    try:
        raw = base64.urlsafe_b64decode(payload + padding)
        claims = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}
    return claims if isinstance(claims, dict) else {}


class ChatGPTCodexAdapter:
    """Adapter for the ChatGPT / Codex provider."""

    provider_id = PROVIDER_ID
    kind = KIND

    #: Top-level request fields this provider's upstream answers with a 400
    #: "Unsupported parameter" for. The router reads these off the adapter and
    #: drops them for this route, recording the substitution - the fields are
    #: all part of OpenAI's Chat dialect, so a caller setting one is behaving
    #: normally and should not be failed for it. See the constant's comment
    #: for the live evidence behind each entry.
    UNSUPPORTED_PAYLOAD_FIELDS = CODEX_UNSUPPORTED_PAYLOAD_FIELDS

    def __init__(
        self,
        credentials,
        client: UpstreamClient | None = None,
        issuer: str = ISSUER,
        chatgpt_base: str = CHATGPT_BASE,
        client_id: str = CLIENT_ID,
    ) -> None:
        self._credentials = credentials
        self._client = client or UpstreamClient(user_agent=USER_AGENT)
        self._issuer = issuer.rstrip("/")
        self._chatgpt_base = chatgpt_base.rstrip("/")
        self._client_id = client_id

    # --- device-code authorisation ---------------------------------------

    def start_device_authorization(self) -> DeviceAuthorization:
        """Ask the provider for a one-time device code."""
        url = f"{self._issuer}/api/accounts/deviceauth/usercode"
        body = json.dumps({"client_id": self._client_id}).encode("utf-8")
        try:
            response = self._client.request(
                "POST",
                url,
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                body=body,
                provider_id=PROVIDER_ID,
                # Issuing a code may be repeated: an unused code expires
                # unused, and repeating it costs nothing.
                retry_safe=True,
            )
        except AdapterError as exc:
            if exc.status is not None:
                # The service answered; it is failing at its end. Say so,
                # and keep the upstream status, which is the useful detail.
                raise AdapterError(
                    exc.error_class,
                    f"the OpenAI sign-in service returned an error ({exc.message})",
                    provider_id=PROVIDER_ID,
                    status=exc.status,
                    upstream_code=exc.upstream_code,
                ) from exc
            raise AdapterError(
                ERROR_TRANSIENT,
                "could not reach the OpenAI sign-in service",
                provider_id=PROVIDER_ID,
            ) from exc

        if response.status == 404:
            raise AdapterError(
                ERROR_AUTH,
                "device-code sign-in is not enabled for this account or workspace",
                provider_id=PROVIDER_ID,
                status=response.status,
            )
        if response.status >= 400:
            raise error_from_response(
                response.status,
                response.body,
                PROVIDER_ID,
                parse_json_body(response.body),
                operation="the ChatGPT / Codex device code request",
            )

        payload = parse_json_body(response.body)
        if not isinstance(payload, dict):
            raise AdapterError(ERROR_TRANSIENT, "the sign-in service returned an unexpected response", provider_id=PROVIDER_ID)

        user_code = payload.get("user_code") or payload.get("usercode")
        device_auth_id = payload.get("device_auth_id")
        if not isinstance(user_code, str) or not isinstance(device_auth_id, str):
            raise AdapterError(ERROR_TRANSIENT, "the sign-in service returned an unexpected response", provider_id=PROVIDER_ID)

        interval = payload.get("interval")
        interval = interval if isinstance(interval, int) and interval > 0 else 5

        return DeviceAuthorization(
            verification_url=f"{self._issuer}/codex/device",
            user_code=user_code,
            device_auth_id=device_auth_id,
            interval=interval,
        )

    def poll_device_authorization(self, authorization: DeviceAuthorization) -> DeviceAuthorization:
        """Poll once for the owner's approval.

        ``403`` and ``404`` mean "not yet" - the owner has not entered the
        code, or has not confirmed it yet. Any other failure is reported.
        """
        if authorization.state == DEVICE_APPROVED:
            return authorization
        if time.monotonic() > authorization.expires_at:
            authorization.state = DEVICE_EXPIRED
            authorization.error = "the device code expired before it was approved"
            return authorization

        body = json.dumps(
            {"device_auth_id": authorization.device_auth_id, "user_code": authorization.user_code}
        ).encode("utf-8")
        try:
            response = self._client.request(
                "POST",
                f"{self._issuer}/api/accounts/deviceauth/token",
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                body=body,
                provider_id=PROVIDER_ID,
                # Polling is a read; repeating it is free.
                retry_safe=True,
            )
        except AdapterError:
            authorization.state = DEVICE_PENDING
            return authorization

        if response.status in (403, 404):
            authorization.state = DEVICE_PENDING
            return authorization
        if response.status >= 400:
            error = error_from_response(
                response.status,
                response.body,
                PROVIDER_ID,
                parse_json_body(response.body),
                operation="the ChatGPT / Codex device code poll",
            )
            authorization.state = DEVICE_DENIED
            authorization.error = error.message
            return authorization

        payload = parse_json_body(response.body)
        if not isinstance(payload, dict):
            authorization.state = DEVICE_DENIED
            authorization.error = "the sign-in service returned an unexpected response"
            return authorization

        authorization_code = payload.get("authorization_code")
        code_verifier = payload.get("code_verifier")
        if not isinstance(authorization_code, str) or not isinstance(code_verifier, str):
            authorization.state = DEVICE_DENIED
            authorization.error = "the sign-in service returned an unexpected response"
            return authorization

        self._exchange_authorization_code(authorization_code, code_verifier)
        authorization.state = DEVICE_APPROVED
        authorization.error = None
        return authorization

    def _exchange_authorization_code(self, authorization_code: str, code_verifier: str) -> None:
        """Exchange the authorization code for tokens and store them."""
        form = {
            "grant_type": "authorization_code",
            "client_id": self._client_id,
            "code": authorization_code,
            "redirect_uri": f"{self._issuer}/deviceauth/callback",
            "code_verifier": code_verifier,
        }
        response = self._client.request(
            "POST",
            f"{self._issuer}/oauth/token",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            body=_form_encode(form),
            provider_id=PROVIDER_ID,
        )
        if response.status >= 400:
            raise error_from_response(
                response.status,
                response.body,
                PROVIDER_ID,
                parse_json_body(response.body),
                operation="the ChatGPT / Codex token exchange",
            )
        self._store_tokens(parse_json_body(response.body))

    def _store_tokens(self, payload: Any) -> None:
        if not isinstance(payload, dict):
            raise AdapterError(ERROR_TRANSIENT, "the sign-in service returned an unexpected response", provider_id=PROVIDER_ID)
        access_token = payload.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise AdapterError(ERROR_TRANSIENT, "the sign-in service returned an unexpected response", provider_id=PROVIDER_ID)

        id_token = payload.get("id_token")
        refresh_token = payload.get("refresh_token")
        account_id = None
        if isinstance(id_token, str):
            claims = _jwt_claims(id_token)
            value = claims.get("chatgpt_account_id")
            if isinstance(value, str):
                account_id = value

        expires_in = payload.get("expires_in")
        expires_at = time.time() + expires_in if isinstance(expires_in, int) else None

        self._credentials.write(
            {
                "access_token": access_token,
                "refresh_token": refresh_token if isinstance(refresh_token, str) else None,
                "id_token": id_token if isinstance(id_token, str) else None,
                "account_id": account_id,
                "expires_at": expires_at,
            }
        )

    def refresh(self) -> bool:
        """Refresh the stored token. Returns whether a new token was stored."""
        payload = self._credentials.read()
        if not payload:
            return False
        refresh_token = payload.get("refresh_token")
        if not isinstance(refresh_token, str) or not refresh_token:
            return False

        form = {
            "grant_type": "refresh_token",
            "client_id": self._client_id,
            "refresh_token": refresh_token,
        }
        try:
            response = self._client.request(
                "POST",
                f"{self._issuer}/oauth/token",
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                },
                body=_form_encode(form),
                provider_id=PROVIDER_ID,
            )
        except AdapterError:
            return False
        if response.status >= 400:
            return False
        try:
            self._store_tokens(parse_json_body(response.body))
        except AdapterError:
            return False
        return True

    def disconnect(self) -> bool:
        """Remove the local credential. Provider-side revocation is the owner's action."""
        return self._credentials.clear()

    # --- provider surface -------------------------------------------------

    def _auth_headers(self) -> dict[str, str]:
        payload = self._credentials.read()
        if not payload:
            raise AdapterError(
                ERROR_AUTH,
                "ChatGPT / Codex is not connected",
                provider_id=PROVIDER_ID,
            )
        token = payload.get("access_token")
        if not isinstance(token, str) or not token:
            raise AdapterError(
                ERROR_AUTH,
                "the stored ChatGPT / Codex credential is incomplete",
                provider_id=PROVIDER_ID,
            )
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
        }
        account_id = payload.get("account_id")
        if isinstance(account_id, str) and account_id:
            headers["ChatGPT-Account-ID"] = account_id
        return headers

    def image_input_evidence(self) -> dict[str, ImageEvidence]:
        """The transcribed image verdicts for this provider, per exact slug.

        The Codex catalogue endpoint publishes no image field, so every claim
        here is a transcription from the model-provider's own page. It is
        exposed through the adapter hook so a boot can reconcile the stored
        rows with it: a claim that never reaches ``config.json`` is
        indistinguishable from false, however good the citation is.
        """
        evidence: dict[str, ImageEvidence] = {}
        for model_id, source in IMAGE_INPUT_SOURCES.items():
            if isinstance(model_id, str) and isinstance(source, str) and source:
                evidence[model_id] = ImageEvidence(
                    supports_image_input=True,
                    source=source,
                    observed_at=IMAGE_INPUT_OBSERVED_AT.get(model_id),
                )
        return evidence

    def list_models(self) -> list[ModelInfo]:
        """Fetch the model list. Never called implicitly.

        The shared adapter contract is ``list[ModelInfo]``, so the generic
        refresh handler can use this adapter exactly as it uses any other.

        The Codex model catalogue has its own shape. It is a list under
        ``models``, and a model is identified by its ``slug`` - not by an
        ``id``. A model also carries the reasoning levels it supports, its
        context window, and whether it is usable through the API at all.
        """
        response = self._client.request(
            "GET",
            f"{self._chatgpt_base}{MODELS_PATH}?client_version={CHATGPT_CLIENT_VERSION}",
            headers=self._auth_headers(),
            provider_id=PROVIDER_ID,
            # A model list may be repeated freely.
            retry_safe=True,
        )
        if response.status >= 400:
            raise error_from_response(
                response.status,
                response.body,
                PROVIDER_ID,
                parse_json_body(response.body),
                operation="the ChatGPT / Codex model list request",
            )
        payload = parse_json_body(response.body)
        entries = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(entries, list):
            entries = payload.get("models") if isinstance(payload, dict) else None

        models: list[ModelInfo] = []
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, dict):
                continue
            # The catalogue identifies a model by its slug. An OpenAI-shaped
            # ``id`` is accepted too, so the same parser serves both.
            model_id = entry.get("slug") or entry.get("id")
            if not isinstance(model_id, str) or not model_id:
                continue

            # A model the account cannot use through the API is not
            # offered: the gateway would only fail on it later.
            supported_in_api = entry.get("supported_in_api")
            if supported_in_api is False:
                continue

            efforts: list[str] = []
            for level in entry.get("supported_reasoning_levels") or []:
                if isinstance(level, dict):
                    effort = level.get("effort")
                    if isinstance(effort, str) and effort and effort not in efforts:
                        efforts.append(effort)
                elif isinstance(level, str) and level and level not in efforts:
                    efforts.append(level)

            # The provider publishes a working window and a ceiling for
            # config overrides. The smaller is the window that applies by
            # default, and the provider reserves a share of it for its own
            # system prompt, tool overhead and output.
            context_window = resolve_context_window(
                entry.get("context_window"), entry.get("max_context_window")
            )
            context_limit = effective_context_limit(
                context_window, entry.get("effective_context_window_percent")
            )

            notes: list[str] = []
            visibility = entry.get("visibility")
            if isinstance(visibility, str) and visibility not in ("list", ""):
                notes.append(f"visibility: {visibility}")
            image_source = IMAGE_INPUT_SOURCES.get(model_id)
            if image_source:
                notes.append(f"image input per {image_source}")

            models.append(
                ModelInfo(
                    model_id=model_id,
                    # The ChatGPT-authenticated upstream is the Responses
                    # protocol. That is a protocol fact, not a per-account
                    # guess.
                    dialect=DIALECT_OPENAI_RESPONSES,
                    supports_streaming=True,
                    supports_function_tools=True,
                    # The Codex catalogue publishes no image field, so this
                    # is transcribed from the model-provider documentation
                    # per exact slug. Absent means false, never a guess.
                    supports_image_input=image_source is not None,
                    # What an account may actually reach, and at what
                    # limits, depends on the account. Unknown.
                    context_window=context_window,
                    context_limit=context_limit,
                    reasoning_efforts=tuple(efforts),
                    retention_class="unknown",
                    limits_source="unknown",
                    source="discovered",
                    notes=(
                        "; ".join(notes)
                        or "the wire dialect is known; availability, limits and "
                        "retention for this account are not published"
                    ),
                )
            )
        return models

    def test_connection(self) -> TestResult:
        """Run an explicit connection test."""
        try:
            self._auth_headers()
        except AdapterError as exc:
            return TestResult(ok=False, state=STATE_FAILED, message=exc.message, detail=exc.to_dict())
        try:
            models = self.list_models()
        except AdapterError as exc:
            return TestResult(ok=False, state=STATE_FAILED, message=exc.message, detail=exc.to_dict())
        return TestResult(
            ok=True,
            state=STATE_CONNECTED,
            message=f"ChatGPT / Codex responded; {len(models)} models visible for this account.",
            detail={"model_count": len(models)},
        )

    def invoke(self, request: ProviderRequest) -> ProviderResponse:
        """Send a request upstream over the Responses protocol.

        The ChatGPT backend is **streaming only**: it rejects a Responses
        request that does not set ``stream: true``, and it expects the same
        request fields OpenAI's own Codex client sends. A client that asked
        for a single, non-streamed completion is served by consuming this
        stream and presenting the result as one object.
        """
        payload = dict(request.payload)
        payload["stream"] = True
        # The fields the Codex client always sends. A caller may set them
        # itself; the rest are defaults.
        payload.setdefault("tool_choice", "auto")
        payload.setdefault("parallel_tool_calls", True)
        # Never ask the provider to store the response.
        payload["store"] = False
        payload.setdefault("include", ["reasoning.encrypted_content"])

        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = self._auth_headers()
        headers["Accept"] = "text/event-stream"

        response = self._client.request(
            "POST",
            f"{self._chatgpt_base}{RESPONSES_PATH}",
            headers=headers,
            body=body,
            stream=True,
            provider_id=PROVIDER_ID,
        )

        if response.status in (401, 403):
            # The token may simply have expired. Refresh once, then give up.
            if self.refresh():
                headers = self._auth_headers()
                headers["Accept"] = "text/event-stream"
                response = self._client.request(
                    "POST",
                    f"{self._chatgpt_base}{RESPONSES_PATH}",
                    headers=headers,
                    body=body,
                    stream=True,
                    provider_id=PROVIDER_ID,
                )

        if response.status >= 400:
            raise error_from_response(
                response.status,
                response.body,
                PROVIDER_ID,
                parse_json_body(response.body),
                operation="the ChatGPT / Codex responses request",
                headers=response.headers,
            )

        return ProviderResponse(
            status=response.status,
            headers=response.headers,
            body=response.body,
            stream=response.stream,
            content_type=response.content_type,
            dialect="openai_responses",
        )


def _form_encode(fields: dict[str, str]) -> bytes:
    from urllib.parse import urlencode

    return urlencode(fields).encode("utf-8")
