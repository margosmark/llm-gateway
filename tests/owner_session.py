"""Owner session helper for fixtures that drive ``app.handle`` directly.

Every owner endpoint sits behind one session cookie, exactly as a browser
sees it. A fixture that calls ``app.handle`` itself needs that cookie the
same way: it completes first-run setup through the real endpoint (one-time
setup code, PBKDF2 at the production work factor) and hands back the
cookie. Seeding the auth store directly would skip the very path under
test, so this helper never does that.
"""
from __future__ import annotations

import json

from gateway.http.core import Headers, Request
from gateway.owner_auth import COOKIE_NAME

FIXTURE_USERNAME = "owner"
FIXTURE_PASSWORD = "fixture-owner-password"


def _authority(app) -> str:
    """The host:port the app's policy allows, taken from its own base_url."""
    base = getattr(app, "base_url", None) or "http://127.0.0.1:8765"
    return str(base).split("://", 1)[-1]


def _handle(app, method: str, path: str, body: dict | None = None, cookie: str | None = None):
    authority = _authority(app)
    headers = [("Host", authority), ("Origin", f"http://{authority}")]
    if cookie:
        headers.append(("Cookie", f"{COOKIE_NAME}={cookie}"))
    request = Request(
        method=method,
        path=path,
        raw_query="",
        headers=Headers(headers),
        body=json.dumps(body).encode("utf-8") if body is not None else b"",
        client_address=("127.0.0.1", 40000),
        server_port=int(authority.rsplit(":", 1)[-1]) if authority.rsplit(":", 1)[-1].isdigit() else 8765,
    )
    return app.handle(request)


def _cookie_from(response) -> str:
    for key, value in response.headers:
        if key.lower() == "set-cookie" and value.startswith(f"{COOKIE_NAME}="):
            return value.partition("=")[2].split(";", 1)[0]
    raise RuntimeError(f"sign-in response carried no {COOKIE_NAME} cookie")


def owner_cookie(app) -> str:
    """Complete first-run setup (or sign in) and return the session token."""
    if app.owner_auth.setup_required:
        setup_code = app.owner_auth.read_setup_code()
        if setup_code is None:
            raise RuntimeError("fixture data directory has no one-time setup code")
        response = _handle(
            app,
            "POST",
            "/owner/api/auth/setup",
            {
                "username": FIXTURE_USERNAME,
                "password": FIXTURE_PASSWORD,
                "setup_code": setup_code,
            },
        )
    else:
        response = _handle(
            app,
            "POST",
            "/owner/api/auth/login",
            {"username": FIXTURE_USERNAME, "password": FIXTURE_PASSWORD},
        )
    if response.status != 200:
        raise RuntimeError(f"fixture owner sign-in failed: {response.status} {response.body!r}")
    return _cookie_from(response)


def cookie_header(app) -> str:
    """A ready-to-send ``Cookie`` header value for a fixture request."""
    return f"{COOKIE_NAME}={owner_cookie(app)}"
