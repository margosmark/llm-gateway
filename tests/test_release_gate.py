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


if __name__ == "__main__":
    unittest.main()
