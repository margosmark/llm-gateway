"""The CLI must never write an argv-supplied secret back to the terminal.

argv is visible in the process list and usually lands in shell history, so
owner-password recovery takes the new password from stdin
(``--reset-owner-password``). A parse that refuses the command line must not
undo that: argparse echoes the very token it rejected, which is how
``--reset-owner-password <password>`` printed the password it was asked to
refuse. Every case here drives the real entry point with a synthetic secret
only and inspects both streams, while pinning the ordinary errors that must
stay helpful.
"""

from __future__ import annotations

import contextlib
import io
import tempfile
import unittest

from gateway.cli import _redact_argv_values, main

# Synthetic only: these strings exist purely to be hunted for in stdout and
# stderr. Nothing here is, or ever was, a real credential.
SECRET = "synthetic-argv-secret-9f3aZ"


def run_cli(argv: list[str]) -> tuple[int, str, str]:
    """Run the entry point, returning (exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = main(argv)
        except SystemExit as raised:
            code = raised.code
    assert isinstance(code, int)
    return code, out.getvalue(), err.getvalue()


class ResetFlagValueTests(unittest.TestCase):
    """The reported leak: a password handed to the recovery flag."""

    def test_a_positional_value_is_withheld_and_stdin_is_pointed_to(self) -> None:
        code, out, err = run_cli(["--reset-owner-password", SECRET])
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertNotIn(SECRET, err)
        self.assertIn("unrecognized arguments", err)
        self.assertIn("read from stdin", err)

    def test_an_attached_value_is_withheld(self) -> None:
        code, out, err = run_cli([f"--reset-owner-password={SECRET}"])
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertNotIn(SECRET, err)
        self.assertIn("ignored explicit argument", err)
        self.assertIn("read from stdin", err)

    def test_a_leading_dash_beside_the_recovery_flag_is_withheld(self) -> None:
        # argparse treats this as an unknown option, so the token reaches
        # the error message on its own; it must not come back out.
        code, out, err = run_cli(["--reset-owner-password", "-" + SECRET])
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertNotIn(SECRET, err)


class GeneralArgvSecretTests(unittest.TestCase):
    """The same contract for every other refused command line."""

    def test_a_bare_positional_value_is_withheld(self) -> None:
        code, out, err = run_cli([SECRET])
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertNotIn(SECRET, err)
        self.assertIn("unrecognized arguments", err)

    def test_a_single_dash_value_is_withheld(self) -> None:
        code, out, err = run_cli(["-" + SECRET])
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertNotIn(SECRET, err)

    def test_an_unknown_flag_is_named_but_its_value_is_withheld(self) -> None:
        code, out, err = run_cli(["--bogus-flag", SECRET])
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertNotIn(SECRET, err)
        self.assertIn("--bogus-flag", err)

    def test_a_value_attached_to_an_unknown_flag_is_withheld(self) -> None:
        code, out, err = run_cli([f"--bogus-flag={SECRET}"])
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertNotIn(SECRET, err)
        self.assertIn("--bogus-flag", err)

    def test_a_value_passed_to_a_numeric_option_is_withheld(self) -> None:
        code, out, err = run_cli(["--max-workers", SECRET])
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertNotIn(SECRET, err)
        self.assertIn("invalid int value", err)

    def test_a_value_attached_to_an_ambiguous_abbreviation_is_withheld(self) -> None:
        # ``--p`` matches both --port and --print-url, and argparse echoes
        # the raw token rather than a quoted value.
        code, out, err = run_cli([f"--p={SECRET}"])
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertNotIn(SECRET, err)
        self.assertIn("ambiguous option", err)
        self.assertIn("--p=<redacted>", err)

    def test_a_secret_in_the_host_slot_is_not_echoed_by_the_refusal(self) -> None:
        # The startup refusals echo the host they refused; a value that is
        # not even an address was never a host, so it is withheld too.
        code, out, err = run_cli(["--host", SECRET])
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertNotIn(SECRET, err)
        self.assertIn("not a private or loopback address", err)


class OrdinaryErrorsStillWorkTests(unittest.TestCase):
    """Redaction must not cost the operator a usable diagnosis."""

    def test_help_still_lists_the_recovery_flag_and_its_stdin_contract(self) -> None:
        code, out, err = run_cli(["--help"])
        self.assertEqual(code, 0)
        self.assertIn("--reset-owner-password", out)
        self.assertIn("stdin", out)
        self.assertEqual(err, "")

    def test_an_unknown_flag_is_still_named(self) -> None:
        code, _out, err = run_cli(["--bogus-flag"])
        self.assertEqual(code, 2)
        self.assertIn("unrecognized arguments: --bogus-flag", err)

    def test_a_bad_port_still_says_why(self) -> None:
        code, _out, err = run_cli(["--port", "abc"])
        self.assertEqual(code, 2)
        self.assertIn("port must be an integer", err)

    def test_a_refused_address_is_still_named(self) -> None:
        code, _out, err = run_cli(["--host", "192.0.2.1"])
        self.assertEqual(code, 2)
        self.assertIn("192.0.2.1", err)
        self.assertIn("not a private or loopback address", err)

    def test_the_recovery_flag_still_reaches_the_stdin_flow(self) -> None:
        # With no value the flag parses and reaches the reset handler, which
        # refuses on an empty data directory before it ever prompts - proving
        # argv carries no password into that path.
        with tempfile.TemporaryDirectory() as tmp:
            code, out, err = run_cli(["--data-dir", tmp, "--reset-owner-password"])
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("no owner account exists yet", err)
        self.assertNotIn(tmp, err)


class RedactionHelperTests(unittest.TestCase):
    """The redaction rule itself, independent of argparse's wording."""

    def test_option_names_survive_while_values_do_not(self) -> None:
        safe = _redact_argv_values(
            "unrecognized arguments: --hst 10.10.11.109",
            ["--hst", "10.10.11.109"],
            set(),
        )
        self.assertEqual(safe, "unrecognized arguments: --hst <redacted>")

    def test_a_value_only_matches_as_a_whole_token(self) -> None:
        safe = _redact_argv_values("invalid int value: 'abcdef'", ["abc"], set())
        self.assertEqual(safe, "invalid int value: 'abcdef'")

    def test_an_escaped_repr_of_the_value_is_redacted_too(self) -> None:
        # argparse prints values with %r, so a newline arrives as the two
        # characters \n; redacting only the raw token would leak it.
        value = "line1\nline2"
        safe = _redact_argv_values(f"invalid int value: {value!r}", [value], set())
        self.assertNotIn("line1", safe)
        self.assertIn("<redacted>", safe)

    def test_empty_values_are_left_alone(self) -> None:
        self.assertEqual(_redact_argv_values("expected one argument", [""], set()),
                         "expected one argument")


if __name__ == "__main__":
    unittest.main()
