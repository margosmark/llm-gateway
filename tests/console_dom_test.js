/* Regression test for the console DOM helper.
 *
 * The bug: boolean attributes were set with setAttribute(key, false), which
 * renders `disabled="false"`. In HTML, `disabled` is a boolean attribute -
 * the mere presence of it disables the element whatever its value. Every
 * button with a `disabled` binding was therefore permanently disabled, so
 * the owner could never enrol a credential.
 *
 * Run: node tests/console_dom_test.js
 */
"use strict";

const path = require("path");
const assert = require("assert");

const consoleJs = path.join(__dirname, "..", "src", "gateway", "static", "console.js");
const helpers = require(consoleJs);
const el = helpers.el;

// A minimal DOM node, enough to observe attribute behaviour.
function makeNode(tag) {
  return {
    tag: tag,
    attributes: {},
    listeners: {},
    children: [],
    textContent: "",
    appendChild(child) {
      this.children.push(child);
    },
    setAttribute(key, value) {
      this.attributes[key] = String(value);
    },
    removeAttribute(key) {
      delete this.attributes[key];
    },
    addEventListener(name, handler) {
      this.listeners[name] = handler;
    },
  };
}

// The helper builds real DOM nodes, so exercise it through a tiny stub.
const domStub = {
  createElement(tag) {
    return makeNode(tag);
  },
  createTextNode(text) {
    return { text: text };
  },
  createDocumentFragment() {
    return makeNode("#fragment");
  },
};
global.document = domStub;

// Re-evaluate the helper against the stub by extracting it from the source.
const fs = require("fs");
const source = fs.readFileSync(consoleJs, "utf8");
const elSource = source.slice(
  source.indexOf("function el(tag, attrs, children)"),
  source.indexOf("function api(path, options)")
);
const elWithStub = new Function("document", elSource + "; return el;")(domStub);

const failures = [];
function check(name, fn) {
  try {
    fn();
    console.log("  ok   " + name);
  } catch (error) {
    failures.push(name + ": " + error.message);
    console.log("  FAIL " + name + " - " + error.message);
  }
}

console.log("console DOM helper");

check("a false boolean attribute is removed, not set to \"false\"", function () {
  const node = elWithStub("button", { disabled: false });
  assert.strictEqual(
    Object.prototype.hasOwnProperty.call(node.attributes, "disabled"),
    false,
    'expected no disabled attribute, got ' + JSON.stringify(node.attributes)
  );
});

check("a true boolean attribute is present", function () {
  const node = elWithStub("button", { disabled: true });
  assert.strictEqual(node.attributes.disabled, "");
});

check("a null attribute is omitted", function () {
  const node = elWithStub("button", { "aria-current": null });
  assert.strictEqual(
    Object.prototype.hasOwnProperty.call(node.attributes, "aria-current"),
    false
  );
});

check("a string attribute is kept", function () {
  const node = elWithStub("button", { type: "button" });
  assert.strictEqual(node.attributes.type, "button");
});

check("an on* attribute becomes a listener, not an attribute", function () {
  const node = elWithStub("button", { onclick: function () {} });
  assert.strictEqual(typeof node.listeners.click, "function");
  assert.strictEqual(
    Object.prototype.hasOwnProperty.call(node.attributes, "onclick"),
    false
  );
});

check("the helper is exported for testing", function () {
  assert.strictEqual(typeof helpers.el, "function");
});

check("empty-state helper renders a semantic live status", function () {
  const node = helpers.emptyState("No discovered models yet", "Refresh the catalogue explicitly.");
  assert.strictEqual(node.className, "empty-state");
  assert.strictEqual(node.attributes.role, "status");
  assert.strictEqual(node.attributes["aria-live"], "polite");
  assert.strictEqual(node.children.length, 2);
  assert.strictEqual(node.children[0].textContent, "No discovered models yet");
  assert.strictEqual(node.children[1].textContent, "Refresh the catalogue explicitly.");
});

check("empty states are conditional on model and alias collections", function () {
  assert(source.includes("state.providers.some(function (provider) { return provider.models.length > 0; })"));
  assert(source.includes("if (!hasDiscoveredModels)"));
  assert(source.includes("if (!state.aliases.length)"));
  assert(source.includes("explicitly choose Refresh catalogue"));
  assert(source.includes("then select Save alias"));
  assert(source.includes("This page does not contact providers automatically."));
});

check("empty states and model table remain mobile-contained", function () {
  const css = fs.readFileSync(path.join(__dirname, "..", "src", "gateway", "static", "console.css"), "utf8");
  assert(css.includes(".empty-state {"));
  assert(css.includes("button:focus-visible"));
  assert(css.includes(".table-wrap { max-width: 100%; overflow-x: auto; }"));
  assert(source.includes('class: "table-wrap"'));
});

check("the direct console does not bootstrap a session or CSRF cookie", function () {
  assert(source.includes("Trusted LAN admin mode — any device that can reach this address can configure and use this gateway."));
  assert(!source.includes("gw_csrf"));
  assert(!source.includes("/api/session"));
});

check("editing an alias reuses its existing route configuration", function () {
  assert(source.includes("var editingAlias = null;"));
  assert(source.includes("text: \"Edit\""));
  assert(source.includes("editingAlias = alias.name; draw();"));
  assert(source.includes("text: editing ? \"Save changes\" : \"Save alias\""));
  assert(source.includes("enabled: editing ? editing.enabled : false"));
  assert(source.includes("text: \"Cancel edit\""));
});

check("phase policy editor has only the three supported phases and enabled-model route selects", function () {
  assert(source.includes('["plan", "implement", "validate"]'));
  assert(source.includes('found.models.filter(function (m) { return m.enabled; })'));
  assert(source.includes('text: "Enable phase routing"'));
  assert(source.includes('text: "Effort (optional)"'));
  assert(source.includes('text: "Use default route"'));
  assert(source.includes('text: "Enable Jev inference"'));
  assert(source.includes('text: "Minimum confidence (0–1)"'));
  assert(source.includes('text: "When uncertain"'));
});

check("removing a phase policy overrides the enabled toggle", function () {
  assert(source.includes("if (!removePhasePolicy.checked && (phaseEnabled.checked || phasePolicy))"));
  assert(source.includes("phase_routing: phaseRouting"));
  assert(source.includes('text: "Remove phase policy"'));
});

check("the console states that a per-phase effort is stored but not applied upstream", function () {
  // R38: the effort round-trips through the config, but the router never
  // applies it. The console must not present it as if it were in effect.
  assert(source.includes("A per-phase effort is stored with the policy"));
  assert(source.includes("the gateway does not apply it to the upstream request"));
});

check("server refusal names the invalid field in the visible error message", function () {
  assert(source.includes('if (serverError && serverError.field) message += " (field: " + serverError.field + ")";'));
  assert(source.includes('toast(error.message || "something went wrong", "bad")'));
});

check("disabled models cannot be selected for phase routes", function () {
  assert(source.includes('found.models.filter(function (m) { return m.enabled; })'));
});

check("phase UI contains no request-content source", function () {
  const phaseEditor = source.slice(source.indexOf("var phasePolicy ="), source.indexOf("// --- profiles"));
  assert(!phaseEditor.includes("messages"));
  assert(!phaseEditor.includes("completion"));
});

// --- the status page against a fixture API payload ----------------------
//
// The analytics cards must render what /owner/api/state actually reports:
// the fallback rate, the labelled reasons, a day selector over the durable
// rollup, and a gap that is drawn as a gap instead of a zero.

function collectText(node, out) {
  out = out || [];
  if (node === null || node === undefined) return out;
  if (typeof node === "string") {
    out.push(node);
    return out;
  }
  if (typeof node !== "object") return out;
  if (node.textContent) out.push(String(node.textContent));
  if (node.text) out.push(String(node.text));
  (node.children || []).forEach(function (child) { collectText(child, out); });
  return out;
}

function findAll(node, predicate, out) {
  out = out || [];
  if (!node || typeof node !== "object") return out;
  if (predicate(node)) out.push(node);
  (node.children || []).forEach(function (child) { findAll(child, predicate, out); });
  return out;
}

function fixtureDay(status, extra) {
  return Object.assign({
    day: "2026-10-21", status: status, requests: null, input_tokens: null,
    output_tokens: null, cached_tokens: null, fallbacks: null, errors: null,
    fallback_rate: null, fallback_reasons: null
  }, extra || {});
}

function fixtureUsage(groups) {
  return groups;
}

const PUBLISHED = "https://opencode.ai/docs/go/";

function fixtureState() {
  return {
    initialized: true,
    revision: 4,
    surface: "owner",
    access_mode: "lan_noauth",
    access_mode_description: "Trusted LAN admin mode.",
    base_url: "http://10.10.11.109:8765",
    published_models: ["coding"],
    release: { service: "personal-llm-gateway", source_revision: "abc1234", source_tree: "tree999", image_revision: "sha256:deadbeef" },
    checklist: [{ step: 1, title: "Connect a provider", done: true }],
    providers: [],
    aliases: [],
    limits: [],
    cooldown_policy: { exhausted_base_seconds: 60, exhausted_max_seconds: 900, probe_timeout_seconds: 5 },
    cooldown_details: {},
    ledger: [],
    ledger_durability: { enabled: true, retention_days: 30, billing_system: false },
    totals: { requests: 8, errors: 1, fallbacks: 3, rate_limited: 0, exhausted: 0 },
    usage_costs: {
      window_hours: 24,
      coverage: { source: "bounded_recent_events", events_considered: 8, requests_without_usage: 2, incomplete: false },
      alias: {
        coding: { requests: 6, input_tokens: 3110, output_tokens: 315, amount_usd: 0.00045, known_amount_usd: 0.00045, known_cost_requests: 5, unknown_cost_requests: 1, unknown_cost_input_tokens: 500, unknown_cost_output_tokens: 100, cached_tokens_unreported: 1, cost_incomplete: true, cost_reason: "no_published_price", source: PUBLISHED, observed_at: "2026-10-22" },
        pm: { requests: 2, input_tokens: 0, output_tokens: 0, amount_usd: null, known_amount_usd: 0.0, known_cost_requests: 0, unknown_cost_requests: 2, unknown_cost_input_tokens: 0, unknown_cost_output_tokens: 0, cost_incomplete: true, cost_reason: "no_published_price", source: null, observed_at: null }
      },
      provider: {
        opencode_go: { requests: 6, input_tokens: 3110, output_tokens: 315, amount_usd: 0.00045, known_amount_usd: 0.00045, known_cost_requests: 5, unknown_cost_requests: 1, unknown_cost_input_tokens: 500, unknown_cost_output_tokens: 100, cached_tokens_unreported: 1, cost_incomplete: true, cost_reason: "missing_cached_tokens", source: PUBLISHED, observed_at: "2026-10-22" },
        chatgpt_codex: { requests: 2, input_tokens: 0, output_tokens: 0, amount_usd: null, known_amount_usd: 0.0, known_cost_requests: 0, unknown_cost_requests: 2, unknown_cost_input_tokens: 0, unknown_cost_output_tokens: 0, cost_incomplete: true, cost_reason: "no_published_price", source: null, observed_at: null }
      },
      model: {}
    },
    analytics: {
      available: true,
      unavailable_reason: null,
      source: "request-ledger.jsonl",
      schema: "daily-rollup/v1",
      generated_at: "2026-10-24T12:00:00Z",
      coverage: { first_row_ts: "2026-10-19T08:00:00Z", incomplete: false, rows_skipped: 0 },
      days: 7,
      window: { from: "2026-10-18", to: "2026-10-24" },
      gaps: ["2026-10-21"],
      fallback: {
        overall: {
          requests: 8, fallbacks: 3, fallback_rate: 0.375,
          fallback_reasons: [
            { reason: "cooldown", count: 2, share: 0.666667 },
            { reason: "route_ineligible", count: 1, share: 0.333333 }
          ]
        },
        alias: { coding: { requests: 6, fallbacks: 3, fallback_rate: 0.5, fallback_reasons: [{ reason: "cooldown", count: 2, share: 0.666667 }, { reason: "route_ineligible", count: 1, share: 0.333333 }] } },
        provider: { opencode_go: { requests: 7, fallbacks: 3, fallback_rate: 0.428571, fallback_reasons: [{ reason: "cooldown", count: 2, share: 0.666667 }, { reason: "route_ineligible", count: 1, share: 0.333333 }] } },
        model: { "claude-haiku-5-5": { requests: 6, fallbacks: 3, fallback_rate: 0.5, fallback_reasons: [{ reason: "cooldown", count: 2, share: 0.666667 }, { reason: "route_ineligible", count: 1, share: 0.333333 }] } }
      },
      series: [
        fixtureDay("outside_coverage", { day: "2026-10-18" }),
        fixtureDay("ok", { day: "2026-10-19", requests: 3, input_tokens: 3000, output_tokens: 300, cached_tokens: 0, fallbacks: 1, errors: 0, fallback_rate: 0.333333, fallback_reasons: [{ reason: "cooldown", count: 1, share: 1 }] }),
        fixtureDay("gap", { day: "2026-10-21" }),
        fixtureDay("open", { day: "2026-10-24", requests: 1, input_tokens: 500, output_tokens: 50, cached_tokens: 0, fallbacks: 0, errors: 0, fallback_rate: 0, fallback_reasons: [] })
      ],
      usage: {
        "2026-10-18": null,
        "2026-10-19": fixtureUsage({
          alias: { coding: { requests: 3, input_tokens: 3000, output_tokens: 300, cached_tokens: 0, fallbacks: 1, amount_usd: 0.00045, cost_reason: null, source: PUBLISHED, observed_at: "2026-10-22" } },
          provider: { opencode_go: { requests: 3, input_tokens: 3000, output_tokens: 300, cached_tokens: 0, fallbacks: 1, amount_usd: 0.00045, cost_reason: null, source: PUBLISHED, observed_at: "2026-10-22" } },
          model: {
            "claude-haiku-5-5": { requests: 2, input_tokens: 3000, output_tokens: 300, cached_tokens: 0, fallbacks: 1, amount_usd: 0.00045, cost_reason: null, source: PUBLISHED, observed_at: "2026-10-22" },
            "beam-free": { requests: 1, input_tokens: 0, output_tokens: 0, cached_tokens: 0, fallbacks: 0, amount_usd: null, cost_reason: "no_published_price", source: null, observed_at: null }
          }
        }),
        "2026-10-21": null,
        "2026-10-24": fixtureUsage({
          alias: { coding: { requests: 1, input_tokens: 500, output_tokens: 50, cached_tokens: 0, fallbacks: 0, amount_usd: 0.00005, cost_reason: null, source: PUBLISHED, observed_at: "2026-10-22" } },
          provider: { opencode_go: { requests: 1, input_tokens: 500, output_tokens: 50, cached_tokens: 0, fallbacks: 0, amount_usd: 0.00005, cost_reason: null, source: PUBLISHED, observed_at: "2026-10-22" } },
          model: { "claude-haiku-5-5": { requests: 1, input_tokens: 500, output_tokens: 50, cached_tokens: 0, fallbacks: 0, amount_usd: 0.00005, cost_reason: null, source: PUBLISHED, observed_at: "2026-10-22" } }
        })
      }
    }
  };
}

const rendered = helpers.renderStatusForTest(fixtureState());
const renderedText = collectText(rendered).join("\n");

check("the fallback analytics card renders the rate and the standing metadata line", function () {
  assert(renderedText.includes("Fallback analytics"));
  assert(renderedText.includes("Metadata only. Prompts, responses, tokens and secrets are never stored."));
  assert(renderedText.includes("37.5%"), "expected the overall fallback rate, got none");
  assert(renderedText.includes("No recorded rows on 2026-10-21 - shown as a gap, never as zero."));
});

check("fallback reasons are labelled and grouped by alias, model and provider", function () {
  assert(renderedText.includes("Local cooldown held the route"));
  assert(renderedText.includes("Primary route not eligible"));
  assert(renderedText.includes("By alias"));
  assert(renderedText.includes("By model"));
  assert(renderedText.includes("By provider"));
  assert(renderedText.includes("claude-haiku-5-5"));
  assert(renderedText.includes("Share of fallbacks"));
});

check("the daily trend draws a gap as a gap, not as a zero", function () {
  const rows = findAll(rendered, function (node) { return node.tag === "tr"; });
  const gapRow = rows.filter(function (row) {
    const text = collectText(row).join(" | ");
    return text.includes("2026-10-21") && text.includes("gap");
  })[0];
  assert(gapRow, "expected a trend row for the gap day");
  const cells = gapRow.children.filter(function (child) { return child.tag === "td"; });
  assert.strictEqual(cells.length, 8, "expected day + 6 values + record");
  assert.strictEqual(cells[1].textContent, "—", "a gap day must not render 0 requests");
  assert.strictEqual(cells[cells.length - 1].textContent, "no data — gap, not zero");
  const openRow = rows.filter(function (row) {
    return collectText(row).join(" | ").includes("2026-10-24");
  })[0];
  assert.strictEqual(openRow.children.filter(function (c) { return c.tag === "td"; })[1].textContent, "1");
});

check("the usage table keeps the live ring as the default window", function () {
  const selects = findAll(rendered, function (node) { return node.tag === "select"; });
  assert.strictEqual(selects.length, 1, "expected exactly one window selector");
  const options = findAll(selects[0], function (node) { return node.tag === "option"; });
  const values = options.map(function (option) { return option.attributes.value; });
  assert.deepStrictEqual(values, ["live", "2026-10-24", "2026-10-21", "2026-10-19", "2026-10-18"]);
  assert.strictEqual(options[0].attributes.selected, "", "the live ring is the default window");
  assert(options[0].textContent.includes("Last 24 hours (live ring)"));
  assert(options.filter(function (o) { return o.attributes.value === "2026-10-21"; })[0].textContent.includes("no data"));
  assert(renderedText.includes("No published actual/list price"), "an unpriced group says the price is not published");
  assert(!renderedText.includes("Unknown price source"), "a priced-by-reason group gets a specific label, not the generic one");
});

check("a mixed priced/unpriced group shows its known subtotal plus unknown coverage", function () {
  const text = collectText(rendered).join("\n");
  assert(text.includes("$0.000450"), "the known subtotal must render as dollars");
  assert(text.includes("known subtotal; 1 of 6 requests unpriced (500 in / 100 out tokens)"),
    "the unknown request/token coverage must be stated beside the subtotal");
  assert(text.includes("no published price and are excluded"), "the exclusion reason is named");
  assert(text.includes("cached-token count missing on some requests"),
    "cached-token incompleteness is explicit");
  assert(text.includes("cached count missing on 1 request(s)"),
    "the rollup's cached completeness marker must reach the console");
  const unknownGroup = text.includes("0 of 2 requests priced; 2 unknown (0 in / 0 out)");
  assert(unknownGroup, "an all-unpriced group states its unknown coverage instead of a bare Unknown");
});

check("limits are labelled as enforced caps, never as catalogue metadata", function () {
  const text = collectText(rendered).join("\n");
  assert(text.includes("No cost limit is configured"), "an empty limits snapshot states that plainly");
  assert(text.includes("reference metadata, not configured budgets"),
    "catalogue monthly_limit_usd is not presented as a cost limit");
  assert(text.includes("Enforceable spend caps"), "the card says what a limit here actually is");
});

check("selecting a rollup day renders that day's priced rows and fallbacks", function () {
  const root = helpers.renderStatusForTest(fixtureState(), "2026-10-19");
  const text = collectText(root).join("\n");
  assert(text.includes("UTC day 2026-10-19"));
  assert(text.includes("$0.000450"), "the priced row must show its estimate");
  assert(text.includes("Unknown"), "the unpriced model stays Unknown");
  assert(text.includes(PUBLISHED));
  const tables = findAll(root, function (node) {
    return node.tag === "table" && collectText(node).includes("Price source / observed");
  });
  assert.strictEqual(tables.length, 3, "expected one table per group: alias, provider, model");
  tables.forEach(function (table) {
    const headers = findAll(table, function (node) { return node.tag === "th"; }).map(function (th) { return th.textContent; });
    assert(["Alias", "Provider", "Model"].includes(headers[0]), "unexpected group " + headers[0]);
    assert.deepStrictEqual(headers.slice(1), [
      "Requests", "Input tokens", "Output tokens", "Fallbacks", "Estimated USD", "Price source / observed"
    ]);
  });
});

check("a day with no rollup rows is stated, not counted as zero", function () {
  const text = collectText(helpers.renderStatusForTest(fixtureState(), "2026-10-21")).join("\n");
  assert(text.includes("No rows recorded for 2026-10-21"));
  assert(text.includes("gap in the record, not zero usage"));
});

check("unavailable analytics are reported instead of rendered as zeros", function () {
  const state = fixtureState();
  state.analytics = { available: false, unavailable_reason: "durable_rollup_disabled", series: [], usage: {}, gaps: [], fallback: {} };
  const root = helpers.renderStatusForTest(state);
  const text = collectText(root).join("\n");
  assert(text.includes("Durable fallback analytics are unavailable"));
  assert(text.includes("request-ledger persistence is off"));
  assert.strictEqual(findAll(root, function (node) { return node.tag === "select"; }).length, 0);
  assert(!text.includes("37.5%"));
});

check("the analytics payload is metadata only", function () {
  assert(!renderedText.includes("req_"));
  assert(!/\bsk-[A-Za-z0-9_\-]{16,}/.test(renderedText));
  assert(!renderedText.includes("messages"));
});

if (failures.length) {
  console.error("\n" + failures.length + " check(s) failed");
  process.exit(1);
}
console.log("\nall checks passed");
