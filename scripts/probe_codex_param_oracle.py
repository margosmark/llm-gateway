"""Confirmation probe: is the 400 about the image, or about a parameter?

The differential probe showed the gateway's own image shape is ACCEPTED
(429 = body passed validation, allowance wall) while ``temperature`` is
rejected with 400. This isolates the variable: the same parameter test with
and without an image in the request.
"""

from __future__ import annotations

import copy
import json
import os
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


def post(client, headers, payload, label) -> None:
    body = json.dumps(payload).encode("utf-8")
    response = client.request(
        "POST", f"{CHATGPT_BASE}{RESPONSES_PATH}",
        headers={**headers, "Accept": "text/event-stream"}, body=body,
        stream=True, provider_id="chatgpt_codex")
    parsed = parse_json_body(response.body)
    if isinstance(parsed, dict) and "detail" in parsed:
        detail = json.dumps(parsed.get("detail"), default=str)[:160]
        print(f"{label:52s} status={response.status:4d}  REJECTED  {detail}")
    elif isinstance(parsed, dict) and "error" in parsed:
        etype = parsed["error"].get("type") if isinstance(parsed["error"], dict) else None
        print(f"{label:52s} status={response.status:4d}  ACCEPTED ({etype})")
    else:
        print(f"{label:52s} status={response.status:4d}  OTHER")


def base(model, with_image: bool) -> dict:
    content = [{"type": "input_text", "text": "say ok"}]
    if with_image:
        content.append({"type": "input_image", "image_url": DATA_URL})
    return {
        "model": model,
        "input": [{"role": "user", "content": content}],
        "stream": True,
        "tool_choice": "auto",
        "parallel_tool_calls": True,
        "store": False,
        "include": ["reasoning.encrypted_content"],
    }


def main() -> None:
    store = CredentialStore(Path(os.environ["GATEWAY_DATA_DIR"]))
    headers = auth_headers(store)
    client = UpstreamClient(user_agent="personal-llm-gateway/2.0")

    print("=== parameter rejection is image-independent (gpt-5.6-terra) ===")
    for label, mutate in [
        ("text  baseline", lambda p: None),
        ("text  + temperature", lambda p: p.__setitem__("temperature", 0.1)),
        ("text  + top_p", lambda p: p.__setitem__("top_p", 0.9)),
        ("text  + stop", lambda p: p.__setitem__("stop", ["x"])),
        ("text  + metadata", lambda p: p.__setitem__("metadata", {"a": "b"})),
        ("image baseline", lambda p: None),
        ("image + temperature", lambda p: p.__setitem__("temperature", 0.1)),
        ("image + top_p", lambda p: p.__setitem__("top_p", 0.9)),
        ("image + stop", lambda p: p.__setitem__("stop", ["x"])),
    ]:
        payload = base("gpt-5.6-terra", label.startswith("image"))
        mutate(payload)
        post(client, headers, payload, label)

    print()
    print("=== does the allowance wall hide a rejection? (same body, temperature) ===")
    # If temperature were checked after the allowance, we would see 429. We
    # do not: it 400s while the identical body without it 429s. That proves
    # body validation runs first and the 400 is trustworthy evidence.
    ok = base("gpt-5.6-terra", True)
    post(client, headers, ok, "image, no temperature")
    bad = base("gpt-5.6-terra", True)
    bad["temperature"] = 0.1
    post(client, headers, bad, "image, temperature=0.1")

    print()
    print("=== other enabled models: same rule? ===")
    for model in ["gpt-6-luna"]:
        p = base(model, True)
        post(client, headers, p, f"{model} image baseline")
        p2 = copy.deepcopy(p)
        p2["temperature"] = 0.1
        post(client, headers, p2, f"{model} image + temperature")


if __name__ == "__main__":
    main()
