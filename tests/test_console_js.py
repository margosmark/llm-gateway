"""Runs the console's JavaScript regression test.

The console is the only surface the owner actually uses, and it is plain
JavaScript with no build step. Its DOM helper has a non-obvious contract -
boolean attributes must be *removed* rather than set to ``false``, because
``disabled`` is a boolean attribute in HTML - and that contract is easier to
break than to notice.

The test is skipped when Node is not installed; it never fails the suite
because of a missing optional tool.
"""

from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

SCRIPT = Path(__file__).parent / "console_dom_test.js"


class ConsoleJavaScriptTests(unittest.TestCase):
    def test_the_console_dom_helper_handles_boolean_attributes(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not installed")
        result = subprocess.run(
            [node, str(SCRIPT)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(
            result.returncode,
            0,
            "the console DOM helper test failed:\n" + result.stdout + result.stderr,
        )


if __name__ == "__main__":
    unittest.main()
