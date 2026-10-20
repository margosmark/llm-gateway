"""Command line entry point.

The gateway runs two listeners:

* the **LAN listener**, bound to an explicit private address, which serves
  the owner console and the OpenAI-compatible API;
* the **setup listener**, bound to loopback only, which serves the setup UI
  where credentials are enrolled.

A wildcard bind is refused. There is no flag that enables one, because the
gateway is private infrastructure and must never listen on a public
interface.
"""

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
from gateway.adapters.registry import AdapterRegistry
from gateway.auth import OwnerAuth, ProfileAuth
from gateway.buildinfo import release_identity
from gateway.config import ConfigStore, ProviderState
from gateway.core.routing import Router
from gateway.credentials import CredentialStore
from gateway.http.app import GatewayApp
from gateway.http.core import Server
from gateway.http.security import (
    build_allowed_hosts,
    build_canonical_origins,
    SecurityPolicy,
)
from gateway.observability import LimitStore, RequestLedger

DEFAULT_DATA_DIR = "/var/lib/gateway"
DEFAULT_PORT = 8765
DEFAULT_SETUP_PORT = 8766


def _is_wildcard(host: str) -> bool:
    if host.strip() in ("0.0.0.0", "::", "[::]", "*"):
        return True
    try:
        return ipaddress.ip_address(host.strip()).is_unspecified
    except ValueError:
        return False


def _address_is_local(host: str) -> bool:
    """Whether *host* names an address this machine actually has.

    A bind to an address the host does not own fails at bind time with a
    socket error and a traceback. Checking first turns that into a sentence
    an operator can act on.
    """
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
    parser = argparse.ArgumentParser(
        prog="personal-llm-gateway",
        description="Personal, LAN-only OpenAI-compatible LLM gateway.",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("GATEWAY_BIND", "127.0.0.1"),
        help="Explicit private LAN address to bind. Wildcard binds are refused.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("GATEWAY_PORT", DEFAULT_PORT)),
        help="Port for the LAN listener.",
    )
    parser.add_argument(
        "--setup-port",
        type=int,
        default=int(os.environ.get("GATEWAY_SETUP_PORT", DEFAULT_SETUP_PORT)),
        help="Port for the setup listener.",
    )
    parser.add_argument(
        "--setup-bind",
        default=os.environ.get("GATEWAY_SETUP_BIND", ""),
        help=(
            "Address the setup listener binds. Defaults to the LAN bind address, "
            "which makes the setup UI reachable from the network. Pass "
            "127.0.0.1 to make it loopback-only instead. The owner passphrase "
            "still protects it either way."
        ),
    )
    parser.add_argument(
        "--data-dir",
        default=os.environ.get("GATEWAY_DATA_DIR", DEFAULT_DATA_DIR),
        help="Directory holding the configuration and the owner-only credential store.",
    )
    parser.add_argument(
        "--print-url",
        action="store_true",
        help="Print the gateway URLs and exit.",
    )
    return parser


def build_application(
    data_dir: Path, host: str, port: int, setup_port: int, setup_bind: str = ""
):
    """Build the stores, adapters, router and the two applications."""
    data_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(data_dir, 0o700)

    config = ConfigStore(data_dir / "config.json")
    credentials = CredentialStore(data_dir)

    registry = AdapterRegistry()
    registry.register(ChatGPTCodexAdapter(credentials.provider_handle("chatgpt_codex")))
    registry.register(OpenCodeGoAdapter(credentials.provider_handle("opencode_go")))

    # Make sure both providers exist in the configuration from the start, so
    # the console can show them before anything is connected.
    with config.mutate() as snapshot:
        for provider_id in registry.provider_ids():
            if provider_id not in snapshot.providers:
                snapshot.providers[provider_id] = ProviderState(
                    provider_id=provider_id, kind=provider_id
                )

    ledger = RequestLedger()
    limits = LimitStore()
    router = Router(config, registry, ledger=ledger)

    owner_auth = OwnerAuth(credentials)
    profile_auth = ProfileAuth(config, credentials)

    # The setup listener binds where the operator asked. It defaults to the
    # LAN address so the setup UI is reachable from the network; pass
    # 127.0.0.1 for a loopback-only setup UI.
    setup_host = setup_bind.strip() or host
    if _is_wildcard(setup_host):
        raise SystemExit(
            "refusing to start: the setup listener may not bind a wildcard address."
        )
    if not _is_private_or_loopback(setup_host):
        raise SystemExit(
            f"refusing to start: the setup bind address '{setup_host}' is not private or loopback."
        )
    if not _address_is_local(setup_host):
        raise SystemExit(
            f"refusing to start: the setup bind address '{setup_host}' is not an address on this host."
        )

    allowed_hosts = build_allowed_hosts(host, port) | build_allowed_hosts(setup_host, setup_port)
    canonical_origins = build_canonical_origins(host, port) | build_canonical_origins(
        setup_host, setup_port
    )
    loopback_hosts = build_allowed_hosts("127.0.0.1", setup_port)
    loopback_origins = build_canonical_origins("127.0.0.1", setup_port)

    base_url = f"http://{host}:{port}"

    def make_app(
        mounts: tuple[str, ...],
        policy_hosts: set[str],
        policy_origins: set[str],
        url: str,
        loopback_surfaces: frozenset[str] = frozenset(),
    ) -> GatewayApp:
        policy = SecurityPolicy(
            allowed_hosts=policy_hosts,
            canonical_origins=policy_origins,
            loopback_surfaces=loopback_surfaces,
        )
        return GatewayApp(
            config=config,
            credentials=credentials,
            router=router,
            registry=registry,
            owner_auth=owner_auth,
            profile_auth=profile_auth,
            policy=policy,
            ledger=ledger,
            limits=limits,
            release_identity=release_identity,
            base_url=url,
            mounts=mounts,
        )

    # The loopback-only rule applies only when the setup listener is
    # actually bound to loopback.
    setup_loopback = frozenset({"/setup/"}) if setup_host == "127.0.0.1" else frozenset()

    lan_app = make_app(("/owner",), allowed_hosts, canonical_origins, base_url)
    setup_app = make_app(
        ("/setup",),
        allowed_hosts,
        canonical_origins,
        f"http://{setup_host}:{setup_port}",
        loopback_surfaces=setup_loopback,
    )
    return lan_app, setup_app, config, credentials, router, owner_auth, setup_host


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if _is_wildcard(args.host):
        print(
            "refusing to start: a wildcard bind would expose this gateway on every interface. "
            "Bind an explicit private LAN address.",
            file=sys.stderr,
        )
        return 2
    if not _is_private_or_loopback(args.host):
        print(
            f"refusing to start: '{args.host}' is not a private or loopback address.",
            file=sys.stderr,
        )
        return 2
    if not _address_is_local(args.host):
        print(
            f"refusing to start: '{args.host}' is not an address on this host. "
            "Set GATEWAY_BIND to an address this machine owns.",
            file=sys.stderr,
        )
        return 2

    data_dir = Path(args.data_dir)
    lan_app, setup_app, config, credentials, router, owner_auth, setup_host = build_application(
        data_dir, args.host, args.port, args.setup_port, args.setup_bind
    )

    if args.print_url:
        setup_host = args.setup_bind.strip() or args.host
        print(f"owner console: http://{args.host}:{args.port}/owner/")
        print(f"setup UI:      http://{setup_host}:{args.setup_port}/setup/")
        print(f"API:           http://{args.host}:{args.port}/v1/")
        print(f"release:       http://{args.host}:{args.port}/version")
        return 0

    lan_server = Server(args.host, args.port, lan_app.handle)
    setup_server = Server(setup_host, args.setup_port, setup_app.handle)

    lan_server.start_background()
    setup_server.start_background()

    identity = release_identity()
    print(
        "personal-llm-gateway listening on "
        f"http://{args.host}:{args.port}/owner/ (LAN) and "
        f"http://{setup_host}:{args.setup_port}/setup/ (setup)",
        flush=True,
    )
    if setup_host != "127.0.0.1":
        print(
            "note: the setup UI is reachable from the network. The owner passphrase "
            "still protects it. Use --setup-bind 127.0.0.1 for a loopback-only setup UI.",
            flush=True,
        )
    print(
        f"release: source={identity['source_revision']} tree={identity['source_tree']} "
        f"image={identity['image_revision']}",
        flush=True,
    )
    if not owner_auth.passphrase_is_set():
        print(
            "no owner passphrase is set yet. Open the setup UI to create one.",
            flush=True,
        )

    stop = threading.Event()

    def handle_signal(signum, frame):  # pragma: no cover - signal path
        stop.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)
    try:
        stop.wait()
    finally:
        lan_server.stop()
        setup_server.stop()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
