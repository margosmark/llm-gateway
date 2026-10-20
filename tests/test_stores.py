"""Tests for atomic, permission-restricted persistence."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from gateway.stores import atomic_write_json, ensure_private_dir, read_json, remove_file


class AtomicWriteTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_write_then_read_round_trips(self) -> None:
        target = self.root / "state" / "config.json"
        atomic_write_json(target, {"revision": 3, "name": "coding"})
        self.assertEqual(read_json(target), {"revision": 3, "name": "coding"})

    def test_file_is_owner_only(self) -> None:
        target = self.root / "config.json"
        atomic_write_json(target, {"a": 1})
        mode = target.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600, f"expected 0600, got {oct(mode)}")

    def test_parent_directory_is_owner_only(self) -> None:
        target = self.root / "nested" / "config.json"
        atomic_write_json(target, {"a": 1})
        mode = (target.parent).stat().st_mode & 0o777
        self.assertEqual(mode, 0o700, f"expected 0700, got {oct(mode)}")

    def test_write_replaces_without_leaving_temp_files(self) -> None:
        target = self.root / "config.json"
        atomic_write_json(target, {"a": 1})
        atomic_write_json(target, {"a": 2})
        self.assertEqual(read_json(target), {"a": 2})
        leftovers = [p.name for p in self.root.iterdir() if p.name.startswith(".config.json")]
        self.assertEqual(leftovers, [], f"temp files left behind: {leftovers}")

    def test_read_json_returns_default_for_missing_file(self) -> None:
        self.assertIsNone(read_json(self.root / "missing.json"))
        self.assertEqual(read_json(self.root / "missing.json", {"d": 1}), {"d": 1})

    def test_read_json_returns_default_for_corrupt_file(self) -> None:
        target = self.root / "corrupt.json"
        target.write_text("{not json", encoding="utf-8")
        self.assertEqual(read_json(target, {"fallback": True}), {"fallback": True})

    def test_remove_file_reports_whether_anything_was_removed(self) -> None:
        target = self.root / "gone.json"
        self.assertFalse(remove_file(target))
        target.write_text("{}", encoding="utf-8")
        self.assertTrue(remove_file(target))
        self.assertFalse(target.exists())

    def test_ensure_private_dir_is_idempotent(self) -> None:
        path = ensure_private_dir(self.root / "a" / "b")
        self.assertTrue(path.is_dir())
        ensure_private_dir(path)
        self.assertEqual(path.stat().st_mode & 0o777, 0o700)

    def test_written_content_is_valid_json_with_trailing_newline(self) -> None:
        target = self.root / "config.json"
        atomic_write_json(target, {"a": 1})
        raw = target.read_text(encoding="utf-8")
        self.assertTrue(raw.endswith("\n"))
        json.loads(raw)


if __name__ == "__main__":
    unittest.main()
