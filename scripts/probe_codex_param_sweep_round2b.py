"""Round-2b: which spellings of the output-length control survive today.

The round-2 sweep proved the Chat spellings (``max_tokens``,
``max_completion_tokens``) are refused. The bridge renders
``max_output_tokens`` for a converted request, and that spelling was never
probed: were the backend to refuse it as well, a chat request carrying
``max_tokens`` would still fail after the drop list. Same method as round 1
- one field per request, identical baseline body as the control.

Prints status codes and field names only. Never a prompt, token or
credential.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, "/app/src")

from gateway.adapters.http_client import UpstreamClient, parse_json_body
from gateway.credentials import CredentialStore

CHATGPT_BASE = "https://chatgpt.com/backend-api/codex"
RESPONSES_PATH = "/responses"

CANDIDATES: list[tuple[str, object]] = [
    ("max_output_tokens", 64),
    ("max_tokens", 64),
    ("max_completion_tokens", 64),
    ("temperature", 0.1),
    ("reasoning_effort", "low"),
]


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


def baseline() -> dict:
    return {
        "model": "gpt-5.6-terra",
        "input": [{"role": "user", "content": [{"type": "input_text", "text": "say ok"}]}],
        "stream": True,
        "tool_choice": "auto",
        "parallel_tool_calls": True,
        "store": False,
        "include": ["reasoning.encrypted_content"],
        "reasoning": {"effort": "low"},
    }


def classify(status: int, body: bytes | None) -> tuple[str, str]:
    parsed = parse_json_body(body)
    if isinstance(parsed, dict) and "detail" in parsed:
        return "REJECTED", json.dumps(parsed.get("detail"), default=str)[:140]
    if isinstance(parsed, dict) and "error" in parsed:
        return "ACCEPTED", str(parsed["error"].get("type"))[:40]
    return f"status={status}", ""


def main() -> None:
    store = CredentialStore(Path(os.environ["GATEWAY_DATA_DIR"]))
    headers = auth_headers(store)
    client = UpstreamClient(user_agent="personal-llm-gateway/2.0")

    response = client.request(
        "POST",
        f"{CHATGPT_BASE}{RESPONSES_PATH}",
        headers={**headers, "Accept": "text/event-stream"},
        body=json.dumps(baseline()).encode("utf-8"),
        stream=True,
        provider_id="chatgpt_codex",
    )
    kind, detail = classify(response.status, response.body)
    print(f"{'(baseline)':22s}      status={response.status:4d}  {kind:9s} {detail}")
    if kind != "ACCEPTED" and not (response.status == 429):
        print("SWEEP SKIPPED: the baseline itself was not accepted by validation")
        return

    for field, value in CANDIDATES:
        payload = baseline()
        payload[field] = value
        response = client.request(
            "POST",
            f"{CHATGPT_BASE}{RESPONSES_PATH}",
            headers={**headers, "Accept": "text/event-stream"},
            body=json.dumps(payload).encode("utf-8"),
            stream=True,
            provider_id="chatgpt_codex",
        )
        kind, detail = classify(response.status, response.body)
        print(f"{field:22s}      status={response.status:4d}  {kind:9s} {detail}")


main()
