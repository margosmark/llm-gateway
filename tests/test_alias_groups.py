from __future__ import annotations

import unittest

from gateway.alias_groups import set_group_enabled
from gateway.config import Alias, GatewayConfig, RouteRef
from gateway.identifiers import ValidationError


class AliasGroupTests(unittest.TestCase):
    def setUp(self):
        self.config = GatewayConfig()
        self.first = Alias("one", enabled=False, primary=RouteRef("a", "m1"), fallbacks=[RouteRef("b", "m2")], fallback_authorized=True, tag="coding")
        self.second = Alias("two", enabled=False, primary=RouteRef("c", "m3"), tag="coding")
        self.other = Alias("other", enabled=True, primary=RouteRef("d", "m4"), tag="other")
        self.config.aliases = {a.name: a for a in (self.first, self.second, self.other)}

    def test_publish_and_disable_tag_only_toggle_enabled_and_preserve_route_policy(self):
        first_routes = (self.first.primary, list(self.first.fallbacks), self.first.fallback_authorized)
        names = set_group_enabled(self.config, "coding", True)
        self.assertEqual(names, ["one", "two"])
        self.assertTrue(self.first.enabled)
        self.assertTrue(self.second.enabled)
        self.assertTrue(self.other.enabled)
        self.assertEqual((self.first.primary, self.first.fallbacks, self.first.fallback_authorized), first_routes)
        self.assertEqual(set_group_enabled(self.config, "coding", False), ["one", "two"])
        self.assertFalse(self.first.enabled)
        self.assertFalse(self.second.enabled)
        self.assertEqual(self.first.fallbacks, [RouteRef("b", "m2")])

    def test_invalid_or_empty_tag_refuses_without_mutating_config(self):
        with self.assertRaises(ValidationError):
            set_group_enabled(self.config, "../bad", False)
        with self.assertRaises(ValidationError):
            set_group_enabled(self.config, "missing", False)
        self.assertFalse(self.first.enabled)
        self.assertFalse(self.second.enabled)

    def test_publish_refuses_incomplete_group_atomically(self):
        self.second.primary = None
        with self.assertRaises(ValidationError):
            set_group_enabled(self.config, "coding", True)
        self.assertFalse(self.first.enabled)
        self.assertFalse(self.second.enabled)

    def test_alias_tag_round_trips_and_legacy_defaults_empty(self):
        alias = Alias("tagged", tag="coding")
        self.assertEqual(Alias.from_dict(alias.to_dict()).tag, "coding")
        self.assertEqual(Alias.from_dict({"name": "legacy"}).tag, "")


if __name__ == "__main__":
    unittest.main()
