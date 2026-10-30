"""Single explicit trusted-LAN listener for the gateway."""
from __future__ import annotations

import argparse
import ipaddress
import os
import signal
import sys
import threading
from pathlib import Path

from gateway.adapters.anthropic import AnthropicAdapter
from gateway.adapters.chatgpt_codex import ChatGPTCodexAdapter
from gateway.adapters.opencode_go import OpenCodeGoAdapter
from gateway.adapters.opencode_console import OpenCodeConsoleAdapter
from gateway.adapters.registry import AdapterRegistry
from gateway.auth import ProfileAuth
from gateway.buildinfo import release_identity
from gateway.backup import snapshot_interval_seconds
from gateway.circuits import EXHAUSTED_MAX_SECONDS, CircuitStore
from gateway.snapshots import SnapshotWorker
from gateway.config import ConfigStore, ProviderState
from gateway.core.capabilities import apply_image_evidence, image_evidence
from gateway.core.routing import Router
from gateway.credentials import CredentialStore
from gateway.http.app import GatewayApp
from gateway.http.core import (
    DEFAULT_MAX_WORKERS,
    DEFAULT_RATE_LIMIT_REQUESTS,
    DEFAULT_RATE_LIMIT_WINDOW_SECONDS,
    Server,
)
from gateway.http.security import build_allowed_hosts, build_canonical_origins, SecurityPolicy, format_authority_host
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


def _exhausted_cooldown_max_seconds(value: str) -> int:
    """Validate the confirmed-exhaustion cooldown cap.

    The cap bounds how long a provider-wide ``exhausted`` circuit may hold
    a route. It must be a positive whole number of seconds, so a typo
    cannot silently disable the recovery bound.
    """
    try:
        seconds = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "exhausted cooldown cap must be a whole number of seconds"
        ) from exc
    if seconds < 1:
        raise argparse.ArgumentTypeError(
            "exhausted cooldown cap must be at least 1 second"
        )
    return seconds


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
        address = ipaddress.ip_address(host.strip())
        family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
        endpoint = (host, 0, 0, 0) if address.version == 6 else (host, 0)
        with socket.socket(family, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(endpoint)
        return True
    except (OSError, ValueError):
        return False


def _is_private_or_loopback(host: str) -> bool:
    try:
        address = ipaddress.ip_address(host.strip())
    except ValueError:
        return False
    if address.is_loopback:
        return True
    if address.version == 4:
        supported = (ipaddress.ip_network("10.0.0.0/8"),
                     ipaddress.ip_network("172.16.0.0/12"),
                     ipaddress.ip_network("192.168.0.0/16"))
    else:
        supported = (ipaddress.ip_network("fc00::/7"),)
    return not address.is_link_local and not address.is_multicast and any(
        address in network for network in supported
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="personal-llm-gateway", description="Trusted-LAN OpenAI-compatible LLM gateway.")
    parser.add_argument("--host", default=os.environ.get("GATEWAY_BIND", "127.0.0.1"), help="Explicit private address to bind; wildcard binds are refused.")
    parser.add_argument("--port", type=_gateway_port, default=os.environ.get("GATEWAY_PORT", str(DEFAULT_PORT)), help="Port for the gateway listener; 8766 is reserved.")
    parser.add_argument("--data-dir", default=os.environ.get("GATEWAY_DATA_DIR", DEFAULT_DATA_DIR), help="Directory holding configuration and credentials.")
    parser.add_argument("--max-workers", type=int, default=int(os.environ.get("GATEWAY_MAX_WORKERS", DEFAULT_MAX_WORKERS)), help="Maximum concurrent HTTP workers (default: 32).")
    parser.add_argument("--rate-limit-requests", type=int, default=int(os.environ.get("GATEWAY_RATE_LIMIT_REQUESTS", DEFAULT_RATE_LIMIT_REQUESTS)), help="Requests per client in the rolling window (default: 120).")
    parser.add_argument("--rate-limit-window-seconds", type=float, default=float(os.environ.get("GATEWAY_RATE_LIMIT_WINDOW_SECONDS", DEFAULT_RATE_LIMIT_WINDOW_SECONDS)), help="Per-client rolling window in seconds (default: 60).")
    parser.add_argument(
        "--exhausted-cooldown-max-seconds",
        type=_exhausted_cooldown_max_seconds,
        default=os.environ.get("GATEWAY_EXHAUSTED_COOLDOWN_MAX_SECONDS", str(EXHAUSTED_MAX_SECONDS)),
        help=(
            "Longest window a confirmed provider/account exhaustion may hold a route "
            f"(default: {EXHAUSTED_MAX_SECONDS}; configurable with GATEWAY_EXHAUSTED_COOLDOWN_MAX_SECONDS)."
        ),
    )
    parser.add_argument("--print-url", action="store_true", help="Print gateway URLs and exit.")
    return parser


def build_application(
    data_dir: Path,
    host: str,
    port: int,
    exhausted_max_seconds: int = EXHAUSTED_MAX_SECONDS,
):
    data_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(data_dir, 0o700)
    config = ConfigStore(data_dir / "config.json")
    credentials = CredentialStore(data_dir)
    registry = AdapterRegistry()
    registry.register(AnthropicAdapter(credentials.provider_handle("anthropic")))
    registry.register(ChatGPTCodexAdapter(credentials.provider_handle("chatgpt_codex")))
    registry.register(OpenCodeGoAdapter(credentials.provider_handle("opencode_go")))
    registry.register(OpenCodeConsoleAdapter(credentials.provider_handle("opencode_console")))
    reconciled: list[tuple[str, str]] = []
    with config.mutate() as snapshot:
        for provider_id in registry.provider_ids():
            if provider_id not in snapshot.providers:
                snapshot.providers[provider_id] = ProviderState(provider_id=provider_id, kind=provider_id)
        # A capability that lives only in the dated catalogue is
        # indistinguishable from false in the stored rows, because the
        # router, the alias catalogue and every client read the row. So each
        # boot re-applies, per exact model id, the image evidence each
        # adapter holds in local files and local maps. No provider is called,
        # no row is added, and nothing that is not evidenced changes.
        reconciled = apply_image_evidence(
            snapshot,
            {
                provider_id: image_evidence(registry.get(provider_id))
                for provider_id in registry.provider_ids()
            },
        )
    if reconciled:
        print(
            "image capability reconciled from local evidence: "
            + ", ".join(f"{provider_id}/{model_id}" for provider_id, model_id in reconciled),
            flush=True,
        )
    ledger = RequestLedger(persistence_path=data_dir / "request-ledger.jsonl")
    limits = LimitStore()
    # The open circuits are remembered on disk, so a gateway that restarts
    # during an outage does not call the exhausted provider again. The
    # confirmed-exhaustion window is bounded, so a reopened subscription
    # window is never stranded behind a multi-hour local hold.
    circuits = CircuitStore(
        data_dir / "circuits.json", exhausted_max_seconds=exhausted_max_seconds
    )
    router = Router(config, registry, ledger=ledger, circuits=circuits)
    profile_auth = ProfileAuth(config, credentials)
    policy = SecurityPolicy(allowed_hosts=build_allowed_hosts(host, port), canonical_origins=build_canonical_origins(host, port))
    app = GatewayApp(config=config, credentials=credentials, router=router, registry=registry,
        profile_auth=profile_auth, policy=policy, ledger=ledger, limits=limits,
        release_identity=release_identity, base_url=f"http://{format_authority_host(host)}:{port}", mounts=("/owner",))
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
    if args.max_workers < 1 or args.rate_limit_requests < 1 or args.rate_limit_window_seconds <= 0:
        print("refusing to start: worker and rate-limit settings must be positive.", file=sys.stderr)
        return 2
    app, config, _, _ = build_application(
        Path(args.data_dir), args.host, args.port,
        exhausted_max_seconds=args.exhausted_cooldown_max_seconds,
    )
    authority = format_authority_host(args.host)
    if args.print_url:
        print(f"console: http://{authority}:{args.port}/owner/\nAPI:     http://{authority}:{args.port}/v1/\nrelease: http://{authority}:{args.port}/version")
        return 0
    snapshots = SnapshotWorker(config, snapshot_interval_seconds(config.snapshot()))
    server = Server(
        args.host, args.port, app.handle, max_workers=args.max_workers,
        rate_limit_requests=args.rate_limit_requests,
        rate_limit_window_seconds=args.rate_limit_window_seconds,
    )
    snapshots.start()
    server.start_background()
    identity = release_identity()
    print(f"personal-llm-gateway listening on http://{authority}:{args.port}/owner/ (trusted LAN)", flush=True)
    print(f"release: source={identity['source_revision']} tree={identity['source_tree']} image={identity['image_revision']}", flush=True)
    if identity["source_revision"] == "unknown" or identity["image_revision"] == "unknown":
        # Say so plainly: a deployment that cannot identify itself cannot
        # be audited, and it is easy not to notice.
        print(
            "warning: the release identity is unknown, so this deployment cannot say what it "
            "is. Deploy with scripts/release.py, which stamps the source revision and the "
            "image digest into the state volume, bound to the running container.",
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
        snapshots.stop()
        # Give back every pooled provider connection. Without this the
        # sockets are only reclaimed when the process exits, and a stop
        # that returns while connections are still open is not a stop.
        app.registry.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
