#!/usr/bin/env python3
"""Prepare the gateway state directory for the non-root runtime.

A Docker named volume is created root-owned the first time it is mounted,
so the gateway - which runs as UID/GID 10001 - cannot write its own
configuration or credential store. This fixes **one** directory and nothing
else.

The script is deliberately narrow, because it runs as root:

* it touches exactly one path, the gateway state directory;
* it creates that directory if it is missing, with mode 0700;
* it sets the directory's owner to the runtime UID/GID;
* it changes nothing else - no files, no other directories, no image
  layers, no host paths;
* it prints what it did.

It is idempotent, so it is safe to run on every start.

Usage:

    python3 scripts/init_state_dir.py [--path /var/lib/gateway] [--uid 10001] [--gid 10001]
"""

from __future__ import annotations

import argparse
import os
import stat
import sys
from pathlib import Path

DEFAULT_PATH = "/var/lib/gateway"
DEFAULT_UID = 10001
DEFAULT_GID = 10001


def init_state_dir(path: Path, uid: int, gid: int, apply: bool = True) -> dict[str, object]:
    """Create the state directory and set its ownership.

    Returns a description of what was done. With *apply* false nothing is
    changed, which is what the dry run and the test use.
    """
    created = not path.exists()
    if apply:
        path.mkdir(parents=True, exist_ok=True)
    elif created:
        # Nothing to own yet; report the intent.
        return {"path": str(path), "created": True, "applied": False, "mode": "0700", "owner": f"{uid}:{gid}"}

    if apply:
        try:
            os.chown(path, uid, gid)
        except PermissionError:
            raise SystemExit(
                f"cannot set the owner of {path} to {uid}:{gid}: this script must run as root. "
                "It is meant to run as the one-shot state-init service in compose.yaml, "
                "which is the only part of the deployment that runs as root."
            )
        os.chmod(path, stat.S_IRWXU)

    info = path.stat()
    return {
        "path": str(path),
        "created": created,
        "applied": apply,
        "mode": oct(stat.S_IMODE(info.st_mode)),
        "owner": f"{info.st_uid}:{info.st_gid}",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--path", default=os.environ.get("GATEWAY_DATA_DIR", DEFAULT_PATH))
    parser.add_argument("--uid", type=int, default=int(os.environ.get("GATEWAY_UID", DEFAULT_UID)))
    parser.add_argument("--gid", type=int, default=int(os.environ.get("GATEWAY_GID", DEFAULT_GID)))
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would be done without changing anything",
    )
    args = parser.parse_args(argv)

    result = init_state_dir(
        Path(args.path), args.uid, args.gid, apply=not args.dry_run
    )
    if args.dry_run:
        print("would prepare " + str(result))
    else:
        print("prepared " + str(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
