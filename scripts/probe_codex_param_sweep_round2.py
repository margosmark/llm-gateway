"""Round 2: which request fields does the Codex backend reject *today*?

Round 1 (`probe_codex_param_sweep.py`, commit 1d9fe2f) built
``CODEX_UNSUPPORTED_PAYLOAD_FIELDS``. Round 2 answers the question the
"malformed" 502 incident left open: is the deployed drop list still the whole
truth, on **both enabled models** (``gpt-5.6-terra`` and ``gpt-6-luna``), for
text *and* image input, and is anything the gateway itself adds
(``tool_choice``, ``parallel_tool_calls``, ``store``, ``include``) rejected on
either model?

The methodology is round 1's, unchanged and deliberately so:

* one field per request, everything else held at the known-good baseline, so a
  400 implicates that field alone;
* the identical body without the field must produce the account-allowance
  wall, never a 400 - that proves validation ran *before* the allowance check,
  so a 400 is a genuine shape rejection rather than an exhausted account;
* if the baseline itself answers 200/SSE the allowance has recovered, the
  sweep stops rather than spending the account, and only the already-declared
  controls are re-confirmed.

Prints status codes and field names only. Never prints a prompt, an image
payload, a token or a credential.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, "/app/src")

from gateway.adapters.http_client import UpstreamClient, parse_json_body
from gateway.credentials import CredentialStore
from gateway.observability import redact

CHATGPT_BASE = "https://chatgpt.com/backend-api/codex"
RESPONSES_PATH = "/responses"

#: The two models the production aliases actually route to.
MODELS = ("gpt-5.6-terra", "gpt-6-luna")

TINY_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
DATA_URL = f"data:image/png;base64,{TINY_PNG_B64}"

#: Controls: fields round 1 proved rejected. They re-confirm the methodology
#: still discriminates before any new candidate is trusted.
CONTROLS = [
    ("temperature", 0.3),
    ("max_tokens", 4000),
]

#: New candidates. Everything here is either a Chat Completions field a
#: client may send (and the gateway may forward on a same-dialect route) or a
#: field the gateway itself adds to every Codex request.
CANDIDATES = [
    ("stream_options", {"include_usage": True}),
    ("functions", [{"name": "f", "parameters": {"type": "object"}}]),
    ("function_call", "auto"),
    ("max_tool_calls", 3),
    ("prediction", {"type": "content", "content": "ok"}),
    ("modalities", ["text"]),
    ("web_search_options", {}),
    ("prompt_cache_key", "probe"),
    ("safety_identifier", "probe"),
    ("store", True),
    ("previous_response_id", "resp-probe"),
    ("truncation", "disabled"),
    ("reasoning_effort", "high"),
    ("parallel_tool_calls", False),
    ("tool_choice", "auto"),
    ("include", []),
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


def baseline(model: str, with_image: bool) -> dict:
    """The body the adapter actually sends, before the router's strip."""
    content: list[dict] = [{"type": "input_text", "text": "say ok"}]
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


def _safe_detail(parsed) -> str:
    """Field names and messages only, bounded and redacted.

    A rejection body is provider text and could echo request content, so only
    ``loc`` path segments (identifiers) and short ``msg`` strings are kept.
    """
    if not isinstance(parsed, dict):
        return ""
    detail = parsed.get("detail")
    entries = detail if isinstance(detail, list) else [detail]
    safe: list[str] = []
    for entry in entries[:4]:
        if isinstance(entry, dict):
            loc = entry.get("loc")
            if isinstance(loc, list):
                path = ".".join(str(part) for part in loc if isinstance(part, (str, int)))
                if path:
                    safe.append(path[:80])
            msg = entry.get("msg")
            if isinstance(msg, str):
                safe.append(redact(msg)[:80])
        elif isinstance(entry, str):
            safe.append(redact(entry)[:80])
    return "; ".join(safe)


def classify(status: int, body: bytes | None) -> tuple[str, str]:
    parsed = parse_json_body(body)
    if isinstance(parsed, dict) and "detail" in parsed:
        return "REJECTED", _safe_detail(parsed)
    if isinstance(parsed, dict) and "error" in parsed:
        error = parsed.get("error")
        kind = error.get("type") if isinstance(error, dict) else None
        return "ACCEPTED", redact(str(kind))[:40]
    return f"status={status}", ""


def probe(client, headers, model: str, field: str | None, value, with_image: bool) -> tuple[int, str, str]:
    payload = baseline(model, with_image)
    if field is not None:
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
    return response.status, kind, detail


def main() -> None:
    store = CredentialStore(Path(os.environ["GATEWAY_DATA_DIR"]))
    headers = auth_headers(store)
    client = UpstreamClient(user_agent="personal-llm-gateway/2.0")

    rejected: dict[str, list[str]] = {}
    accepted: dict[str, list[str]] = {}
    allowed_to_spend = True

    for model in MODELS:
        for with_image in (False, True):
            tag = "image" if with_image else "text "
            status, kind, detail = probe(client, headers, model, None, None, with_image)
            print(f"{'BASELINE':22s} {model:14s} {tag}  status={status:4d}  {kind:9s} {detail}")
            if status in (200, 201):
                # The allowance has recovered: every further accepted body
                # would spend the account. Stop the sweep, keep only the
                # controls that prove the method still discriminates.
                allowed_to_spend = False
            if kind == "REJECTED":
                print("ABORT: the baseline itself is rejected; the sweep would not be interpretable")
                return

            for field, value in (CONTROLS + (CANDIDATES if allowed_to_spend else [])):
                status, kind, detail = probe(client, headers, model, field, value, with_image)
                print(f"{field:22s} {model:14s} {tag}  status={status:4d}  {kind:9s} {detail}")
                bucket = rejected if kind == "REJECTED" else accepted
                bucket.setdefault(field, [])
                if model not in bucket[field]:
                    bucket[field].append(model)
                if status in (200, 201):
                    allowed_to_spend = False

    print()
    print("REJECTED_FIELDS", json.dumps(rejected, sort_keys=True))
    print("ACCEPTED_FIELDS", json.dumps(accepted, sort_keys=True))
    if not allowed_to_spend:
        print("NOTE the allowance recovered mid-sweep; later candidates were not all probed")


if __name__ == "__main__":
    main()
