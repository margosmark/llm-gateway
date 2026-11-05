"""Single explicit trusted-LAN listener for the gateway."""
from __future__ import annotations

import argparse
import ipaddress
import os
import re
import signal
import sys
import threading
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any, NoReturn

from gateway.adapters.anthropic import AnthropicAdapter
from gateway.adapters.chatgpt_codex import ChatGPTCodexAdapter
from gateway.adapters.local_openai import FLAVORS as LOCAL_FLAVORS
from gateway.adapters.local_openai import LocalOpenAIAdapter
from gateway.adapters.opencode_go import OpenCodeGoAdapter
from gateway.adapters.opencode_console import OpenCodeConsoleAdapter
from gateway.adapters.registry import AdapterRegistry
from gateway.auth import ProfileAuth
from gateway.buildinfo import release_identity
from gateway.backup import snapshot_interval_seconds
from gateway.circuits import EXHAUSTED_MAX_SECONDS, RATE_LIMITED_MAX_SECONDS, TRANSIENT_BACKOFF_MAX_SECONDS, CircuitStore
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
RESET_OWNER_PASSWORD_FLAG = "--reset-owner-password"
REDACTED_ARGV_VALUE = "<redacted>"
_UNRECOGNIZED_PREFIX = "unrecognized arguments: "
# Every long option build_parser() registers is `--` + lowercase
# letters/digits/hyphens; tests/test_cli_argv_secrets.py pins this grammar to
# the parser. An unknown long token outside it cannot be a typo of any option
# this parser defines, so it is a value (or a secret), not a name.
_FLAG_SHAPE = re.compile(r"--[a-z0-9][a-z0-9_-]*")


def _flag_shaped(token: str) -> bool:
    """True when an unknown long token could be an option name of this parser."""
    return bool(_FLAG_SHAPE.fullmatch(token))


def _within_two_edits(a: str, b: str) -> bool:
    """True when ``a`` is within Levenshtein distance 2 of ``b``."""
    if abs(len(a) - len(b)) > 2:
        return False
    previous = list(range(len(b) + 1))
    for i, char_a in enumerate(a, 1):
        current = [i]
        for j, char_b in enumerate(b, 1):
            current.append(
                min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (char_a != char_b))
            )
        if min(current) > 2:
            return False
        previous = current
    return previous[-1] <= 2


def _resolves_to_reset(head: str, known: set[str]) -> bool:
    """True when argparse resolves ``head`` to the recovery flag.

    argparse matches a long option by exact string, then by unambiguous
    prefix, so ``--reset`` and ``--reset-owner-p`` ARE the recovery flag on
    the command line. The regime has to follow how the flag resolves, not
    its literal spelling - otherwise the abbreviation of the reset flow is
    redacted as an ordinary parse and its value comes back out.
    """
    if not head.startswith("--"):
        return False
    if head == RESET_OWNER_PASSWORD_FLAG:
        return True
    if head in known:
        return False
    matches = [option for option in known if option.startswith(head)]
    return matches == [RESET_OWNER_PASSWORD_FLAG]


def _is_recovery_typo(head: str, known: set[str]) -> bool:
    """True when ``head`` is a close misspelling of the recovery flag.

    ``--rest-owner-password`` is a recovery attempt, not an unknown flag:
    entering the recovery regime withholds the rest of the line, and the
    typo'd flag itself keeps its name (it is the diagnosis).
    """
    if not head.startswith("--") or head in known:
        return False
    return _within_two_edits(head, RESET_OWNER_PASSWORD_FLAG)


def _reset_flag_in_play(argv: Sequence[str], known_options: Iterable[str] = ()) -> bool:
    """True when the recovery flow is on the command line being parsed.

    Counts every token argparse resolves to ``--reset-owner-password``
    (exact, ``=``-attached, or an unambiguous abbreviation) and close
    typos of it. The recovery regime withholds argv values, so it must
    trigger on how the flag is used, not on its literal spelling.
    """
    known = set(known_options)
    for token in argv:
        if not token.startswith("-"):
            continue
        head, _sep, _value = token.partition("=")
        if _resolves_to_reset(head, known) or _is_recovery_typo(head, known):
            return True
    return False


def _hide_argv_value(message: str, value: str) -> str:
    """Remove one argv-supplied value wherever it appears in ``message``.

    The match is anchored to whole occurrences, so a value can never rewrite
    part of a longer word (``abc`` leaves ``abcdef`` alone). A value that
    happens to be a word argparse itself uses is still removed - losing a
    word is the safe direction when the alternative is echoing a password.
    The ``%r`` spelling is removed too: argparse quotes values, so a newline
    arrives as the two characters ``\\n`` and the raw token alone misses it.
    """
    if not value:
        return message
    for spelling in (value, repr(value)[1:-1]):
        if not spelling:
            continue
        pattern = r"(?<![A-Za-z0-9_])" + re.escape(spelling) + r"(?![A-Za-z0-9_])"
        message = re.sub(pattern, REDACTED_ARGV_VALUE, message)
    return message


def _bare_value_needles(token: str, *, post_separator: bool = False) -> list[str]:
    """The parts of one argv token argparse may interpolate as a value.

    - After the ``--`` separator every token is a positional value, dashes
      included: option parsing has stopped, so nothing there is a name.
    - A long token this parser could have received as a flag
      (``--long``, ``--long=value``) contributes only what followed the
      ``=``: the name stays, because naming a typo'd flag is the
      diagnosis.
    - Everything else - a bare value, a positional carrying ``=``, a long
      token that is not flag-shaped - is itself the value, so the whole
      token is withheld. Rewriting ``secret=`` to ``secret=<redacted>``
      would still print the secret, and a ``--``-leading token outside the
      flag grammar cannot be an option name.
    """
    if post_separator:
        return [token]
    if token == "--":
        return []
    head, sep, value = token.partition("=")
    if head.startswith("--") and _flag_shaped(head):
        return [value] if sep and value else []
    return [token]


def _whole_token_needles(token: str) -> list[str]:
    """Every spelling of one argv token that could carry a secret."""
    needles = [token]
    _head, sep, value = token.partition("=")
    if sep and value:
        needles.append(value)
    return needles


def _redact_argv_values(message: str, argv: Sequence[str], known_options: Iterable[str]) -> str:
    """Strip argv-supplied values out of an argument-parsing error.

    argv is visible in the process list and usually lands in shell history,
    which is why ``--reset-owner-password`` takes the new password from
    stdin. A refused parse must not defeat that: argparse otherwise writes
    the very token it rejected straight back to stderr.

    Three rules, in order:

    1. Recovery regime - any token argparse resolves to the recovery flag,
       and close typos of it. Nothing else on the line can be a legitimate
       value, so every token that is not a known option is withheld
       wherever the message puts it. The typo'd flag itself keeps its name:
       it is the diagnosis.
    2. Ordinary regime, the ``unrecognized arguments: ...`` tail: only the
       shapes argparse itself interpolated are scrubbed - a positional
       value (whole token, so ``secret=`` cannot come back out), the tail
       of an attached ``=``, and any token after the ``--`` separator,
       dashes included - so a refusal that deliberately names a fixed value
       (``port 8766 is reserved``) keeps saying it.
    3. Everywhere: a raw token carrying ``=`` is rewritten to
       ``head=<redacted>`` only when the head is flag-shaped - naming a
       flag is the diagnosis. A head that cannot be a flag means the whole
       token was a value, and the whole token is withheld.

    The documented boundary (docs/OWNER_AUTH.md): an unknown flag-shaped
    token is named verbatim, because that is the preserved diagnosis - a
    leading-``--`` secret that is byte-identical to one, on a line with no
    recovery signal, cannot be told apart from a typo'd flag.
    """
    known = set(known_options)

    # argv tokens paired with whether argparse already stopped option
    # parsing before them (everything after a bare ``--`` is a value).
    entries: list[tuple[str, bool]] = []
    after_separator = False
    for token in argv:
        if token and token not in known:
            entries.append((token, after_separator))
        if token == "--":
            after_separator = True

    if _reset_flag_in_play(argv, known):
        for token, _post_separator in entries:
            head, _sep, value = token.partition("=")
            if _is_recovery_typo(head, known):
                # The misspelled recovery flag stays named; an attached
                # value is still a value.
                if value:
                    message = _hide_argv_value(message, value)
                continue
            for needle in _whole_token_needles(token):
                message = _hide_argv_value(message, needle)
        return message

    if message.startswith(_UNRECOGNIZED_PREFIX):
        prefix, separator, tail = message.partition(_UNRECOGNIZED_PREFIX)
        for token, post_separator in entries:
            for needle in _bare_value_needles(token, post_separator=post_separator):
                tail = _hide_argv_value(tail, needle)
        message = prefix + separator + tail

    for token, _post_separator in entries:
        for needle in _whole_token_needles(token):
            quoted = repr(needle)
            if quoted in message:
                message = message.replace(quoted, f"'{REDACTED_ARGV_VALUE}'")
        # An ambiguous abbreviation keeps its head with the attached value
        # redacted: ``--po=secret`` could match --port, --print-url.
        if "=" in token and token in message:
            head, _sep, _value = token.partition("=")
            if head.startswith("--") and _flag_shaped(head):
                message = message.replace(token, f"{head}={REDACTED_ARGV_VALUE}")
            else:
                # Not a flag-shaped head, so the token was a value: withhold
                # all of it. ``secret=<redacted>`` still contains ``secret=``.
                message = message.replace(token, REDACTED_ARGV_VALUE)
    return message


class _RedactingArgumentParser(argparse.ArgumentParser):
    """An argument parser that redacts argv values out of its refusals.

    ``error()`` is the single funnel every refused parse goes through -
    unknown flags, unexpected positionals, bad conversions - so redacting
    there covers the whole parse-error surface rather than the one reported
    invocation.
    """

    def parse_known_args(self, *args: Any, **kwargs: Any) -> Any:
        # Capture the argv this parse is about, on the public entry point:
        # error() must redact the tokens the caller actually supplied, which
        # is not sys.argv when a test drives main([...]) with an explicit
        # list. The base method carries an overload set, so this wrapper is
        # typed loosely on purpose and forwards its arguments untouched.
        argv = args[0] if args and args[0] is not None else kwargs.get("args")
        self._active_argv = list(sys.argv[1:] if argv is None else argv)
        return super().parse_known_args(*args, **kwargs)

    def error(self, message: str) -> NoReturn:
        argv = getattr(self, "_active_argv", None)
        if argv is None:  # parser.error() called outside a parse
            argv = list(sys.argv[1:])
        safe = _redact_argv_values(message, argv, self._option_string_actions)
        if _reset_flag_in_play(argv, self._option_string_actions) and (
            safe.startswith(_UNRECOGNIZED_PREFIX)
            or RESET_OWNER_PASSWORD_FLAG in safe
        ):
            safe += (
                f"; {RESET_OWNER_PASSWORD_FLAG} takes no value: "
                "the new owner password is read from stdin"
            )
        super().error(safe)



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


def _cooldown_max_seconds(value: str) -> int:
    """Validate a cooldown-window cap in seconds.

    The cap bounds how long a local circuit may hold a route for one
    failure class. It must be a positive whole number of seconds, so a
    typo cannot silently disable the bound.
    """
    try:
        seconds = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "cooldown cap must be a whole number of seconds"
        ) from exc
    if seconds < 1:
        raise argparse.ArgumentTypeError("cooldown cap must be at least 1 second")
    return seconds


def build_parser() -> argparse.ArgumentParser:
    parser = _RedactingArgumentParser(prog="personal-llm-gateway", description="Trusted-LAN OpenAI-compatible LLM gateway.")
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
    parser.add_argument(
        "--rate-limited-cooldown-max-seconds",
        type=_cooldown_max_seconds,
        default=os.environ.get("GATEWAY_RATE_LIMITED_COOLDOWN_MAX_SECONDS", str(RATE_LIMITED_MAX_SECONDS)),
        help=(
            "Longest window a provider rate-limit may hold a route when no Retry-After was given "
            f"(default: {RATE_LIMITED_MAX_SECONDS}; configurable with GATEWAY_RATE_LIMITED_COOLDOWN_MAX_SECONDS)."
        ),
    )
    parser.add_argument(
        "--transient-cooldown-max-seconds",
        type=_cooldown_max_seconds,
        default=os.environ.get("GATEWAY_TRANSIENT_COOLDOWN_MAX_SECONDS", str(TRANSIENT_BACKOFF_MAX_SECONDS)),
        help=(
            "Longest window a transient or unclassified failure may hold a route "
            f"(default: {TRANSIENT_BACKOFF_MAX_SECONDS}; configurable with GATEWAY_TRANSIENT_COOLDOWN_MAX_SECONDS)."
        ),
    )
    parser.add_argument("--print-url", action="store_true", help="Print gateway URLs and exit.")
    parser.add_argument(
        "--reset-owner-password",
        action="store_true",
        help=(
            "Local recovery: read a new owner password from stdin (never from argv), "
            "revoke every owner session, then exit. Requires access to the data directory."
        ),
    )
    return parser


def build_application(
    data_dir: Path,
    host: str,
    port: int,
    exhausted_max_seconds: int = EXHAUSTED_MAX_SECONDS,
    rate_limited_max_seconds: int = RATE_LIMITED_MAX_SECONDS,
    transient_max_seconds: int = TRANSIENT_BACKOFF_MAX_SECONDS,
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
    # The three local servers share one adapter. Each reads its own saved
    # endpoint from the config store on every request, so registering one
    # makes no outbound call and a saved URL takes effect without a restart.
    for provider_id in LOCAL_FLAVORS:
        registry.register(
            LocalOpenAIAdapter(provider_id, credentials.provider_handle(provider_id), config)
        )
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
        data_dir / "circuits.json", exhausted_max_seconds=exhausted_max_seconds,
        rate_limited_max_seconds=rate_limited_max_seconds,
        transient_max_seconds=transient_max_seconds,
    )
    router = Router(config, registry, ledger=ledger, circuits=circuits)
    profile_auth = ProfileAuth(config, credentials)
    policy = SecurityPolicy(allowed_hosts=build_allowed_hosts(host, port), canonical_origins=build_canonical_origins(host, port))
    app = GatewayApp(config=config, credentials=credentials, router=router, registry=registry,
        profile_auth=profile_auth, policy=policy, ledger=ledger, limits=limits,
        release_identity=release_identity, base_url=f"http://{format_authority_host(host)}:{port}", mounts=("/owner",))
    return app, config, credentials, router


def _echo_bind_target(host: str) -> str:
    """Name a refused bind target only when it is actually an address.

    An address the operator typed is the useful part of that refusal, so a
    syntactically valid IP is echoed as it always was. Anything else in the
    slot was never a host - it is a misplaced argument, and possibly a
    password - so it is withheld rather than printed back.
    """
    try:
        ipaddress.ip_address(host.strip())
    except ValueError:
        return REDACTED_ARGV_VALUE
    return host


def _reset_owner_password(data_dir: Path) -> int:
    """Documented local recovery for a forgotten owner password.

    The password is read from stdin, never from ``argv``: argv is visible
    in the process list and in shell history. The operation rewrites the
    stored hash and revokes every session, so a cookie that was already
    stolen stops working. It is reachable only with filesystem access to
    the data directory - there is no HTTP surface for it.
    """
    import getpass

    from gateway.owner_auth import OwnerAuthError, OwnerAuthStore

    store = OwnerAuthStore(data_dir)
    if store.setup_required:
        print("no owner account exists yet; use the one-time setup code instead.", file=sys.stderr)
        return 2
    try:
        new_password = getpass.getpass("new owner password: ")
        confirmation = getpass.getpass("repeat new owner password: ")
    except (EOFError, KeyboardInterrupt):
        print("\naborted; nothing was changed.", file=sys.stderr)
        return 2
    if new_password != confirmation:
        print("passwords do not match; nothing was changed.", file=sys.stderr)
        return 2
    try:
        result = store.reset_password(new_password)
    except OwnerAuthError as exc:
        print(f"refusing: {exc.message}", file=sys.stderr)
        return 2
    print(
        f"owner password replaced for {result['username']}; every owner session was revoked.",
        flush=True,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.reset_owner_password:
        return _reset_owner_password(Path(args.data_dir))
    if _is_wildcard(args.host):
        print("refusing to start: bind an explicit private LAN address; wildcard binds are refused.", file=sys.stderr)
        return 2
    if not _is_private_or_loopback(args.host):
        print(f"refusing to start: '{_echo_bind_target(args.host)}' is not a private or loopback address.", file=sys.stderr)
        return 2
    if not _address_is_local(args.host):
        print(f"refusing to start: '{_echo_bind_target(args.host)}' is not an address on this host. Set GATEWAY_BIND to an address this machine owns.", file=sys.stderr)
        return 2
    if args.max_workers < 1 or args.rate_limit_requests < 1 or args.rate_limit_window_seconds <= 0:
        print("refusing to start: worker and rate-limit settings must be positive.", file=sys.stderr)
        return 2
    app, config, _, _ = build_application(
        Path(args.data_dir), args.host, args.port,
        exhausted_max_seconds=args.exhausted_cooldown_max_seconds,
        rate_limited_max_seconds=args.rate_limited_cooldown_max_seconds,
        transient_max_seconds=args.transient_cooldown_max_seconds,
    )
    # ``--print-url`` paths in tests stub build_application with a None app;
    # the setup banner is only meaningful when there is a real application.
    if app is not None and app.setup_code_created:
        # Server-side only: stdout of the gateway process and an owner-only
        # file in the data directory. No HTTP surface ever returns it.
        print(
            "first-run owner setup required: one-time setup code "
            f"(server-side only, also at {app.owner_auth.setup_code_path}): "
            f"{app.setup_code_created}",
            flush=True,
        )
    elif app is not None and app.owner_auth.setup_required:
        print(
            "first-run owner setup required: the one-time setup code is at "
            f"{app.owner_auth.setup_code_path} (server-side only)",
            flush=True,
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
