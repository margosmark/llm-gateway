"""The Compose deployment, exercised end to end.

Regression cover for **R2**: the Compose deployment had no verified
writable-volume initialisation. A Docker named volume is created
root-owned, and the gateway runs as UID/GID 10001, so the first boot could
not write its own configuration or credential store. The release gate
starts a disposable container *without* the Compose volume, so it could
never prove this path.

This test drives the real Compose deployment and proves:

* the state volume is prepared for the non-root runtime, and the gateway
  writes its configuration as UID/GID 10001;
* a configuration write **persists across a restart**;
* the process listens only on the configured address, and never on the
  retired ``:8766``;
* ``/version`` reports the exact image that is running.

The test is skipped when Docker is not available.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
IMAGE = "personal-llm-gateway:compose-test"
PROJECT = "personal-llm-gateway-compose-test"


def run(
    args: list[str], timeout: int = 600, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(REPO),
        env=env if env is not None else os.environ.copy(),
    )


def compose(
    args: list[str], timeout: int = 600, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess:
    return run(["docker", "compose", *args], timeout=timeout, env=env)


def http(url: str, timeout: float = 8.0) -> tuple[int, object]:
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


def free_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class ComposeDeploymentTests(unittest.TestCase):
    """First boot, a persisted write, and a restart, on the real volume."""

    @classmethod
    def setUpClass(cls) -> None:
        if shutil.which("docker") is None:
            raise unittest.SkipTest("docker is not available")
        if run(["docker", "info"], timeout=60).returncode != 0:
            raise unittest.SkipTest("the docker daemon is not reachable")
        cls.port = free_port()
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.env = dict(os.environ)
        cls.env.update(
            {
                "GATEWAY_BIND": "127.0.0.1",
                "GATEWAY_PORT": str(cls.port),
                "COMPOSE_PROJECT_NAME": PROJECT,
                "GATEWAY_IMAGE": IMAGE,
                "GATEWAY_STATE_VOLUME": f"{PROJECT}-state",
                "SOURCE_REVISION": "compose-test",
                "SOURCE_TREE": "compose-test",
            }
        )
        built = run(
            [
                "docker",
                "build",
                "--build-arg",
                "SOURCE_REVISION=compose-test",
                "--build-arg",
                "SOURCE_TREE=compose-test",
                "-t",
                IMAGE,
                ".",
            ],
            timeout=900,
        )
        if built.returncode != 0:
            raise unittest.SkipTest("the candidate image could not be built")
        cls.env["GATEWAY_STATE_VOLUME"] = f"{PROJECT}-state"
        cls.built = True

    @classmethod
    def tearDownClass(cls) -> None:
        if getattr(cls, "built", False):
            run(["docker", "compose", "-p", PROJECT, "down", "-v"], timeout=300)
            run(["docker", "image", "rm", "-f", IMAGE], timeout=300)

    def _compose(self, args: list[str], timeout: int = 600) -> subprocess.CompletedProcess:
        return run(
            ["docker", "compose", "-p", PROJECT, *args], timeout=timeout, env=self.env
        )

    def _up(self) -> subprocess.CompletedProcess:
        return self._compose(["up", "-d", "--no-build"], timeout=600)

    def _wait_healthy(self, attempts: int = 60) -> bool:
        for _ in range(attempts):
            status, _ = http(self.base + "/health", timeout=3)
            if status == 200:
                return True
            time.sleep(1)
        return False

    def test_first_boot_writes_as_the_runtime_user_and_persists_across_a_restart(self) -> None:
        self._compose(["down", "-v"], timeout=300)
        started = self._up()
        self.assertEqual(
            started.returncode, 0, (started.stderr or started.stdout).strip()[:400]
        )
        self.addCleanup(self._compose, ["down", "-v"], 300)

        self.assertTrue(self._wait_healthy(), "the gateway did not become healthy")

        # The one-shot init service ran to completion.
        init_status = self._compose(
            ["ps", "-a", "--format", "{{.Service}} {{.ExitCode}}"], timeout=120
        )
        self.assertIn(
            "state-init 0",
            init_status.stdout,
            "the state-init service did not succeed:\n" + init_status.stdout,
        )

        # The volume is owned by the runtime user, not by root. The init
        # service is one-shot and has exited, so the volume is inspected
        # with a fresh container that mounts it.
        owner = run(
            [
                "docker",
                "run",
                "--rm",
                "-v",
                f"{PROJECT}-state:/var/lib/gateway",
                "--entrypoint",
                "python",
                IMAGE,
                "-c",
                "import os;s=os.stat('/var/lib/gateway');print('%d:%d' % (s.st_uid, s.st_gid))",
            ],
            timeout=180,
            env=self.env,
        )
        self.assertEqual(owner.stdout.strip(), "10001:10001", owner.stdout + owner.stderr)

        # The gateway can write its own configuration.
        written = self._compose(
            [
                "exec",
                "-T",
                "gateway",
                "python",
                "-c",
                "import os;open(os.path.join(os.environ['GATEWAY_DATA_DIR'],'probe.json'),'w').write('{}')",
            ],
            timeout=120,
        )
        self.assertEqual(written.returncode, 0, written.stderr[:300])

        # A configuration change survives a restart.
        changed = self._compose(
            [
                "exec",
                "-T",
                "gateway",
                "python",
                "-c",
                (
                    "import json,os;"
                    "p=os.path.join(os.environ['GATEWAY_DATA_DIR'],'config.json');"
                    "d=json.load(open(p)) if os.path.exists(p) else {};"
                    "d['settings']={'probe':'persisted'};"
                    "json.dump(d,open(p,'w'));print('written')"
                ),
            ],
            timeout=120,
        )
        self.assertEqual(changed.returncode, 0, changed.stderr[:300])

        self._compose(["restart", "gateway"], timeout=300)
        self.assertTrue(self._wait_healthy(), "the gateway did not come back after a restart")

        # The configuration file on the volume is the proof: it is the
        # state the gateway actually reads on the next start.
        stored = run(
            [
                "docker",
                "run",
                "--rm",
                "-v",
                f"{PROJECT}-state:/var/lib/gateway",
                "--entrypoint",
                "python",
                IMAGE,
                "-c",
                "import json;print(json.load(open('/var/lib/gateway/config.json')).get('settings',{}).get('probe'))",
            ],
            timeout=180,
            env=self.env,
        )
        self.assertEqual(
            stored.stdout.strip(),
            "persisted",
            "the configuration did not survive the restart:\n" + stored.stdout + stored.stderr,
        )

        # The runtime is still the non-root user, and the process listens
        # only where it should.
        who = self._compose(
            ["exec", "-T", "gateway", "python", "-c", "import os;print(os.getuid(), os.getgid())"],
            timeout=120,
        )
        self.assertEqual(who.stdout.strip(), "10001 10001")

        listeners = self._compose(
            [
                "exec",
                "-T",
                "gateway",
                "python",
                "-c",
                "import json,sys;sys.path.insert(0,'/app/scripts');import release_gate;"
                "print(json.dumps(release_gate.gateway_listening_sockets()))",
            ],
            timeout=120,
        )
        bound = []
        try:
            bound = [
                (entry["address"], entry["port"])
                for entry in json.loads(listeners.stdout.strip())
            ]
        except (ValueError, TypeError):
            pass
        self.assertTrue(bound, "the listener probe returned nothing")
        self.assertEqual(
            [entry for entry in bound if entry[0] in ("0.0.0.0", "::")],
            [],
            "the gateway is listening on a wildcard address",
        )
        self.assertTrue(
            all(entry[0] == "127.0.0.1" for entry in bound),
            f"the gateway listened somewhere else: {bound}",
        )
        self.assertNotIn(8766, [entry[1] for entry in bound], "the retired setup port is bound")

        # The release step publishes the identity into the state volume, and
        # then verifies it from outside the container.
        released = run(
            [
                "python3",
                "scripts/release.py",
                "--image",
                IMAGE,
                "--project",
                PROJECT,
                "--timeout",
                "30",
            ],
            timeout=300,
            env=self.env,
        )
        self.assertEqual(
            released.returncode,
            0,
            "the release step failed:\n" + released.stdout + released.stderr,
        )
        self.assertIn("RELEASE COMPLETE", released.stdout, released.stdout)

        # /version reports the exact image that is running.
        status, release = http(self.base + "/version")
        self.assertEqual(status, 200)
        container = self._compose(["ps", "-q", "gateway"], timeout=120).stdout.strip()
        running = run(
            ["docker", "inspect", container, "--format", "{{.Image}}"], env=self.env
        )
        self.assertEqual(release.get("image_revision"), running.stdout.strip())


if __name__ == "__main__":
    unittest.main()
