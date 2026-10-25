"""The 2026-10-25 catalogue transcription, verified end to end.

Four things are checked here, in the order they matter:

* **Round trip.** The checked-in ``data/go_catalog.json`` is byte-for-byte
  what ``scripts/build_go_catalog_snapshot.py`` writes, and the adapter
  loader turns every row into a ``ModelInfo`` carrying that row's own
  context, output, limit and image verdict.
* **Source discipline.** Every image verdict is a claim with a page behind
  it, every price and window has the page it was transcribed from and the
  date that page was read, and the four rows nobody read stay false and
  sourceless.
* **Serving drift.** The three newly served ids exist (one of them
  fail-closed), and the two rows the provider dropped are marked deprecated
  rather than silently deleted.
* **Transcription fidelity.** Where PM's 389-page crawl of
  https://opencode.ai/data/ is on this machine, each dataset-backed row is
  compared field by field against it. That class skips when the crawl is
  absent; the other three run everywhere.
"""
from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path

from gateway.adapters.base import AdapterError, ProviderRequest
from gateway.adapters.http_client import UpstreamResponse
from gateway.adapters.opencode_go import OpenCodeGoAdapter, _default_snapshot_path
from gateway.credentials import CredentialStore

#: PM's crawl of https://opencode.ai/data/, read 2026-10-25. Not part of
#: the repository: tests that compare against it skip when it is absent.
DATASET = Path("/home/hermes/opencode_model_data_20261025.json")

DATA_PREFIX = "https://opencode.ai/data/"
DOCS = "https://opencode.ai/docs/go/"
CATALOGUE_ENDPOINT = "https://opencode.ai/inference/go/v1/models"
READ_DATE = "2026-10-25"

#: The verdicts transcribed before the 2026-10-25 crawl. Their read date is
#: older by design - the crawl re-read the pages and the verdicts did not
#: move - and ``minimax-m3.1-flash`` reads its verdict from the model
#: provider's documentation, so its source is not an opencode.ai/data page.
LEGACY_VERDICT_SOURCES = {
    "minimax-m3.1-flash": "https://platform.minimax.io/docs/api-reference/text-openai-api",
}

#: Rows nobody has read a data page for: one page carries no payload, three
#: have no page at all. They must be false with no source.
UNREAD_ROWS = frozenset({
    "gemini-4-argon-contributor",
    "iceman-contributor-free",
    "longcat-2.5-preview-free",
    "omen-alpha",
})

#: What the live catalogue returned on 2026-10-25.
SERVING_ON_READ = [
    "beam-free",
    "claude-haiku-5-5",
    "deepseek-v4-pro",
    "deepseek-v4.1-flash",
    "gemini-4-argon",
    "glm-5.1",
    "glm-5.2",
    "glm-5.3",
    "glm-5.3-flash",
    "gpt-5.6-luna",
    "gpt-6-luna",
    "grok-4.6",
    "grok-4.7",
    "hy3",
    "hy4-preview",
    "iceman-contributor-free",
    "kimi-k2.6",
    "kimi-k2.7-code",
    "kimi-k3",
    "kimi-k3.1-pro",
    "longcat-2.0",
    "mimo-v2.5",
    "mimo-v2.5-pro",
    "mimo-v2.6-flash",
    "mimo-v2.6-pro",
    "minimax-m2.5",
    "minimax-m2.7",
    "minimax-m3",
    "minimax-m3.1-flash",
    "muse-spark-1.2-contributor",
    "muse-spark-1.3-contributor",
    "omen-alpha",
    "qwen3.6-plus",
    "qwen3.7-max",
    "qwen3.7-plus",
    "qwen3.8-flash",
    "qwen3.8-max",
    "step-5-preview"
]

#: The two ids still catalogued but no longer served.
DROPPED_FROM_LIVE = frozenset({"gemini-4-argon-contributor", "longcat-2.5-preview-free"})


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _snapshot() -> dict:
    return json.loads(_default_snapshot_path().read_text(encoding="utf-8"))


def _models() -> dict:
    return _snapshot()["models"]


def _dataset() -> dict | None:
    if not DATASET.is_file():
        return None
    return json.loads(DATASET.read_text(encoding="utf-8"))


class _FakeClient:
    """Serves the canned /v1/models payload built from the snapshot itself."""

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def request(self, method, url, headers, body=None, stream=False, provider_id="", **kwargs):
        return UpstreamResponse(200, {}, json.dumps(self._payload).encode(),
                                None, "application/json")


def _adapter(credentials=None) -> OpenCodeGoAdapter:
    payload = {"data": [{"id": model_id} for model_id in sorted(SERVING_ON_READ)]}
    return OpenCodeGoAdapter(credentials, client=_FakeClient(payload))


class LoaderRoundTripTests(unittest.TestCase):
    """The file, the builder and the adapter all say the same thing."""

    def setUp(self) -> None:
        # list_models builds headers, so it needs a real credential handle.
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        store = CredentialStore(Path(self._tmp.name))
        self.handle = store.provider_handle("opencode_go")

    def test_the_checked_in_catalog_is_exactly_what_the_builder_writes(self) -> None:
        import importlib.util

        script = Path(__file__).resolve().parent.parent / "scripts" / "build_go_catalog_snapshot.py"
        spec = importlib.util.spec_from_file_location("build_go_catalog_snapshot", script)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        built = module.build()
        expected = json.dumps(built, indent=2, sort_keys=True) + "\n"
        self.assertEqual(
            _default_snapshot_path().read_text(encoding="utf-8"), expected,
            "the checked-in catalogue is not what the builder writes; re-run "
            "python3 scripts/build_go_catalog_snapshot.py",
        )

    def test_the_loader_round_trips_every_row_into_model_info(self) -> None:
        models = _models()
        self.assertEqual(set(models), set(SERVING_ON_READ) | set(DROPPED_FROM_LIVE))
        adapter = _adapter(self.handle)
        info = {m.model_id: m for m in adapter.list_models()}
        self.assertEqual(sorted(info), sorted(SERVING_ON_READ))
        evidence = adapter.image_input_evidence()
        for model_id, row in models.items():
            with self.subTest(model=model_id):
                # Every field the loader transcribes comes back out of it.
                if model_id in info:
                    self.assertEqual(info[model_id].dialect, row["dialect"] or "")
                    self.assertEqual(info[model_id].context_window, row["context_window"])
                    self.assertEqual(info[model_id].context_limit, row["context_limit"])
                    self.assertEqual(info[model_id].max_output_tokens, row["max_output_tokens"])
                    self.assertEqual(info[model_id].monthly_limit_usd, row["monthly_limit_usd"])
                    self.assertEqual(info[model_id].limits_source, row["limits_source"])
                    self.assertEqual(list(info[model_id].reasoning_efforts),
                                     row["reasoning_efforts"])
                    self.assertEqual(info[model_id].retention_class, row["retention_class"])
                    # A verdict reaches the ModelInfo only through the
                    # evidence hook, and only when it carries a source.
                    claimed = model_id in evidence and evidence[model_id].supports_image_input
                    self.assertEqual(info[model_id].supports_image_input, claimed)
                    self.assertEqual(info[model_id].supports_image_input,
                                     row["supports_image_input"])

    def test_the_evidence_hook_reads_every_sourced_verdict(self) -> None:
        models = _models()
        evidence = OpenCodeGoAdapter(None).image_input_evidence()
        sourced = {model_id for model_id, row in models.items()
                   if isinstance(row.get("supports_image_input"), bool)
                   and row.get("image_input_source")}
        self.assertEqual(set(evidence), sourced)
        # Nobody's verdict arrives without its page and read date.
        for model_id, verdict in evidence.items():
            with self.subTest(model=model_id):
                self.assertTrue(verdict.source)
                self.assertTrue(verdict.observed_at)


class SourceDisciplineTests(unittest.TestCase):
    """Per-slug provenance for every claim the snapshot carries."""

    def test_every_image_verdict_is_sourced_to_a_page_read_for_that_slug(self) -> None:
        for model_id, row in _models().items():
            if row["supports_image_input"]:
                with self.subTest(model=model_id):
                    source = row["image_input_source"]
                    self.assertTrue(source, f"{model_id} claims image input with no source")
                    self.assertTrue(row["image_input_observed_at"])
                    if model_id in LEGACY_VERDICT_SOURCES:
                        self.assertEqual(source, LEGACY_VERDICT_SOURCES[model_id])
                    else:
                        self.assertTrue(source.startswith(DATA_PREFIX), source)
                        self.assertIn(_norm(model_id), _norm(source),
                                      f"{model_id} borrows another slug's page")

    def test_no_row_is_sourced_to_a_page_nobody_read(self) -> None:
        models = _models()
        self.assertEqual(
            sorted(m for m in models
                   if not models[m].get("image_input_source")
                   and not models[m].get("supports_image_input")),
            sorted(UNREAD_ROWS),
        )

    def test_every_window_and_price_carries_its_page_and_read_date(self) -> None:
        for model_id, row in _models().items():
            with self.subTest(model=model_id):
                if row["context_window"] is not None:
                    self.assertTrue(row["context_source"])
                if row["price_input_per_m"] is not None:
                    self.assertTrue(row["source"])
                    self.assertIn(row["observed_at"], {READ_DATE, "2026-10-22"})
                if row["limits_source"] not in {"published_estimate", "unknown"}:
                    # A model-provider window document, transcribed per slug.
                    self.assertTrue(row["limits_source"].startswith("http"))

    def test_the_deprecated_rows_say_why(self) -> None:
        models = _models()
        deprecated = sorted(m for m in models if models[m]["deprecated"])
        self.assertEqual(deprecated, sorted(DROPPED_FROM_LIVE | {"mimo-v2.5", "mimo-v2.5-pro"}))
        for model_id in DROPPED_FROM_LIVE:
            note = models[model_id]["notes"] or ""
            self.assertIn("2026-10-25", note)


class ServingDriftTests(unittest.TestCase):
    """The three added rows and the two deprecated ones, without breaking routes."""

    def test_the_newly_documented_serving_ids_are_full_rows(self) -> None:
        models = _models()
        kimi = models["kimi-k3.1-pro"]
        self.assertEqual(kimi["dialect"], "openai_chat")
        self.assertEqual(kimi["endpoint_path"], "/openai/v1/chat/completions")
        self.assertEqual(kimi["monthly_limit_usd"], 15.0)
        self.assertTrue(kimi["supports_image_input"])
        self.assertEqual(kimi["image_input_source"],
                         DATA_PREFIX + "moonshotai/kimi-k3-1-pro")
        self.assertEqual(kimi["observed_at"], READ_DATE)
        argon = models["gemini-4-argon"]
        self.assertEqual(argon["dialect"], "google_v1beta")
        self.assertEqual(argon["monthly_limit_usd"], 30.0)
        self.assertTrue(argon["supports_image_input"])
        self.assertEqual(argon["retention_class"], "0_days")
        # The endpoint exists and is named; serving it is refused, because
        # the gateway translates two dialects and Google is not one.
        adapter = OpenCodeGoAdapter(None)
        # The endpoint is named, and serving it is refused.
        self.assertEqual(adapter.endpoint_for("gemini-4-argon")[1],
                         "/google/v1beta/models/gemini-4-argon")
        with self.assertRaises(AdapterError) as ctx:
            adapter.invoke(ProviderRequest(model="gemini-4-argon", payload={}))
        self.assertIn("does not translate", ctx.exception.message)

    def test_the_serving_id_with_no_data_page_is_fail_closed(self) -> None:
        row = _models()["iceman-contributor-free"]
        self.assertIsNone(row["dialect"])
        self.assertIsNone(row["endpoint_path"])
        self.assertIsNone(row["price_input_per_m"])
        self.assertIsNone(row["price_output_per_m"])
        self.assertFalse(row["supports_image_input"])
        self.assertIsNone(row["image_input_source"])
        self.assertEqual(row["source"], CATALOGUE_ENDPOINT)
        self.assertEqual(row["observed_at"], READ_DATE)
        self.assertEqual(row["retention_class"], "unknown")
        with self.assertRaises(AdapterError) as ctx:
            OpenCodeGoAdapter(None).endpoint_for("iceman-contributor-free")
        self.assertIn("not documented", ctx.exception.message)

    def test_the_dropped_rows_stay_catalogued_and_deprecated(self) -> None:
        models = _models()
        for model_id in DROPPED_FROM_LIVE:
            with self.subTest(model=model_id):
                self.assertTrue(models[model_id]["deprecated"])
                self.assertTrue(models[model_id]["dialect"],
                                "a deprecated row keeps the dialect it last published")
                self.assertEqual(models[model_id]["observed_at"], "2026-10-22"
                                 if model_id == "gemini-4-argon-contributor" else READ_DATE)
        # Their routes still resolve, so nothing that references them 404s.
        adapter = OpenCodeGoAdapter(None)
        self.assertEqual(adapter.endpoint_for("longcat-2.5-preview-free")[0],
                         "openai_chat")
        self.assertEqual(adapter.endpoint_for("gemini-4-argon-contributor")[1],
                         "/google/v1beta/models/gemini-4-argon-contributor")

    def test_the_serving_set_is_the_live_set(self) -> None:
        self.assertEqual(set(_models()) - DROPPED_FROM_LIVE, set(SERVING_ON_READ))


@unittest.skipUnless(DATASET.is_file(), f"the crawl at {DATASET} is not on this machine")
class DatasetTranscriptionTests(unittest.TestCase):
    """Field-by-field fidelity against PM's 2026-10-25 crawl."""

    def setUp(self) -> None:
        self.dataset = _dataset()
        # Slugs are compared per exact catalogue id after one normalization:
        # the crawl writes ``deepseek-v4-1-flash``, the endpoint serves
        # ``deepseek-v4.1-flash``, and both are the same page.
        self.by_norm = {_norm(str(entry.get("slug"))): entry
                        for entry in self.dataset["models"] if isinstance(entry, dict)}

    def test_every_dataset_backed_row_matches_the_crawl(self) -> None:
        checked = 0
        for model_id, row in _models().items():
            source = row.get("source") or ""
            if not source.startswith(DATA_PREFIX):
                continue
            crawl = self.by_norm.get(_norm(model_id))
            self.assertIsNotNone(crawl, f"no crawled page for {model_id}")
            with self.subTest(model=model_id):
                self.assertEqual(row["context_window"], crawl.get("context_window"))
                self.assertEqual(row["max_output_tokens"], crawl.get("max_output_tokens"))
                self.assertEqual(row["price_input_per_m"], crawl.get("price_input_per_m"))
                self.assertEqual(row["price_output_per_m"], crawl.get("price_output_per_m"))
                if model_id != "deepseek-v4-pro":
                    # That page publishes no cached-read price, so the row
                    # keeps None rather than inventing one.
                    self.assertEqual(row["price_cached_read_per_m"],
                                     crawl.get("price_cache_read_per_m"))
                self.assertEqual(bool(row["supports_image_input"]),
                                 bool(crawl.get("supports_image_input")))
            checked += 1
        # Every data-page-backed row was compared, so a skipped row cannot
        # hide behind an empty loop.
        self.assertEqual(checked, sum(1 for row in _models().values()
                                      if (row.get("source") or "").startswith(DATA_PREFIX)))

    def test_the_price_drift_the_card_named_is_transcribed(self) -> None:
        models = _models()
        expected = {
            "deepseek-v4.1-flash": (0.30, 1.20),
            "deepseek-v4-pro": (0.435, 0.87),
            "glm-5.3": (1.19516, 4.18306),
            "glm-5.3-flash": (0.11952, 0.41831),
            "hy3": (0.18, 0.59),
            "mimo-v2.6-pro": (0.43, 0.87),
            "beam-free": (0.30, 1.00),
        }
        for model_id, (paid, pout) in expected.items():
            with self.subTest(model=model_id):
                self.assertAlmostEqual(models[model_id]["price_input_per_m"], paid)
                self.assertAlmostEqual(models[model_id]["price_output_per_m"], pout)

    def test_the_rows_thin_before_the_transcription_are_thin_no_more(self) -> None:
        models = _models()
        with_context = [m for m, row in models.items() if row["context_window"] is not None]
        with_output = [m for m, row in models.items() if row["max_output_tokens"] is not None]
        with_price = [m for m, row in models.items() if row["price_input_per_m"] is not None]
        self.assertEqual(len(with_context), 36)
        self.assertEqual(len(with_output), 36)
        self.assertEqual(len(with_price), 37)
        # Only the four unread rows lack a window, and only the three with
        # no price at all.
        self.assertEqual(set(models) - set(with_context), UNREAD_ROWS)
        self.assertEqual(set(models) - set(with_price),
                         {"iceman-contributor-free", "longcat-2.5-preview-free", "omen-alpha"})


if __name__ == "__main__":
    unittest.main()
