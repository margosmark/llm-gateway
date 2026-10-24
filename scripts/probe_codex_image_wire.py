"""Live probe: which Codex Responses image shapes does the upstream accept?

Runs *inside* the deployed gateway container (read-only rootfs) and prints
ONLY structural facts: HTTP status codes and the *names* of the fields the
upstream returns. No prompt text, no completion text, no image bytes, no
credential values are ever printed.

The upstream validates the request body BEFORE it checks the account's
allowance, so a rejected shape answers 400 while an accepted shape that
cannot be served answers 429. That difference is the oracle used here.

Usage (from the task worktree):

    ssh devbox 'docker exec -i personal-llm-gateway-gateway-1 python3 -' \
        < scripts/probe_codex_image_wire.py
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request

RESPONSES_URL = "https://chatgpt.com/backend-api/codex/responses"
CREDENTIAL_FILE = "/var/lib/gateway/credentials/chatgpt_codex.json"

# A 1x1 transparent PNG: the smallest valid image that is still a real PNG.
TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)
DATA_URL = "data:image/png;base64," + base64.b64encode(TINY_PNG).decode("ascii")


def auth_headers() -> dict[str, str]:
    with open(CREDENTIAL_FILE, "r", encoding="utf-8") as handle:
        credential = json.load(handle)
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "Authorization": "Bearer " + credential["access_token"],
    }
    account_id = credential.get("account_id")
    if isinstance(account_id, str) and account_id:
        headers["ChatGPT-Account-ID"] = account_id
    return headers


def field_names(body: bytes) -> list[str]:
    """Return the field names of an upstream error body, never its values."""
    try:
        payload = json.loads(body.decode("utf-8", "replace"))
    except Exception:
        return ["<unparseable>"]
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            return sorted(str(key) for key in error)
        return sorted(str(key) for key in payload)
    return ["<not-an-object>"]


def post(model: str, payload: dict) -> tuple[int, list[str]]:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        RESPONSES_URL, data=body, headers=auth_headers(), method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, field_names(response.read(4096))
    except urllib.error.HTTPError as exc:
        return exc.code, field_names(exc.read(4096))


def base_payload(model: str, content: list[dict]) -> dict:
    """The fields the Codex client always sends, per upstream ResponsesApiRequest."""
    return {
        "model": model,
        "stream": True,
        "tool_choice": "auto",
        "parallel_tool_calls": True,
        "store": False,
        "include": ["reasoning.encrypted_content"],
        "input": [{"role": "user", "content": content}],
    }


def shapes(model: str) -> list[tuple[str, dict]]:
    text = {"type": "input_text", "text": "describe the image"}
    return [
        (
            "gateway_current_chat_to_responses",
            base_payload(
                model,
                [text, {"type": "input_image", "image_url": DATA_URL}],
            ),
        ),
        (
            "upstream_canonical_inline_with_detail",
            base_payload(
                model,
                [
                    text,
                    {
                        "type": "input_image",
                        "image_url": DATA_URL,
                        "detail": "high",
                    },
                ],
            ),
        ),
        (
            "upstream_canonical_typed_message",
            {
                **base_payload(
                    model,
                    [
                        text,
                        {
                            "type": "input_image",
                            "image_url": DATA_URL,
                            "detail": "high",
                        },
                    ],
                ),
                "input": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": [
                            text,
                            {
                                "type": "input_image",
                                "image_url": DATA_URL,
                                "detail": "high",
                            },
                        ],
                    }
                ],
            },
        ),
        (
            "image_url_as_object",
            base_payload(
                model,
                [text, {"type": "input_image", "image_url": {"url": DATA_URL}}],
            ),
        ),
        (
            "text_only_control",
            base_payload(model, [text]),
        ),
    ]


def main() -> None:
    model = "gpt-5.6-terra"
    for name, payload in shapes(model):
        status, fields = post(model, payload)
        print(f"{name}: status {status} fields {fields}")


if __name__ == "__main__":
    main()
