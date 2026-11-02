"""HTTP surface for the bounded OpenCode Go capability probe.

Every test here is offline: the gateway under test talks only to a loopback
upstream that answers with canned Chat Completions payloads. Nothing in this
file contacts a provider.
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import tempfile
import threading
import time
import unittest
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar

from gateway import costs
from gateway.adapters.http_client import UpstreamClient
from gateway.adapters.opencode_go import OpenCodeGoAdapter
from gateway.config import ModelEntry
from gateway.credentials import CredentialStore
from tests.test_end_to_end import GatewayHarness

PROVIDER = "opencode_go"
MODEL = "mimo-v2.6-flash"
ALL_PROBES = ["text_generation", "image_input", "function_tools"]
OWNER = {
    "authorize_billing_route": True,
    "spend_policy": "estimate_accepted",
    "max_spend_estimate_usd": 0.01,
}
SYNTHETIC_KEY = "synthetic-probe-key-0123456789"
LEAKY_TEXT = "LEAKY-PROVIDER-TEXT-MUST-NOT-PERSIST"

# The shipped catalogue carries its real crawl date, which ages: past
# PRICE_MAX_AGE_DAYS every preview fails closed on staleness and the whole
# file goes red for the wrong reason (the 2026-11-25 bomb wave). setUp
# re-points the catalogue at a temp copy re-dated FRESH_AS_OF - one day back,
# fresh and never future at any wall clock - preserving every rate and
# provenance byte. Staleness itself stays proved against a pinned old date
# in test_unknown_or_stale_pricing_fails_closed.
FRESH_AS_OF = (datetime.now(UTC).date() - timedelta(days=1)).isoformat()
STALE_AS_OF = (datetime.now(UTC).date() - timedelta(days=60)).isoformat()
_PRISTINE_CATALOGUE = costs._CATALOG_PATH


def _strict_json_loads(body: str) -> Any:
    """``json.loads`` that rejects the non-RFC-8259 tokens.

    ``json.loads`` accepts ``NaN``/``Infinity``/``-Infinity`` by default
    (``allow_nan``), so a body carrying one would parse here while the
    console's ``JSON.parse`` throws. ``parse_constant`` fires on exactly those
    three constants; raising turns a silent pass into a failure, mirroring
    what the browser does with the same bytes.
    """

    def _reject(token: str) -> Any:
        raise AssertionError(f"non-RFC-8259 token {token!r} in body: {body}")

    return json.loads(body, parse_constant=_reject)


_NODE_PROBE = (
    "const fs=require('fs');"
    "const p=JSON.parse(fs.readFileSync(process.argv[1],'utf8'));"
    "process.stdout.write(JSON.stringify({"
    "reported_cost_usd:p.reported_cost_usd,"
    "reported_cost_type:typeof p.reported_cost_usd,"
    "blocked_reason:p.blocked_reason,cancelled:p.cancelled,"
    "request_count:p.request_count,"
    "statuses:[...new Set(p.results.map((r)=>r.status))].sort()}));"
)


def _node_json_parse(body: bytes) -> dict[str, Any]:
    """Parse the exact response bytes with node's ``JSON.parse``.

    ``json.loads`` accepts ``Infinity``/``NaN``, so a Python-only strictness
    check can pass on a body the owner console cannot parse. Node's
    ``JSON.parse`` is what ``static/console.js`` runs on these bytes, so the
    raw body must survive it: a non-RFC-8259 token makes node exit non-zero
    and this helper raises with the parser's error instead of returning.
    """
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as handle:
        handle.write(body)
        path = handle.name
    try:
        probe = subprocess.run(
            ["node", "-e", _NODE_PROBE, path], capture_output=True, text=True, timeout=60, check=False
        )
    finally:
        os.unlink(path)
    if probe.returncode != 0:
        raise AssertionError(f"node JSON.parse rejected the body: {probe.stderr.strip()}")
    return json.loads(probe.stdout)


def _non_finite_paths(value: Any, where: str = "$") -> list[str]:
    """Every path in a parsed structure that holds a non-finite float."""
    if isinstance(value, float):
        return [] if math.isfinite(value) else [f"{where}={value!r}"]
    if isinstance(value, dict):
        return [path for key, item in value.items() for path in _non_finite_paths(item, f"{where}.{key}")]
    if isinstance(value, list):
        return [path for index, item in enumerate(value) for path in _non_finite_paths(item, f"{where}[{index}]")]
    return []


def _usage() -> dict[str, Any]:
    return {"prompt_tokens": 120, "completion_tokens": 40, "cost": 0.000002}


def _text_answer(nonce: str, usage: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "id": "probe-1",
        "object": "chat.completion",
        "created": 0,
        "model": MODEL,
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": nonce}, "finish_reason": "stop"}
        ],
        "usage": usage,
    }


def _visual_answer(usage: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "id": "probe-2",
        "object": "chat.completion",
        "created": 0,
        "model": MODEL,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {
                                "name": "probe_visual_answer",
                                "arguments": json.dumps(
                                    {"blue_circle_count": 2, "red_square_present": True}
                                ),
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": usage,
    }


class _UpstreamHandler(BaseHTTPRequestHandler):
    """Loopback stand-in for the Go chat endpoint."""

    protocol_version = "HTTP/1.1"
    requests: ClassVar[list[dict[str, Any]]] = []
    status: ClassVar[int] = 200
    usage: ClassVar[dict[str, Any] | None] = _usage()
    delay: ClassVar[float] = 0.0

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            parsed = json.loads(raw.decode("utf-8")) if raw else None
        except ValueError:
            parsed = None
        type(self).requests.append(
            {
                "path": self.path,
                "headers": {key.lower(): value for key, value in self.headers.items()},
                "body": parsed,
            }
        )
        if type(self).delay:
            time.sleep(type(self).delay)
        messages = parsed.get("messages") if isinstance(parsed, dict) else None
        content = messages[0].get("content") if isinstance(messages, list) else None
        if isinstance(content, str):
            answer = _text_answer(content.rsplit(": ", 1)[-1], type(self).usage)
        else:
            answer = _visual_answer(type(self).usage)
        payload = json.dumps(answer).encode("utf-8")
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args, **kwargs) -> None:
        pass


class ProbeHttpTests(unittest.TestCase):
    """The owner-facing endpoints, and everything they must not do."""

    def setUp(self) -> None:
        _UpstreamHandler.requests = []
        _UpstreamHandler.status = 200
        _UpstreamHandler.usage = _usage()
        _UpstreamHandler.delay = 0.0

        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), _UpstreamHandler)
        self.upstream_thread = threading.Thread(target=self.upstream.serve_forever, daemon=True)
        self.upstream_thread.start()

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        # Re-date the shipped catalogue instead of trusting its crawl date:
        # the fresh-tariff path must be exercised at any wall clock, and the
        # re-dated copy keeps every rate and provenance byte of the real one.
        catalogue = json.loads(_PRISTINE_CATALOGUE.read_text(encoding="utf-8"))
        for row in catalogue.get("models", {}).values():
            if isinstance(row, dict):
                for key, value in row.items():
                    if key.endswith("observed_at") and isinstance(value, str):
                        row[key] = FRESH_AS_OF
        staged = Path(self._tmp.name) / "go_catalog.json"
        staged.write_text(json.dumps(catalogue), encoding="utf-8")
        self._catalogue_before = costs._CATALOG_PATH
        costs._CATALOG_PATH = staged
        self.addCleanup(self._restore_catalogue)
        credentials = CredentialStore(Path(self._tmp.name))
        credentials.provider_handle(PROVIDER).write({"api_key": SYNTHETIC_KEY})
        self.adapter = OpenCodeGoAdapter(
            credentials=credentials.provider_handle(PROVIDER),
            client=UpstreamClient(),
            base_url=f"http://127.0.0.1:{self.upstream.server_address[1]}",
        )

        self.h = GatewayHarness()
        self.addCleanup(self.h.close)
        self.h.registry.register(self.adapter)
        with self.h.config.mutate() as config:
            provider = config.providers[PROVIDER]
            provider.state = "connected"
            provider.models[MODEL] = ModelEntry(
                model_id=MODEL,
                enabled=True,
                dialect="openai_chat",
                source="discovered",
                supports_function_tools=False,
                supports_image_input=False,
            )

        def stop_upstream() -> None:
            self.upstream.shutdown()
            self.upstream.server_close()
            self.upstream_thread.join(timeout=5)

        self.addCleanup(stop_upstream)

    # --- helpers -------------------------------------------------------

    def _restore_catalogue(self) -> None:
        costs._CATALOG_PATH = self._catalogue_before

    def _preview(self, probes=ALL_PROBES, owner=OWNER, headers=None, provider=PROVIDER, model=MODEL):
        status, payload, headers_out = self.h.request(
            "POST",
            "/owner/api/capabilities/preview",
            {"provider_id": provider, "model_id": model, "probes": probes, "owner": owner},
            headers=headers,
        )
        return status, payload or {}, headers_out

    def _run(self, confirmation, revision, probes=ALL_PROBES, owner=OWNER, extra=None, headers=None):
        body: dict[str, Any] = {
            "provider_id": PROVIDER,
            "model_id": MODEL,
            "probes": probes,
            "owner": owner,
            "confirmation": confirmation,
            "base_revision": revision,
        }
        body.update(extra or {})
        status, payload, headers_out = self.h.request(
            "POST", "/owner/api/capabilities/run", body, headers=headers
        )
        return status, payload or {}, headers_out

    def _confirmation(self, probes=ALL_PROBES, owner=OWNER):
        status, payload, _ = self._preview(probes=probes, owner=owner)
        self.assertEqual(status, 200, payload)
        return payload["confirmation"], self.h.config.snapshot().revision

    def _revision(self) -> int:
        return self.h.config.snapshot().revision

    # --- gates ---------------------------------------------------------

    def test_preview_and_run_refuse_a_foreign_origin(self):
        headers = {"Origin": "http://evil.example"}
        status, _, _ = self._preview(headers=headers)
        self.assertEqual(status, 403)
        confirmation, revision = self._confirmation()
        status, _, _ = self._run(confirmation, revision, headers=headers)
        self.assertEqual(status, 403)
        self.assertEqual(_UpstreamHandler.requests, [], "a refused run must send nothing")

    def test_an_unregistered_pair_keeps_the_existing_refusal(self):
        status, payload, _ = self._preview(provider="chatgpt_codex", model="gpt-6-luna")
        self.assertEqual(status, 422)
        self.assertFalse(payload["available"])
        self.assertIn("probe protocol", payload["reason"])
        status, payload, _ = self._preview()
        self.assertEqual(status, 200)
        confirmation, revision = payload["confirmation"], self._revision()
        status, payload, _ = self.h.request(
            "POST",
            "/owner/api/capabilities/run",
            {
                "provider_id": "chatgpt_codex",
                "model_id": "gpt-6-luna",
                "probes": ALL_PROBES,
                "owner": OWNER,
                "confirmation": confirmation,
                "base_revision": revision,
            },
        )
        payload = payload or {}
        self.assertEqual(status, 422)
        self.assertEqual(payload["error"]["type"], "no_probe_protocol")
        self.assertEqual(_UpstreamHandler.requests, [])

    def test_priced_but_unregistered_model_reports_the_registry_blocker_and_real_price(self):
        """End-to-end regression for Mark's minimax-m2.7 report (card t_784e39d5).

        The deployed console said "no trustworthy price ... Price: Unknown"
        for a model the catalogue prices. Here the harness stages a real
        catalogue model that carries a fresh tariff but has no protocol
        entry: the preview must refuse with the registry as the sole blocker,
        render the real published tariff, and send nothing upstream.
        """
        with self.h.config.mutate() as config:
            provider = config.providers[PROVIDER]
            provider.models["minimax-m2.7"] = ModelEntry(
                model_id="minimax-m2.7",
                enabled=True,
                dialect="openai_chat",
                source="discovered",
                supports_function_tools=False,
                supports_image_input=False,
            )
        status, payload, _ = self._preview(model="minimax-m2.7", owner=OWNER)
        self.assertEqual(status, 422, payload)
        self.assertFalse(payload["available"])
        self.assertEqual(payload["code"], "no_probe_protocol")
        self.assertNotIn("trustworthy price", payload["reason"])
        # The real catalogue tariff, not "Unknown". The status is whatever
        # freshness the live snapshot warrants (the dated snapshot ages), but
        # a priced model must never collapse to "unknown": the rates and
        # provenance are always rendered, and the refusal never blames price.
        price = payload["price"]
        self.assertIn(price["status"], {"known", "stale"})
        self.assertEqual(price["input_per_m"], 0.3)
        self.assertEqual(price["output_per_m"], 1.2)
        self.assertEqual(price["source"], "https://opencode.ai/data/minimax/minimax-m2-7")
        self.assertEqual(price["as_of"], FRESH_AS_OF)
        self.assertIsNone(price["amount"], "no run estimate exists without a protocol")
        self.assertNotIn("confirmation", payload)
        self.assertEqual(_UpstreamHandler.requests, [], "a refused preview sends nothing")

    def test_registry_coverage_is_reported_on_the_evidence_read(self):
        status, payload, _ = self.h.request(
            "GET",
            f"/owner/api/capabilities?provider_id={PROVIDER}&model_id={MODEL}",
        )
        self.assertEqual(status, 200, payload)
        registry = payload["probe_registry"]
        self.assertEqual(registry["registered_count"], 1)
        self.assertEqual(registry["registered_pairs"], [["opencode_go", "mimo-v2.6-flash"]])
        self.assertGreaterEqual(registry["staged_model_count"], 1)
        self.assertTrue(registry["this_pair_registered"])

    def test_preview_is_unavailable_until_the_owner_selects_the_route(self):
        for owner in (None, {}, {"authorize_billing_route": False}):
            with self.subTest(owner=owner):
                status, payload, _ = self._preview(owner=owner)
                self.assertEqual(status, 422)
                self.assertFalse(payload["available"])
                self.assertEqual(payload["code"], "route_authorization_required")
                self.assertIn("billing route", payload["reason"])
                self.assertNotIn("confirmation", payload)
        status, payload, _ = self._preview(probes=[])
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["type"], "invalid_request")

    def test_preview_states_the_exact_execution_contract(self):
        status, payload, _ = self._preview()
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["available"])
        self.assertEqual(payload["code"], "ready")
        self.assertEqual(payload["maximum_requests"], 2)
        self.assertEqual((payload["concurrency"], payload["retries"], payload["streaming"]), (1, 0, False))
        self.assertEqual(payload["protocol_dialect"], "openai_chat")
        self.assertEqual(payload["price"]["status"], "known")
        self.assertEqual(payload["price"]["source"], "https://opencode.ai/data/xiaomi/mimo-v2-6-flash")
        self.assertEqual(payload["price"]["as_of"], FRESH_AS_OF)
        self.assertGreater(payload["estimate"]["total_usd"], 0)
        self.assertLess(payload["estimate"]["total_usd"], OWNER["max_spend_estimate_usd"])
        self.assertFalse(payload["estimate"]["hard_ceiling_available"])
        self.assertTrue(payload["estimate_is_not_a_bill_guarantee"])
        self.assertEqual(payload["protocol_version"], "opencode_go_bounded_v1")
        self.assertLessEqual(payload["timeout_seconds"], 15)
        self.assertTrue(payload["confirmation"])
        self.assertTrue(payload["confirmation_expires_at"])
        self.assertEqual(_UpstreamHandler.requests, [], "preview must send nothing upstream")

    def test_a_hard_cap_request_is_refused_not_pretended(self):
        owner = dict(OWNER, spend_policy="hard_cap_required")
        status, payload, _ = self._preview(owner=owner)
        self.assertEqual(status, 422)
        self.assertEqual(payload["code"], "hard_cap_unavailable")
        self.assertIn("no provider-enforced", payload["reason"])
        self.assertFalse(payload["estimate"]["hard_ceiling_available"])

    def test_a_raw_nan_ceiling_is_refused_on_preview_and_run(self):
        """SEC-1: `NaN` in the JSON body is not a spend ceiling.

        Python's decoder accepts the non-standard literal, so the raw body is
        sent byte-for-byte (a dict body would be re-encoded and never carry
        `NaN`). Preview must refuse without issuing a confirmation, and the run
        route — which recomputes the preview before it consumes the
        confirmation — must refuse before anything reaches the upstream.
        """
        def raw_body(**overlaps: Any) -> bytes:
            fields = {
                "provider_id": PROVIDER,
                "model_id": MODEL,
                "probes": ["text_generation"],
                "owner": dict(OWNER, max_spend_estimate_usd=0.05),
            }
            fields.update(overlaps)
            text = json.dumps(fields).replace('"max_spend_estimate_usd": 0.05', '"max_spend_estimate_usd": NaN')
            return text.encode("utf-8")

        status, preview_out, _ = self.h.request("POST", "/owner/api/capabilities/preview", raw_body())
        payload = preview_out if isinstance(preview_out, dict) else {}
        self.assertEqual(status, 422, preview_out)
        self.assertFalse(payload["available"])
        self.assertEqual(payload["code"], "spend_ceiling_required")
        self.assertNotIn("confirmation", payload)
        self.assertFalse(payload["estimate"]["hard_ceiling_available"])
        self.assertEqual(_UpstreamHandler.requests, [], "preview must send nothing upstream")

        confirmation, revision = self._confirmation(probes=["text_generation"])
        status, run_out, _ = self.h.request(
            "POST",
            "/owner/api/capabilities/run",
            raw_body(confirmation=confirmation, base_revision=revision),
        )
        error = run_out.get("error", {}) if isinstance(run_out, dict) else {}
        self.assertEqual(status, 422, run_out)
        self.assertEqual(error.get("type"), "spend_ceiling_required")
        self.assertEqual(_UpstreamHandler.requests, [], "a NaN ceiling must never execute a run")

    def test_unknown_or_stale_pricing_fails_closed(self):
        original = self.h.app.probe_price_lookup
        try:
            self.h.app.probe_price_lookup = lambda provider_id, model_id: None
            status, payload, _ = self._preview()
            self.assertEqual(status, 422)
            self.assertEqual(payload["code"], "no_trusted_price")
            self.assertIsNone(payload["estimate"]["total_usd"])
            self.h.app.probe_price_lookup = (
                lambda provider_id, model_id: {
                    "input_per_m": 0.14,
                    "output_per_m": 0.28,
                    "source": "https://opencode.ai/data/xiaomi/mimo-v2-6-flash",
                    "observed_at": STALE_AS_OF,
                }
            )
            status, payload, _ = self._preview()
            self.assertEqual(status, 422)
            self.assertEqual(payload["code"], "no_trusted_price")
            self.assertIn("stale", payload["reason"])
        finally:
            self.h.app.probe_price_lookup = original

    def test_non_finite_catalogue_rates_never_leak_invalid_json_onto_the_wire(self):
        """A non-finite catalogue rate must not make the refusal body unparseable.

        A catalogue carrying an overflowing JSON number (``1e400`` -> ``inf``
        in Python) or a bare ``NaN`` token passes ``isinstance(v, (int,
        float))`` in the tariff block. Serialised with ``allow_nan=True`` (the
        ``json_response`` default) those emit the non-RFC-8259 tokens
        ``Infinity``/``-Infinity``/``NaN``, so the console's ``JSON.parse``
        throws instead of showing the honest ``no_trusted_price`` refusal. The
        poison goes through the DATA layer (``gateway.costs._CATALOG_PATH``),
        so the real ``catalog_price_for`` -> ``price_for`` -> ``published_prices``
        path runs, never the ``probe_price_lookup`` seam. The RAW body must be
        strict-JSON and the run must still fail closed.
        """
        from gateway import costs

        def _catalogue(input_rate, cached_rate):
            return {
                "models": {
                    MODEL: {
                        "price_input_per_m": input_rate,
                        "price_output_per_m": 1.2,
                        "price_cached_read_per_m": cached_rate,
                        "source": "https://opencode.ai/data/xiaomi/mimo-v2-6-flash",
                        "observed_at": FRESH_AS_OF,
                    }
                }
            }

        # A non-finite *input* rate makes price_status() report "unknown", so
        # the tariff gate refuses deterministically -- the poisoned rates are
        # exactly the ones the tariff block would otherwise serialise. The
        # overflow literal is standards-valid JSON Python reads as inf; the NaN
        # variant writes the bare token Python's own json.loads accepts. Each
        # case names the rate keys poisoned (must be omitted from the block)
        # and the finite key that must survive untouched.
        variants = {
            "input_and_cached_overflow": (
                _catalogue(1e400, 1e400),
                ("input_per_m", "cached_read_per_m"),
                {"output_per_m": 1.2},
            ),
            "input_nan_literal": (
                _catalogue(float("nan"), 0.3),
                ("input_per_m",),
                {"output_per_m": 1.2, "cached_read_per_m": 0.3},
            ),
        }
        original = costs._CATALOG_PATH
        try:
            for name, (catalogue, poisoned_keys, finite_rates) in variants.items():
                with self.subTest(poison=name), tempfile.TemporaryDirectory() as tmp:
                    poisoned = Path(tmp) / "go_catalog.json"
                    poisoned.write_text(json.dumps(catalogue), encoding="utf-8")
                    costs._CATALOG_PATH = poisoned
                    status, raw, _ = self.h.request(
                        "POST",
                        "/owner/api/capabilities/preview",
                        {"provider_id": PROVIDER, "model_id": MODEL,
                         "probes": ALL_PROBES, "owner": OWNER},
                        raw=True,
                    )
                    self.assertEqual(status, 422)
                    assert isinstance(raw, bytes), raw
                    body = raw.decode("utf-8")
                    parsed = _strict_json_loads(body)
                    self.assertFalse(parsed["available"])
                    self.assertEqual(parsed["code"], "no_trusted_price")
                    price = parsed["price"]
                    for key in poisoned_keys:
                        self.assertNotIn(key, price)
                    for key, value in finite_rates.items():
                        self.assertEqual(price[key], value)
                    self.assertNotIn("confirmation", parsed)
                    self.assertEqual(
                        _UpstreamHandler.requests, [],
                        "a refused preview sends nothing upstream",
                    )
        finally:
            costs._CATALOG_PATH = original

    def test_an_overflowing_provider_cost_never_reaches_the_run_body(self):
        """R66 F-1: a provider's overflowing `usage.cost` must stay strict JSON.

        The upstream payload carries the overflowing literal ``1e400``,
        which Python decodes as ``inf``; the old ``_reported_cost`` accepted
        it (``inf >= 0``) and ``json_response`` (``allow_nan=True``) emitted
        the non-RFC-8259 token ``Infinity`` — so the console's
        ``JSON.parse`` threw *after a paid run had executed and persisted*,
        the owner saw a generic failure, and a re-run meant duplicate
        spend. The raw 200 body must parse strictly, must carry no
        accounted cost, and must not claim usable cost accounting.
        """
        _UpstreamHandler.usage = {"prompt_tokens": 120, "completion_tokens": 40, "cost": 1e400}
        probes = ["text_generation"]
        confirmation, revision = self._confirmation(probes=probes)
        status, raw, _ = self.h.request(
            "POST",
            "/owner/api/capabilities/run",
            {
                "provider_id": PROVIDER,
                "model_id": MODEL,
                "probes": probes,
                "owner": OWNER,
                "confirmation": confirmation,
                "base_revision": revision,
            },
            raw=True,
        )
        self.assertEqual(status, 200)
        assert isinstance(raw, bytes), raw
        body = raw.decode("utf-8")
        self.assertNotIn("Infinity", body)
        parsed = _strict_json_loads(body)  # raises on Infinity/NaN/-Infinity
        self.assertIsNone(parsed["reported_cost_usd"], "an unusable cost is not accounted cost")
        statuses = sorted({row["status"] for row in parsed["results"]})
        self.assertEqual(statuses, ["inconclusive"], statuses)
        self.assertEqual(parsed["blocked_reason"], "cost_accounting")

    def test_a_nan_provider_cost_yields_inconclusive_results(self):
        """R66 F-2: a `NaN` provider `usage.cost` is not cost accounting.

        ``NaN < 0`` is ``False``, so the old ``cost_accounting_ok`` returned
        ``True`` and the run recorded ``observed_supported`` evidence — a
        claim the scope's rule says must be inconclusive. The upstream sends
        the bare ``NaN`` token (valid to Python's decoder, not to RFC 8259);
        the run body must stay strict JSON and the result inconclusive.
        """
        _UpstreamHandler.usage = {"prompt_tokens": 120, "completion_tokens": 40, "cost": float("nan")}
        probes = ["text_generation"]
        confirmation, revision = self._confirmation(probes=probes)
        status, raw, _ = self.h.request(
            "POST",
            "/owner/api/capabilities/run",
            {
                "provider_id": PROVIDER,
                "model_id": MODEL,
                "probes": probes,
                "owner": OWNER,
                "confirmation": confirmation,
                "base_revision": revision,
            },
            raw=True,
        )
        self.assertEqual(status, 200)
        assert isinstance(raw, bytes), raw
        parsed = _strict_json_loads(raw.decode("utf-8"))
        statuses = sorted({row["status"] for row in parsed["results"]})
        self.assertEqual(statuses, ["inconclusive"], statuses)
        self.assertNotIn("observed_supported", statuses)
        self.assertIsNone(parsed["reported_cost_usd"])
        self.assertEqual(parsed["blocked_reason"], "cost_accounting")

    def test_an_unrepresentable_cost_total_never_reaches_the_run_body(self):
        """SEC-R66-A: two boundary-accepted costs must not serialise as `Infinity`.

        `_usable_cost` bounds each operand, but `run_suite` accumulated them
        with no total check, so `usage.cost = 1e308` on both responses of a
        two-request run summed to `+inf` and the **200** body carried the
        non-RFC-8259 token `Infinity` — after the paid run had executed and
        persisted, so the console's `JSON.parse` threw and a re-run meant
        duplicate spend. The exact bytes must survive node's `JSON.parse`,
        report no cost, and name the accounting as the honest stop reason;
        a claim made before the overflow is withdrawn, never left standing.
        """
        _UpstreamHandler.usage = {"prompt_tokens": 120, "completion_tokens": 40, "cost": 1e308}
        confirmation, revision = self._confirmation()
        status, raw, _ = self.h.request(
            "POST",
            "/owner/api/capabilities/run",
            {
                "provider_id": PROVIDER,
                "model_id": MODEL,
                "probes": ALL_PROBES,
                "owner": OWNER,
                "confirmation": confirmation,
                "base_revision": revision,
            },
            raw=True,
        )
        self.assertEqual(status, 200)
        assert isinstance(raw, bytes), raw
        self.assertNotIn(b"Infinity", raw)
        parsed = _node_json_parse(raw)  # raises: node's JSON.parse is the console's parser
        self.assertEqual(parsed["reported_cost_type"], "object", "typeof null is 'object'")
        self.assertIsNone(parsed["reported_cost_usd"])
        self.assertEqual(parsed["blocked_reason"], "cost_accounting")
        self.assertEqual(parsed["request_count"], 2)
        self.assertEqual(parsed["statuses"], ["inconclusive"])
        self.assertFalse(parsed["cancelled"])
        # and the Python-side strict reader agrees on the same bytes
        self.assertIsNone(_strict_json_loads(raw.decode("utf-8"))["reported_cost_usd"])

    def test_persisted_evidence_after_an_overflowing_run_stays_clean(self):
        """The money is recorded before the body goes out — the record must be clean.

        The escape this closes is response-body-only, so the stored run
        record after an overflowing run must hold no non-finite value and
        must propose nothing: an unrepresentable total claims no capability
        and leaves the owner with an honest `inconclusive` run.
        """
        _UpstreamHandler.usage = {"prompt_tokens": 120, "completion_tokens": 40, "cost": 1e308}
        confirmation, revision = self._confirmation()
        status, raw, _ = self.h.request(
            "POST",
            "/owner/api/capabilities/run",
            {
                "provider_id": PROVIDER,
                "model_id": MODEL,
                "probes": ALL_PROBES,
                "owner": OWNER,
                "confirmation": confirmation,
                "base_revision": revision,
            },
            raw=True,
        )
        self.assertEqual(status, 200)
        assert isinstance(raw, bytes), raw

        text = Path(self.h.data_dir / "capability_discovery.json").read_text()
        self.assertNotIn("Infinity", text)
        store = _strict_json_loads(text)  # non-RFC-8259 token would raise here
        self.assertEqual(_non_finite_paths(store), [], "the stored record must hold no non-finite value")
        self.assertEqual(len(store["runs"]), 1)
        run = store["runs"][0]
        self.assertEqual(run["request_count"], 2)
        statuses = {row["status"] for row in run["results"]}
        self.assertFalse(statuses & {"observed_supported", "observed_unsupported"}, statuses)
        self.assertEqual(statuses, {"inconclusive"}, statuses)
        self.assertEqual(store["proposals"][0]["status"], "pending")
        self.assertEqual(store["proposals"][0]["fields"], [], "an inconclusive run proposes no capability change")

    def test_a_genuine_cost_total_is_reported_exactly(self):
        """No drift: representable costs still sum plainly, on the wire and in the record.

        Two responses at `0.000002` are `0.000004` — the same arithmetic
        `d81700b` performed — and the run keeps every claim it made.
        """
        _UpstreamHandler.usage = {"prompt_tokens": 120, "completion_tokens": 40, "cost": 0.000002}
        confirmation, revision = self._confirmation()
        status, raw, _ = self.h.request(
            "POST",
            "/owner/api/capabilities/run",
            {
                "provider_id": PROVIDER,
                "model_id": MODEL,
                "probes": ALL_PROBES,
                "owner": OWNER,
                "confirmation": confirmation,
                "base_revision": revision,
            },
            raw=True,
        )
        self.assertEqual(status, 200)
        assert isinstance(raw, bytes), raw
        parsed = _node_json_parse(raw)
        self.assertEqual(parsed["reported_cost_usd"], 0.000004)
        self.assertEqual(parsed["reported_cost_type"], "number")
        self.assertIsNone(parsed["blocked_reason"])
        self.assertEqual(parsed["request_count"], 2)
        self.assertEqual(parsed["statuses"], ["observed_supported"])

    def test_a_refused_huge_token_counter_is_recorded_not_carried(self):
        """SEC-R66-C on the run path: a refused count is a record, not a number.

        A 401-digit `prompt_tokens` is valid JSON a provider may send. The
        exact bytes must survive node's `JSON.parse` (no digit run, no
        `Infinity`), the count must not be claimed — but the run must still
        say the provider *reported* one: `invalid_usage: true` beside the
        nulls, in the body and in the persisted evidence. This is the
        information-loss finding on candidate `d12d980`, where a refused
        counter read exactly like absent usage. Cost accounting was fine, so
        the claims stand: metadata recorded honestly, not an inconclusive run.
        """
        huge = 10 ** 400
        _UpstreamHandler.usage = {"prompt_tokens": huge, "completion_tokens": 40, "cost": 0.000002}
        confirmation, revision = self._confirmation()
        status, raw, _ = self.h.request(
            "POST",
            "/owner/api/capabilities/run",
            {
                "provider_id": PROVIDER,
                "model_id": MODEL,
                "probes": ALL_PROBES,
                "owner": OWNER,
                "confirmation": confirmation,
                "base_revision": revision,
            },
            raw=True,
        )
        self.assertEqual(status, 200)
        assert isinstance(raw, bytes), raw
        self.assertNotIn(b"Infinity", raw)
        self.assertNotIn(str(huge).encode("ascii"), raw, "the refused count must not be carried")
        parsed = _node_json_parse(raw)  # raises: node's JSON.parse is the console's parser
        self.assertEqual(parsed["statuses"], ["observed_supported"])
        self.assertIsNone(parsed["blocked_reason"])
        # The detail rows come from the same bytes under Python's strict reader
        # (`_node_json_parse` returns only the scalar summary fields).
        body = _strict_json_loads(raw.decode("utf-8"))
        for row in body["results"]:
            with self.subTest(capability=row["capability"], surface="body"):
                usage = row["reported_usage"]
                self.assertIsNone(usage["input_tokens"])
                self.assertEqual(usage["output_tokens"], 40)
                self.assertIs(usage["invalid_usage"], True)

        text = Path(self.h.data_dir / "capability_discovery.json").read_text()
        self.assertNotIn(str(huge), text)
        store = _strict_json_loads(text)
        self.assertEqual(_non_finite_paths(store), [])
        for row in store["runs"][0]["results"]:
            with self.subTest(capability=row["capability"], surface="store"):
                usage = row["reported_usage"]
                self.assertIsNone(usage["input_tokens"])
                self.assertEqual(usage["output_tokens"], 40)
                self.assertIs(usage["invalid_usage"], True)

    # --- confirmation --------------------------------------------------

    def test_a_run_needs_the_confirmation_issued_by_its_own_preview(self):
        status, payload, _ = self._run(None, self._revision())
        self.assertEqual(status, 422)
        self.assertEqual(payload["error"]["type"], "confirmation_required")
        status, payload, _ = self._run("made-up-token", self._revision())
        self.assertEqual(status, 422)
        self.assertEqual(payload["error"]["type"], "confirmation_invalid")
        self.assertEqual(_UpstreamHandler.requests, [])

        confirmation, revision = self._confirmation(probes=["text_generation"])
        status, payload, _ = self._run(confirmation, revision)
        self.assertEqual(status, 422)
        self.assertEqual(payload["error"]["type"], "confirmation_mismatch")
        self.assertEqual(_UpstreamHandler.requests, [])

    def test_a_confirmation_is_single_use(self):
        confirmation, revision = self._confirmation()
        status, payload, _ = self._run(confirmation, revision)
        self.assertEqual(status, 200, payload)
        status, payload, _ = self._run(confirmation, revision)
        self.assertEqual(status, 422)
        self.assertEqual(payload["error"]["type"], "confirmation_invalid")
        self.assertEqual(len(_UpstreamHandler.requests), 2, "the replay must not send anything")

    def test_a_stale_base_revision_is_refused_before_the_confirmation_is_spent(self):
        confirmation, revision = self._confirmation()
        status, payload, _ = self._run(confirmation, revision + 5)
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["type"], "stale_revision")
        self.assertEqual(_UpstreamHandler.requests, [])
        status, payload, _ = self._run(confirmation, self._revision())
        self.assertEqual(status, 200, payload)
        self.assertEqual(len(_UpstreamHandler.requests), 2)

    # --- the run -------------------------------------------------------

    def test_a_confirmed_run_sends_the_bounded_suite_and_records_evidence(self):
        confirmation, revision = self._confirmation()
        status, payload, _ = self._run(confirmation, revision)
        self.assertEqual(status, 200, payload)
        statuses = {row["capability"]: row["status"] for row in payload["results"]}
        self.assertEqual(statuses, {
            "text_generation": "observed_supported",
            "image_input": "observed_supported",
            "function_tools": "observed_supported",
        })
        self.assertEqual(payload["request_count"], 2)
        self.assertIsNone(payload["blocked_reason"])
        self.assertTrue(payload["run_id"])
        self.assertTrue(payload["proposal_id"])

        self.assertEqual(len(_UpstreamHandler.requests), 2)
        first, second = _UpstreamHandler.requests
        for sent in _UpstreamHandler.requests:
            self.assertEqual(sent["path"], "/openai/v1/chat/completions")
            self.assertEqual(sent["body"]["stream"], False)
            self.assertEqual(sent["headers"]["x-opencode-session"], payload["session_id"])
        self.assertNotIn("tools", first["body"])
        self.assertEqual(second["body"]["tools"][0]["function"]["name"], "probe_visual_answer")
        content = second["body"]["messages"][0]["content"]
        kinds = [part["type"] for part in content]
        self.assertIn("image_url", kinds)
        self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/png;base64,"))

        status, payload, _ = self.h.request(
            "GET", f"/owner/api/capabilities?provider_id={PROVIDER}&model_id={MODEL}"
        )
        assert isinstance(payload, dict), payload
        evidence: dict[str, Any] = payload
        self.assertEqual(status, 200)
        self.assertEqual(len(evidence["runs"]), 1)
        self.assertEqual(len(evidence["runs"][0]["results"]), 3)
        self.assertEqual(evidence["runs"][0]["evidence_method"], "synthetic_inline_probe_v1")
        self.assertEqual(evidence["runs"][0]["protocol_version"], "opencode_go_bounded_v1")
        self.assertEqual(evidence["runs"][0]["trigger"], "owner_console")
        self.assertEqual(evidence["runs"][0]["request_count"], 2)
        self.assertEqual(len(evidence["proposals"]), 1)
        fields = {field["field"]: field["decision"] for field in evidence["proposals"][0]["fields"]}
        self.assertEqual(fields, {"supports_image_input": "pending", "supports_function_tools": "pending"})
        persisted = Path(self.h.data_dir / "capability_discovery.json").read_text()
        self.assertNotIn(LEAKY_TEXT, persisted)
        self.assertNotIn("data:image/png", persisted)
        self.assertNotIn(SYNTHETIC_KEY, persisted)

    def test_the_run_changes_no_config_no_route_and_no_ledger(self):
        config_before = self.h.config.snapshot()
        row_before = config_before.providers[PROVIDER].models[MODEL].to_dict()
        cooldowns_before = self.h.router.circuits.snapshot()
        ledger_before = self.h.router._ledger.recent(limit=10)
        confirmation, revision = self._confirmation()
        status, payload, _ = self._run(confirmation, revision)
        self.assertEqual(status, 200, payload)
        config_after = self.h.config.snapshot()
        self.assertEqual(config_after.revision, revision)
        self.assertEqual(config_after.providers[PROVIDER].models[MODEL].to_dict(), row_before)
        self.assertEqual(self.h.router.circuits.snapshot(), cooldowns_before)
        self.assertEqual(self.h.router._ledger.recent(limit=10), ledger_before)

    def test_an_upstream_error_is_inconclusive_and_never_retried(self):
        for status_code, expected in ((500, "transport"), (503, "transport"), (401, "auth"), (429, "rate_limited")):
            with self.subTest(status=status_code):
                _UpstreamHandler.requests = []
                _UpstreamHandler.status = status_code
                confirmation, revision = self._confirmation()
                status, payload, _ = self._run(confirmation, revision)
                self.assertEqual(status, 200, payload)
                self.assertEqual(payload["blocked_reason"], "prior_failure")
                self.assertEqual(payload["request_count"], 1)
                self.assertEqual(
                    {row["capability"]: row["status"] for row in payload["results"]},
                    {
                        "text_generation": "inconclusive",
                        "image_input": "not_tested",
                        "function_tools": "not_tested",
                    },
                )
                self.assertEqual(payload["results"][0]["error_class"], expected)
                self.assertEqual(len(_UpstreamHandler.requests), 1, "one shot: no retry, no failover")
                _UpstreamHandler.status = 200

    def test_missing_cost_accounting_stops_the_run(self):
        _UpstreamHandler.usage = {"prompt_tokens": 120, "completion_tokens": 40}
        confirmation, revision = self._confirmation()
        status, payload, _ = self._run(confirmation, revision)
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["blocked_reason"], "cost_accounting")
        self.assertEqual(payload["request_count"], 1)
        self.assertEqual(len(_UpstreamHandler.requests), 1)
        by_capability = {row["capability"]: row["status"] for row in payload["results"]}
        self.assertEqual(by_capability["text_generation"], "inconclusive")
        self.assertEqual(by_capability["image_input"], "not_tested")
        self.assertEqual(by_capability["function_tools"], "not_tested")

    def test_a_second_run_is_refused_while_one_is_in_flight(self):
        _UpstreamHandler.delay = 1.5
        # Both confirmations are issued before either run starts: a preview
        # is refused while a run is in flight.
        confirmation, revision = self._confirmation()
        confirmation_2, revision_2 = self._confirmation()
        outcome: dict[str, Any] = {}

        def first() -> None:
            outcome["status"], outcome["payload"], _ = self._run(confirmation, revision)

        worker = threading.Thread(target=first)
        worker.start()
        try:
            deadline = time.time() + 5
            while not self.h.app.probe_gate.active_targets and time.time() < deadline:
                time.sleep(0.05)
            self.assertTrue(self.h.app.probe_gate.active_targets, "the run never became active")
            status, payload, _ = self._preview()
            assert isinstance(payload, dict), payload
            self.assertEqual(status, 422)
            self.assertEqual(payload.get("code"), "probe_run_in_progress")
            status, payload, _ = self._run(confirmation_2, revision_2)
            self.assertEqual(status, 409)
            self.assertEqual(payload["error"]["type"], "probe_run_in_progress")
        finally:
            _UpstreamHandler.delay = 0.0
            worker.join(timeout=10)
        self.assertEqual(outcome["status"], 200, outcome.get("payload"))

    def test_the_owner_can_cancel_a_run_in_flight(self):
        _UpstreamHandler.delay = 2.0
        confirmation, revision = self._confirmation()
        outcome: dict[str, Any] = {}

        def first() -> None:
            outcome["status"], outcome["payload"], _ = self._run(confirmation, revision)

        worker = threading.Thread(target=first)
        worker.start()
        try:
            deadline = time.time() + 5
            while not self.h.app.probe_gate.active_targets and time.time() < deadline:
                time.sleep(0.05)
            self.assertTrue(self.h.app.probe_gate.active_targets)
            status, payload, _ = self.h.request(
                "POST",
                "/owner/api/capabilities/probe/cancel",
                {"provider_id": PROVIDER, "model_id": MODEL},
            )
            payload = payload or {}
            self.assertEqual(status, 200, payload)
            self.assertTrue(payload["cancelled"])
        finally:
            _UpstreamHandler.delay = 0.0
            worker.join(timeout=10)
        self.assertEqual(outcome["status"], 200, outcome.get("payload"))
        self.assertTrue(outcome["payload"]["cancelled"])
        self.assertEqual(outcome["payload"]["blocked_reason"], "cancelled")
        self.assertEqual(len(_UpstreamHandler.requests), 1, "a cancelled run sends nothing more")

    def test_cancel_reports_honestly_when_nothing_is_running(self):
        status, payload, _ = self.h.request(
            "POST",
            "/owner/api/capabilities/probe/cancel",
            {"provider_id": PROVIDER, "model_id": MODEL},
        )
        self.assertEqual(status, 200, payload or {})
        self.assertFalse((payload or {}).get("cancelled"))

    # --- prerequisites -------------------------------------------------

    def test_the_provider_must_be_staged_and_connected(self):
        with self.h.config.mutate() as config:
            config.providers[PROVIDER].models.pop(MODEL)
        status, payload, _ = self._preview()
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["type"], "unknown_model")
        with self.h.config.mutate() as config:
            config.providers[PROVIDER].models[MODEL] = ModelEntry(
                model_id=MODEL, enabled=True, dialect="openai_chat", source="discovered"
            )
            config.providers[PROVIDER].state = "not_configured"
        status, payload, _ = self._preview()
        assert isinstance(payload, dict), payload
        self.assertEqual(status, 422, payload)
        self.assertEqual(payload.get("code"), "provider_not_configured")
        self.assertNotIn("confirmation", payload)
        status, payload, _ = self._run(None, self._revision())
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["type"], "provider_not_configured")
        self.assertEqual(_UpstreamHandler.requests, [])

    def test_an_adapter_without_the_probe_seam_is_refused(self):
        from tests.test_end_to_end import FakeAdapter

        self.h.registry.register(FakeAdapter(provider_id=PROVIDER, dialect="openai_chat"))
        status, payload, _ = self._preview()
        assert isinstance(payload, dict), payload
        self.assertEqual(status, 422, payload)
        self.assertEqual(payload.get("code"), "adapter_not_probe_capable")
        status, payload, _ = self._run(None, self._revision())
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["type"], "adapter_not_probe_capable")
        self.assertEqual(_UpstreamHandler.requests, [])


if __name__ == "__main__":
    unittest.main()
