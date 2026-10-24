"""Differential probe of the Codex Responses wire contract.

Upstream validates the request body BEFORE the account allowance check, so
the status is a clean oracle for shape:

* ``400`` (``{"detail": ...}``)  -> the body was rejected: shape is wrong.
* ``429`` (``{"error": {...}}``) -> the body was ACCEPTED, and only the
  shared account allowance stopped it. Shape is right.

So every variant compared against a known-good control isolates exactly which
field makes the backend reject a request. Prints only status codes, JSON field
names and structural labels - never a prompt, a token, an image payload or a
credential.
"""

from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, "/app/src")

from gateway.adapters.http_client import UpstreamClient, parse_json_body
from gateway.bridge.requests import convert_request
from gateway.credentials import CredentialStore

CHATGPT_BASE = "https://chatgpt.com/backend-api/codex"
RESPONSES_PATH = "/responses"

#: A 1x1 PNG. Non-sensitive and tiny.
TINY_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
DATA_URL = f"data:image/png;base64,{TINY_PNG_B64}"


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
    """The fields the Codex client always sends (the gateway's own defaults).

    ``stream`` is only defaulted when the caller did not state one, so the
    non-streaming variant stays a real test of the flag.
    """
    body = dict(payload)
    body.setdefault("stream", True)
    body.setdefault("tool_choice", "auto")
    body.setdefault("parallel_tool_calls", True)
    body["store"] = False
    body.setdefault("include", ["reasoning.encrypted_content"])
    return body


def verdict(status: int, body: bytes | None) -> str:
    parsed = parse_json_body(body)
    if isinstance(parsed, dict):
        if "detail" in parsed:
            return "REJECTED (400 shape)"
        if "error" in parsed:
            err = parsed.get("error")
            etype = err.get("type") if isinstance(err, dict) else None
            if etype == "usage_limit_reached":
                return "ACCEPTED (allowance)"
    return f"OTHER status={status}"


def post(client: UpstreamClient, headers: dict, payload: dict, label: str) -> str:
    body = json.dumps(codex_fields(payload)).encode("utf-8")
    response = client.request(
        "POST",
        f"{CHATGPT_BASE}{RESPONSES_PATH}",
        headers={**headers, "Accept": "text/event-stream"},
        body=body,
        stream=True,
        provider_id="chatgpt_codex",
    )
    result = verdict(response.status, response.body)
    print(f"{label:46s} status={response.status:4d}  {result}")
    if response.status == 400:
        parsed = parse_json_body(response.body)
        detail = json.dumps(parsed.get("detail"), default=str)[:500]
        print(f"{'':46s} detail={detail}")
    return result


def main() -> None:
    store = CredentialStore(Path(os.environ["GATEWAY_DATA_DIR"]))
    headers = auth_headers(store)
    client = UpstreamClient(user_agent="personal-llm-gateway/2.0")

    # --- controls -------------------------------------------------------
    text_only = {
        "model": "gpt-5.6-terra",
        "messages": [{"role": "user", "content": [{"type": "text", "text": "say ok"}]}],
        "stream": True,
    }
    converted_text = convert_request(text_only, "openai_chat", "openai_responses")
    post(client, headers, converted_text, "1 text-only (control)")

    # --- what the gateway emits today for an image ----------------------
    chat_image = copy.deepcopy(text_only)
    chat_image["messages"][0]["content"] = [
        {"type": "text", "text": "say ok"},
        {"type": "image_url", "image_url": {"url": DATA_URL}},
    ]
    converted_image = convert_request(chat_image, "openai_chat", "openai_responses")
    shape = json.dumps(converted_image.get("input"), default=str)
    shape = shape.replace(DATA_URL, "<data-url>")
    print(f"{'gateway emits image as:':46s} {shape[:300]}")
    post(client, headers, converted_image, "2 image (gateway-converted)")

    # --- vary one field at a time off the accepted baseline -------------
    def variant(label: str, mutate) -> None:
        payload = copy.deepcopy(converted_image)
        mutate(payload)
        post(client, headers, payload, label)

    def add_instructions(p):
        p["instructions"] = "Be terse."

    def add_tools(p):
        p["tools"] = [{
            "type": "function",
            "name": "lookup",
            "description": "",
            "parameters": {"type": "object", "properties": {}},
        }]

    def tools_strict(p):
        p["tools"] = [{
            "type": "function",
            "name": "lookup",
            "description": "",
            "strict": False,
            "parameters": {"type": "object", "properties": {}},
        }]

    def add_reasoning(p):
        p["reasoning"] = {"effort": "high"}

    def add_max_out(p):
        p["max_output_tokens"] = 64

    def add_temperature(p):
        p["temperature"] = 0.2

    def add_stop(p):
        p["stop"] = ["\n\n"]

    def add_metadata(p):
        p["metadata"] = {"k": "v"}

    def nested_image_url(p):
        for item in p.get("input", []):
            for part in (item.get("content") or []) if isinstance(item, dict) else []:
                if isinstance(part, dict) and part.get("type") == "input_image":
                    part["image_url"] = {"url": DATA_URL}

    def typed_message(p):
        for item in p.get("input", []):
            if isinstance(item, dict) and "role" in item:
                item["type"] = "message"

    def detail_auto(p):
        for item in p.get("input", []):
            for part in (item.get("content") or []) if isinstance(item, dict) else []:
                if isinstance(part, dict) and part.get("type") == "input_image":
                    part["detail"] = "auto"

    def detail_high(p):
        for item in p.get("input", []):
            for part in (item.get("content") or []) if isinstance(item, dict) else []:
                if isinstance(part, dict) and part.get("type") == "input_image":
                    part["detail"] = "high"

    def drop_detail_field(p):
        for item in p.get("input", []):
            for part in (item.get("content") or []) if isinstance(item, dict) else []:
                if isinstance(part, dict) and part.get("type") == "input_image":
                    part.pop("detail", None)

    def http_image_url(p):
        for item in p.get("input", []):
            for part in (item.get("content") or []) if isinstance(item, dict) else []:
                if isinstance(part, dict) and part.get("type") == "input_image":
                    part["image_url"] = "https://raw.githubusercontent.com/octocat/Hello-World/master/README"

    def assistant_image(p):
        p["input"].append({
            "role": "assistant",
            "content": [
                {"type": "output_text", "text": "here you go"},
                {"type": "input_image", "image_url": DATA_URL},
            ],
        })

    def jpeg_image(p):
        for item in p.get("input", []):
            for part in (item.get("content") or []) if isinstance(item, dict) else []:
                if isinstance(part, dict) and part.get("type") == "input_image":
                    part["image_url"] = (
                        "data:image/jpeg;base64,"
                        "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0a"
                        "HBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAABAAEBAREA/8QAFAABAAAAAAAA"
                        "AAAAAAAAAAAACf/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AKp//2Q=="
                    )

    def webp_image(p):
        for item in p.get("input", []):
            for part in (item.get("content") or []) if isinstance(item, dict) else []:
                if isinstance(part, dict) and part.get("type") == "input_image":
                    part["image_url"] = "data:image/webp;base64,UklGRiQAAABXRUJQVlA4IBgAAAAwAQCdASoBAAEAAwA0JaQAA3AA/vuUAAA="

    def bare_image_url_field(p):
        for item in p.get("input", []):
            for part in (item.get("content") or []) if isinstance(item, dict) else []:
                if isinstance(part, dict) and part.get("type") == "input_image":
                    part.pop("image_url", None)
                    part["image"] = DATA_URL

    def file_id_shape(p):
        for item in p.get("input", []):
            for part in (item.get("content") or []) if isinstance(item, dict) else []:
                if isinstance(part, dict) and part.get("type") == "input_image":
                    part.pop("image_url", None)
                    part["file_id"] = "file-abc123"

    print()
    print("--- single-field variations off the accepted baseline ---")
    variant("+ instructions (system prompt)", add_instructions)
    variant("+ tools", add_tools)
    variant("+ tools with strict:false", tools_strict)
    variant("+ reasoning {effort:high}", add_reasoning)
    variant("+ max_output_tokens", add_max_out)
    variant("+ temperature", add_temperature)
    variant("+ stop", add_stop)
    variant("+ metadata", add_metadata)
    variant("image_url nested {url}", nested_image_url)
    variant("message item typed 'message'", typed_message)
    variant("input_image detail=auto", detail_auto)
    variant("input_image detail=high", detail_high)
    variant("image_url http(s) remote", http_image_url)
    variant("assistant msg with input_image", assistant_image)
    variant("image_url jpeg data url", jpeg_image)
    variant("image_url webp data url", webp_image)
    variant("image part uses 'image' field", bare_image_url_field)
    variant("image part uses file_id", file_id_shape)

    # non-streaming is what the gateway's buffered path sends
    print()
    print("--- stream flag ---")
    nonstream = copy.deepcopy(converted_image)
    nonstream["stream"] = False
    body = json.dumps(codex_fields(nonstream)).encode("utf-8")
    resp = client.request(
        "POST", f"{CHATGPT_BASE}{RESPONSES_PATH}",
        headers={**headers, "Accept": "text/event-stream"}, body=body,
        stream=True, provider_id="chatgpt_codex")
    print(f"{'stream:false body':46s} status={resp.status:4d}  {verdict(resp.status, resp.body)}")


if __name__ == "__main__":
    main()
