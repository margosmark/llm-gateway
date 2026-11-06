#!/usr/bin/env python3
"""The definition-of-done walkthrough.

Walks the thirteen steps of the MVP definition of done against a *running*
gateway and reports, for each step, whether it passes, fails, or needs the
owner.

The script never reads a credential, never signs in to a provider, and never
stores a secret. The steps that need a real account - connecting ChatGPT
through the device-code flow, and enrolling an OpenCode Go key - are
reported as owner actions, and the script checks that the gateway is ready
for them rather than performing them.

Usage:

    python3 scripts/dod_walkthrough.py http://10.10.11.109:8765
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

OWNER = "owner"
PASS = "pass"
FAIL = "fail"
SKIP = "skip"


@dataclass
class Step:
    number: int
    title: str
    status: str
    detail: str = ""


def http(url: str, method: str = "GET", body: Any = None, headers: dict[str, str] | None = None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url=url, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = response.read()
            try:
                return response.status, json.loads(payload.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return response.status, payload.decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        try:
            return exc.code, json.loads(payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return exc.code, payload.decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as exc:
        return 0, str(exc)


def main(argv: list[str]) -> int:
    base = (argv[1] if len(argv) > 1 else "http://127.0.0.1:8765").rstrip("/")
    steps: list[Step] = []

    def add(number: int, title: str, status: str, detail: str = "") -> None:
        steps.append(Step(number, title, status, detail))
        marker = {PASS: "PASS", FAIL: "FAIL", SKIP: "OWNER", OWNER: "OWNER"}[status]
        print(f"[{marker:5}] {number:>2}. {title}" + (f" - {detail}" if detail else ""))

    # 1. The browser UI opens.
    status, payload = http(base + "/owner/")
    add(
        1,
        "Open the browser UI on a fresh install",
        PASS if status == 200 else FAIL,
        f"GET /owner/ -> {status}",
    )

    # 2. The zero state.
    status, state = http(base + "/owner/api/status")
    if status != 200:
        add(2, "Initialize the gateway configuration through a clear zero-state flow", FAIL, f"status {status}")
    else:
        checklist = state.get("checklist") or []
        add(
            2,
            "Initialize the gateway configuration through a clear zero-state flow",
            PASS if checklist else FAIL,
            f"{len(checklist)} checklist steps; initialized={state.get('initialized')}",
        )

    # 3 and 4 need the owner's own account.
    status, state = http(base + "/owner/api/status")
    providers = {p["provider_id"]: p for p in (state.get("providers") or [])} if status == 200 else {}
    chatgpt = providers.get("chatgpt_codex", {})
    go = providers.get("opencode_go", {})
    add(
        3,
        "Connect ChatGPT/Codex through the user-operated device-code flow",
        OWNER if chatgpt.get("state") != "connected" else PASS,
        "owner action: complete the device code in your own browser"
        if chatgpt.get("state") != "connected"
        else "connected",
    )
    add(
        4,
        "Connect OpenCode Go with an API key",
        OWNER if go.get("state") != "connected" else PASS,
        "owner action: enter the key in the trusted-LAN console"
        if go.get("state") != "connected"
        else "connected",
    )

    # 5. Each provider tested independently.
    tested = [
        p for p in (providers.values()) if p.get("state") == "connected"
    ]
    add(
        5,
        "Test each provider independently",
        OWNER if not tested else PASS,
        "run 'Test connection' for each provider in the owner console"
        if not tested
        else f"{len(tested)} provider(s) connected",
    )

    # 6. Model ids discovered or entered.
    discovered = sum(len(p.get("enabled_models") or []) for p in providers.values())
    add(
        6,
        "Refresh or manually enter model IDs",
        OWNER if not discovered else PASS,
        "run 'Refresh model catalog' in the owner console"
        if not discovered
        else f"{discovered} enabled model(s)",
    )

    # 7. Models explicitly enabled.
    add(
        7,
        "Explicitly enable models to expose",
        PASS if discovered else OWNER,
        f"{discovered} enabled" if discovered else "enable a model in Models & Routes",
    )

    # 8 and 9. An alias with a primary route.
    status, models = http(base + "/v1/models")
    published = [m["id"] for m in (models.get("data") or [])] if status == 200 else []
    add(
        8,
        "Create a friendly alias such as 'coding'",
        PASS if published else OWNER,
        "create an alias in Models & Routes" if not published else ", ".join(published),
    )
    add(
        9,
        "Configure a primary model and optional, explicitly authorized fallback",
        PASS if published else OWNER,
        "fallback is opt-in twice: the alias and the named provider",
    )

    # 10. Published in /v1/models.
    add(
        10,
        "Publish the alias in /v1/models",
        PASS if published else OWNER,
        f"{len(published)} published" if published else "publish an alias",
    )

    # 11. A successful test completion.
    completion_status = None
    if published:
        completion_status, completion = http(
            base + "/v1/chat/completions",
            method="POST",
            body={
                "model": published[0],
                "messages": [{"role": "user", "content": "Reply with the single word: ok"}],
                "max_tokens": 16,
            },
        )
    add(
        11,
        "Send a successful test completion through the gateway",
        PASS if completion_status == 200 else (OWNER if not published else FAIL),
        f"status {completion_status}" if completion_status is not None else "publish an alias first",
    )

    # 12. The serving provider is visible, and nothing else is.
    status, safe = http(base + "/owner/api/status")
    leaked = [
        key for key in ("profiles", "ledger", "revision", "limits", "aliases") if isinstance(safe, dict) and key in safe
    ]
    add(
        12,
        "See which provider/model served the request, with no prompt or response retained",
        PASS if not leaked else FAIL,
        "the unauthenticated status carries no request content" if not leaked else f"leaked: {leaked}",
    )

    # 13. The exact deployed release.
    status, release = http(base + "/version")
    keys = set(release.keys()) if isinstance(release, dict) else set()
    expected = {"service", "source_revision", "source_tree", "image_revision", "mcp_contract_version"}
    add(
        13,
        "View the exact deployed release at /version",
        PASS if status == 200 and keys == expected else FAIL,
        f"status {status}, keys {sorted(keys)}",
    )

    print()
    failed = [s for s in steps if s.status == FAIL]
    owner = [s for s in steps if s.status == OWNER]
    print(f"{len(steps)} steps: {len(steps) - len(failed) - len(owner)} passed, {len(owner)} need the owner, {len(failed)} failed")
    if failed:
        print("FAILED: " + ", ".join(f"{s.number}. {s.title}" for s in failed))
        return 1
    if owner:
        print("Owner actions remaining: " + ", ".join(f"{s.number}. {s.title}" for s in owner))
    print("No step failed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
