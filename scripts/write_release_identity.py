#!/usr/bin/env python3
"""Write the release identity into the state volume, bound to this container.

Run **inside** the running gateway container (``docker compose exec`` /
``docker exec``), as the runtime user. It records the image identity
together with the id of the container it was written for, so the gateway
can refuse to report a record that belongs to an earlier container. The
file is replaced atomically, so a reader never sees a partial write.

The write is refused when this container's own id cannot be resolved: an
unbound record is exactly the stale-identity defect this replaces.

Usage:

    python /app/scripts/write_release_identity.py sha256:<64 hex>
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.environ.get("GATEWAY_SRC", "/app/src"))

from gateway.buildinfo import own_container_id

#: The only identity shape the gateway's release identity accepts.
_IDENTITY = re.compile(r"^sha256:[0-9a-f]{64}$")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write the release identity into the state volume, bound to this container")
    parser.add_argument("identity", help="the bare sha256:<64 hex> image identity")
    parser.add_argument(
        "--data-dir",
        default=os.environ.get("GATEWAY_DATA_DIR", "/var/lib/gateway"),
        help="the state directory the gateway reads release.json from",
    )
    args = parser.parse_args(argv)

    if not _IDENTITY.match(args.identity):
        print(
            f"refusing to write: {args.identity!r} is not a bare sha256:<64 hex> digest",
            file=sys.stderr,
        )
        return 1

    container_id = own_container_id()
    if container_id is None:
        print(
            "refusing to write: this container does not expose its own id, "
            "so the record could never be verified against the running container",
            file=sys.stderr,
        )
        return 1

    payload = json.dumps({"image_revision": args.identity, "container_id": container_id})
    data_dir = Path(args.data_dir)
    fd, tmp = tempfile.mkstemp(dir=data_dir, prefix=".release-", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(payload)
    os.chmod(tmp, 0o600)
    os.replace(tmp, data_dir / "release.json")
    print(data_dir / "release.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
