"""Prove the fix against the live backend: strip vs no-strip, same request.

The account is at its allowance wall, which makes this a clean experiment.
Validation runs *before* the allowance check, so:

  * ``400 Unsupported parameter`` -> the body was rejected: not stripped.
  * ``429 usage_limit_reached``   -> the body PASSED validation and only the
    allowance stopped it: the field was stripped.

Both arms send the identical Chat Completions request carrying
``temperature``; the only difference is whether the gateway's routing strip
(``UNSUPPORTED_PAYLOAD_FIELDS``, read off the real adapter) is applied. A
400 before and a 429 after is direct proof the fix removes the 502.

Prints status codes and field names only. Never prints a prompt, an image
payload, a token or a credential.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.environ.get("FIXED_SRC", "/app/src"))

from gateway.adapters.base import ProviderRequest  # noqa: E402
from gateway.adapters.chatgpt_codex import (  # noqa: E402
    CHATGPT_BASE,
    RESPONSES_PATH,
    ChatGPTCodexAdapter,
)
from gateway.adapters.http_client import UpstreamClient, parse_json_body  # noqa: E402
from gateway.bridge.requests import convert_request  # noqa: E402
from gateway.credentials import CredentialStore  # noqa: E402
from gateway.core.routing import _unsupported_payload_fields  # noqa: E402

TINY_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
DATA_URL = f"data:image/png;base64,{TINY_PNG_B64}"

#: The production shape that produced the 502s: a vision/summariser call,
#: non-streaming, with a temperature. Exactly what Hermes sends.
CHAT_BODY = {
    "model": "gpt-5.6-terra",
    "messages": [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "reply with the single word ok"},
                {"type": "image_url", "image_url": {"url": DATA_URL}},
            ],
        }
    ],
    "temperature": 0.1,
}


def auth_headers(store: CredentialStore) -> dict[str, str]:
    payload = store.provider_handle("chatgpt_codex").read() or {}
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "Authorization": f"Bearer {payload.get('access_token')}",
    }
    account_id = payload.get("account_id")
    if isinstance(account_id, str) and account_id:
        headers["ChatGPT-Account-ID"] = account_id
    return headers


def classify(status: int, body: bytes | None) -> str:
    parsed = parse_json_body(body)
    if isinstance(parsed, dict) and "detail" in parsed:
        msg = ""
        detail = parsed.get("detail")
        if isinstance(detail, list) and detail and isinstance(detail[0], dict):
            msg = str(detail[0].get("msg"))
        return f"REJECTED  {msg[:60]}"
    if isinstance(parsed, dict) and "error" in parsed:
        err = parsed.get("error")
        etype = err.get("type") if isinstance(err, dict) else None
        return f"ACCEPTED  ({etype})"
    return f"OTHER status={status}"


def send(client, headers, payload: dict, label: str) -> str:
    body = json.dumps(payload).encode("utf-8")
    response = client.request(
        "POST",
        f"{CHATGPT_BASE}{RESPONSES_PATH}",
        headers={**headers, "Accept": "text/event-stream"},
        body=body,
        stream=True,
        provider_id="chatgpt_codex",
    )
    result = classify(response.status, response.body)
    print(f"{label:34s} status={response.status:4d}  {result}")
    return result


def build_payload(strip: bool) -> tuple[dict, list[str]]:
    """Build the upstream payload exactly as the gateway does."""
    payload = dict(CHAT_BODY)
    payload["stream"] = True
    converted = convert_request(payload, "openai_chat", "openai_responses")
    # The adapter always forces these; they are not what is under test.
    converted["stream"] = True
    converted.setdefault("tool_choice", "auto")
    converted.setdefault("parallel_tool_calls", True)
    converted["store"] = False
    converted.setdefault("include", ["reasoning.encrypted_content"])

    if not strip:
        return converted, sorted(
            f for f in _unsupported_payload_fields(ChatGPTCodexAdapter) if f in converted
        )

    dropped = sorted(
        f for f in _unsupported_payload_fields(ChatGPTCodexAdapter) if f in converted
    )
    for name in dropped:
        converted.pop(name, None)
    return converted, dropped


def main() -> None:
    store = CredentialStore(Path(os.environ["GATEWAY_DATA_DIR"]))
    headers = auth_headers(store)
    client = UpstreamClient(user_agent="personal-llm-gateway/2.0")

    print("=== the production failure shape: image + temperature, non-streaming ===")
    before, would_drop = build_payload(strip=False)
    print(f"fields the router would drop: {json.dumps(would_drop)}")
    print(f"image part present: {'input_image' in json.dumps(before.get('input'), default=str)}")
    print()

    print("--- BEFORE the fix (fields forwarded) ---")
    send(client, headers, before, "image + temperature")

    print()
    print("--- AFTER the fix (router strip applied) ---")
    after, dropped = build_payload(strip=True)
    send(client, headers, after, "image + temperature stripped")

    print()
    print("--- control: no temperature at all ---")
    control = dict(CHAT_BODY)
    control.pop("temperature", None)
    control["stream"] = True
    control = convert_request(control, "openai_chat", "openai_responses")
    control["stream"] = True
    control.setdefault("tool_choice", "auto")
    control.setdefault("parallel_tool_calls", True)
    control["store"] = False
    control.setdefault("include", ["reasoning.encrypted_content"])
    send(client, headers, control, "image, no temperature")

    print()
    print("RESULT: a 400 before and a 429 after proves the strip removes the 502.")
    print("        The 429 is the account allowance wall, not a shape rejection.")


if __name__ == "__main__":
    main()
