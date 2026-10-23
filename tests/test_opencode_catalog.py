"""V2 selector catalogue: only published metadata, no provider calls or secrets."""

import unittest

from gateway.config import Alias, ModelEntry, RouteRef
from tests.test_end_to_end import GatewayHarness


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.harness = GatewayHarness()
        self.addCleanup(self.harness.close)

    def test_published_only_and_context_unknown_if_any_route_unknown(self):
        with self.harness.config.mutate() as config:
            config.providers["chatgpt_codex"].models["gpt-6-luna"].context_limit = 258400
            config.providers["chatgpt_codex"].models["gpt-6-luna"].reasoning_efforts = ["low", "high"]
            config.providers["opencode_go"].models["go"] = ModelEntry(
                model_id="go", dialect="openai_chat", enabled=True, context_limit=None
            )
            config.aliases["pm"] = Alias(
                name="pm", enabled=True, primary=RouteRef("chatgpt_codex", "gpt-6-luna"),
                fallbacks=[RouteRef("opencode_go", "go")],
            )
            config.aliases["hidden"] = Alias(name="hidden", enabled=False, primary=RouteRef("opencode_go", "go"))
        status, body, _ = self.harness.request("GET", "/v1/alias-catalog")
        self.assertEqual(status, 200)
        self.assertEqual([x["id"] for x in body["data"]], ["pm"])
        self.assertEqual(body["data"][0]["context"]["limit"], None)
        self.assertEqual(body["data"][0]["primary_reasoning_efforts"], ["low", "high"])
        self.assertEqual(self.harness.adapter.calls, [])
        self.assertEqual(self.harness.adapter.model_list_calls, 0)
        self.assertNotIn("providers", body)
        self.assertNotIn("ledger", body)

    def test_finite_primary_and_unpublish(self):
        with self.harness.config.mutate() as config:
            config.providers["chatgpt_codex"].models["gpt-6-luna"].context_limit = 258400
            config.aliases["known"] = Alias(
                name="known", enabled=True, primary=RouteRef("chatgpt_codex", "gpt-6-luna")
            )
        _, body, _ = self.harness.request("GET", "/v1/alias-catalog")
        self.assertEqual(body["data"][0]["context"]["limit"], 258400)
        with self.harness.config.mutate() as config:
            config.aliases["known"].enabled = False
        _, body, _ = self.harness.request("GET", "/v1/alias-catalog")
        self.assertEqual(body["data"], [])

    def test_documented_go_windows_remove_the_client_budget_and_publish_efforts(self):
        with self.harness.config.mutate() as config:
            config.providers["chatgpt_codex"].models["gpt-6-luna"].context_limit = 258400
            config.providers["opencode_go"].models["minimax-m3.1-flash"] = ModelEntry(
                model_id="minimax-m3.1-flash",
                dialect="openai_chat",
                enabled=True,
                context_window=1_000_000,
                context_limit=1_000_000,
                reasoning_efforts=["low", "medium", "high", "xhigh", "max"],
            )
            config.providers["opencode_go"].models["mimo-v2.6-flash"] = ModelEntry(
                model_id="mimo-v2.6-flash",
                dialect="openai_chat",
                enabled=True,
                context_window=1_048_576,
                context_limit=1_048_576,
            )
            config.aliases["coding"] = Alias(
                name="coding", enabled=True,
                primary=RouteRef("chatgpt_codex", "gpt-6-luna"),
                fallbacks=[
                    RouteRef("opencode_go", "minimax-m3.1-flash"),
                    RouteRef("opencode_go", "mimo-v2.6-flash"),
                ],
            )
            config.aliases["flash"] = Alias(
                name="flash", enabled=True,
                primary=RouteRef("opencode_go", "minimax-m3.1-flash"),
                fallbacks=[RouteRef("opencode_go", "mimo-v2.6-flash")],
            )
        _, body, _ = self.harness.request("GET", "/v1/alias-catalog")
        by_id = {item["id"]: item for item in body["data"]}
        self.assertEqual(by_id["coding"]["context"]["limit"], 258400)
        self.assertTrue(by_id["coding"]["context"]["known"])
        self.assertEqual(by_id["flash"]["context"]["limit"], 1_000_000)
        self.assertEqual(
            by_id["flash"]["primary_reasoning_efforts"],
            ["low", "medium", "high", "xhigh", "max"],
        )

    def test_host_and_forwarded_headers_are_refused(self):
        for headers in ({"Host": "evil.invalid"}, {"Forwarded": "host=evil.invalid"}):
            status, _, _ = self.harness.request("GET", "/v1/alias-catalog", headers=headers)
            self.assertIn(status, (400, 403))


if __name__ == "__main__":
    unittest.main()
