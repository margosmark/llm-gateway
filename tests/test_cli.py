"""Tests for the command line entry point.

The bind-address rules are the gateway's first line of defence, so they are
tested directly rather than only through the running service.
"""

from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from unittest import mock

from gateway.cli import _is_private_or_loopback, _is_wildcard, build_application, build_parser, main


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
    def test_parser_has_no_setup_listener_options(self) -> None:
        args = build_parser().parse_args(["--host", "10.10.11.109"])
        self.assertFalse(hasattr(args, "setup_bind"))
        self.assertFalse(hasattr(args, "setup_port"))

    def test_reserved_setup_port_is_rejected_from_cli_before_startup(self) -> None:
        error = StringIO()
        with redirect_stderr(error), mock.patch("gateway.cli.build_application") as build_app, \
                mock.patch("gateway.cli.Server") as server:
            with self.assertRaises(SystemExit) as raised:
                main(["--host", "10.10.11.109", "--port", "8766"])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("port 8766 is reserved", error.getvalue())
        build_app.assert_not_called()
        server.assert_not_called()

    def test_reserved_setup_port_is_rejected_from_environment_before_startup(self) -> None:
        error = StringIO()
        with mock.patch.dict("os.environ", {"GATEWAY_PORT": "8766"}), \
                redirect_stderr(error), mock.patch("gateway.cli.build_application") as build_app, \
                mock.patch("gateway.cli.Server") as server:
            with self.assertRaises(SystemExit) as raised:
                main(["--host", "10.10.11.109"])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("port 8766 is reserved", error.getvalue())
        build_app.assert_not_called()
        server.assert_not_called()

    def test_allowed_explicit_private_port_remains_configurable(self) -> None:
        with mock.patch("gateway.cli._address_is_local", return_value=True), \
                mock.patch("gateway.cli.build_application", return_value=(None, None, None, None)) as build_app:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["--host", "10.10.11.109", "--port", "9876", "--print-url"]), 0)
        build_app.assert_called_once()
        self.assertEqual(build_app.call_args.args[1:], ("10.10.11.109", 9876))


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

    def test_both_providers_are_present_from_the_start(self) -> None:
        result = build_application(self.data_dir, "10.10.11.109", 8765)
        config = result[1]
        snapshot = config.snapshot()
        self.assertIn("chatgpt_codex", snapshot.providers)
        self.assertIn("opencode_go", snapshot.providers)

    def test_a_fresh_install_is_empty(self) -> None:
        result = build_application(self.data_dir, "10.10.11.109", 8765)
        snapshot = result[1].snapshot()
        self.assertFalse(snapshot.initialized)
        self.assertEqual(snapshot.aliases, {})
        self.assertEqual(snapshot.profiles, {})
        self.assertEqual(snapshot.published_aliases(), [])

    def test_owner_console_is_mounted_on_the_only_application(self) -> None:
        app, _, _, _ = build_application(self.data_dir, "10.10.11.109", 8765)
        self.assertEqual(app.mounts, ("/owner",))
        self.assertNotIn("/setup", app.mounts)

    def test_the_credential_store_is_owner_only(self) -> None:
        result = build_application(self.data_dir, "10.10.11.109", 8765)
        credentials = result[2]
        self.assertEqual(credentials.root.stat().st_mode & 0o777, 0o700)


if __name__ == "__main__":
    unittest.main()
