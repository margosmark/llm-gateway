"""Complete the set of parameters the Codex backend rejects.

The differential probe proved ``temperature``, ``top_p``, ``stop`` and
``metadata`` are rejected with 400 "Unsupported parameter" while the identical
body without them is accepted (429 = allowance wall, validation already
passed). This finishes the sweep over every other sampling/control field the
gateway could ever forward, so the strip list is complete rather than a
guessed subset.

Prints status and the rejected field name only. Never prints a prompt, an
image payload, a token or a credential.
"""

from __future__ import annotations

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

# Each candidate is (field, value). One field per request, everything else
# held at the known-good baseline, so a 400 implicates that field alone.
CANDIDATES: list[tuple[str, object]] = [
    ("temperature", 0.1),
    ("top_p", 0.9),
    ("n", 1),
    ("stop", ["\n"]),
    ("stop_sequences", ["\n"]),
    ("presence_penalty", 0.0),
    ("frequency_penalty", 0.0),
    ("logit_bias", {"1": 1}),
    ("logprobs", True),
    ("top_logprobs", 2),
    ("seed", 42),
    ("user", "unit-test"),
    ("metadata", {"k": "v"}),
    ("response_format", {"type": "json_object"}),
    ("service_tier", "auto"),
    ("max_tokens", 64),
    ("max_completion_tokens", 64),
    ("suffix", "x"),
    ("logit_bias", {"-1": 1}),
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


def baseline(with_image: bool) -> dict:
    content: list[dict] = [{"type": "input_text", "text": "say ok"}]
    if with_image:
        content.append({"type": "input_image", "image_url": DATA_URL})
    return {
        "model": "gpt-5.6-terra",
        "input": [{"role": "user", "content": content}],
        "stream": True,
        "tool_choice": "auto",
        "parallel_tool_calls": True,
        "store": False,
        "include": ["reasoning.encrypted_content"],
    }


def classify(status: int, body: bytes | None) -> tuple[str, str]:
    parsed = parse_json_body(body)
    if isinstance(parsed, dict) and "detail" in parsed:
        detail = json.dumps(parsed.get("detail"), default=str)
        return "REJECTED", detail[:120]
    if isinstance(parsed, dict) and "error" in parsed:
        return "ACCEPTED", str(parsed["error"].get("type"))[:40]
    return f"status={status}", ""


def main() -> None:
    store = CredentialStore(Path(os.environ["GATEWAY_DATA_DIR"]))
    headers = auth_headers(store)
    client = UpstreamClient(user_agent="personal-llm-gateway/2.0")

    rejected: list[str] = []
    accepted: list[str] = []

    for field, value in CANDIDATES:
        for with_image in (False, True):
            payload = baseline(with_image)
            payload[field] = value
            body = json.dumps(payload).encode("utf-8")
            response = client.request(
                "POST",
                f"{CHATGPT_BASE}{RESPONSES_PATH}",
                headers={**headers, "Accept": "text/event-stream"},
                body=body,
                stream=True,
                provider_id="chatgpt_codex",
            )
            kind, detail = classify(response.status, response.body)
            tag = "image" if with_image else "text "
            print(f"{field:22s} {tag}  status={response.status:4d}  {kind:9s} {detail}")
            if kind == "REJECTED":
                if field not in rejected:
                    rejected.append(field)
            elif field not in accepted:
                accepted.append(field)

    print()
    print("REJECTED_FIELDS", json.dumps(rejected))
    print("ACCEPTED_FIELDS", json.dumps(accepted))


if __name__ == "__main__":
    main()
