"""Tests for the release gate's listener inspection.

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
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

GATE = Path(__file__).resolve().parent.parent / "scripts" / "release_gate.py"
_spec = importlib.util.spec_from_file_location("release_gate_under_test", GATE)
assert _spec and _spec.loader
release_gate = importlib.util.module_from_spec(_spec)
# dataclasses resolves annotations through sys.modules, so register first.
sys.modules[_spec.name] = release_gate
_spec.loader.exec_module(release_gate)

TCP_HEADER = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode"


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


if __name__ == "__main__":
    unittest.main()
