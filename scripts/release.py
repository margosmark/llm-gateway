#!/usr/bin/env python3
"""Release an inspected candidate onto the Compose deployment.

This is the cutover step. It assumes the candidate has **already** been
built and gated, and it never rebuilds or retags the artifact: the image
that passed the gate is the image that is deployed.

What it does:

1. Resolves the immutable identity of the candidate - the registry digest
   if it has one, otherwise the image ID. That value is what identifies
   the release.
2. Starts the Compose deployment, whose one-shot ``state-init`` service
   prepares the state volume for the non-root runtime.
3. Writes that identity into the state volume, atomically, so ``/version``
   reports the artifact that is actually running.
4. Verifies from outside the container that ``/version`` reports exactly
   that identity, that the health endpoint answers, and that the process
   listens only on the configured address.

Every step is checked. A mismatch stops the release rather than leaving a
deployment that cannot say what it is.

Usage:

    python3 scripts/release.py --image personal-llm-gateway:candidate
    python3 scripts/release.py --image personal-llm-gateway:candidate --verify-only
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

GATE = Path(__file__).resolve().parent / "release_gate.py"


def run(args: list[str], timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


#: The Compose project the release targets. Overridden by --project.
PROJECT = ""


def compose(args: list[str], timeout: int = 300) -> subprocess.CompletedProcess:
    prefix = ["docker", "compose"]
    if PROJECT:
        prefix += ["-p", PROJECT]
    return run([*prefix, *args], timeout=timeout)


def image_identity(reference: str) -> str | None:
    """The immutable identity of an image: its registry digest, else its ID."""
    result = run(["docker", "image", "inspect", reference, "--format", "{{index .RepoDigests 0}}"])
    digest = result.stdout.strip() if result.returncode == 0 else ""
    if digest and digest.startswith("sha256:"):
        return digest
    result = run(["docker", "image", "inspect", reference, "--format", "{{.Id}}"])
    identity = result.stdout.strip() if result.returncode == 0 else ""
    if identity.startswith("sha256:"):
        return identity
    return None


def http(url: str, timeout: float = 10.0) -> tuple[int, object]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            body = response.read()
            try:
                return response.status, json.loads(body.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return response.status, body.decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        body = exc.read()
        try:
            return exc.code, json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return exc.code, body.decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as exc:
        return 0, str(exc)


def write_identity(identity: str) -> tuple[bool, str]:
    """Write the release identity into the state volume, atomically.

    The write happens inside the running container, as the runtime user, so
    it uses the same permissions the gateway itself has. The file is
    replaced atomically, so a reader never sees a partial write.
    """
    payload = json.dumps({"image_revision": identity})
    code = (
        "import json,os,tempfile;"
        "d=os.environ['GATEWAY_DATA_DIR'];"
        "fd,tmp=tempfile.mkstemp(dir=d,prefix='.release-',suffix='.tmp');"
        "os.write(fd,(" + json.dumps(payload) + ").encode());"
        "os.close(fd);"
        "os.chmod(tmp,0o600);"
        "os.replace(tmp, os.path.join(d,'release.json'));"
        "print(os.path.join(d,'release.json'))"
    )
    result = compose(["exec", "-T", "gateway", "python", "-c", code])
    if result.returncode != 0:
        return False, (result.stderr or result.stdout).strip()[:200]
    return True, result.stdout.strip()


def build_candidate(image: str) -> subprocess.CompletedProcess:
    """Build the candidate, stamping the source identity into the image.

    The revision and the tree come from the checkout, so the image can say
    what it was built from. Without them the release identity is "unknown",
    which is true but useless.
    """
    repo = Path(__file__).resolve().parent.parent
    revision = run(["git", "rev-parse", "HEAD"], timeout=60).stdout.strip()
    tree = run(["git", "rev-parse", "HEAD^{tree}"], timeout=60).stdout.strip()
    if not revision or not tree:
        print("could not read the git revision; build the image manually", file=sys.stderr)
        return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="")
    print(f"building {image} from {revision[:12]}")
    return run(
        [
            "docker",
            "build",
            "--build-arg",
            f"SOURCE_REVISION={revision}",
            "--build-arg",
            f"SOURCE_TREE={tree}",
            "-t",
            image,
            ".",
        ],
        timeout=1800,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--image", default="personal-llm-gateway:candidate")
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="verify the running deployment without starting or changing it",
    )
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument(
        "--project",
        default=os.environ.get("COMPOSE_PROJECT_NAME", ""),
        help="the Compose project to release onto",
    )
    parser.add_argument(
        "--build",
        action="store_true",
        help=(
            "build the candidate from the current checkout first, passing the "
            "source revision and tree so the release identity is truthful"
        ),
    )
    args = parser.parse_args(argv)
    global PROJECT
    PROJECT = args.project

    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        marker = "PASS" if ok else "FAIL"
        print(f"[{marker}] {name}" + (f" - {detail}" if detail else ""), flush=True)
        if not ok:
            failures.append(name)

    if args.build:
        built = build_candidate(args.image)
        if built.returncode != 0:
            print((built.stderr or built.stdout).strip()[:600], file=sys.stderr)
            return 1

    identity = image_identity(args.image)
    if identity is None:
        print(f"the image '{args.image}' does not exist; build it and run the release gate first", file=sys.stderr)
        return 1
    print(f"candidate identity: {identity}")

    if not args.verify_only:
        # Deploy exactly the image that was inspected, without rebuilding.
        os.environ["GATEWAY_IMAGE"] = args.image
        started = compose(["up", "-d", "--no-build"])
        if started.returncode != 0:
            print((started.stderr or started.stdout).strip()[:400], file=sys.stderr)
            return 1
        print("deployment started")

        wrote, detail = write_identity(identity)
        check("the release identity was written to the state volume", wrote, detail)

    # --- verify from outside the container -------------------------------
    host = os.environ.get("GATEWAY_BIND", "10.10.11.109")
    port = os.environ.get("GATEWAY_PORT", "8765")
    base = f"http://{host}:{port}"

    healthy = False
    for _ in range(args.timeout):
        status, _ = http(base + "/health", timeout=3)
        if status == 200:
            healthy = True
            break
        time.sleep(1)
    check("the deployment answers /health", healthy, base)

    status, payload = http(base + "/version")
    keys = sorted(payload.keys()) if isinstance(payload, dict) else []
    check(
        "/version returns the fixed four-key schema",
        status == 200 and keys == ["image_revision", "service", "source_revision", "source_tree"],
        f"status={status} keys={keys}",
    )
    reported = payload.get("image_revision") if isinstance(payload, dict) else None
    check(
        "/version reports the exact deployed image identity",
        reported == identity,
        f"reported={reported} expected={identity}",
    )

    container = compose(["ps", "-q", "gateway"]).stdout.strip()
    running = run(["docker", "inspect", container, "--format", "{{.Image}}"]) if container else None
    check(
        "the running container is the inspected image",
        bool(running) and running.returncode == 0 and running.stdout.strip() == identity,
        f"container={running.stdout.strip() if running else 'not found'}",
    )

    listeners = compose(
        ["exec", "-T", "gateway", "python", "-c",
         "import json,sys;sys.path.insert(0,'/app/scripts');import release_gate;"
         "print(json.dumps(release_gate.gateway_listening_sockets()))"]
    )
    bound = []
    try:
        data = json.loads(listeners.stdout.strip())
        bound = [(e["address"], e["port"]) for e in data if isinstance(e, dict)]
    except (ValueError, TypeError):
        pass
    wildcard = [entry for entry in bound if entry[0] in ("0.0.0.0", "::")]
    check("the gateway does not listen on a wildcard address", not wildcard, str(bound))
    check(
        "the gateway listens only on the configured address",
        all(entry[0] == host for entry in bound) if bound else False,
        f"listening={bound} expected={host}",
    )

    if failures:
        print("\nRELEASE FAILED: " + ", ".join(failures), file=sys.stderr)
        return 1
    print("\nRELEASE COMPLETE")
    print(f"  image:    {identity}")
    print(f"  console: {base}/owner/")
    print(f"  api:     {base}/v1/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
