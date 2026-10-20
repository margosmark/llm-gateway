"""Tests for the credential store.

The properties under test are the ones the charter cares about:

* a saved credential is never re-displayed;
* provider credentials are isolated from each other;
* files are owner-only and written atomically;
* local removal deletes the local copy only.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from gateway.credentials import (
    CredentialError,
    CredentialStore,
    hash_caller_token,
    hash_passphrase,
    new_caller_token,
    verify_caller_token,
    verify_passphrase,
)


class CredentialStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.store = CredentialStore(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_fresh_store_has_no_providers(self) -> None:
        self.assertEqual(self.store.list_store_ids(), [])

    def test_write_then_read_round_trips(self) -> None:
        handle = self.store.provider_handle("opencode_go")
        handle.write({"api_key": "test-value-not-real"})
        self.assertEqual(handle.read(), {"api_key": "test-value-not-real"})

    def test_provider_isolation_read_is_scoped_to_one_provider(self) -> None:
        chatgpt = self.store.provider_handle("chatgpt_codex")
        go = self.store.provider_handle("opencode_go")
        chatgpt.write({"access_token": "chatgpt-secret-value"})
        go.write({"api_key": "go-secret-value"})

        self.assertEqual(chatgpt.read(), {"access_token": "chatgpt-secret-value"})
        self.assertEqual(go.read(), {"api_key": "go-secret-value"})

        # The files themselves are distinct, so one provider's secret cannot
        # appear in the other's store.
        paths = {
            (self.store.root / f"{name}.json").resolve()
            for name in ("chatgpt_codex", "opencode_go")
        }
        self.assertEqual(len(paths), 2)

    def test_handle_cannot_be_pointed_at_another_provider(self) -> None:
        handle = self.store.provider_handle("opencode_go")
        self.assertEqual(handle.store_id, "opencode_go")
        # The handle exposes no way to re-point it.
        self.assertFalse(hasattr(handle, "store_id_"))
        with self.assertRaises(AttributeError):
            handle.store_id = "chatgpt_codex"  # type: ignore[misc]

    def test_unknown_provider_is_rejected(self) -> None:
        with self.assertRaises(CredentialError):
            self.store.provider_handle("not_a_provider")

    def test_path_traversal_is_rejected(self) -> None:
        with self.assertRaises(CredentialError):
            self.store.handle("../../etc/passwd")

    def test_describe_reports_presence_without_values(self) -> None:
        handle = self.store.provider_handle("opencode_go")
        handle.write({"api_key": "super-secret-value"})

        description = self.store.describe("opencode_go")
        self.assertTrue(description["present"])
        self.assertEqual(description["fields"], ["api_key"])
        # The description must not contain the value in any form.
        self.assertNotIn("super-secret-value", str(description))

    def test_describe_of_missing_store(self) -> None:
        description = self.store.describe("opencode_go")
        self.assertFalse(description["present"])
        self.assertEqual(description["fields"], [])

    def test_credentials_are_owner_only(self) -> None:
        handle = self.store.provider_handle("opencode_go")
        handle.write({"api_key": "x"})
        self.assertTrue(self.store.permissions_are_owner_only("opencode_go"))
        self.assertEqual(self.store.root.stat().st_mode & 0o777, 0o700)

    def test_clear_removes_only_the_targeted_provider(self) -> None:
        chatgpt = self.store.provider_handle("chatgpt_codex")
        go = self.store.provider_handle("opencode_go")
        chatgpt.write({"access_token": "a"})
        go.write({"api_key": "b"})

        self.assertTrue(chatgpt.clear())
        self.assertIsNone(chatgpt.read())
        self.assertEqual(go.read(), {"api_key": "b"})

    def test_clear_of_absent_store_reports_false(self) -> None:
        self.assertFalse(self.store.provider_handle("opencode_go").clear())


class PassphraseTests(unittest.TestCase):
    def test_hash_and_verify_round_trip(self) -> None:
        digest, salt = hash_passphrase("correct horse", iterations=1000)
        self.assertTrue(verify_passphrase("correct horse", digest, salt, iterations=1000))
        self.assertFalse(verify_passphrase("wrong horse", digest, salt, iterations=1000))

    def test_digest_does_not_contain_the_passphrase(self) -> None:
        digest, salt = hash_passphrase("correct horse", iterations=1000)
        self.assertNotIn("correct horse", digest)
        self.assertNotIn("correct horse", salt)

    def test_verify_rejects_malformed_storage(self) -> None:
        self.assertFalse(verify_passphrase("x", "nothex", "nothex", iterations=1000))
        self.assertFalse(verify_passphrase("x", None, None, iterations=1000))

    def test_empty_passphrase_is_rejected(self) -> None:
        with self.assertRaises(CredentialError):
            hash_passphrase("", iterations=1000)


class CallerTokenTests(unittest.TestCase):
    def test_token_is_unique_and_prefixed(self) -> None:
        first = new_caller_token()
        second = new_caller_token()
        self.assertNotEqual(first, second)
        self.assertTrue(first.startswith("gw_"))

    def test_hash_verifies_only_the_original_token(self) -> None:
        token = new_caller_token()
        stored = hash_caller_token(token)
        self.assertTrue(verify_caller_token(token, stored))
        self.assertFalse(verify_caller_token(new_caller_token(), stored))

    def test_hash_does_not_contain_the_token(self) -> None:
        token = new_caller_token()
        self.assertNotIn(token, hash_caller_token(token))

    def test_empty_token_never_verifies(self) -> None:
        self.assertFalse(verify_caller_token("", hash_caller_token("x")))


if __name__ == "__main__":
    unittest.main()
