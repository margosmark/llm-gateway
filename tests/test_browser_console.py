"""Drives the owner console in a real browser.

The spec requires a browser check: "A browser test verifies direct console
use and the visible trusted-LAN notice at desktop and 390 px viewport
widths."

The test starts a real gateway on a free loopback port and drives Chromium
against it. It is **skipped** when a browser or the Playwright module is
not available, because a missing optional tool must not fail the suite.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path

from gateway.adapters.registry import AdapterRegistry
from gateway.auth import ProfileAuth
from gateway.config import ConfigStore, ProviderState
from gateway.core.routing import Router
from gateway.credentials import CredentialStore
from gateway.http.app import GatewayApp
from gateway.logging import RequestLogger
from gateway.http.core import Server
from gateway.http.security import SecurityPolicy, build_allowed_hosts, build_canonical_origins
from gateway.observability import LimitStore, RequestLedger

SCRIPT = Path(__file__).parent / "browser_console_test.js"

#: Places a Playwright install may live. The repository does not vendor it.
PLAYWRIGHT_CANDIDATES = (
    "/home/marslan/.hermes/hermes-agent/node_modules",
    str(Path.home() / ".npm-global/lib/node_modules"),
    str(Path.home() / ".local/lib/node_modules"),
)


def _node_path() -> str | None:
    existing = os.environ.get("NODE_PATH")
    parts = [p for p in existing.split(os.pathsep) if p] if existing else []
    for candidate in PLAYWRIGHT_CANDIDATES:
        if os.path.isdir(os.path.join(candidate, "playwright")) and candidate not in parts:
            parts.append(candidate)
    return os.pathsep.join(parts) if parts else None


def _chromium() -> str | None:
    for candidate in (
        os.environ.get("GATEWAY_CHROMIUM"),
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
        shutil.which("google-chrome"),
    ):
        if candidate and Path(candidate).exists():
            return candidate
    return None


class BrowserConsoleTests(unittest.TestCase):
    def test_the_console_opens_directly_and_shows_the_trusted_lan_notice(self) -> None:
        node = shutil.which("node")
        chromium = _chromium()
        node_path = _node_path()
        if not node or not chromium or not node_path:
            self.skipTest("node, a chromium binary, or the playwright module is not available")

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            config = ConfigStore(data_dir / "config.json")
            credentials = CredentialStore(data_dir)
            registry = AdapterRegistry()
            # The real startup registers both providers, so the console has
            # provider cards before anything is connected. The harness must
            # do the same, or the console renders empty.
            with config.mutate() as snapshot:
                for provider_id in ("chatgpt_codex", "opencode_go"):
                    snapshot.providers[provider_id] = ProviderState(
                        provider_id=provider_id, kind=provider_id
                    )
            router = Router(config, registry, ledger=RequestLedger())
            port = _free_port()
            policy = SecurityPolicy(
                allowed_hosts=build_allowed_hosts("127.0.0.1", port),
                canonical_origins=build_canonical_origins("127.0.0.1", port),
            )
            app = GatewayApp(
                config=config,
                credentials=credentials,
                router=router,
                registry=registry,
                profile_auth=ProfileAuth(config, credentials),
                policy=policy,
                ledger=RequestLedger(),
                limits=LimitStore(),
                release_identity=lambda: {
                    "service": "personal-llm-gateway",
                    "source_revision": "synthetic",
                    "source_tree": "synthetic",
                    "image_revision": "unknown",
                },
                base_url=f"http://127.0.0.1:{port}",
                mounts=("/owner",),
                logger=RequestLogger(enabled=False),
            )
            server = Server("127.0.0.1", port, app.handle)
            server.start_background()
            self.addCleanup(server.stop)

            environment = dict(os.environ)
            environment["NODE_PATH"] = node_path
            environment["GATEWAY_CHROMIUM"] = chromium
            result = subprocess.run(
                [node, str(SCRIPT), f"http://127.0.0.1:{port}"],
                capture_output=True,
                text=True,
                timeout=180,
                env=environment,
            )
            self.assertEqual(
                result.returncode,
                0,
                "the browser check failed:\n" + result.stdout + result.stderr,
            )


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


if __name__ == "__main__":
    unittest.main()
