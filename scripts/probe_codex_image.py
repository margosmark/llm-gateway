"""Probe the ChatGPT / Codex upstream for the image wire shape.

Run *inside the gateway container*, from the deployed source tree, so the
request uses the same egress path and the same credential store the gateway
uses. It prints only status codes and structural field names - never a
prompt, a completion, a token or an image payload.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, "/app/src")

from gateway.adapters.http_client import UpstreamClient, parse_json_body  # noqa: E402
from gateway.bridge.requests import convert_request  # noqa: E402
from gateway.credentials import CredentialStore  # noqa: E402

CHATGPT_BASE = "https://chatgpt.com/backend-api/codex"
RESPONSES_PATH = "/responses"

#: A 1x1 PNG. Non-sensitive, and small enough that the probe stays cheap.
TINY_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
DATA_URL = f"data:image/png;base64,{TINY_PNG_B64}"

_DATA_URL_RE = re.compile(r"data:[^\"'\s]{1,200}")


def redact(text: str) -> str:
    """Remove anything that could echo request content."""
    return _DATA_URL_RE.sub("<data-url-redacted>", text)


def auth_headers(store: CredentialStore) -> dict[str, str]:
    payload = store.provider_handle("chatgpt_codex").read() or {}
    token = payload.get("access_token")
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "Authorization": f"Bearer {token}",
    }
    account_id = payload.get("account_id")
    if isinstance(account_id, str) and account_id:
        headers["ChatGPT-Account-ID"] = account_id
    return headers


def codex_fields(payload: dict) -> dict:
    """The fields the Codex client always sends."""
    body = dict(payload)
    body["stream"] = True
    body.setdefault("tool_choice", "auto")
    body.setdefault("parallel_tool_calls", True)
    body["store"] = False
    body.setdefault("include", ["reasoning.encrypted_content"])
    return body


def post(client: UpstreamClient, headers: dict, payload: dict, label: str) -> int:
    body = json.dumps(codex_fields(payload)).encode("utf-8")
    response = client.request(
        "POST",
        f"{CHATGPT_BASE}{RESPONSES_PATH}",
        headers={**headers, "Accept": "text/event-stream"},
        body=body,
        stream=True,
        provider_id="chatgpt_codex",
    )
    status = response.status
    names = sorted(parse_json_body(response.body).keys()) if response.body else []
    print(f"[{label}] status={status} body_fields={names}")
    if status >= 400:
        parsed = parse_json_body(response.body)
        error = parsed.get("error") if isinstance(parsed, dict) else None
        if isinstance(error, dict):
            print(
                f"[{label}] error_fields={sorted(error.keys())} "
                f"type={error.get('type')!r} code={error.get('code')!r} "
                f"param={error.get('param')!r}"
            )
            message = error.get("message")
            if isinstance(message, str):
                print(f"[{label}] error_message={redact(message)[:400]!r}")
    return status


def main() -> None:
    store = CredentialStore(Path(os.environ["GATEWAY_DATA_DIR"]))
    headers = auth_headers(store)
    client = UpstreamClient(user_agent="personal-llm-gateway/2.0")

    chat_payload = {
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
        "stream": True,
    }

    # Control: the same request with no image at all.
    control = json.loads(json.dumps(chat_payload))
    control["messages"][0]["content"] = [{"type": "text", "text": "reply with the single word ok"}]
    post(client, headers, control, "control-text")

    # A: what the gateway emits today (bridge Chat -> Responses).
    converted = convert_request(chat_payload, "openai_chat", "openai_responses")
    print(f"[A] converted_input_shape={json.dumps(converted['input'], default=str)[:0]}")
    post(client, headers, converted, "A-gateway-converted")

    # B: the same content, but the message item carries its own type.
    typed = json.loads(json.dumps(converted))
    for item in typed.get("input", []):
        if isinstance(item, dict) and "role" in item:
            item["type"] = "message"
    post(client, headers, typed, "B-typed-message")

    # C: the image part with an explicit detail field.
    detailed = json.loads(json.dumps(converted))
    for item in detailed.get("input", []):
        for part in (item.get("content") or []) if isinstance(item, dict) else []:
            if isinstance(part, dict) and part.get("type") == "input_image":
                part["detail"] = "auto"
    post(client, headers, detailed, "C-input-image-detail")

    # D: the image url nested under an object.
    nested = json.loads(json.dumps(converted))
    for item in nested.get("input", []):
        for part in (item.get("content") or []) if isinstance(item, dict) else []:
            if isinstance(part, dict) and part.get("type") == "input_image":
                part["image_url"] = {"url": DATA_URL}
    post(client, headers, nested, "D-nested-image-url")

    # E: the Codex protocol's own shape - a message item whose content holds
    # an input_text part and an input_image part with a data url.
    codex_shape = {
        "model": "gpt-5.6-terra",
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "reply with the single word ok"},
                    {"type": "input_image", "image_url": DATA_URL},
                ],
            }
        ],
    }
    post(client, headers, codex_shape, "E-codex-shape")


if __name__ == "__main__":
    main()
