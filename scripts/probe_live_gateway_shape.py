"""Replay the failing production shape against the LIVE gateway.

This is a non-sensitive, image-free-prompt probe: a 1x1 PNG and the literal
word "ok". It reproduces what Hermes' auxiliary vision path actually sends
(a NON-streaming Chat Completions request that carries ``temperature``) and
separates the two candidate failure causes:

  * image-capability exclusion  -> 503 no_eligible_route, route skipped
  * unsupported sampling param  -> 502 malformed, upstream 400

Prints only status, error class, serving provider/model and skipped reasons.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

GATEWAY = "http://10.10.11.109:8765/v1/chat/completions"

TINY_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
DATA_URL = f"data:image/png;base64,{TINY_PNG_B64}"


def post(label: str, payload: dict) -> None:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        GATEWAY, data=body, method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            status, raw = resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read()
    except Exception as exc:  # noqa: BLE001
        print(f"{label:44s} TRANSPORT {type(exc).__name__}: {exc}")
        return
    try:
        parsed = json.loads(raw)
    except Exception:  # noqa: BLE001
        parsed = {}
    err = parsed.get("error") or {}
    detail = parsed.get("detail")
    serving = parsed.get("serving_provider") or parsed.get("provider") or parsed.get("model")
    msg = str(err.get("message") or detail or "")[:150]
    print(f"{label:44s} status={status} type={err.get('type')!r} model={serving!r}")
    if msg:
        print(f"{'':44s}   message={msg!r}")
    for k in ("skipped", "attempts", "fallback_reason"):
        if k in parsed:
            print(f"{'':44s}   {k}={json.dumps(parsed[k])[:400]}")


def main() -> None:
    text = [{"type": "text", "text": "reply with the single word ok"}]
    image = text + [{"type": "image_url", "image_url": {"url": DATA_URL}}]

    print("=== alias 'pm' (primary: chatgpt_codex/gpt-5.6-terra) ===")
    post("A text, stream=true", {"model": "pm", "messages": [{"role": "user", "content": text}], "stream": True})
    post("B text, stream=false", {"model": "pm", "messages": [{"role": "user", "content": text}]})
    post("C text, stream=false + temperature", {
        "model": "pm", "messages": [{"role": "user", "content": text}], "temperature": 0.1})
    post("D image, stream=false", {
        "model": "pm", "messages": [{"role": "user", "content": image}]})
    post("E image, stream=false + temperature", {
        "model": "pm", "messages": [{"role": "user", "content": image}], "temperature": 0.1})
    post("F image, stream=true", {
        "model": "pm", "messages": [{"role": "user", "content": image}], "stream": True})


if __name__ == "__main__":
    main()
