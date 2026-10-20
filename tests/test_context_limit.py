"""The reported context limit.

The provider publishes a **working window** and a **ceiling for config
overrides**, and reserves a share of the window for its own system prompt,
tool overhead and output. The number a client can rely on is the smallest
of those constraints.

At the alias level the rule is the same, and it matters more: an alias may
be served by a fallback, so its limit is the smallest limit among the
routes that could serve it. If any eligible route's limit is not
published, the alias's limit is **unknown** - a bound that cannot be
verified is not a bound.
"""

from __future__ import annotations

import json
import unittest

from gateway.adapters.chatgpt_codex import ChatGPTCodexAdapter
from gateway.core.capabilities import (
    DEFAULT_EFFECTIVE_CONTEXT_PERCENT,
    alias_context_limit,
    effective_context_limit,
    resolve_context_window,
)
from tests.test_adapter_contract import ScriptedUpstream


class ResolutionTests(unittest.TestCase):
    def test_the_smaller_window_applies_by_default(self) -> None:
        self.assertEqual(resolve_context_window(272000, 872000), 272000)
        self.assertEqual(resolve_context_window(None, 872000), 872000)
        self.assertEqual(resolve_context_window(272000, None), 272000)
        self.assertIsNone(resolve_context_window(None, None))

    def test_the_providers_headroom_is_reserved(self) -> None:
        self.assertEqual(effective_context_limit(272000), 258400)
        self.assertEqual(
            effective_context_limit(272000, DEFAULT_EFFECTIVE_CONTEXT_PERCENT), 258400
        )
        self.assertEqual(effective_context_limit(272000, 90), 244800)

    def test_an_absent_limit_stays_unknown(self) -> None:
        self.assertIsNone(effective_context_limit(None))
        self.assertIsNone(effective_context_limit(0))


class AliasLimitTests(unittest.TestCase):
    def test_the_alias_limit_is_the_smallest_of_its_routes(self) -> None:
        result = alias_context_limit(
            [("chatgpt_codex → gpt-6-luna", 258400), ("opencode_go → mimo", 131072)]
        )
        self.assertTrue(result["known"])
        self.assertEqual(result["limit"], 131072)

    def test_an_unknown_route_limit_makes_the_alias_limit_unknown(self) -> None:
        result = alias_context_limit(
            [("chatgpt_codex → gpt-6-luna", 258400), ("opencode_go → mimo", None)]
        )
        self.assertFalse(result["known"])
        self.assertIsNone(result["limit"])
        self.assertIn("not published", result["reason"])

    def test_an_alias_with_no_routes_has_no_limit(self) -> None:
        result = alias_context_limit([])
        self.assertFalse(result["known"])


class CatalogueParsingTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile
        from pathlib import Path

        from gateway.credentials import CredentialStore

        self._tmp = tempfile.TemporaryDirectory()
        self.store = CredentialStore(Path(self._tmp.name))
        self.store.provider_handle("chatgpt_codex").write({"access_token": "synthetic"})

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _adapter(self, payload: dict) -> ChatGPTCodexAdapter:
        return ChatGPTCodexAdapter(
            self.store.provider_handle("chatgpt_codex"),
            client=ScriptedUpstream(json.dumps(payload).encode()),
        )

    def test_the_catalogue_window_and_headroom_become_the_limit(self) -> None:
        adapter = self._adapter(
            {
                "models": [
                    {
                        "slug": "gpt-6-luna",
                        "context_window": 272000,
                        "max_context_window": 872000,
                        "supported_in_api": True,
                    }
                ]
            }
        )
        model = adapter.list_models()[0]
        self.assertEqual(model.context_window, 272000)
        self.assertEqual(model.context_limit, 258400)

    def test_a_published_headroom_is_used(self) -> None:
        adapter = self._adapter(
            {
                "models": [
                    {
                        "slug": "gpt-6-luna",
                        "context_window": 100000,
                        "effective_context_window_percent": 90,
                        "supported_in_api": True,
                    }
                ]
            }
        )
        model = adapter.list_models()[0]
        self.assertEqual(model.context_limit, 90000)

    def test_a_model_without_a_window_reports_an_unknown_limit(self) -> None:
        adapter = self._adapter({"models": [{"slug": "gpt-6-luna", "supported_in_api": True}]})
        model = adapter.list_models()[0]
        self.assertIsNone(model.context_window)
        self.assertIsNone(model.context_limit)


if __name__ == "__main__":
    unittest.main()
