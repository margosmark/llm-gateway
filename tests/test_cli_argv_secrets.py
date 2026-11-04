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
import re
import tempfile
import unittest
from pathlib import Path

from gateway import cli
from gateway.cli import (
    RESET_OWNER_PASSWORD_FLAG,
    _redact_argv_values,
    _reset_flag_in_play,
    build_parser,
    main,
)

# Synthetic only: these strings exist purely to be hunted for in stdout and
# stderr. Nothing here is, or ever was, a real credential.
SECRET = "synthetic-argv-secret-9f3aZ"

# The two secret forms the SEC-R1 re-gate found leaking when only the benign
# form above was tested: a leading `--` (looks like an option name) and a
# trailing/embedded `=` (the redaction used to rewrite the token into a
# string that still contained it).
SECRETS = {
    "plain": SECRET,
    "dashed": "--SynthDashed9!",
    "eq_end": "SynthTail=",
    "eq_mid": "SynthHead=Tail9",
    "single_dash": "-SynthSingle9",
}

# The refused command-line shapes the re-gate exercised, one row per shape.
# {flag} is the documented recovery flag.
SHAPES = [
    ("documented space", ["{flag}", "{secret}"]),
    ("documented equals", ["{flag}={secret}"]),
    ("accepted abbrev p", ["--reset-owner-p", "{secret}"]),
    ("accepted abbrev 2", ["--reset-owner", "{secret}"]),
    ("misspelled flag", ["--rest-owner-password", "{secret}"]),
    ("no flag at all", ["{secret}"]),
    ("beside unknown flag", ["--bogus-flag", "{secret}"]),
    ("unknown flag attached", ["--bogus-flag={secret}"]),
    ("after --", ["--", "{secret}"]),
    ("reset flag + data-dir", ["{flag}", "--data-dir", "{data_dir}", "{secret}"]),
    ("abbrev + data-dir", ["--reset-owner-p", "--data-dir", "{data_dir}", "{secret}"]),
]

# Every option string the parser registers, used as ground truth by the
# regime-detection tests below.
_KNOWN_OPTIONS = set(build_parser()._option_string_actions)


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


class RefusedShapeSecretMatrixTests(unittest.TestCase):
    """SEC-R1: every refused shape, every secret form, nothing written back.

    This is the re-gate's 11 shapes x 5 secret forms run through the real
    entry point. The leading-`--` and `=`-suffixed rows were the leaks the
    single benign secret above never exercised: each row here was RED on the
    tree the re-gate rejected and is GREEN after the fix.
    """

    def test_no_refused_parse_echoes_any_secret_form(self) -> None:
        for shape_label, template in SHAPES:
            for secret_name, secret in SECRETS.items():
                with self.subTest(shape=shape_label, secret=secret_name):
                    with tempfile.TemporaryDirectory(prefix="argv_secret_") as tmp:
                        data_dir = str(Path(tmp) / "data")
                        argv = [
                            t.format(
                                secret=secret,
                                flag=RESET_OWNER_PASSWORD_FLAG,
                                data_dir=data_dir,
                            )
                            for t in template
                        ]
                        code, out, err = run_cli(argv)
                    blob = out + err
                    self.assertEqual(code, 2, blob)
                    self.assertEqual(out, "")
                    self.assertNotIn(secret, blob, f"{shape_label} [{secret_name}]")
                    if "=" in secret:
                        # The whole token is withheld, not just the part
                        # after `=`: `head=<redacted>` still names the head.
                        head = secret.split("=", 1)[0] + "="
                        self.assertNotIn(head, blob, f"head disclosure {shape_label} [{secret_name}]")


class RecoveryRegimeDetectionTests(unittest.TestCase):
    """The recovery regime must trigger on how argparse *resolves* a token."""

    def test_exact_and_attached_forms_are_in_play(self) -> None:
        self.assertTrue(_reset_flag_in_play([RESET_OWNER_PASSWORD_FLAG], _KNOWN_OPTIONS))
        self.assertTrue(_reset_flag_in_play([f"{RESET_OWNER_PASSWORD_FLAG}=x"], _KNOWN_OPTIONS))

    def test_unambiguous_abbreviations_are_in_play(self) -> None:
        # argparse accepts these as the recovery flag, so they ARE the reset
        # flow; before the fix only the exact token entered the regime.
        for head in ("--reset", "--reset-owner-p", "--reset-owner", "--reset-owner-pass"):
            with self.subTest(head=head):
                self.assertTrue(_reset_flag_in_play([head, "--SynthDashed9!"], _KNOWN_OPTIONS))

    def test_a_close_typo_of_the_recovery_flag_is_in_play(self) -> None:
        # A near-miss of the recovery flag is a recovery attempt, not an
        # unknown flag to be echoed back (SEC-R1, requirement 4).
        for head in ("--rest-owner-password", "--rest-owner-password=x",
                     "--reset-owner-passwordx", "--reser-owner-password"):
            with self.subTest(head=head):
                self.assertTrue(_reset_flag_in_play([head, "--SynthDashed9!"], _KNOWN_OPTIONS))

    def test_unrelated_flags_are_not_in_play(self) -> None:
        for argv in (["--bogus-flag"], ["--port"], ["--host"], ["--p"],
                     ["--print-url"], ["--hst"], ["--data"], ["--max"], ["-h"],
                     ["-x"], ["--h"]):
            with self.subTest(argv=argv):
                self.assertFalse(_reset_flag_in_play(argv, _KNOWN_OPTIONS))


class OrdinaryRegimeRuleTests(unittest.TestCase):
    """The ordinary regime's per-token rules, independent of argparse."""

    def test_tokens_after_the_separator_are_values_even_when_dashed(self) -> None:
        safe = _redact_argv_values(
            "unrecognized arguments: --SynthDashed9!",
            ["--", "--SynthDashed9!"],
            set(),
        )
        self.assertNotIn("--SynthDashed9!", safe)
        self.assertEqual(safe, "unrecognized arguments: <redacted>")

    def test_a_positional_token_with_equals_is_withheld_whole(self) -> None:
        # Rewriting it to `head=<redacted>` would still print the secret
        # when the value after `=` is empty (SEC-R1 Class B).
        for message in ("unrecognized arguments: SynthTail=",
                        "unrecognized arguments: SynthHead=Tail9"):
            with self.subTest(message=message):
                safe = _redact_argv_values(message, [message.split(": ", 1)[1]], set())
                self.assertEqual(safe, "unrecognized arguments: <redacted>")

    def test_an_option_like_head_stays_named(self) -> None:
        # Naming a flag-shaped head is the diagnosis; the value goes.
        safe = _redact_argv_values(
            "unrecognized arguments: --bogus-flag=SynthTail=",
            ["--bogus-flag=SynthTail="],
            set(),
        )
        self.assertEqual(safe, "unrecognized arguments: --bogus-flag=<redacted>")

    def test_flag_shape_matches_every_registered_long_option(self) -> None:
        # The naming rule is pinned to the parser: every long option this
        # parser defines is a flag-shaped token, so naming flag-shaped
        # unknown tokens can never cost an operator a real flag's name.
        # If a future option breaks this, the rule must be revisited
        # consciously instead of drifting.
        long_options = [o for o in build_parser()._option_string_actions if o.startswith("--")]
        self.assertGreater(len(long_options), 5)
        for option in long_options:
            with self.subTest(option=option):
                self.assertIsNotNone(
                    re.fullmatch(r"--[a-z0-9][a-z0-9_-]*", option),
                    f"{option} is not flag-shaped",
                )

    def test_flag_shape_classifier_behaves_as_documented(self) -> None:
        # Named (could be an option name of this parser):
        for token in ("--bogus-flag", "--hst", "--rest-owner-password", "--p", "--v1"):
            with self.subTest(token=token):
                self.assertTrue(cli._flag_shaped(token))
        # Withheld (cannot be an option name of this parser):
        for token in ("--SynthDashed9!", "--Summer2026!", "--has space", "--"):
            with self.subTest(token=token):
                self.assertFalse(cli._flag_shaped(token))


class DocumentedResidualBoundaryTests(unittest.TestCase):
    """SEC-R1's documented residual boundary (docs/OWNER_AUTH.md).

    A leading-`--` secret that is byte-identical to a flag-shaped unknown
    token, on a command line with no recovery signal, cannot be told apart
    from a typo'd flag - and the preserved contract names unknown flags
    verbatim (`unrecognized arguments: --bogus-flag`). That row is the
    documented boundary, not an oversight: these tests pin it so the
    boundary cannot drift silently in either direction.
    """

    FLAG_SHAPED_SECRET = "--summer2026-pass"

    def test_flag_shaped_unknown_token_next_to_another_is_named_verbatim(self) -> None:
        code, _out, err = run_cli(["--bogus-flag", self.FLAG_SHAPED_SECRET])
        self.assertEqual(code, 2)
        self.assertIn(
            f"unrecognized arguments: --bogus-flag {self.FLAG_SHAPED_SECRET}",
            err,
        )

    def test_flag_shaped_unknown_token_alone_is_named_verbatim(self) -> None:
        code, _out, err = run_cli([self.FLAG_SHAPED_SECRET])
        self.assertEqual(code, 2)
        self.assertIn(f"unrecognized arguments: {self.FLAG_SHAPED_SECRET}", err)

    def test_non_flag_shaped_dashed_token_is_withheld_in_the_same_shapes(self) -> None:
        # The boundary is the shape, not the dashes: this is the counterpart
        # row that was RED on the rejected tree and is GREEN now.
        for argv in (["--bogus-flag", "--SynthDashed9!"], ["--SynthDashed9!"]):
            with self.subTest(argv=argv):
                code, _out, err = run_cli(argv)
                self.assertEqual(code, 2)
                self.assertNotIn("--SynthDashed9!", err)


if __name__ == "__main__":
    unittest.main()
