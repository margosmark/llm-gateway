"""Identifiers and routes are validated before anything is persisted.

Regression cover for **R4**: the configuration accepted an empty or
arbitrary alias and profile identifier, and accepted a route that pointed
at an unknown provider or model. The result was a published alias that
appeared in ``/v1/models`` and then failed during routing.

The properties under test:

* an identifier has one grammar and one length limit;
* a route must name a registered provider and an **enabled** model;
* a fallback may not repeat the primary or another fallback;
* a route that cannot be served is refused **before** publication;
* a new profile **denies** until access is granted.
"""

from __future__ import annotations

import json
import unittest

from gateway.config import Alias, Profile, RouteRef
from gateway.identifiers import (
    MAX_IDENTIFIER_LENGTH,
    ValidationError,
    validate_alias_name,
    validate_model_id,
    validate_profile_id,
)
from tests.test_end_to_end import FAKE_PROVIDER, GatewayHarness


class IdentifierGrammarTests(unittest.TestCase):
    def test_a_normal_identifier_is_accepted(self) -> None:
        for value in ("coding", "fast", "laptop-1", "a.b_c", "X9"):
            self.assertEqual(validate_alias_name(value), value)

    def test_an_empty_identifier_is_refused(self) -> None:
        for value in ("", "   ", None, 7, []):
            with self.assertRaises(ValidationError):
                validate_alias_name(value)

    def test_an_identifier_must_start_with_a_letter_or_digit(self) -> None:
        for value in (".hidden", "-dash", "_under", "../escape"):
            with self.assertRaises(ValidationError):
                validate_alias_name(value)

    def test_an_identifier_may_not_contain_a_path_separator(self) -> None:
        for value in ("a/b", "a\\b", "a b", "a;b", "a\0b"):
            with self.assertRaises(ValidationError):
                validate_alias_name(value)

    def test_an_identifier_is_length_limited(self) -> None:
        validate_alias_name("a" * MAX_IDENTIFIER_LENGTH)
        with self.assertRaises(ValidationError):
            validate_alias_name("a" * (MAX_IDENTIFIER_LENGTH + 1))

    def test_a_profile_id_uses_the_same_grammar(self) -> None:
        self.assertEqual(validate_profile_id("laptop"), "laptop")
        with self.assertRaises(ValidationError):
            validate_profile_id("../etc")

    def test_a_model_id_may_contain_a_colon_and_a_slash(self) -> None:
        self.assertEqual(validate_model_id("openai/gpt-6-luna"), "openai/gpt-6-luna")
        self.assertEqual(validate_model_id("vendor:model-v1"), "vendor:model-v1")
        with self.assertRaises(ValidationError):
            validate_model_id("bad model")


class AliasValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = GatewayHarness()
        self.addCleanup(self.harness.close)

    def _save(self, body: dict) -> tuple[int, dict]:
        status, payload, _ = self.harness.request("POST", "/owner/api/alias", body=body)
        return status, payload

    def test_an_alias_with_an_empty_name_is_refused(self) -> None:
        status, payload = self._save(
            {"action": "save", "name": "", "primary": {"provider": FAKE_PROVIDER, "model": "gpt-6-luna"}}
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["type"], "invalid_alias")

    def test_an_alias_with_an_illegal_name_is_refused(self) -> None:
        status, payload = self._save(
            {"action": "save", "name": "../escape", "primary": {"provider": FAKE_PROVIDER, "model": "gpt-6-luna"}}
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["type"], "invalid_alias")

    def test_a_route_to_an_unknown_provider_is_refused(self) -> None:
        status, payload = self._save(
            {"action": "save", "name": "coding", "primary": {"provider": "nope", "model": "gpt-6-luna"}}
        )
        self.assertEqual(status, 400)
        self.assertIn("not registered", payload["error"]["message"])

    def test_a_route_to_an_unknown_model_is_refused(self) -> None:
        status, payload = self._save(
            {"action": "save", "name": "coding", "primary": {"provider": FAKE_PROVIDER, "model": "no-such-model"}}
        )
        self.assertEqual(status, 400)
        self.assertIn("not known", payload["error"]["message"])

    def test_a_route_to_a_disabled_model_is_refused(self) -> None:
        with self.harness.config.mutate() as config:
            provider = config.providers[FAKE_PROVIDER]
            provider.models["disabled-model"] = __import__(
                "gateway.config", fromlist=["ModelEntry"]
            ).ModelEntry(model_id="disabled-model", enabled=False, dialect="openai_responses")
        status, payload = self._save(
            {"action": "save", "name": "coding", "primary": {"provider": FAKE_PROVIDER, "model": "disabled-model"}}
        )
        self.assertEqual(status, 400)
        self.assertIn("not enabled", payload["error"]["message"])

    def test_a_fallback_repeating_the_primary_is_refused(self) -> None:
        status, payload = self._save(
            {
                "action": "save",
                "name": "coding",
                "primary": {"provider": FAKE_PROVIDER, "model": "gpt-6-luna"},
                "fallbacks": [{"provider": FAKE_PROVIDER, "model": "gpt-6-luna"}],
            }
        )
        self.assertEqual(status, 400)
        self.assertIn("repeats the primary", payload["error"]["message"])

    def test_a_repeated_fallback_is_refused(self) -> None:
        with self.harness.config.mutate() as config:
            provider = config.providers[FAKE_PROVIDER]
            provider.models["other-model"] = __import__(
                "gateway.config", fromlist=["ModelEntry"]
            ).ModelEntry(model_id="other-model", enabled=True, dialect="openai_responses")
        status, payload = self._save(
            {
                "action": "save",
                "name": "coding",
                "primary": {"provider": FAKE_PROVIDER, "model": "gpt-6-luna"},
                "fallbacks": [
                    {"provider": FAKE_PROVIDER, "model": "other-model"},
                    {"provider": FAKE_PROVIDER, "model": "other-model"},
                ],
            }
        )
        self.assertEqual(status, 400)
        self.assertIn("repeats an earlier route", payload["error"]["message"])

    def test_a_refused_alias_is_not_persisted(self) -> None:
        self._save(
            {"action": "save", "name": "coding", "primary": {"provider": "nope", "model": "gpt-6-luna"}}
        )
        self.assertNotIn("coding", self.harness.config.snapshot().aliases)

    def test_a_refused_alias_never_appears_in_the_catalogue(self) -> None:
        self._save(
            {"action": "save", "name": "coding", "primary": {"provider": FAKE_PROVIDER, "model": "no-such-model"}}
        )
        status, models, _ = self.harness.request("GET", "/v1/models")
        self.assertEqual(status, 200)
        self.assertEqual(models["data"], [])

    def test_a_valid_alias_is_saved_and_publishable(self) -> None:
        status, _ = self._save(
            {"action": "save", "name": "coding", "primary": {"provider": FAKE_PROVIDER, "model": "gpt-6-luna"}}
        )
        self.assertEqual(status, 200)
        status, _, _ = self.harness.request(
            "POST", "/owner/api/alias", body={"action": "publish", "name": "coding", "enabled": True}
        )
        self.assertEqual(status, 200)
        status, models, _ = self.harness.request("GET", "/v1/models")
        self.assertEqual([m["id"] for m in models["data"]], ["coding"])


class ProfileValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = GatewayHarness()
        self.addCleanup(self.harness.close)

    def test_a_profile_with_an_illegal_id_is_refused(self) -> None:
        status, payload, _ = self.harness.request(
            "POST", "/owner/api/profile", body={"action": "save", "profile_id": "../etc", "name": "x"}
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["type"], "invalid_profile")
        self.assertNotIn("../etc", self.harness.config.snapshot().profiles)

    def test_a_profile_with_an_empty_id_is_refused(self) -> None:
        status, payload, _ = self.harness.request(
            "POST", "/owner/api/profile", body={"action": "save", "profile_id": "", "name": "x"}
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["type"], "invalid_profile")

    def test_a_profile_with_an_illegal_alias_is_refused(self) -> None:
        status, payload, _ = self.harness.request(
            "POST",
            "/owner/api/profile",
            body={"action": "save", "profile_id": "laptop", "name": "Laptop", "allowed_aliases": ["../x"]},
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["type"], "invalid_profile")

    def test_a_new_profile_denies_until_access_is_granted(self) -> None:
        from gateway.auth import profile_allows

        profile = Profile(profile_id="laptop", name="Laptop")
        self.assertFalse(profile_allows(profile, "coding"))

    def test_an_explicit_grant_is_honoured(self) -> None:
        from gateway.auth import profile_allows

        profile = Profile(profile_id="laptop", name="Laptop", allowed_aliases=["coding"])
        self.assertTrue(profile_allows(profile, "coding"))
        self.assertFalse(profile_allows(profile, "other"))

    def test_the_all_aliases_grant_is_separate_and_explicit(self) -> None:
        from gateway.auth import profile_allows

        profile = Profile(profile_id="laptop", name="Laptop", all_aliases=True)
        self.assertTrue(profile_allows(profile, "anything"))

    def test_the_anonymous_profile_may_use_every_published_alias(self) -> None:
        profile = self.harness.profile_auth.anonymous_profile()
        from gateway.auth import profile_allows

        self.assertTrue(profile_allows(profile, "coding"))


if __name__ == "__main__":
    unittest.main()
