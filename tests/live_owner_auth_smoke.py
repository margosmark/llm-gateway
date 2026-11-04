"""Live smoke test: owner login and client API key over real sockets.

Spawns a real gateway process on a throwaway data directory and a local
port, drives it over HTTP, then reports one line per check. This is an
isolated fixture: it never touches a production data directory or a
production port, and it stops the process it started.

Run it by hand (it is not collected by ``unittest discover``):

    PYTHONPATH=src python3 tests/live_owner_auth_smoke.py

Env: GATEWAY_SMOKE_PORT (default 8799), GATEWAY_SMOKE_DATA_DIR.
"""
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
HOST = "127.0.0.1"
PORT = int(os.environ.get("GATEWAY_SMOKE_PORT", "8799"))
BASE = f"http://{HOST}:{PORT}"
ROOT = Path(os.environ.get("GATEWAY_SMOKE_DATA_DIR") or tempfile.mkdtemp(prefix="owner_smoke_"))
DATA = ROOT / "data"
LOG = ROOT / "server.log"

failures = []


def check(name, cond, detail=None):
    text = "" if detail is None else str(detail)
    print(("  ok   " if cond else "  FAIL ") + name + ((" - " + text) if text and not cond else ""))
    if not cond:
        failures.append(name + ((" - " + text) if text else ""))


def call(method, path, body=None, cookie=None, headers=None, token=None):
    req = urllib.request.Request(BASE + path, method=method)
    req.add_header("Host", f"{HOST}:{PORT}")
    req.add_header("Origin", BASE)
    if cookie:
        req.add_header("Cookie", cookie)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data=data, timeout=20) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, dict(resp.headers), raw
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        return exc.code, dict(exc.headers), raw


def cookie_from(headers):
    sc = headers.get("Set-Cookie", "")
    return sc.split(";")[0] if sc else None


def main():
    if ROOT.exists():
        subprocess.run(["rm", "-rf", str(ROOT)])
    DATA.mkdir(parents=True)
    log = open(LOG, "w")
    proc = subprocess.Popen(
        [sys.executable, "-m", "gateway", "--host", HOST, "--port", str(PORT),
         "--data-dir", str(DATA)],
        cwd=str(REPO),
        env={**os.environ, "PYTHONPATH": "src"},
        stdout=log, stderr=subprocess.STDOUT,
    )
    cookie = None
    try:
        for _ in range(80):
            time.sleep(0.5)
            try:
                st, _, raw = call("GET", "/owner/api/auth/session")
                if st == 200:
                    break
            except Exception:
                continue
        else:
            print("server never became ready")
            return 1

        # 1. fresh runtime: setup required, no session
        payload = json.loads(raw)
        check("fresh runtime requires first-run setup", payload.get("setup_required") is True, payload)
        check("fresh runtime has no session", payload.get("authenticated") is False, payload)
        code_file = DATA / "owner_setup_code"
        check("setup code exists on the host only, owner-readable",
              code_file.exists() and (code_file.stat().st_mode & 0o777) == 0o600,
              oct(code_file.stat().st_mode & 0o777) if code_file.exists() else "missing")

        # 2. setup code must never appear in an HTTP response
        st, _, raw = call("GET", "/owner/api/auth/session")
        check("setup code is not in any HTTP response", code_file.read_text().strip() not in raw, raw[:200])

        # 3. state is refused before sign-in
        st, _, raw = call("GET", "/owner/api/state")
        check("state refused before sign-in (401)", st == 401, f"{st} {raw[:160]}")
        st, _, raw = call("POST", "/owner/api/model", {"action": "clear"})
        check("mutation refused before sign-in (401)", st == 401, f"{st} {raw[:160]}")

        # 4. wrong password throttled, correct one works
        st, _, raw = call("POST", "/owner/api/auth/login", {"username": "owner", "password": "wrong"})
        check("login before setup is refused", st == 409 and "setup_required" in raw, f"{st} {raw[:160]}")
        setup_code = json.loads(code_file.read_text())["setup_code"]
        st, hdrs, raw = call("POST", "/owner/api/auth/setup",
                             {"setup_code": setup_code, "username": "owner", "password": "smoke-Passw0rd-1"})
        check("first-run setup succeeds", st == 200, f"{st} {raw[:200]}")
        cookie = cookie_from(hdrs)
        check("setup sets an HttpOnly SameSite=Strict cookie",
              cookie and "HttpOnly" in hdrs.get("Set-Cookie", "") and "SameSite=Strict" in hdrs.get("Set-Cookie", ""),
              hdrs.get("Set-Cookie"))
        check("setup code file consumed", not code_file.exists(), "still present")
        st, _, raw = call("POST", "/owner/api/auth/setup",
                          {"setup_code": setup_code, "username": "second", "password": "second-Passw0rd"})
        # A second setup cannot create or replace the account: once an
        # account exists the endpoint refuses before it even looks at a code.
        check("setup cannot be replayed after the account exists",
              st == 409 and "already_configured" in raw, f"{st} {raw[:160]}")
        st, _, raw = call("POST", "/owner/api/auth/login",
                          {"username": "second", "password": "second-Passw0rd"})
        check("no default admin/admin account exists", st in (401, 409, 429), f"{st} {raw[:160]}")

        # 5. signed in: state and mutations work
        st, _, raw = call("GET", "/owner/api/state", cookie=cookie)
        check("state readable with session", st == 200 and "profiles" in raw, f"{st} {raw[:120]}")
        st, _, raw = call("GET", "/owner/api/state")
        check("state refused without session", st == 401, f"{st}")
        st, _, raw = call("POST", "/owner/api/auth/logout", {}, cookie=cookie)
        check("logout succeeds", st == 200, f"{st}")
        st, _, raw = call("GET", "/owner/api/state", cookie=cookie)
        check("session dead after logout", st == 401, f"{st}")

        # sign back in
        st, hdrs, raw = call("POST", "/owner/api/auth/login",
                             {"username": "owner", "password": "smoke-Passw0rd-1"})
        cookie = cookie_from(hdrs)
        check("password sign-in works", st == 200 and cookie, f"{st} {raw[:160]}")

        # 6. keyless model API by default
        st, _, raw = call("POST", "/v1/chat/completions",
                          {"model": "x", "messages": [{"role": "user", "content": "hi"}]})
        check("model API is keyless by default (not 401)", st != 401, f"{st} {raw[:160]}")

        # 7. set a key: model API now requires it, console still works
        secret = "smoke-client-key-0123456789-abcdefghijklmnop"
        st, _, raw = call("POST", "/owner/api/auth/client-key", {"action": "set", "key": secret}, cookie=cookie)
        check("owner can set a client key", st == 200, f"{st} {raw[:200]}")
        check("key value is not echoed in the response", secret not in raw, raw[:300])
        check("response states client impact", "Bearer" in raw, raw[:300])

        st, _, raw = call("POST", "/v1/chat/completions",
                          {"model": "x", "messages": [{"role": "user", "content": "hi"}]})
        check("unauthenticated model call refused once key set", st == 401, f"{st} {raw[:200]}")
        check("refusal is invalid_api_key", "invalid_api_key" in raw, raw[:200])
        st, _, raw = call("POST", "/v1/chat/completions",
                          {"model": "x", "messages": [{"role": "user", "content": "hi"}]}, token=secret)
        check("bearer call passes auth with key", st != 401, f"{st} {raw[:200]}")
        st, _, raw = call("GET", "/version")
        check("/version unchanged by key", st == 200, f"{st}")
        st, _, raw = call("GET", "/owner/api/state", cookie=cookie)
        check("console unaffected by key", st == 200, f"{st}")

        # 8. clear restores keyless
        st, _, raw = call("POST", "/owner/api/auth/client-key", {"action": "clear"}, cookie=cookie)
        check("owner can clear the key", st == 200, f"{st} {raw[:160]}")
        st, _, raw = call("POST", "/v1/chat/completions",
                          {"model": "x", "messages": [{"role": "user", "content": "hi"}]})
        check("clearing restores keyless access", st != 401, f"{st} {raw[:160]}")

        # 9. login throttling: five failures in the window, then 429 -
        # and the correct password is refused too while it is open.
        wrong = []
        for i in range(5):
            st, _, raw = call("POST", "/owner/api/auth/login",
                              {"username": "owner", "password": f"nope-{i}"})
            wrong.append(st)
        check("failed sign-ins are refused (401) or throttled (429)",
              all(s in (401, 429) for s in wrong), str(wrong))
        st, hdrs, raw = call("POST", "/owner/api/auth/login",
                             {"username": "owner", "password": "nope-5"})
        check("sixth sign-in is throttled (429 + Retry-After)",
              st == 429 and "Retry-After" in hdrs, f"{st} {hdrs.get('Retry-After')}")
        st, _, raw = call("POST", "/owner/api/auth/login",
                          {"username": "owner", "password": "smoke-Passw0rd-1"})
        check("correct password is refused while throttled", st == 429, f"{st} {raw[:120]}")

        # 10. stored file never holds plaintext
        blob = b"".join(p.read_bytes() for p in DATA.rglob("*") if p.is_file())
        check("no plaintext client key in the data dir", secret.encode() not in blob, "found plaintext")
        check("no plaintext owner password in the data dir", b"smoke-Passw0rd-1" not in blob, "found plaintext")
        log_text = Path(LOG).read_text(errors="replace")
        check("no secrets in server log", secret not in log_text and "smoke-Passw0rd-1" not in log_text,
              "found plaintext")
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
    print("\n" + ("SMOKE FAILED: " + ", ".join(failures) if failures else "SMOKE OK: all checks passed"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())