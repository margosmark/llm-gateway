"""Tests for the command line entry point.

The bind-address rules are the gateway's first line of defence, so they are
tested directly rather than only through the running service.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gateway.cli import _is_private_or_loopback, _is_wildcard, build_application, build_parser


class BindRuleTests(unittest.TestCase):
    def test_wildcard_addresses_are_recognised(self) -> None:
        for host in ("0.0.0.0", "::", "[::]", "*", " 0.0.0.0 "):
            self.assertTrue(_is_wildcard(host), host)

    def test_explicit_addresses_are_not_wildcards(self) -> None:
        for host in ("10.10.11.109", "192.168.0.30", "127.0.0.1", "fe80::1"):
            self.assertFalse(_is_wildcard(host), host)

    def test_private_and_loopback_addresses_are_accepted(self) -> None:
        for host in ("10.10.11.109", "192.168.0.30", "172.16.0.1", "127.0.0.1"):
            self.assertTrue(_is_private_or_loopback(host), host)

    def test_public_addresses_are_refused(self) -> None:
        # Documentation ranges (192.0.2.0/24, 198.51.100.0/24,
        # 203.0.113.0/24) are treated as private by Python's ipaddress
        # module, so the test uses genuinely routable public addresses.
        for host in ("8.8.8.8", "1.1.1.1", "93.184.216.34", "not-an-address"):
            self.assertFalse(_is_private_or_loopback(host), host)


class ParserTests(unittest.TestCase):
    def test_the_setup_bind_defaults_to_the_lan_address(self) -> None:
        args = build_parser().parse_args(["--host", "10.10.11.109"])
        self.assertEqual(args.setup_bind, "")

    def test_the_setup_bind_can_be_pinned_to_loopback(self) -> None:
        args = build_parser().parse_args(["--host", "10.10.11.109", "--setup-bind", "127.0.0.1"])
        self.assertEqual(args.setup_bind, "127.0.0.1")


class ApplicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        # The bind-address pre-flight check asks the operating system
        # whether the address is local. The test host may not own the
        # address the test is about, so that one check is stubbed; the
        # rejection paths are tested directly.
        patcher = mock.patch("gateway.cli._address_is_local", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_the_setup_listener_defaults_to_the_lan_address(self) -> None:
        result = build_application(self.data_dir, "10.10.11.109", 8765, 8766)
        self.assertEqual(result[-1], "10.10.11.109")

    def test_the_setup_listener_can_be_pinned_to_loopback(self) -> None:
        result = build_application(self.data_dir, "10.10.11.109", 8765, 8766, "127.0.0.1")
        self.assertEqual(result[-1], "127.0.0.1")

    def test_a_non_local_setup_bind_is_refused(self) -> None:
        with mock.patch("gateway.cli._address_is_local", return_value=False):
            with self.assertRaises(SystemExit):
                build_application(self.data_dir, "10.10.11.109", 8765, 8766, "10.10.11.109")

    def test_a_wildcard_setup_bind_is_refused(self) -> None:
        with self.assertRaises(SystemExit):
            build_application(self.data_dir, "10.10.11.109", 8765, 8766, "0.0.0.0")

    def test_a_public_setup_bind_is_refused(self) -> None:
        with self.assertRaises(SystemExit):
            build_application(self.data_dir, "10.10.11.109", 8765, 8766, "8.8.8.8")

    def test_both_providers_are_present_from_the_start(self) -> None:
        result = build_application(self.data_dir, "10.10.11.109", 8765, 8766)
        config = result[2]
        snapshot = config.snapshot()
        self.assertIn("chatgpt_codex", snapshot.providers)
        self.assertIn("opencode_go", snapshot.providers)

    def test_a_fresh_install_is_empty(self) -> None:
        result = build_application(self.data_dir, "10.10.11.109", 8765, 8766)
        snapshot = result[2].snapshot()
        self.assertFalse(snapshot.initialized)
        self.assertEqual(snapshot.aliases, {})
        self.assertEqual(snapshot.profiles, {})
        self.assertEqual(snapshot.published_aliases(), [])

    def test_the_setup_surface_is_mounted_only_on_the_setup_listener(self) -> None:
        result = build_application(self.data_dir, "10.10.11.109", 8765, 8766)
        lan_app, setup_app = result[0], result[1]
        self.assertEqual(lan_app.mounts, ("/owner",))
        self.assertEqual(setup_app.mounts, ("/setup",))

    def test_the_credential_store_is_owner_only(self) -> None:
        result = build_application(self.data_dir, "10.10.11.109", 8765, 8766)
        credentials = result[3]
        self.assertEqual(credentials.root.stat().st_mode & 0o777, 0o700)


if __name__ == "__main__":
    unittest.main()
