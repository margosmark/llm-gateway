"""Single explicit trusted-LAN listener for the gateway."""
from __future__ import annotations

import argparse
import ipaddress
import os
import signal
import sys
import threading
from pathlib import Path

from gateway.adapters.chatgpt_codex import ChatGPTCodexAdapter
from gateway.adapters.opencode_go import OpenCodeGoAdapter
from gateway.adapters.opencode_console import OpenCodeConsoleAdapter
from gateway.adapters.registry import AdapterRegistry
from gateway.auth import ProfileAuth
from gateway.buildinfo import release_identity
from gateway.circuits import CircuitStore
from gateway.config import ConfigStore, ProviderState
from gateway.core.routing import Router
from gateway.credentials import CredentialStore
from gateway.http.app import GatewayApp
from gateway.http.core import Server
from gateway.http.security import build_allowed_hosts, build_canonical_origins, SecurityPolicy
from gateway.observability import LimitStore, RequestLedger

DEFAULT_DATA_DIR = "/var/lib/gateway"
DEFAULT_PORT = 8765
RETIRED_SETUP_PORT = 8766


def _gateway_port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be an integer") from exc
    if port == RETIRED_SETUP_PORT:
        raise argparse.ArgumentTypeError(
            "port 8766 is reserved and cannot be used; choose another gateway port"
        )
    return port


def _is_wildcard(host: str) -> bool:
    if host.strip() in ("0.0.0.0", "::", "[::]", "*"):
        return True
    try:
        return ipaddress.ip_address(host.strip()).is_unspecified
    except ValueError:
        return False


def _address_is_local(host: str) -> bool:
    import socket
    if host.strip() in ("localhost", "127.0.0.1", "::1"):
        return True
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind((host, 0))
        return True
    except OSError:
        return False


def _is_private_or_loopback(host: str) -> bool:
    try:
        address = ipaddress.ip_address(host.strip())
    except ValueError:
        return False
    return address.is_private or address.is_loopback


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="personal-llm-gateway", description="Trusted-LAN OpenAI-compatible LLM gateway.")
    parser.add_argument("--host", default=os.environ.get("GATEWAY_BIND", "127.0.0.1"), help="Explicit private address to bind; wildcard binds are refused.")
    parser.add_argument("--port", type=_gateway_port, default=os.environ.get("GATEWAY_PORT", str(DEFAULT_PORT)), help="Port for the gateway listener; 8766 is reserved.")
    parser.add_argument("--data-dir", default=os.environ.get("GATEWAY_DATA_DIR", DEFAULT_DATA_DIR), help="Directory holding configuration and credentials.")
    parser.add_argument("--print-url", action="store_true", help="Print gateway URLs and exit.")
    return parser


def build_application(data_dir: Path, host: str, port: int):
    data_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(data_dir, 0o700)
    config = ConfigStore(data_dir / "config.json")
    credentials = CredentialStore(data_dir)
    registry = AdapterRegistry()
    registry.register(ChatGPTCodexAdapter(credentials.provider_handle("chatgpt_codex")))
    registry.register(OpenCodeGoAdapter(credentials.provider_handle("opencode_go")))
    registry.register(OpenCodeConsoleAdapter(credentials.provider_handle("opencode_console")))
    with config.mutate() as snapshot:
        for provider_id in registry.provider_ids():
            if provider_id not in snapshot.providers:
                snapshot.providers[provider_id] = ProviderState(provider_id=provider_id, kind=provider_id)
    ledger = RequestLedger(persistence_path=data_dir / "request-ledger.jsonl")
    limits = LimitStore()
    # The open circuits are remembered on disk, so a gateway that restarts
    # during an outage does not call the exhausted provider again.
    circuits = CircuitStore(data_dir / "circuits.json")
    router = Router(config, registry, ledger=ledger, circuits=circuits)
    profile_auth = ProfileAuth(config, credentials)
    policy = SecurityPolicy(allowed_hosts=build_allowed_hosts(host, port), canonical_origins=build_canonical_origins(host, port))
    app = GatewayApp(config=config, credentials=credentials, router=router, registry=registry,
        profile_auth=profile_auth, policy=policy, ledger=ledger, limits=limits,
        release_identity=release_identity, base_url=f"http://{host}:{port}", mounts=("/owner",))
    return app, config, credentials, router


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if _is_wildcard(args.host):
        print("refusing to start: bind an explicit private LAN address; wildcard binds are refused.", file=sys.stderr)
        return 2
    if not _is_private_or_loopback(args.host):
        print(f"refusing to start: '{args.host}' is not a private or loopback address.", file=sys.stderr)
        return 2
    if not _address_is_local(args.host):
        print(f"refusing to start: '{args.host}' is not an address on this host. Set GATEWAY_BIND to an address this machine owns.", file=sys.stderr)
        return 2
    app, _, _, _ = build_application(Path(args.data_dir), args.host, args.port)
    if args.print_url:
        print(f"console: http://{args.host}:{args.port}/owner/\nAPI:     http://{args.host}:{args.port}/v1/\nrelease: http://{args.host}:{args.port}/version")
        return 0
    server = Server(args.host, args.port, app.handle)
    server.start_background()
    identity = release_identity()
    print(f"personal-llm-gateway listening on http://{args.host}:{args.port}/owner/ (trusted LAN)", flush=True)
    print(f"release: source={identity['source_revision']} tree={identity['source_tree']} image={identity['image_revision']}", flush=True)
    if identity["source_revision"] == "unknown" or identity["image_revision"] == "unknown":
        # Say so plainly: a deployment that cannot identify itself cannot
        # be audited, and it is easy not to notice.
        print(
            "warning: the release identity is unknown, so this deployment cannot say what it "
            "is. Deploy with scripts/release.py, which stamps the source revision and the "
            "image digest into the state volume.",
            flush=True,
        )
    stop = threading.Event()
    def handle_signal(signum, frame):
        stop.set()
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)
    try:
        stop.wait()
    finally:
        server.stop()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
