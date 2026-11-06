"""Regression tests for the release-identity container binding.

Production defect (2026-10-28/29, three consecutive cutovers): ``/version``
kept reporting the image digest an earlier release step had written into
``release.json`` on the persistent state volume, while the running container
was a different image each time. The gateway had no way to notice, because
the record was accepted regardless of which container it had been written
for.

The identity is now bound to the container that was running when the release
step wrote it, and the digest is reported only while that container is the
one running. These tests pin that fail-closed contract: a stale, unbound or
malformed record is reported as ``unknown``, never as a wrong digest.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from gateway import buildinfo
from gateway.buildinfo import own_container_id, release_identity

#: A mount table shaped like the one observed inside the production
#: container (cgroup namespace private; docker bind-mounts
#: /var/lib/docker/containers/<id>/{resolv.conf,hostname,hosts}).
PRODUCTION_MOUNTINFO = """\
673 448 0:53 / / ro,relatime - overlay overlay rw,lowerdir=/var/lib/docker/overlay2/l/UNRGJETDQBTHLHHQWH3S4XNTHG,upperdir=/var/lib/docker/overlay2/d2df8fe3b57caed239af4b7098029b919770b474f3d5343f72a9972279e99618/diff
691 673 8:1 /var/lib/docker/containers/{container}/resolv.conf /etc/resolv.conf ro,relatime - ext4 /dev/sda1 rw
692 673 8:1 /var/lib/docker/containers/{container}/hostname /etc/hostname ro,relatime - ext4 /dev/sda1 rw
694 673 8:1 /var/lib/docker/volumes/personal-llm-gateway-state/_data /var/lib/gateway rw,relatime master:1 - ext4 /dev/sda1 rw
"""

#: A mount table with no container mounts at all (bare process).
HOST_MOUNTINFO = """\
673 448 0:53 / / rw,relatime - overlay overlay rw
"""

CONTAINER_A = "a" * 64
CONTAINER_B = "b" * 64
DIGEST = "sha256:" + "c" * 64


class _FileTestCase(unittest.TestCase):
    """Writes a synthetic release record and mount table per test."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.release_path = self.dir / "release.json"
        self.mountinfo_path = self.dir / "mountinfo"

    def write_record(self, payload: object) -> None:
        self.release_path.write_text(json.dumps(payload), encoding="utf-8")

    def write_mountinfo(self, text: str) -> None:
        self.mountinfo_path.write_text(text, encoding="utf-8")

    def identity(self) -> dict[str, str]:
        return release_identity(
            release_path=self.release_path,
            mountinfo_path=self.mountinfo_path,
        )


class OwnContainerIdTests(_FileTestCase):
    def test_the_production_mount_shape_resolves_the_container_id(self) -> None:
        self.write_mountinfo(PRODUCTION_MOUNTINFO.format(container=CONTAINER_A))
        self.assertEqual(own_container_id(self.mountinfo_path), CONTAINER_A)

    def test_a_host_process_has_no_container_id(self) -> None:
        self.write_mountinfo(HOST_MOUNTINFO)
        self.assertIsNone(own_container_id(self.mountinfo_path))

    def test_ambiguity_is_not_guessed(self) -> None:
        text = PRODUCTION_MOUNTINFO.format(container=CONTAINER_A) + (
            f"695 673 8:1 /var/lib/docker/containers/{CONTAINER_B}/resolv.conf "
            "/etc/other.conf ro,relatime - ext4 /dev/sda1 rw\n"
        )
        self.write_mountinfo(text)
        self.assertIsNone(own_container_id(self.mountinfo_path))

    def test_a_short_hex_path_is_not_a_container_id(self) -> None:
        self.write_mountinfo(
            f"691 673 8:1 /var/lib/docker/containers/{'a' * 63}/resolv.conf "
            "/etc/resolv.conf ro,relatime - ext4 /dev/sda1 rw\n"
        )
        self.assertIsNone(own_container_id(self.mountinfo_path))

    def test_an_unreadable_mount_table_is_not_a_container_id(self) -> None:
        self.assertIsNone(own_container_id(self.dir / "missing"))


class BoundRecordTests(_FileTestCase):
    """A record written for THIS container is reported."""

    def setUp(self) -> None:
        super().setUp()
        self.write_mountinfo(PRODUCTION_MOUNTINFO.format(container=CONTAINER_A))

    def test_a_bound_record_is_reported(self) -> None:
        self.write_record({"image_revision": DIGEST, "container_id": CONTAINER_A})
        self.assertEqual(self.identity()["image_revision"], DIGEST)

    def test_the_container_id_is_case_insensitive(self) -> None:
        self.write_record({"image_revision": DIGEST, "container_id": CONTAINER_A.upper()})
        self.assertEqual(self.identity()["image_revision"], DIGEST)

    def test_source_fields_are_unaffected_by_the_binding(self) -> None:
        self.write_record(
            {
                "source_revision": "d" * 40,
                "source_tree": "e" * 40,
                "image_revision": DIGEST,
                "container_id": CONTAINER_A,
            }
        )
        identity = self.identity()
        self.assertEqual(identity["source_revision"], "d" * 40)
        self.assertEqual(identity["source_tree"], "e" * 40)
        self.assertEqual(identity["image_revision"], DIGEST)


class StaleAndUnboundRecordTests(_FileTestCase):
    """The production defect, pinned: a record from another container is
    never reported, no matter how valid the digest looks."""

    def setUp(self) -> None:
        super().setUp()
        self.write_mountinfo(PRODUCTION_MOUNTINFO.format(container=CONTAINER_B))

    def test_a_record_written_for_an_earlier_container_is_unknown(self) -> None:
        # Exactly the defect: the digest belongs to a previous cutover's
        # container while a new container is running.
        self.write_record({"image_revision": DIGEST, "container_id": CONTAINER_A})
        self.assertEqual(self.identity()["image_revision"], "unknown")

    def test_the_production_legacy_record_shape_is_unknown(self) -> None:
        # The exact on-disk shape observed in production on 2026-10-29:
        # a bare digest with no container binding.
        self.write_record({"image_revision": "sha256:" + "a" * 64})
        self.assertEqual(self.identity()["image_revision"], "unknown")

    def test_a_malformed_container_id_is_unknown(self) -> None:
        self.write_record({"image_revision": DIGEST, "container_id": "not-a-container"})
        self.assertEqual(self.identity()["image_revision"], "unknown")

    def test_a_non_digest_recorded_value_is_unknown_even_when_bound(self) -> None:
        self.write_record({"image_revision": "f" * 40, "container_id": CONTAINER_B})
        self.assertEqual(self.identity()["image_revision"], "unknown")

    def test_source_fields_survive_a_stale_image_record(self) -> None:
        self.write_record(
            {
                "source_revision": "d" * 40,
                "source_tree": "e" * 40,
                "image_revision": DIGEST,
                "container_id": CONTAINER_A,
            }
        )
        identity = self.identity()
        self.assertEqual(identity["source_revision"], "d" * 40)
        self.assertEqual(identity["source_tree"], "e" * 40)
        self.assertEqual(identity["image_revision"], "unknown")

    def test_an_unresolvable_own_container_never_trusts_the_record(self) -> None:
        # A container environment this code does not understand: fail
        # closed rather than report a digest that cannot be verified.
        self.write_mountinfo(HOST_MOUNTINFO)
        self.write_record({"image_revision": DIGEST, "container_id": CONTAINER_B})
        self.assertEqual(self.identity()["image_revision"], "unknown")


class NoRecordTests(_FileTestCase):
    def test_no_release_file_reports_unknown(self) -> None:
        self.write_mountinfo(PRODUCTION_MOUNTINFO.format(container=CONTAINER_A))
        identity = self.identity()
        self.assertEqual(identity["image_revision"], "unknown")
        self.assertEqual(identity["service"], "personal-llm-gateway")

    def test_a_corrupt_release_file_reports_unknown(self) -> None:
        self.write_mountinfo(PRODUCTION_MOUNTINFO.format(container=CONTAINER_A))
        self.release_path.write_text("{not json", encoding="utf-8")
        self.assertEqual(self.identity()["image_revision"], "unknown")

    def test_there_is_no_build_time_fallback_for_the_image_digest(self) -> None:
        # A digest baked into the image can never be the digest of the
        # image being built - it does not exist yet - so an environment
        # value must never be reported as image provenance.
        self.write_mountinfo(PRODUCTION_MOUNTINFO.format(container=CONTAINER_A))
        original = buildinfo._BUILD_SOURCE_REVISION  # the seam under test
        buildinfo._BUILD_SOURCE_REVISION = "d" * 40
        self.addCleanup(setattr, buildinfo, "_BUILD_SOURCE_REVISION", original)
        identity = self.identity()
        self.assertEqual(identity["source_revision"], "d" * 40)
        self.assertEqual(identity["image_revision"], "unknown")


class SchemaTests(_FileTestCase):
    def test_the_five_key_schema_is_unchanged(self) -> None:
        self.write_mountinfo(PRODUCTION_MOUNTINFO.format(container=CONTAINER_A))
        self.write_record({"image_revision": DIGEST, "container_id": CONTAINER_A})
        self.assertEqual(
            sorted(self.identity().keys()),
            ["image_revision", "mcp_contract_version", "service", "source_revision", "source_tree"],
        )


class WriterTests(_FileTestCase):
    """``scripts/write_release_identity.py`` binds what it writes, and
    refuses to write anything it could not bind."""

    @classmethod
    def setUpClass(cls) -> None:
        import importlib.util

        path = Path(__file__).resolve().parent.parent / "scripts" / "write_release_identity.py"
        spec = importlib.util.spec_from_file_location("write_release_identity_under_test", path)
        assert spec and spec.loader
        cls.writer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.writer)

    def setUp(self) -> None:
        super().setUp()
        self._original = self.writer.own_container_id
        self.writer.own_container_id = lambda: CONTAINER_A  # type: ignore[assignment]
        self.addCleanup(setattr, self.writer, "own_container_id", self._original)

    def _run(self, *argv: str) -> int:
        import contextlib
        import io

        with contextlib.redirect_stdout(io.StringIO()):
            return self.writer.main([*argv, "--data-dir", str(self.dir)])

    def test_a_bound_record_is_written(self) -> None:
        self.assertEqual(self._run(DIGEST), 0)
        record = json.loads(self.release_path.read_text(encoding="utf-8"))
        self.assertEqual(
            record, {"image_revision": DIGEST, "container_id": CONTAINER_A}
        )
        # ...and the gateway reports exactly that record.
        self.write_mountinfo(PRODUCTION_MOUNTINFO.format(container=CONTAINER_A))
        self.assertEqual(self.identity()["image_revision"], DIGEST)

    def test_the_record_is_mode_0600(self) -> None:
        self.assertEqual(self._run(DIGEST), 0)
        self.assertEqual(self.release_path.stat().st_mode & 0o777, 0o600)

    def test_an_unresolvable_container_id_refuses_the_write(self) -> None:
        self.writer.own_container_id = lambda: None  # type: ignore[assignment]
        self.assertEqual(self._run(DIGEST), 1)
        self.assertFalse(self.release_path.exists())

    def test_a_non_digest_identity_refuses_the_write(self) -> None:
        self.assertEqual(self._run("a" * 40), 1)
        self.assertEqual(self._run("sha256:" + "a" * 63), 1)
        self.assertEqual(self._run("personal-llm-gateway:candidate"), 1)
        self.assertFalse(self.release_path.exists())


if __name__ == "__main__":
    unittest.main()
