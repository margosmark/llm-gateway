"""The capability model's own declaration hygiene.

A dataclass cannot hold two fields of the same name: the second declaration
silently overwrites the first, and the shadowed one is invisible at runtime.
``ModelInfo`` once declared ``context_window`` twice, so the second
declaration - and the reader's expectation of what it did - depended on
which line was read.

The runtime cannot detect this, because ``dataclasses.fields`` reports a
mapping and a mapping cannot have a duplicate key. The source can, so this
test reads the source.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PACKAGE = REPO / "src" / "gateway"


def _shadowed_fields(path: Path) -> list[tuple[str, str, int]]:
    """Return every ``(class, field, line)`` declared more than once."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[str, str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        seen: dict[str, int] = {}
        for statement in node.body:
            if not isinstance(statement, ast.AnnAssign):
                continue
            if not isinstance(statement.target, ast.Name):
                continue
            name = statement.target.id
            if name in seen:
                found.append((node.name, name, statement.lineno))
            seen[name] = statement.lineno
    return found


class DeclarationHygieneTests(unittest.TestCase):
    def test_no_class_declares_the_same_field_twice(self) -> None:
        offenders: list[str] = []
        for path in sorted(PACKAGE.rglob("*.py")):
            for class_name, field, line in _shadowed_fields(path):
                relative = path.relative_to(REPO)
                offenders.append(f"{relative}:{line}: {class_name}.{field}")
        self.assertEqual(
            offenders,
            [],
            "a field declared twice is silently shadowed: " + "; ".join(offenders),
        )

    def test_the_capability_model_declares_each_field_once(self) -> None:
        from gateway.core.capabilities import ModelInfo

        names = [field.name for field in ModelInfo.__dataclass_fields__.values()]
        self.assertEqual(len(names), len(set(names)))
        self.assertIn("context_window", names)
        self.assertIn("context_limit", names)


if __name__ == "__main__":
    unittest.main()
