"""Atomic, permission-restricted JSON persistence.

Every write in the gateway goes through :func:`atomic_write_json` so that a
crash mid-write can never leave a half-written credential file or a torn
configuration behind. The pattern is the same everywhere:

1. Write the new content to a temporary file in the *same* directory.
2. ``fsync`` it so the bytes are on disk.
3. ``os.replace`` it over the target - atomic on POSIX.
4. ``fsync`` the directory so the rename itself is durable.
5. Set restrictive permissions.

The directory holding credentials is created with mode ``0700`` and files
inside it with mode ``0600``.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

SECRET_DIR_MODE = 0o700
SECRET_FILE_MODE = 0o600


def ensure_private_dir(path: Path) -> Path:
    """Create *path* (and parents) with owner-only permissions."""
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, SECRET_DIR_MODE)
    except OSError:
        pass
    return path


def atomic_write_json(path: Path, payload: Any, mode: int = SECRET_FILE_MODE) -> None:
    """Write *payload* to *path* atomically with restrictive permissions."""
    ensure_private_dir(path.parent)
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    )
    tmp_path = Path(handle.name)
    try:
        with handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise
    try:
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def read_json(path: Path, default: Any = None) -> Any:
    """Read JSON from *path*, returning *default* on any failure."""
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return default
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return default


def remove_file(path: Path) -> bool:
    """Delete *path* if present. Returns whether anything was removed."""
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False
