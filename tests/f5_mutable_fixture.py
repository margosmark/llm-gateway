#!/usr/bin/env python3
"""F5 mutable isolated fixture — orchestrator (test-only, loopback-only).

Runs the exact pinned candidate gateway on 127.0.0.1 with a synthetic data
directory authored here (never copied from another task's workspace), then
drives tests/f5_mutable_browser.js in real Chromium against it. Server-side
cooldown/primary mutations are performed from inside the browser test through
the gateway's own owner API (same-origin fetch, canonical Origin header) at
the exact moments the page holds a stale snapshot — no proxy, no product
edits, no production contact, and no lifecycle commands against any shared
stack: this script starts and stops only the child process it spawned.

Usage:
    python3 tests/f5_mutable_fixture.py \
        --candidate /path/to/exact-sha-checkout \
        [--port 55041] [--data-dir DIR] [--evidence-dir DIR] [--skip-sha-check]

Exit code 0 means every browser assertion passed. Evidence (JSON + PNGs)
is written under --evidence-dir.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

PINNED_SHA = "a8f5f1326b3af03f2ce8aa9a54c56f44f79a6567"
PINNED_TREE = "4f5f87b3a300792799016f47cc1468408bf661e9"

FALLBACK_TRIGGERS = [
    "exhausted",
    "rate_limited",
    "upstream_unavailable",
    "explicit_cooldown",
    "route_ineligible",
    "probe_contention",
]


def model_entry(model_id: str, dialect: str) -> dict:
    return {
        "model_id": model_id,
        "enabled": True,
        "dialect": dialect,
        "source": "manual",
        "observed_at": None,
        "limits_known": False,
        "notes": "F5 fixture model (synthetic; no provider contact)",
        "reasoning_efforts": [],
        "supports_streaming": True,
        "supports_function_tools": True,
        "supports_builtin_tools": False,
        "supports_image_input": False,
        "context_window": None,
        "context_limit": None,
        "max_output_tokens": None,
        "retention_class": "unknown",
        "monthly_limit_usd": None,
        "limits_source": "unknown",
        "limits_observed_at": None,
    }


def provider(provider_id: str, kind: str, state: str, models: list[str], dialect: str) -> dict:
    return {
        "provider_id": provider_id,
        "kind": kind,
        "state": state,
        "last_test_at": None,
        "last_error": None,
        "catalog_observed_at": None,
        "models": {m: model_entry(m, dialect) for m in models},
    }


def alias(name: str, primary: tuple[str, str], fallbacks: list[tuple[str, str]],
          triggers: list[str], confirmed: list[str], cooldown: int) -> dict:
    return {
        "name": name,
        "enabled": True,
        "primary": {"provider": primary[0], "model": primary[1]},
        "fallbacks": [{"provider": p, "model": m} for p, m in fallbacks],
        "fallback_triggers": triggers,
        "fallback_authorized": True,
        "fallback_confirmed_providers": confirmed,
        "fallback_error_classes": [],
        "cooldown_seconds": cooldown,
        "phase_routing": None,
    }


def synthetic_config() -> dict:
    """A fully synthetic trusted-LAN config.

    coding: all triggers, connected providers, but its primary route carries a
            seeded open circuit at boot -> verdict "Cooldown" at first render.
    flash:  all triggers, connected providers, no circuit -> verdict "Healthy".
            The browser watches this card flip to "Stale" past the 60s
            threshold and back to "Healthy" with fresh, mutated data after a
            focus/visibility refresh.
    """
    return {
        "revision": 1,
        "initialized": True,
        "access_mode": "lan_noauth",
        "providers": {
            "anthropic": provider(
                "anthropic", "anthropic", "connected", ["claude-fixture"],
                "anthropic_messages",
            ),
            "chatgpt_codex": provider(
                "chatgpt_codex", "chatgpt_codex", "not_configured", [], "openai_responses"),
            "opencode_go": provider(
                "opencode_go", "opencode_go", "connected",
                ["kimi-smoke2", "kimi-smoke2b"], "openai_chat",
            ),
            "opencode_console": provider(
                "opencode_console", "opencode_console", "not_configured", [], "openai_chat"),
        },
        "aliases": {
            "coding": alias(
                "coding",
                ("opencode_go", "kimi-smoke2"),
                [("anthropic", "claude-fixture")],
                FALLBACK_TRIGGERS,
                ["anthropic"],
                300,
            ),
            "flash": alias(
                "flash",
                ("anthropic", "claude-fixture"),
                [("opencode_go", "kimi-smoke2b")],
                FALLBACK_TRIGGERS,
                ["opencode_go"],
                300,
            ),
        },
        "profiles": {},
        "settings": {},
    }


def synthetic_circuits(now: float) -> dict:
    """One seeded OPEN circuit on coding's primary route (opencode_go/kimi-smoke2).

    Loaded by CircuitStore at boot; makes coding render "Cooldown" until the
    browser clears it server-side via POST /owner/api/settings clear_cooldowns.
    """
    return {
        "version": 1,
        "circuits": {
            "opencode_go/kimi-smoke2": {
                "provider": "opencode_go",
                "model": "kimi-smoke2",
                "provider_wide": False,
                "armed": True,
                "error_class": "rate_limited",
                "failures": 3,
                "opened_at": now,
                "until": now + 1800.0,
                "reason": "F5 fixture: seeded open circuit for routing-health evidence",
            }
        },
    }


def git_output(candidate: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(candidate), *args],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def http_json(url: str, timeout: float = 10.0) -> tuple[int, dict | str]:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode()
            status = resp.status
    except urllib.error.HTTPError as exc:
        body = exc.read().decode()
        status = exc.code
    try:
        return status, json.loads(body)
    except (ValueError, json.JSONDecodeError):
        return status, body


def as_dict(value: dict | str) -> dict:
    """Narrow an http_json payload to dict, failing loudly if it is not."""
    if not isinstance(value, dict):
        raise RuntimeError(f"expected a JSON object, got: {value!r}")
    return value


def wait_for_gateway(base: str, deadline: float = 30.0) -> None:
    end = time.time() + deadline
    while time.time() < end:
        try:
            status, _ = http_json(f"{base}/health", timeout=2.0)
            status2, _ = http_json(f"{base}/owner/api/state", timeout=2.0)
            if status == 200 and status2 == 200:
                return
        except (urllib.error.URLError, ConnectionError, socket.timeout):
            pass
        time.sleep(0.3)
    raise RuntimeError(f"gateway at {base} did not become ready within {deadline}s")


def main() -> int:
    here = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", default=str(here / ".f5" / "candidate"),
                        help="checkout of the pinned candidate (exact SHA)")
    parser.add_argument("--port", type=int, default=55041)
    parser.add_argument("--data-dir", default=None,
                        help="fresh synthetic data dir (default: <workspace>/.f5/data)")
    parser.add_argument("--evidence-dir", default=None,
                        help="evidence output dir (default: <workspace>/.f5/evidence)")
    parser.add_argument("--skip-sha-check", action="store_true")
    args = parser.parse_args()

    candidate = Path(args.candidate).resolve()
    if not (candidate / "src" / "gateway").is_dir():
        print(f"FATAL: {candidate} does not look like a gateway checkout", file=sys.stderr)
        return 2

    if not args.skip_sha_check:
        head = git_output(candidate, "rev-parse", "HEAD")
        tree = git_output(candidate, "rev-parse", "HEAD^{tree}")
        print(f"candidate HEAD={head} tree={tree}")
        if head != PINNED_SHA or tree != PINNED_TREE:
            print(f"FATAL: candidate is not the pinned SHA/tree\n"
                  f"  expected {PINNED_SHA} / {PINNED_TREE}", file=sys.stderr)
            return 2
        status = git_output(candidate, "status", "--porcelain")
        if status:
            print("FATAL: candidate checkout has local modifications:\n" + status, file=sys.stderr)
            return 2

    data_dir = Path(args.data_dir).resolve() if args.data_dir else here / ".f5" / "data"
    evidence_dir = Path(args.evidence_dir).resolve() if args.evidence_dir else here / ".f5" / "evidence"
    if data_dir.exists():
        shutil.rmtree(data_dir)
    data_dir.mkdir(parents=True)
    evidence_dir.mkdir(parents=True, exist_ok=True)

    now = time.time()
    (data_dir / "config.json").write_text(json.dumps(synthetic_config(), indent=2) + "\n")
    (data_dir / "circuits.json").write_text(json.dumps(synthetic_circuits(now), indent=2) + "\n")
    print(f"synthetic data dir: {data_dir} (config.json + seeded circuits.json)")

    base = f"http://127.0.0.1:{args.port}"
    env = dict(os.environ)
    env["PYTHONPATH"] = str(candidate / "src")
    proc = subprocess.Popen(
        [sys.executable, "-m", "gateway", "--host", "127.0.0.1",
         "--port", str(args.port), "--data-dir", str(data_dir)],
        cwd=str(candidate), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        wait_for_gateway(base)
        print(f"isolated candidate gateway up on {base} (pid {proc.pid})")

        state_status, state_raw = http_json(f"{base}/owner/api/state")
        state = as_dict(state_raw)
        (evidence_dir / "server-state-before-browser.json").write_text(
            json.dumps(state, indent=2, sort_keys=True) + "\n")
        print(f"pre-browser /owner/api/state: HTTP {state_status}, revision={state.get('revision')}, "
              f"aliases={[a['name'] for a in state.get('aliases', [])]}")

        node_env = dict(os.environ)
        node_env["NODE_PATH"] = os.environ.get(
            "F5_NODE_PATH", "/home/hermes/electricity-dashboard/webapp/node_modules")
        node_env["F5_BASE"] = base
        node_env["F5_EVIDENCE_DIR"] = str(evidence_dir)
        result = subprocess.run(
            ["node", str(here / "tests" / "f5_mutable_browser.js")],
            cwd=str(here), env=node_env,
            capture_output=True, text=True,
        )
        print("=== browser test stdout ===")
        print(result.stdout)
        if result.stderr:
            print("=== browser test stderr ===", file=sys.stderr)
            print(result.stderr, file=sys.stderr)
        (evidence_dir / "browser-test-output.txt").write_text(
            result.stdout + "\n--- stderr ---\n" + result.stderr)

        post_status, post_raw = http_json(f"{base}/owner/api/state")
        post = as_dict(post_raw)
        (evidence_dir / "server-state-after-browser.json").write_text(
            json.dumps(post, indent=2, sort_keys=True) + "\n")
        print(f"post-browser /owner/api/state: HTTP {post_status}, revision={post.get('revision')}")
        return result.returncode
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        print("isolated gateway stopped (only the child this script spawned)")


if __name__ == "__main__":
    sys.exit(main())
