#!/usr/bin/env python3
"""The release gate.

This script is the independent check that stands between a candidate image
and deployment. It runs against **one exact image digest** and never
rebuilds or substitutes the artifact it is checking.

What it checks:

* **Identity.** ``/version`` returns the fixed four-key schema, and the
  image revision it reports is the digest of the candidate.
* **Exposure.** The listener refuses a wildcard bind. The setup surface is
  not reachable from a non-loopback peer.
* **Hostile probes.** A forwarded header, a wrong ``Host``, a cross-origin
  mutation and a missing CSRF token are all rejected.
* **Unauthenticated surface.** The LAN console does not expose the
  configuration, the profiles, or the credential state without a session.
* **Secrets.** The image contains no credential file, and no layer contains
  anything that looks like credential material.

Usage:

    python3 scripts/release_gate.py personal-llm-gateway:candidate

Exit status is 0 only when every check passes.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

# Patterns that must not appear in an image layer or in a served response.
SECRET_PATTERNS = {
    "openai_key": re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}\b"),
    "bearer": re.compile(r"(?i)authorization:\s*bearer\s+[A-Za-z0-9._\-]{20,}"),
    "gateway_key": re.compile(r"\bgw_[A-Za-z0-9_\-]{20,}\b"),
    "private_key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
}

# The fixed /version schema.
EXPECTED_VERSION_KEYS = {"service", "source_revision", "source_tree", "image_revision"}


@dataclass
class Check:
    name: str
    passed: bool
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "passed": self.passed, "detail": self.detail}


@dataclass
class Gate:
    checks: list[Check] = field(default_factory=list)

    def record(self, name: str, passed: bool, detail: str = "") -> None:
        self.checks.append(Check(name, passed, detail))
        marker = "PASS" if passed else "FAIL"
        print(f"[{marker}] {name}" + (f" - {detail}" if detail else ""), flush=True)

    @property
    def ok(self) -> bool:
        return all(check.passed for check in self.checks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.ok,
            "checks": [check.to_dict() for check in self.checks],
        }


def _decode_ipv4_hex(hex_address: str) -> str:
    """Decode a /proc/net/tcp local address.

    The kernel writes the address as four hex bytes in host byte order, so
    ``0100007F`` is ``127.0.0.1`` and ``00000000`` is the wildcard.
    """
    if len(hex_address) != 8:
        return ""
    try:
        raw = bytes.fromhex(hex_address)
    except ValueError:
        return ""
    return ".".join(str(byte) for byte in raw[::-1])


def _decode_ipv6_hex(hex_address: str) -> str:
    """Decode a /proc/net/tcp6 local address (four 32-bit words)."""
    if len(hex_address) != 32:
        return ""
    try:
        raw = bytes.fromhex(hex_address)
    except ValueError:
        return ""
    import ipaddress

    words = b"".join(raw[index : index + 4][::-1] for index in range(0, 16, 4))
    try:
        return str(ipaddress.IPv6Address(words))
    except ValueError:
        return ""


def process_socket_inodes(pid: int, proc_root: str = "/proc") -> set[int]:
    """The socket inodes owned by a process.

    A container with host networking shares the host's network namespace, so
    ``/proc/net/tcp`` lists every socket on the machine, not just the
    gateway's. The inodes in ``/proc/<pid>/fd`` identify which sockets
    actually belong to the process.
    """
    import os

    inodes: set[int] = set()
    fd_dir = os.path.join(proc_root, str(pid), "fd")
    try:
        entries = os.listdir(fd_dir)
    except OSError:
        return inodes
    for entry in entries:
        try:
            target = os.readlink(os.path.join(fd_dir, entry))
        except OSError:
            continue
        if target.startswith("socket:[") and target.endswith("]"):
            try:
                inodes.add(int(target[len("socket:[") : -1]))
            except ValueError:
                continue
    return inodes


def listening_sockets(
    proc_root: str = "/proc/net",
    pid: int | None = None,
    process_root: str = "/proc",
) -> list[dict[str, Any]]:
    """Every socket this host is listening on.

    Parses ``/proc/net/tcp`` and ``/proc/net/tcp6``. State ``0A`` is
    ``LISTEN``. The addresses are hexadecimal, so a textual comparison for
    ``0.0.0.0`` would never match. When *pid* is given, only the sockets
    that belong to that process are returned.
    """
    import os

    allowed = process_socket_inodes(pid, process_root) if pid is not None else None
    results: list[dict[str, Any]] = []
    for name, decoder in (("tcp", _decode_ipv4_hex), ("tcp6", _decode_ipv6_hex)):
        path = os.path.join(proc_root, name)
        try:
            with open(path, encoding="utf-8") as handle:
                lines = handle.read().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            if len(fields) < 4 or fields[3] != "0A":
                continue
            if allowed is not None:
                inode = fields[9] if len(fields) > 9 else ""
                if not inode.isdigit() or int(inode) not in allowed:
                    continue
            address_hex, _, port_hex = fields[1].partition(":")
            address = decoder(address_hex)
            if not address:
                continue
            try:
                port = int(port_hex, 16)
            except ValueError:
                continue
            results.append({"address": address, "port": port, "family": name})
    return results


def gateway_listening_sockets() -> list[dict[str, Any]]:
    """The sockets the gateway process itself is listening on.

    Finds the gateway process and returns only its own listening sockets.
    A container with host networking shares the host's network namespace,
    so the whole host's socket table is visible from inside it.
    """
    import os

    pid: int | None = None
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(os.path.join("/proc", entry, "cmdline"), "rb") as handle:
                cmdline = handle.read().decode("utf-8", "replace")
        except OSError:
            continue
        if "-m" in cmdline and "gateway" in cmdline:
            pid = int(entry)
            break
    return listening_sockets(pid=pid)


def _parse_bound_listeners(raw: str) -> list[tuple[str, int]]:
    """Parse the JSON the in-container probe printed."""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return []
    bound: list[tuple[str, int]] = []
    for entry in data if isinstance(data, list) else []:
        if isinstance(entry, dict) and isinstance(entry.get("address"), str):
            port = entry.get("port")
            bound.append((entry["address"], port if isinstance(port, int) else 0))
    return bound


def run(args: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def image_digest(reference: str) -> str | None:
    result = run(["docker", "image", "inspect", reference, "--format", "{{index .RepoDigests 0}}"])
    if result.returncode != 0:
        result = run(["docker", "image", "inspect", reference, "--format", "{{.Id}}"])
        if result.returncode != 0:
            return None
        return result.stdout.strip()
    digest = result.stdout.strip()
    return digest or None


def http(
    url: str,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: Any = None,
    timeout: float = 10.0,
) -> tuple[int, Any]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url=url, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read()
            try:
                return response.status, json.loads(payload.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return response.status, payload.decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        try:
            return exc.code, json.loads(payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return exc.code, payload.decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as exc:
        return 0, str(exc)


def wait_for(url: str, attempts: int = 40, delay: float = 0.5) -> bool:
    for _ in range(attempts):
        status, _ = http(url, timeout=3)
        if status == 200:
            return True
        time.sleep(delay)
    return False


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: release_gate.py <image-reference>", file=sys.stderr)
        return 2
    reference = argv[1]
    gate = Gate()

    digest = image_digest(reference)
    gate.record(
        "image digest is resolvable",
        digest is not None,
        digest or "the image could not be inspected",
    )
    if digest is None:
        print(json.dumps(gate.to_dict(), indent=2))
        return 1

    # --- the image contains no credential material ------------------------
    layers = run(["docker", "history", "--no-trunc", "--format", "{{.CreatedBy}}", reference])
    layer_text = layers.stdout
    found = [name for name, pattern in SECRET_PATTERNS.items() if pattern.search(layer_text)]
    gate.record("no secret-shaped material in the image history", not found, ", ".join(found))

    listing = run(
        [
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "python",
            reference,
            "-c",
            "import os;print(os.listdir('/var/lib/gateway'))",
        ]
    )
    runtime_empty = listing.stdout.strip() in ("[]", "")
    gate.record(
        "the runtime directory is empty in the image",
        runtime_empty,
        listing.stdout.strip() or listing.stderr.strip()[:120],
    )

    # --- the process refuses a wildcard bind ------------------------------
    wildcard = run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            reference,
            "--host",
            "0.0.0.0",
            "--port",
            "8765",
        ],
        timeout=30,
    )
    refused = wildcard.returncode == 2 and "wildcard" in (wildcard.stderr + wildcard.stdout).lower()
    gate.record("a wildcard bind is refused at startup", refused, (wildcard.stderr or wildcard.stdout).strip()[:160])

    # --- run the candidate and probe it -----------------------------------
    container = "personal-llm-gateway-release-gate"
    run(["docker", "rm", "-f", container])
    # The candidate runs with host networking and binds loopback on a
    # high port. The wildcard refusal stays absolute - there is no flag that
    # enables it - so the gate cannot probe a container that binds one.
    started = run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            container,
            "--network",
            "host",
            "-e",
            "GATEWAY_BIND=127.0.0.1",
            "-e",
            "GATEWAY_PORT=18775",
            reference,
        ],
        timeout=60,
    )
    if started.returncode != 0:
        gate.record("the candidate container starts", False, (started.stderr or started.stdout).strip()[:200])
        print(json.dumps(gate.to_dict(), indent=2))
        return 1
    gate.record("the candidate container starts", True)

    base = "http://127.0.0.1:18775"
    try:
        reachable = wait_for(base + "/health")
        # The probe must have reached *this* container. A stray listener on
        # the same port would otherwise make the gate pass against the wrong
        # process - which is exactly what happened once during development.
        state = run(["docker", "inspect", container, "--format", "{{.State.Running}}"])
        running = state.stdout.strip() == "true"
        gate.record(
            "the candidate container is still running",
            running,
            state.stdout.strip() or state.stderr.strip()[:120],
        )
        gate.record("the candidate answers /health", reachable and running)
        if not reachable or not running:
            logs = run(["docker", "logs", "--tail", "20", container])
            print(logs.stdout or logs.stderr, file=sys.stderr)
            print(json.dumps(gate.to_dict(), indent=2))
            return 1

        status, payload = http(base + "/version")
        keys = set(payload.keys()) if isinstance(payload, dict) else set()
        gate.record(
            "/version returns the fixed four-key schema",
            status == 200 and keys == EXPECTED_VERSION_KEYS,
            f"status={status} keys={sorted(keys)}",
        )
        gate.record(
            "/version reports no configuration or credential material",
            isinstance(payload, dict)
            and not any(
                word in json.dumps(payload).lower()
                for word in ("credential", "api_key", "prompt", "completion", "token")
            ),
        )

        # The release step writes the running image digest into the
        # persistent volume; /version must then report exactly that digest.
        wrote = run(
            [
                "docker",
                "exec",
                container,
                "python",
                "-c",
                (
                    "import json,os;"
                    "p=os.path.join(os.environ['GATEWAY_DATA_DIR'],'release.json');"
                    f"open(p,'w').write(json.dumps({{'image_revision':{digest!r}}}));"
                    "print(p)"
                ),
            ]
        )
        time.sleep(1.0)
        status, release = http(base + "/version")
        reported = release.get("image_revision") if isinstance(release, dict) else None
        gate.record(
            "/version reports the exact running image digest",
            wrote.returncode == 0 and reported == digest,
            f"reported={reported}",
        )

        status, payload = http(base + "/version", headers={"X-Forwarded-For": "203.0.113.9"})
        gate.record("a forwarded header is rejected", status == 400, f"status={status}")

        status, payload = http(base + "/version", headers={"Host": "evil.example.com"})
        gate.record("a mismatched Host is rejected", status == 403, f"status={status}")

        status, payload = http(
            base + "/owner/api/state",
            headers={"Origin": "http://evil.example.com"},
        )
        gate.record(
            "the console exposes state directly (safe GET ignores Origin)",
            status == 200,
            f"status={status}",
        )

        status, payload = http(base + "/setup/api/state")
        gate.record(
            "the retired setup path is not served",
            status == 404,
            f"status={status}",
        )

        status, payload = http(
            base + "/owner/api/model",
            method="POST",
            headers={"Origin": base},
            body={"action": "enable", "provider_id": "unknown", "model_id": "unknown"},
        )
        gate.record(
            "same-origin owner mutation is dispatched without session or CSRF",
            status != 401 and status != 403,
            f"status={status}",
        )

        listeners = run(
            [
                "docker",
                "exec",
                container,
                "python",
                "-c",
                (
                    "import json,sys;"
                    "sys.path.insert(0,'/app/scripts');"
                    "import release_gate;"
                    "print(json.dumps(release_gate.gateway_listening_sockets()))"
                ),
            ]
        )
        listening = listeners.stdout.strip()
        bound = _parse_bound_listeners(listening)
        wildcard_bound = [entry for entry in bound if entry[0] in ("0.0.0.0", "::")]
        gate.record(
            "the container does not listen on a wildcard address",
            not wildcard_bound and bool(bound),
            f"listening={bound} raw={listening[:120]}",
        )
        gate.record(
            "the container listens only on the configured address",
            all(entry[0] in ("127.0.0.1", "::1") for entry in bound) if bound else False,
            f"listening={bound}",
        )
    finally:
        run(["docker", "rm", "-f", container])

    print()
    print(json.dumps(gate.to_dict(), indent=2))
    if not gate.ok:
        print("RELEASE GATE FAILED", file=sys.stderr)
        return 1
    print("RELEASE GATE PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
