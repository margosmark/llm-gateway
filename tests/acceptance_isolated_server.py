"""Real isolated-server acceptance: invalid save atomicity and console policy gap.

Run: PYTHONPATH=src python3 tests/acceptance_isolated_server.py
Requires Node.js, Playwright (`npm install --no-save playwright`) and Chromium.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def request(url: str, method: str = "GET", body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers=({"Content-Type": "application/json", "Origin": url.split("/owner/")[0]} if data else {}))
    try:
        with urllib.request.urlopen(req, timeout=4) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="gateway-acceptance-") as state_dir:
        data = Path(state_dir)
        # Valid, task-local initial config. Phase-routing policy is deliberately
        # present so a failed alias save must preserve it on disk.
        cfg = {
            "revision": 0, "initialized": True,
            "providers": {
                "chatgpt_codex": {"provider_id": "chatgpt_codex", "kind": "chatgpt_codex", "models": {
                    "gpt-6-luna": {"model_id": "gpt-6-luna", "enabled": True, "dialect": "responses"}}},
                "opencode_go": {"provider_id": "opencode_go", "kind": "opencode_go", "models": {
                    "kimi-k2.5": {"model_id": "kimi-k2.5", "enabled": True, "dialect": "chat_completions"}}},
            },
            "aliases": {"phase-test": {
                "name": "phase-test", "enabled": True,
                "primary": {"provider": "chatgpt_codex", "model": "gpt-6-luna"},
                "fallbacks": [{"provider": "opencode_go", "model": "kimi-k2.5"}],
                "fallback_triggers": ["exhausted"], "fallback_authorized": True,
                "fallback_confirmed_providers": ["opencode_go"], "fallback_error_classes": [],
                "cooldown_seconds": 300,
                "phase_routing": {"enabled": True, "routes": {
                    "plan": {"phase": "plan", "route": {"provider": "chatgpt_codex", "model": "gpt-6-luna"}, "effort": "high"}},
                    "default_route": {"provider": "chatgpt_codex", "model": "gpt-6-luna"},
                    "inference": {"enabled": False, "provider": "opencode_console", "model": "", "minimum_confidence": 0.8, "on_uncertain": "default"}},
            }},
        }
        config_path = data / "config.json"
        config_path.write_text(json.dumps(cfg, separators=(",", ":")), encoding="utf-8")
        port = free_port()
        env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
        proc = subprocess.Popen([sys.executable, "-m", "gateway", "--host", "127.0.0.1", "--port", str(port), "--data-dir", str(data)], cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        base = f"http://127.0.0.1:{port}"
        try:
            for _ in range(100):
                if proc.poll() is not None:
                    raise RuntimeError(f"isolated server exited {proc.returncode}")
                try:
                    request(base + "/owner/api/state")
                    break
                except (OSError, TimeoutError):
                    time.sleep(.1)
            else:
                raise RuntimeError("isolated server did not become ready")
            before = config_path.read_bytes()
            bad_save = {"action": "save", "name": "bad name", "primary": {"provider": "chatgpt_codex", "model": "gpt-6-luna"}, "phase_routing": cfg["aliases"]["phase-test"]["phase_routing"]}
            status, body = request(base + "/owner/api/alias", "POST", bad_save)
            after = config_path.read_bytes()
            parsed = json.loads(body)
            assert status == 400, (status, body)
            assert parsed["error"]["field"] == "alias name", parsed
            assert parsed["error"]["message"] == "the alias name may contain only letters, digits, dots, underscores and hyphens, and must start with a letter or a digit", parsed
            assert before == after, "persisted config changed after rejected save"
            persisted = json.loads(after)
            assert persisted["aliases"]["phase-test"]["phase_routing"] == cfg["aliases"]["phase-test"]["phase_routing"]
            print(f"invalid alias POST: HTTP {status}; field={parsed['error']['field']!r}; message={parsed['error']['message']!r}; config bytes unchanged={len(before)}")
            # Replace task-local file atomically with incomplete trigger policy;
            # state API reads this real on-disk config on the next request.
            persisted["aliases"]["phase-test"]["fallback_triggers"] = ["exhausted"]
            config_path.write_text(json.dumps(persisted, separators=(",", ":")), encoding="utf-8")
            # Trigger fresh state read and rendered browser assertions.
            code = subprocess.run(["node", str(ROOT / "tests/acceptance_policy_gap_browser.js"), base], cwd=ROOT, check=False).returncode
            if code:
                return code
            print("rendered owner console: incomplete policy warnings visible; healthy policy has no false gap")
            return 0
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill(); proc.wait()


if __name__ == "__main__":
    raise SystemExit(main())
