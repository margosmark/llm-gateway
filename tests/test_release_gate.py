"""Tests for the release gate.

Regression cover for **R3**: the gate read ``/proc/net/tcp`` as raw strings
and compared them to ``0.0.0.0``, but the kernel writes hexadecimal
addresses in host byte order, so the comparison could never match. It also
did not look at IPv6.

The parser must:

* decode a hexadecimal IPv4 local address correctly;
* decode a hexadecimal IPv6 local address correctly;
* recognise a **wildcard** bind in both families;
* recognise the explicit loopback binds;
* ignore sockets that are not listening.

The owner-session boundary checks are covered in the other half of this
file: the gate must refuse the superseded no-session answers (a ``200``
state GET, a mutation dispatched with no session) that made it fail every
release against the owner-auth lineage, and it must demand the documented
``401``/``403`` refusals without a session and the real answers with one.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from gateway.adapters.registry import AdapterRegistry
from gateway.auth import ProfileAuth
from gateway.config import ConfigStore
from gateway.core.routing import Router
from gateway.credentials import CredentialStore
from gateway.http.app import GatewayApp
from gateway.http.core import Server
from gateway.http.security import (
    SecurityPolicy,
    build_allowed_hosts,
    build_canonical_origins,
)
from gateway.logging import RequestLogger
from gateway.observability import LimitStore, RequestLedger
from tests.test_end_to_end import free_port

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
GATE = REPO_ROOT / "scripts" / "release_gate.py"
_spec = importlib.util.spec_from_file_location("release_gate_under_test", GATE)
assert _spec and _spec.loader
release_gate = importlib.util.module_from_spec(_spec)
# dataclasses resolves annotations through sys.modules, so register first.
sys.modules[_spec.name] = release_gate
_spec.loader.exec_module(release_gate)

TCP_HEADER = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode"

#: What the boundary checks expect from a gateway that enforces the owner
#: session: refusal without one, service with one.
PRE_AUTH_ANSWERS = [401, 401, 401, 403]
SIGNED_IN_ANSWERS = {
    "setup": 200,
    "state": 200,
    "state_hostile_origin": 200,
    "mutation": 404,
    "mutation_hostile_origin": 403,
}


class DigestResolutionTests(unittest.TestCase):
    """The identity the gate resolves must be one the gateway can report.

    Regression cover for **R31**: the gate resolved a registry digest as
    ``repository@sha256:...`` and wrote that into ``release.json``. The
    gateway's release identity deliberately accepts only a bare
    ``sha256:<64 hex>`` digest, so the value was rejected and ``/version``
    reported ``unknown`` - which made the gate's own identity check fail
    for every image that had a repository digest.
    """

    def _inspect_returns(self, repo_digest: str, image_id: str) -> None:
        def fake_run(args, timeout=120):
            class Result:
                returncode = 0
                stdout = ""
                stderr = ""

            result = Result()
            if "--format" in args:
                template = args[args.index("--format") + 1]
                result.stdout = image_id if template == "{{.Id}}" else repo_digest
            return result

        release_gate.run = fake_run  # type: ignore[assignment]

    def _restore_run(self) -> None:
        release_gate.run = self._original_run  # type: ignore[assignment]

    def setUp(self) -> None:
        self._original_run = release_gate.run

    def tearDown(self) -> None:
        self._restore_run()

    def test_a_repository_digest_is_reduced_to_the_digest(self) -> None:
        digest = "sha256:" + "a" * 64
        self._inspect_returns(f"personal-llm-gateway@{digest}", "sha256:" + "b" * 64)
        self.assertEqual(release_gate.image_digest("personal-llm-gateway:candidate"), digest)

    def test_a_bare_digest_is_kept(self) -> None:
        digest = "sha256:" + "c" * 64
        self._inspect_returns(digest, "sha256:" + "d" * 64)
        self.assertEqual(release_gate.image_digest("personal-llm-gateway:candidate"), digest)

    def test_a_local_image_falls_back_to_its_id(self) -> None:
        identity = "sha256:" + "e" * 64
        self._inspect_returns("", identity)
        self.assertEqual(release_gate.image_digest("personal-llm-gateway:candidate"), identity)

    def test_the_value_is_one_the_gateway_accepts(self) -> None:
        """The gateway accepts a bare digest and nothing else."""
        from gateway.buildinfo import _valid_revision

        digest = "sha256:" + "f" * 64
        self._inspect_returns(f"personal-llm-gateway@{digest}", "sha256:" + "0" * 64)
        resolved = release_gate.image_digest("personal-llm-gateway:candidate")
        self.assertIsNotNone(resolved)
        self.assertEqual(_valid_revision(resolved), resolved)


class DecodeTests(unittest.TestCase):
    def test_ipv4_loopback(self) -> None:
        self.assertEqual(release_gate._decode_ipv4_hex("0100007F"), "127.0.0.1")

    def test_ipv4_wildcard(self) -> None:
        self.assertEqual(release_gate._decode_ipv4_hex("00000000"), "0.0.0.0")

    def test_ipv4_private_address(self) -> None:
        # 10.10.11.109 has network bytes 0A 0A 0B 6D, which the kernel
        # prints as the little-endian u32 6D0B0A0A.
        self.assertEqual(release_gate._decode_ipv4_hex("6D0B0A0A"), "10.10.11.109")

    def test_malformed_ipv4_is_rejected(self) -> None:
        self.assertEqual(release_gate._decode_ipv4_hex(""), "")
        self.assertEqual(release_gate._decode_ipv4_hex("ZZZZ"), "")

    def test_ipv6_loopback(self) -> None:
        # ::1 is 32 hex characters, four little-endian 32-bit words.
        self.assertEqual(
            release_gate._decode_ipv6_hex("00000000000000000000000001000000"), "::1"
        )

    def test_ipv6_wildcard(self) -> None:
        self.assertEqual(
            release_gate._decode_ipv6_hex("0" * 32), "::"
        )

    def test_malformed_ipv6_is_rejected(self) -> None:
        self.assertEqual(release_gate._decode_ipv6_hex("00"), "")


class ProcParsingTests(unittest.TestCase):
    def _write(self, tcp: str, tcp6: str = "") -> str:
        directory = Path(self._tmp.name)
        (directory / "tcp").write_text(tcp, encoding="utf-8")
        if tcp6:
            (directory / "tcp6").write_text(tcp6, encoding="utf-8")
        return str(directory)

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_listening_sockets_are_parsed(self) -> None:
        root = self._write(
            TCP_HEADER
            + "\n   0: 0100007F:2233 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 12345 1"
            + "\n   1: 0100007F:1F91 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 12346 1"
        )
        sockets = release_gate.listening_sockets(root)
        self.assertEqual(len(sockets), 2)
        self.assertEqual(sockets[0]["address"], "127.0.0.1")
        self.assertEqual(sockets[0]["port"], 0x2233)
        self.assertEqual(sockets[1]["port"], 0x1F91)

    def test_non_listening_sockets_are_ignored(self) -> None:
        root = self._write(
            TCP_HEADER
            + "\n   0: 0100007F:2233 0100007F:1F90 01 00000000:00000000 00:00000000 00000000  1000        0 1 1"
            + "\n   1: 00000000:0000 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 2 1"
        )
        sockets = release_gate.listening_sockets(root)
        self.assertEqual(len(sockets), 1)
        self.assertEqual(sockets[0]["address"], "0.0.0.0")

    def test_ipv6_is_inspected(self) -> None:
        root = self._write(
            TCP_HEADER
            + "\n   0: 0100007F:2233 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 1 1",
            TCP_HEADER
            + "\n   0: 00000000000000000000000001000000:2234 00000000000000000000000000000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 2 1",
        )
        sockets = release_gate.listening_sockets(root)
        addresses = {entry["address"] for entry in sockets}
        self.assertIn("127.0.0.1", addresses)
        self.assertIn("::1", addresses)

    def test_a_missing_proc_file_is_tolerated(self) -> None:
        sockets = release_gate.listening_sockets("/nonexistent/path")
        self.assertEqual(sockets, [])

    def test_bound_listeners_are_parsed_from_the_probe_output(self) -> None:
        raw = json.dumps(
            [{"address": "127.0.0.1", "port": 8765, "family": "tcp"}]
        )
        self.assertEqual(release_gate._parse_bound_listeners(raw), [("127.0.0.1", 8765)])

    def test_a_wildcard_probe_output_is_recognised(self) -> None:
        raw = json.dumps([{"address": "0.0.0.0", "port": 8765, "family": "tcp"}])
        bound = release_gate._parse_bound_listeners(raw)
        self.assertTrue([entry for entry in bound if entry[0] in ("0.0.0.0", "::")])

    def test_unparseable_probe_output_is_ignored(self) -> None:
        self.assertEqual(release_gate._parse_bound_listeners("not json"), [])
        self.assertEqual(release_gate._parse_bound_listeners(""), [])


class ProcessSocketTests(unittest.TestCase):
    """The probe must report the gateway's own sockets, not the host's."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _make_process(self, pid: int, inodes: list[int]) -> None:
        fd_dir = self.root / str(pid) / "fd"
        fd_dir.mkdir(parents=True)
        for index, inode in enumerate(inodes):
            target = fd_dir / str(index)
            target.symlink_to(f"socket:[{inode}]")

    def _make_cmdline(self, pid: int, *args: str) -> None:
        process = self.root / str(pid)
        process.mkdir(parents=True, exist_ok=True)
        (process / "cmdline").write_bytes("\0".join(args).encode() + b"\0")

    def test_socket_inodes_are_collected_from_the_process(self) -> None:
        self._make_process(42, [100, 200])
        inodes = release_gate.process_socket_inodes(42, str(self.root))
        self.assertEqual(inodes, {100, 200})

    def test_non_socket_descriptors_are_ignored(self) -> None:
        fd_dir = self.root / "43" / "fd"
        fd_dir.mkdir(parents=True)
        (fd_dir / "0").symlink_to("/dev/null")
        (fd_dir / "1").symlink_to("socket:[7]")
        inodes = release_gate.process_socket_inodes(43, str(self.root))
        self.assertEqual(inodes, {7})

    def test_a_missing_process_yields_no_inodes(self) -> None:
        self.assertEqual(release_gate.process_socket_inodes(999, str(self.root)), set())

    def test_listening_sockets_are_filtered_by_process(self) -> None:
        proc_net = self.root / "net"
        proc_net.mkdir()
        (proc_net / "tcp").write_text(
            TCP_HEADER
            + "\n   0: 0100007F:2233 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000 0 111 1"
            + "\n   1: 00000000:1F91 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000 0 222 1",
            encoding="utf-8",
        )
        self._make_process(42, [111])

        sockets = release_gate.listening_sockets(
            str(proc_net), pid=42, process_root=str(self.root)
        )
        self.assertEqual(len(sockets), 1)
        self.assertEqual(sockets[0]["address"], "127.0.0.1")
        self.assertEqual(sockets[0]["port"], 0x2233)

    def test_a_wildcard_socket_of_another_process_is_excluded(self) -> None:
        proc_net = self.root / "net"
        proc_net.mkdir()
        (proc_net / "tcp").write_text(
            TCP_HEADER
            + "\n   0: 00000000:1F91 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000 0 222 1",
            encoding="utf-8",
        )
        self._make_process(42, [111])
        sockets = release_gate.listening_sockets(
            str(proc_net), pid=42, process_root=str(self.root)
        )
        self.assertEqual(sockets, [])

    def test_missing_gateway_pid_fails_closed_instead_of_scanning_host(self) -> None:
        proc_net = self.root / "net"
        proc_net.mkdir()
        (proc_net / "tcp").write_text(
            TCP_HEADER
            + "\n   0: 0100007F:4967 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000 0 111 1",
            encoding="utf-8",
        )
        self.assertEqual(release_gate.gateway_listening_sockets(str(self.root)), [])

    def test_wrong_process_is_not_attributed_as_gateway(self) -> None:
        self._make_cmdline(42, "python", "-c", "gateway")
        self._make_process(42, [111])
        proc_net = self.root / "net"
        proc_net.mkdir()
        (proc_net / "tcp").write_text(
            TCP_HEADER
            + "\n   0: 0100007F:4967 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000 0 111 1",
            encoding="utf-8",
        )
        self.assertEqual(release_gate.gateway_listening_sockets(str(self.root)), [])

    def test_gateway_listener_scan_excludes_unrelated_host_listener(self) -> None:
        self._make_cmdline(42, "python", "-m", "gateway", "--port", "18775")
        self._make_process(42, [111])
        proc_net = self.root / "net"
        proc_net.mkdir()
        (proc_net / "tcp").write_text(
            TCP_HEADER
            + "\n   0: 0100007F:4967 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000 0 111 1"
            + "\n   1: 00000000:1F91 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000 0 222 1",
            encoding="utf-8",
        )
        self.assertEqual(
            release_gate.gateway_listening_sockets(str(self.root)),
            [{"address": "127.0.0.1", "port": 0x4967, "family": "tcp"}],
        )


class ExpectedListenerTests(unittest.TestCase):
    def test_only_exact_expected_loopback_listener_passes(self) -> None:
        self.assertTrue(release_gate._has_expected_listener([("127.0.0.1", 18775)]))
        self.assertFalse(release_gate._has_expected_listener([("127.0.0.1", 8765)]))
        self.assertFalse(release_gate._has_expected_listener([("127.0.0.1", 8766)]))
        self.assertFalse(release_gate._has_expected_listener([("0.0.0.0", 18775)]))
        self.assertFalse(
            release_gate._has_expected_listener(
                [("127.0.0.1", 18775), ("127.0.0.1", 8766)]
            )
        )


class PreAuthBoundaryTests(unittest.TestCase):
    """Without a session: ``401`` on state and mutation, ``403`` cross-origin.

    The two checks rewritten here used to demand the **superseded**
    no-session answers - a ``200`` state GET and a mutation dispatched
    with no session - so every release against the owner-auth lineage
    failed the gate on a false positive.
    """

    BASE = "http://127.0.0.1:18775"

    def _record(self, answers: list[int]):
        """Run the checks against an ``http()`` stand-in that answers in order."""
        gate = release_gate.Gate()
        queue = list(answers)
        calls: list[dict[str, Any]] = []

        def fake(url, method="GET", headers=None, body=None, timeout=10.0):
            calls.append(
                {"url": url, "method": method, "headers": dict(headers or {}), "body": body}
            )
            # An exhausted queue behaves like a gateway that answers
            # nothing, so a missing answer fails a check instead of
            # raising out of the gate.
            return (queue.pop(0) if queue else 500), {}

        release_gate.record_pre_auth_checks(gate, self.BASE, http_fn=fake)
        return gate, calls

    def test_the_documented_refusals_pass(self) -> None:
        gate, _ = self._record(list(PRE_AUTH_ANSWERS))
        self.assertTrue(gate.ok, [check.to_dict() for check in gate.checks])
        self.assertEqual(len(gate.checks), 4)

    def test_the_superseded_no_session_answers_fail(self) -> None:
        """A 200 state GET and a dispatched mutation are now failures."""
        gate, _ = self._record([200, 200, 404, 403])
        self.assertFalse(gate.ok)
        failed = [check.name for check in gate.checks if not check.passed]
        self.assertEqual(
            failed,
            [
                "the console state is refused without an owner session",
                "a hostile Origin does not open the owner state",
                "an owner mutation is refused without a session",
            ],
        )

    def test_only_401_counts_as_the_session_refusal(self) -> None:
        """A 403 on state means the origin layer answered, not the session gate."""
        gate, _ = self._record([403, 401, 401, 403])
        self.assertFalse(gate.ok)
        self.assertFalse(gate.checks[0].passed)
        self.assertTrue(gate.checks[1].passed)

    def test_a_gateway_that_answers_nothing_fails_every_check(self) -> None:
        gate, _ = self._record([])
        self.assertFalse(gate.ok)
        self.assertEqual([check.passed for check in gate.checks], [False] * 4)

    def test_the_probes_hit_the_documented_routes(self) -> None:
        gate, calls = self._record(list(PRE_AUTH_ANSWERS))
        self.assertTrue(gate.ok)
        self.assertEqual(len(calls), 4)

        state, hostile_state, mutation, cross_origin = calls
        self.assertEqual(state["method"], "GET")
        self.assertTrue(str(state["url"]).endswith(release_gate.OWNER_STATE_PATH))
        self.assertNotIn("Origin", state["headers"])

        self.assertEqual(hostile_state["method"], "GET")
        self.assertEqual(str(hostile_state["url"]), str(state["url"]))
        self.assertEqual(hostile_state["headers"].get("Origin"), release_gate.HOSTILE_ORIGIN)

        self.assertEqual(mutation["method"], "POST")
        self.assertTrue(str(mutation["url"]).endswith(release_gate.OWNER_MUTATION_PATH))
        self.assertEqual(mutation["headers"].get("Origin"), self.BASE)
        self.assertEqual(mutation["body"], dict(release_gate.OWNER_MUTATION_BODY))

        self.assertEqual(cross_origin["method"], "POST")
        self.assertEqual(str(cross_origin["url"]), str(mutation["url"]))
        self.assertEqual(cross_origin["headers"].get("Origin"), release_gate.HOSTILE_ORIGIN)
        self.assertEqual(cross_origin["body"], dict(release_gate.OWNER_MUTATION_BODY))


class SignedInBoundaryTests(unittest.TestCase):
    """With a session: the same routes must answer, or the gate is blind.

    Without this half a gateway that refused *every* request would satisfy
    the unauthenticated checks.
    """

    def test_the_documented_statuses_pass(self) -> None:
        gate = release_gate.Gate()
        release_gate.record_signed_in_checks(gate, dict(SIGNED_IN_ANSWERS))
        self.assertTrue(gate.ok, [check.to_dict() for check in gate.checks])
        self.assertEqual(len(gate.checks), 5)

    def test_a_session_that_opens_nothing_fails(self) -> None:
        statuses = dict(SIGNED_IN_ANSWERS, state=401, state_hostile_origin=401, mutation=401)
        gate = release_gate.Gate()
        release_gate.record_signed_in_checks(gate, statuses)
        self.assertFalse(gate.ok)
        failed = [check.name for check in gate.checks if not check.passed]
        self.assertEqual(
            failed,
            [
                "the console state answers a signed-in session",
                "a hostile Origin does not block a signed-in safe GET",
                "a signed-in same-origin owner mutation is dispatched",
            ],
        )

    def test_a_probe_that_reported_nothing_fails_every_check(self) -> None:
        gate = release_gate.Gate()
        release_gate.record_signed_in_checks(gate, {})
        self.assertFalse(gate.ok)
        self.assertEqual([check.passed for check in gate.checks], [False] * 5)

    def test_a_transport_failure_is_not_read_as_a_dispatch(self) -> None:
        """Status 0 is the probe's transport failure, never a success."""
        statuses = dict(SIGNED_IN_ANSWERS, mutation=0)
        gate = release_gate.Gate()
        release_gate.record_signed_in_checks(gate, statuses)
        self.assertFalse(gate.ok)
        dispatch = next(c for c in gate.checks if c.name.endswith("is dispatched"))
        self.assertFalse(dispatch.passed)
        self.assertEqual(dispatch.detail, "status=0")

    def test_a_probe_failure_is_explained_on_the_first_check(self) -> None:
        gate = release_gate.Gate()
        release_gate.record_signed_in_checks(gate, {}, note="exec failed")
        self.assertIn("exec failed", gate.checks[0].detail)
        # The note belongs to the check whose failure explains the rest.
        self.assertNotIn("exec failed", gate.checks[-1].detail)


class OwnerSessionProbeSourceTests(unittest.TestCase):
    """The probe the gate ships into the candidate container."""

    def test_the_probe_source_compiles(self) -> None:
        compile(release_gate.OWNER_SESSION_PROBE_SOURCE, "<owner-session-probe>", "exec")

    def test_the_probe_source_is_sent_verbatim(self) -> None:
        """The artifact under test cannot substitute its own answers."""
        seen: list[list[str]] = []

        def fake_run(args, timeout=120):
            seen.append(list(args))
            return subprocess.CompletedProcess(args, 0, '{"setup": 200}', "")

        statuses, note = release_gate.run_owner_session_probe("a-container", run_fn=fake_run)
        self.assertEqual(note, "")
        self.assertEqual(statuses, {"setup": 200})
        self.assertEqual(seen[0][:5], ["docker", "exec", "a-container", "python", "-c"])
        self.assertEqual(seen[0][5], release_gate.OWNER_SESSION_PROBE_SOURCE)

    def test_a_probe_that_prints_nothing_fails_closed(self) -> None:
        def fake_run(args, timeout=120):
            return subprocess.CompletedProcess(args, 1, "", "python: can't open file")

        statuses, note = release_gate.run_owner_session_probe("a-container", run_fn=fake_run)
        self.assertEqual(statuses, {})
        self.assertIn("can't open file", note)

    def test_a_non_object_report_is_rejected(self) -> None:
        for stdout in ("[200, 200]", '"200"', "not json", ""):
            with self.subTest(stdout=stdout):
                def fake_run(args, timeout=120, stdout=stdout):
                    return subprocess.CompletedProcess(args, 0, stdout, "")

                statuses, note = release_gate.run_owner_session_probe(
                    "a-container", run_fn=fake_run
                )
                self.assertEqual(statuses, {})
                self.assertTrue(note)

    def test_non_integer_values_are_dropped(self) -> None:
        stdout = json.dumps({"setup": "200", "state": True, "mutation": 404})

        def fake_run(args, timeout=120):
            return subprocess.CompletedProcess(args, 0, stdout, "")

        statuses, note = release_gate.run_owner_session_probe("a-container", run_fn=fake_run)
        self.assertEqual(statuses, {"mutation": 404})
        self.assertEqual(note, "")

    def test_a_runner_that_raises_is_reported_not_raised(self) -> None:
        def fake_run(args, timeout=120):
            raise subprocess.TimeoutExpired(args, 90)

        statuses, note = release_gate.run_owner_session_probe("a-container", run_fn=fake_run)
        self.assertEqual(statuses, {})
        self.assertIn("TimeoutExpired", note)


def _no_release_identity() -> dict[str, str]:
    """The fixture reports no release identity; the boundary checks do not read one."""
    return {}


class _LiveGateway:
    """A live gateway on a free loopback port with a pending first-run
    setup code - the state the release gate's disposable container is
    always in when the probe runs."""

    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.host = "127.0.0.1"
        self.port = free_port(self.host)
        config = ConfigStore(self.data_dir / "config.json")
        credentials = CredentialStore(self.data_dir)
        registry = AdapterRegistry()
        policy = SecurityPolicy(
            allowed_hosts=build_allowed_hosts(self.host, self.port),
            canonical_origins=build_canonical_origins(self.host, self.port),
        )
        self.app = GatewayApp(
            config=config,
            credentials=credentials,
            router=Router(config, registry, ledger=RequestLedger()),
            registry=registry,
            profile_auth=ProfileAuth(config, credentials),
            policy=policy,
            ledger=RequestLedger(),
            limits=LimitStore(),
            release_identity=_no_release_identity,
            base_url=f"http://{self.host}:{self.port}",
            mounts=("/owner",),
            logger=RequestLogger(enabled=False),
        )
        self.server = Server(self.host, self.port, self.app.handle)
        self.server.start_background()

    @property
    def base(self) -> str:
        return f"http://{self.host}:{self.port}"

    def collect(self) -> dict[str, int]:
        """Run the exact source the gate ships, the way the container runs it.

        ``python -c`` with ``GATEWAY_DATA_DIR``/``GATEWAY_PORT`` in the
        environment - the same entry point ``docker exec`` uses - so the
        ``__main__`` block, the imports and the printed report are all
        exercised, not just ``collect()``.
        """
        env = dict(
            os.environ,
            GATEWAY_DATA_DIR=str(self.data_dir),
            GATEWAY_PORT=str(self.port),
            PYTHONPATH=str(SRC_ROOT),
        )
        result = subprocess.run(
            [sys.executable, "-c", release_gate.OWNER_SESSION_PROBE_SOURCE],
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
            check=False,
        )
        if result.returncode != 0:
            raise AssertionError(f"the probe exited {result.returncode}: {result.stderr[-500:]}")
        return json.loads(result.stdout)

    def close(self) -> None:
        self.server.stop()
        self._tmp.cleanup()


class OwnerSessionProbeLiveTests(unittest.TestCase):
    """The probe source against a real gateway over a real socket."""

    def setUp(self) -> None:
        self.gateway = _LiveGateway()
        self.addCleanup(self.gateway.close)

    def test_it_completes_first_run_setup_and_reports_the_documented_statuses(self) -> None:
        statuses = self.gateway.collect()
        self.assertEqual(statuses, SIGNED_IN_ANSWERS)
        # Only integers cross the boundary, so no secret can ride along.
        self.assertTrue(all(isinstance(value, int) for value in statuses.values()))

    def test_the_statuses_satisfy_the_gate_checks(self) -> None:
        gate = release_gate.Gate()
        release_gate.record_signed_in_checks(gate, self.gateway.collect())
        self.assertTrue(gate.ok, [check.to_dict() for check in gate.checks])

    def test_a_second_run_never_guesses_a_credential(self) -> None:
        """The setup code is single-use; a later run reports no session."""
        self.assertEqual(self.gateway.collect()["setup"], 200)
        statuses = self.gateway.collect()
        self.assertEqual(statuses, dict.fromkeys(statuses, 0))
        gate = release_gate.Gate()
        release_gate.record_signed_in_checks(gate, statuses)
        self.assertFalse(gate.ok)


class PreAuthBoundaryLiveTests(unittest.TestCase):
    """The host-side refusal checks against a real gateway."""

    def setUp(self) -> None:
        self.gateway = _LiveGateway()
        self.addCleanup(self.gateway.close)

    def test_the_unauthenticated_boundary_is_401_and_403(self) -> None:
        gate = release_gate.Gate()
        release_gate.record_pre_auth_checks(gate, self.gateway.base)
        self.assertTrue(gate.ok, [check.to_dict() for check in gate.checks])
        self.assertEqual([check.detail for check in gate.checks], ["status=401"] * 3 + ["status=403"])

    def test_the_superseded_answers_are_not_what_the_gateway_gives(self) -> None:
        """Guard the expectation itself: the old checks wanted 200 and a
        dispatched mutation, and neither is what this boundary serves."""
        status, _ = release_gate.http(self.gateway.base + release_gate.OWNER_STATE_PATH)
        self.assertEqual(status, 401)
        status, _ = release_gate.http(
            self.gateway.base + release_gate.OWNER_MUTATION_PATH,
            method="POST",
            headers={"Origin": self.gateway.base},
            body=dict(release_gate.OWNER_MUTATION_BODY),
        )
        self.assertEqual(status, 401)


if __name__ == "__main__":
    unittest.main()
