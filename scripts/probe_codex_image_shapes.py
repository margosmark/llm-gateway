"""Find a Codex model with remaining allowance and compare image wire shapes.

Prints only status codes and structural field names. No prompt, completion,
token, or image payload is printed.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, "/app/src")

from gateway.adapters.http_client import UpstreamClient, parse_json_body  # noqa: E402
from gateway.credentials import CredentialStore  # noqa: E402

CHATGPT_BASE = "https://chatgpt.com/backend-api/codex"
RESPONSES_PATH = "/responses"

TINY_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
DATA_URL = f"data:image/png;base64,{TINY_PNG_B64}"

_DATA_URL_RE = re.compile(r"data:[^\"'\s]{1,200}")


def redact(text: str) -> str:
    return _DATA_URL_RE.sub("<data-url-redacted>", text)


def headers_for(store: CredentialStore) -> dict[str, str]:
    payload = store.provider_handle("chatgpt_codex").read() or {}
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "Authorization": f"Bearer {payload['access_token']}",
    }
    account_id = payload.get("account_id")
    if isinstance(account_id, str) and account_id:
        headers["ChatGPT-Account-ID"] = account_id
    return headers


def post(client: UpstreamClient, headers: dict, payload: dict, model: str, label: str) -> int:
    body = dict(payload)
    body["model"] = model
    body["stream"] = True
    body.setdefault("tool_choice", "auto")
    body.setdefault("parallel_tool_calls", True)
    body["store"] = False
    body.setdefault("include", ["reasoning.encrypted_content"])
    response = client.request(
        "POST",
        f"{CHATGPT_BASE}{RESPONSES_PATH}",
        headers={**headers, "Accept": "text/event-stream"},
        body=json.dumps(body).encode("utf-8"),
        stream=True,
        provider_id="chatgpt_codex",
    )
    status = response.status
    parsed = parse_json_body(response.body)
    names = sorted(parsed.keys()) if isinstance(parsed, dict) else []
    extra = ""
    if status >= 400 and isinstance(parsed, dict):
        error = parsed.get("error")
        if isinstance(error, dict):
            extra = (
                f" error_fields={sorted(error.keys())}"
                f" type={error.get('type')!r} code={error.get('code')!r}"
                f" param={error.get('param')!r}"
            )
            message = error.get("message")
            if isinstance(message, str):
                extra += f" message={redact(message)[:300]!r}"
        elif "detail" in parsed:
            extra = f" detail={redact(str(parsed['detail']))[:300]!r}"
    print(f"[{label}] model={model} status={status} body_fields={names}{extra}")
    return status


def main() -> None:
    store = CredentialStore(Path(os.environ["GATEWAY_DATA_DIR"]))
    headers = headers_for(store)
    client = UpstreamClient(user_agent="personal-llm-gateway/2.0")

    listing = client.request(
        "GET",
        f"{CHATGPT_BASE}/models?client_version=2.0.0",
        headers=headers,
        provider_id="chatgpt_codex",
        retry_safe=True,
    )
    models = [
        m.get("slug")
        for m in (parse_json_body(listing.body).get("models") or [])
        if isinstance(m, dict) and m.get("supported_in_api") is not False and m.get("slug")
    ]
    print("candidates:", models)

    text_payload = {
        "input": [
            {
                "role": "user",
                "content": [{"type": "input_text", "text": "reply with the single word ok"}],
            }
        ]
    }

    usable = None
    for model in models:
        status = post(client, headers, text_payload, model, "text-control")
        if status == 200:
            usable = model
            break
    if not usable:
        print("NO_MODEL_WITH_ALLOWANCE")
        return

    image_variants = {
        "img-A-gateway-shape": {
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "reply with the single word ok"},
                        {"type": "input_image", "image_url": DATA_URL},
                    ],
                }
            ]
        },
        "img-B-typed-message": {
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "reply with the single word ok"},
                        {"type": "input_image", "image_url": DATA_URL},
                    ],
                }
            ]
        },
        "img-C-detail": {
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "reply with the single word ok"},
                        {"type": "input_image", "image_url": DATA_URL, "detail": "auto"},
                    ],
                }
            ]
        },
        "img-D-nested-url": {
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "reply with the single word ok"},
                        {"type": "input_image", "image_url": {"url": DATA_URL}},
                    ],
                }
            ]
        },
        "img-E-file-id-style": {
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "reply with the single word ok"},
                        {"type": "input_image", "file_id": DATA_URL},
                    ],
                }
            ]
        },
    }
    for label, payload in image_variants.items():
        post(client, headers, payload, usable, label)


if __name__ == "__main__":
    main()
