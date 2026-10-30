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

check("capability discovery is per exact model and opt-in", function () {
  assert(source.includes('text: "Discover capabilities"'));
  assert(source.includes('var readUrl = BASE + "/api/capabilities?provider_id="'));
  assert(source.includes('text: "Preview selected probes"'));
  assert(source.includes('text: "Run selected probes"'));
  assert(source.includes('No probes run on page load or catalogue refresh.'));
  assert(source.includes('provider.state !== "connected"'));
});

check("capability preview refuses unknown prices honestly and never runs unavailable probes", function () {
  assert(source.includes('unavailable.reason + " Price: Unknown. No provider request has been sent."'));
  assert(source.includes('"Unavailable; Unknown is not false. No probe was executed."'));
  assert(source.includes('if (preview.available)'));
  assert(source.includes('Price estimates are not guaranteed billing caps.'));
  assert(source.includes('Price estimates are not guaranteed billing caps.'));
});

check("metadata decisions require selected fields, an explicit confirmation, and CAS; no fields preselected", function () {
  assert(source.includes('check.checked = false;'));
  assert(source.includes('text: "Approve selected changes"'));
  assert(source.includes('text: "Reject selected changes"'));
  assert(source.includes('global.confirm("Explicitly " + value'));
  assert(source.includes('expected_revision: expectedRevision'));
  assert(source.includes('expected_fingerprint: fingerprint'));
  assert(source.includes('refresh and review a new diff.'));
});

check("capability evidence and proposal text render through text nodes, not HTML", function () {
  const panel = source.slice(source.indexOf("function renderCapabilityPanel"), source.indexOf("function renderModels()"));
  assert(panel.includes('text: result.capability + ": " + result.status'));
  assert(panel.includes('text: field.field + ": "'));
  assert(!panel.includes("innerHTML"));
  assert(panel.includes("Routing impact unknown; no automatic policy change"));
});

check("trusted-LAN boundary and non-guarantee are explicit in discovery UI", function () {
  assert(source.includes("Any device that can reach this trusted-LAN console is an administrator; there is no individual owner login."));
  assert(source.includes("Probe results are evidence, not guarantees."));
  assert(source.includes("Unknown is not false."));
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

check("the cooldown table renders the reason tied to its own event, with the setting request", function () {
  const view = fixtureState();
  view.cooldown_details = {
    "open-code/model-current": {
      provider: "open-code", model: "model-current", scope: "route",
      error_class: "rate_limited", failures: 1, opened_at: 1792612800, until: 1792612819,
      remaining_seconds: 19, next_probe_at: 1792612819, probe_in_flight: false,
      reason: "rate limited; the route resumes after the provider's window",
      request_id: "request-cooldown-setter", set_at: 1792612800, state: "open"
    }
  };
  const rendered = collectText(helpers.renderSettingsForTest(view)).join("\n");
  assert(rendered.includes("19s"), rendered);
  assert(rendered.includes("rate limited; the route resumes after the provider's window"), rendered);
  assert(rendered.includes("event request-cooldown-setter"), rendered);
  assert(rendered.includes("Local cooldown policy"), rendered);
});

check("the cooldown table has a clear empty state", function () {
  const view = fixtureState();
  view.cooldown_details = {};
  const rendered = collectText(helpers.renderSettingsForTest(view)).join("\n");
  assert(rendered.includes("No local cooldowns are open"), rendered);
});

check("recent outcome note displays skipped-route reasons", function () {
  const view = fixtureState();
  view.ledger = [{
    request_id: "req_1", timestamp: "2026-10-21T12:00:00Z", requested_alias: "coding",
    serving_provider: "opencode_go", serving_model: "model-backup", status: 200,
    fallback_used: true, fallback_reason: "primary route was skipped (cooldown)",
    skipped: [{ provider: "open-code", model: "model-primary", reason: "provider is in a local cooldown" }],
    degradations: []
  }];
  const rendered = collectText(helpers.renderStatusForTest(view)).join("\n");
  assert(rendered.includes("skipped open-code/model-primary (held by local circuit): provider is in a local cooldown"), rendered);
});

check("the usage table keeps the live ring as the default window", function () {
  // The statistics card adds its own window selector; this check is about
  // the usage table's day selector, which is the one offering "live".
  const selects = findAll(rendered, function (node) {
    return node.tag === "select" && findAll(node, function (o) { return o.tag === "option"; })
      .some(function (o) { return o.attributes.value === "live"; });
  });
  assert.strictEqual(selects.length, 1, "expected exactly one usage window selector");
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
  assert.strictEqual(findAll(root, function (node) {
    return node.tag === "select" && findAll(node, function (o) { return o.tag === "option"; })
      .some(function (o) { return o.attributes.value === "live"; });
  }).length, 0);
  assert(!text.includes("37.5%"));
});

check("the analytics payload is metadata only", function () {
  assert(!renderedText.includes("req_"));
  assert(!/\bsk-[A-Za-z0-9_\-]{16,}/.test(renderedText));
  assert(!renderedText.includes("messages"));
});

// --- the cross-provider fallback warning names the selected routes' retention

const retentionSource = source.slice(
  source.indexOf("var RETENTION_WORDS ="),
  source.indexOf("function renderAliasEditor()")
);
const retention = new Function(
  retentionSource +
    "; return { RETENTION_WORDS: RETENTION_WORDS, modelRetentionClass: modelRetentionClass, retentionWarningText: retentionWarningText };"
)();

// The catalogue is the authority for the classes; the test asserts against
// what it actually publishes rather than a copy of it.
const catalog = JSON.parse(
  fs.readFileSync(path.join(__dirname, "..", "data", "go_catalog.json"), "utf8")
);
const ANTHROPIC_MODELS = [
  "claude-haiku-5-5",
  "minimax-m3",
  "qwen3.7-plus",
  "qwen3.8-flash",
  "qwen3.8-max",
];
const catalogState = {
  providers: [
    {
      provider_id: "opencode_go",
      models: ANTHROPIC_MODELS.map((id) => ({
        model_id: id,
        retention_class: catalog.models[id].retention_class,
      })),
    },
  ],
};
const route = (role, model) => ({
  role: role,
  provider: "opencode_go",
  model: model,
  provider_label: "OpenCode Go",
});

check("the warning names the retention class of each route actually selected", function () {
  const text = retention.retentionWarningText(catalogState, [
    route("primary", "claude-haiku-5-5"),
    route("fallback 1", "qwen3.8-flash"),
  ]);
  assert(text.includes("primary claude-haiku-5-5 (OpenCode Go) = 30_days"));
  assert(text.includes("fallback 1 qwen3.8-flash (OpenCode Go) = 0_days"));
  assert(text.includes("These classes differ"));
  assert(text.includes("30-day retention (abuse monitoring)"));
  assert(text.includes("0-day retention"));
  assert(text.includes("actually served"));
});

check("the differing classes are the ones the catalogue publishes", function () {
  // claude-haiku-5-5 is 30 days; the other four Anthropic-dialect models
  // are 0 days - the two classes must both be visible in the warning.
  const classes = ANTHROPIC_MODELS.map((id) =>
    retention.modelRetentionClass(catalogState, "opencode_go", id)
  );
  assert.deepStrictEqual(classes, [
    catalog.models["claude-haiku-5-5"].retention_class,
    catalog.models["minimax-m3"].retention_class,
    catalog.models["qwen3.7-plus"].retention_class,
    catalog.models["qwen3.8-flash"].retention_class,
    catalog.models["qwen3.8-max"].retention_class,
  ]);
  assert.strictEqual(classes[0], "30_days");
  assert.deepStrictEqual(classes.slice(1), ["0_days", "0_days", "0_days", "0_days"]);
  const mixed = retention.retentionWarningText(catalogState, [
    route("primary", "claude-haiku-5-5"),
    route("fallback 1", "minimax-m3"),
  ]);
  assert(mixed.includes("These classes differ"));
});

check("two routes of the same class do not claim the regime changes", function () {
  const text = retention.retentionWarningText(catalogState, [
    route("primary", "qwen3.8-flash"),
    route("fallback 1", "qwen3.8-max"),
  ]);
  assert(text.includes("primary qwen3.8-flash (OpenCode Go) = 0_days"));
  assert(text.includes("fallback 1 qwen3.8-max (OpenCode Go) = 0_days"));
  assert(!text.includes("These classes differ"));
});

check("an unconfigured model reads unknown, never a guess from the provider", function () {
  assert.strictEqual(retention.modelRetentionClass(catalogState, "opencode_go", "no-such-model"), "unknown");
  assert.strictEqual(retention.modelRetentionClass(catalogState, "no_such_provider", "qwen3.8-max"), "unknown");
  const text = retention.retentionWarningText(catalogState, [
    route("primary", "no-such-model"),
    route("fallback 1", "qwen3.8-flash"),
  ]);
  assert(text.includes("primary no-such-model (OpenCode Go) = unknown"));
  assert(text.includes("These classes differ"));
});

check("the fallback warning keeps its generic sentence and adds the classes", function () {
  // The generic cross-provider sentence is untouched; the per-route line is
  // appended under it and is recomputed from the editor's own selections.
  assert(source.includes("It sends a prompt to a different provider, and therefore to a different data-retention regime."));
  assert(source.includes('ANCHOR t_fd37033b "retention-class-warning"'));
  assert(source.includes('retentionSpan.textContent = retentionWarningText(state, selectedRoutes());'));
  assert(source.includes('card.addEventListener("change", renderRetentionWarning);'));
  assert(source.includes("    renderRetentionWarning();"));
  assert(source.includes('role: "primary"'));
});

// --- the leaderboard tab against a fixture payload -----------------------
//
// The leaderboard must render what GET /owner/api/leaderboard actually
// reports, and its three honesty rules are regression-tested here:
//   1. amount_usd null renders as "no published price", never $0.00;
//   2. a day with no recorded rows renders as "no data" in the chart and
//      the matrix — never as a zero bar or a 0-value row;
//   3. null window totals render as absent, not zero.
// The fixture mirrors the shape gateway.leaderboard.build_leaderboard
// emits, including the real gpt-6.1-terra case (507 requests, no price).

const LB_PRICE_SOURCE = "https://opencode.ai/docs/go/";

function lbCost(overrides) {
  return Object.assign({
    amount_usd: null, cost_reason: "no_published_price", cost_incomplete: true,
    source: null, observed_at: null,
    known_cost_requests: 0, unknown_cost_requests: 0,
    unknown_cost_input_tokens: 0, unknown_cost_output_tokens: 0,
    cached_tokens_unreported: 0
  }, overrides || {});
}

function lbValues(requests, input, output, cached, fallbacks, errors) {
  return {
    requests: requests, input_tokens: input, output_tokens: output,
    cached_tokens: cached, fallbacks: fallbacks, errors: errors
  };
}

function fixtureLeaderboard() {
  return {
    available: true, unavailable_reason: null,
    source: "durable", schema: "daily-rollup/v1", generated_at: "2026-10-30T04:00:00Z",
    coverage: { first_row_ts: "2026-10-27T01:00:00Z", incomplete: false },
    days: 7, window: { from: "2026-10-24", to: "2026-10-30" }, timezone: "UTC",
    primary_metric: {
      key: "total_tokens", label: "input + output tokens",
      definition: "input_tokens + output_tokens summed over the window; cached tokens are a subset of input tokens and are never counted a second time. Rank ties break on requests, then provider, then model."
    },
    gaps: ["2026-10-26"],
    totals: {
      requests: 712, input_tokens: 30600000, output_tokens: 1500000, cached_tokens: 50000,
      fallbacks: 11, errors: 2, total_tokens: 32100000, models: 2, models_without_price: 1,
      known_amount_usd: 0.0042, cost_incomplete: true, unknown_cost_requests: 607,
      unknown_cost_input_tokens: 30300000, unknown_cost_output_tokens: 1400000
    },
    ranked: [
      {
        rank: 1, provider: "chatgpt_codex", model: "gpt-6.1-terra",
        requests: 607, input_tokens: 30300000, output_tokens: 1400000, cached_tokens: 0,
        total_tokens: 31700000, share: 0.987539, fallbacks: 2, errors: 0,
        cost: lbCost({
          cost_reason: "no_published_price", unknown_cost_requests: 607,
          unknown_cost_input_tokens: 30300000, unknown_cost_output_tokens: 1400000
        })
      },
      {
        rank: 2, provider: "opencode_go", model: "mimo-v2.6-flash",
        requests: 105, input_tokens: 300000, output_tokens: 100000, cached_tokens: 50000,
        total_tokens: 400000, share: 0.012461, fallbacks: 9, errors: 2,
        cost: lbCost({
          amount_usd: 0.0042, cost_reason: null, known_cost_requests: 105,
          unknown_cost_requests: 0, cost_incomplete: false,
          source: LB_PRICE_SOURCE, observed_at: "2026-10-29"
        })
      }
    ],
    matrix: {
      models: [
        { rank: 1, provider: "chatgpt_codex", model: "gpt-6.1-terra" },
        { rank: 2, provider: "opencode_go", model: "mimo-v2.6-flash" }
      ],
      days: [
        { day: "2026-10-24", status: "outside_coverage" },
        { day: "2026-10-25", status: "outside_coverage" },
        { day: "2026-10-26", status: "gap" },
        { day: "2026-10-27", status: "ok" },
        { day: "2026-10-28", status: "ok" },
        { day: "2026-10-29", status: "ok" },
        { day: "2026-10-30", status: "open" }
      ],
      cells: [
        { day: "2026-10-24", status: "outside_coverage", values: [null, null] },
        { day: "2026-10-25", status: "outside_coverage", values: [null, null] },
        { day: "2026-10-26", status: "gap", values: [null, null] },
        // 10-27: a recorded day where neither model had requests — a
        // measured zero, the one place a 0 is allowed to appear.
        { day: "2026-10-27", status: "ok", values: [lbValues(0, 0, 0, 0, 0, 0), lbValues(0, 0, 0, 0, 0, 0)] },
        { day: "2026-10-28", status: "ok", values: [lbValues(0, 0, 0, 0, 0, 0), lbValues(40, 120000, 40000, 30000, 3, 0)] },
        { day: "2026-10-29", status: "ok", values: [lbValues(100, 5000000, 200000, 0, 0, 0), lbValues(55, 150000, 50000, 20000, 5, 2)] },
        { day: "2026-10-30", status: "open", values: [lbValues(507, 25300000, 1200000, 0, 2, 0), lbValues(10, 30000, 10000, 0, 1, 0)] }
      ]
    }
  };
}

const lbRendered = helpers.renderLeaderboardForTest(fixtureLeaderboard(), fixtureState());
const lbText = collectText(lbRendered).join("\n");

function lbFindTable(root, headerText) {
  return findAll(root, function (node) {
    return node.tag === "table" && collectText(node).some(function (piece) {
      return piece.indexOf(headerText) !== -1;
    });
  })[0];
}

check("the leaderboard tab is wired to its own endpoint, not the state route", function () {
  assert(source.includes('{ id: "leaderboard", label: "Leaderboard" }'));
  assert(source.includes("leaderboard: renderLeaderboard"));
  assert(source.includes('"/api/leaderboard?days="'));
  assert(!source.includes("/api/session"));
});

check("the ranking renders ranks, shares and provider labels", function () {
  const table = lbFindTable(lbRendered, "Tokens in / out / cached");
  assert(table, "expected the ranked table");
  const headers = findAll(table, function (n) { return n.tag === "th"; }).map(function (th) { return th.textContent; });
  assert.deepStrictEqual(headers, [
    "Rank", "Model", "Provider", "Share", "Tokens in / out / cached",
    "Requests", "Fallbacks", "Errors", "Cost"
  ]);
  assert(lbText.includes("#1"));
  assert(lbText.includes("gpt-6.1-terra"));
  assert(lbText.includes("ChatGPT / Codex"), "the provider id must be labelled");
  assert(lbText.includes("98.8%"), "the leader's share must render as a percent");
  assert(lbText.includes("leader"), "rank 1 is marked as the leader");
});

check("an unpriced model renders \"no published price\", never $0.00", function () {
  assert(lbText.includes("no published price"), "the unpriced model must say so");
  assert(lbText.includes("no published actual/list price for this provider and model — never inferred"));
  assert(lbText.includes("0 of 607 requests priced; 607 unknown (30.30M in / 1.40M out)"),
    "the unknown coverage must be stated beside the missing price");
  // $0.00 with no following digits would be a fabricated zero; $0.004200
  // is a real subtotal and must still render. The scan covers every
  // rendered figure (div/td); the methodology's prose sentence that states
  // the rule is a li, not a figure, and is allowed to name it.
  const bareZeros = findAll(lbRendered, function (n) {
    return (n.tag === "div" || n.tag === "td") &&
      typeof n.textContent === "string" && /^\$0\.00(?![0-9])/.test(n.textContent);
  });
  assert.strictEqual(bareZeros.length, 0, "no rendered figure may be a bare $0.00");
  assert(lbText.includes("$0.004200"), "the priced model must show its known subtotal");
  assert(lbText.includes("price source " + LB_PRICE_SOURCE + " · observed 2026-10-29"),
    "the real price source URL and observation date must be cited");
});

check("window totals state the known subtotal with its unpriced coverage", function () {
  assert(lbText.includes("Window totals: 712 requests · 32.10M tokens"));
  assert(lbText.includes("Known cost: $0.004200 — incomplete: 607 request(s) unpriced (30.30M in / 1.40M out tokens)."));
  assert(lbText.includes("1 model(s) have no published price and are never estimated."));
});

check("the matrix renders a no-data day as a span, never as 0-value cells", function () {
  const table = lbFindTable(lbRendered, "Day (UTC)");
  assert(table, "expected the matrix table");
  const rows = findAll(table, function (n) { return n.tag === "tr"; });
  const bodyRows = rows.filter(function (row) {
    return row.children.some(function (child) { return child.tag === "td"; });
  });
  assert.strictEqual(bodyRows.length, 7, "one row per day");
  const gapRow = bodyRows.filter(function (row) {
    return collectText(row).join(" | ").includes("2026-10-26");
  })[0];
  const gapCells = gapRow.children.filter(function (c) { return c.tag === "td"; });
  assert.strictEqual(gapCells.length, 2, "a no-data day has day + one span, never per-model cells");
  assert.strictEqual(gapCells[1].attributes.colspan, "4", "the span covers the record column, both models and the total");
  assert(gapCells[1].textContent.includes("no data — gap in the record, not zero"));
  const outsideRow = bodyRows.filter(function (row) {
    return collectText(row).join(" | ").includes("2026-10-25");
  })[0];
  assert(collectText(outsideRow).join(" | ").includes("no data — before recorded history"));
});

check("the matrix renders a recorded zero day as measured zeros", function () {
  const table = lbFindTable(lbRendered, "Day (UTC)");
  const zeroRow = findAll(table, function (n) { return n.tag === "tr"; }).filter(function (row) {
    return collectText(row).join(" | ").includes("2026-10-27");
  })[0];
  const cells = zeroRow.children.filter(function (c) { return c.tag === "td"; });
  assert.strictEqual(cells.length, 5, "day + record + two models + day total");
  assert.strictEqual(cells[1].textContent, "recorded");
  // Each model cell holds a 0 value div and a 0-request div.
  [2, 3].forEach(function (index) {
    const figures = cells[index].children.filter(function (c) { return c.tag === "div"; });
    assert.deepStrictEqual(figures.map(function (f) { return f.textContent; }), ["0", "0 req"]);
  });
  assert.strictEqual(cells[4].textContent, "0", "the day total is a measured 0");
  // And a day with traffic on only one model still shows the other as 0.
  const partialRow = findAll(table, function (n) { return n.tag === "tr"; }).filter(function (row) {
    return collectText(row).join(" | ").includes("2026-10-28");
  })[0];
  const partialCells = partialRow.children.filter(function (c) { return c.tag === "td"; });
  const bCellFigures = partialCells[2].children.filter(function (c) { return c.tag === "div"; });
  assert.deepStrictEqual(bCellFigures.map(function (f) { return f.textContent; }), ["0", "0 req"]);
  assert.strictEqual(partialCells[4].textContent, "160K", "120000+40000 = 160K day total");
});

check("the chart hatches no-data columns and flat-marks a measured zero", function () {
  const charts = findAll(lbRendered, function (n) { return n.className === "lb-chart"; });
  assert.strictEqual(charts.length, 1, "exactly one stacked chart");
  const columns = charts[0].children;
  assert.strictEqual(columns.length, 7, "one column per day");
  // 10-24 / 10-25 / 10-26: hatched, zero segments.
  [0, 1, 2].forEach(function (index) {
    const area = columns[index].children[0];
    const hatched = area.children.filter(function (c) { return c.className === "lb-nodata"; });
    assert.strictEqual(hatched.length, 1, "column " + index + " must be hatched");
    assert(hatched[0].attributes.title.includes("no data"));
    assert.strictEqual(area.children.filter(function (c) { return c.className === "lb-seg"; }).length, 0);
  });
  // 10-27: a flat measured-zero marker, not hatched, not a bar.
  const zeroArea = columns[3].children[0];
  const zero = zeroArea.children.filter(function (c) { return c.className === "lb-zero"; });
  assert.strictEqual(zero.length, 1, "a recorded zero day gets the zero marker");
  assert.strictEqual(zero[0].textContent, "0");
  assert.strictEqual(zeroArea.children.filter(function (c) { return c.className === "lb-nodata"; }).length, 0);
  // 10-30 (open): stacked segments, one per model with traffic.
  const openArea = columns[6].children[0];
  const segments = openArea.children.filter(function (c) { return c.className === "lb-seg"; });
  assert.strictEqual(segments.length, 2, "both models have traffic on the open day");
  segments.forEach(function (segment) { assert(segment.attributes.title.includes("tokens")); });
  // The column labels name no-data days under the axis.
  assert(columns[2].children[2].textContent, "no data");
  assert.strictEqual(columns[2].children[2].textContent, "no data");
});

check("the chart legend names both models with their rank and provider", function () {
  const legend = findAll(lbRendered, function (n) { return typeof n.className === "string" && n.className.indexOf("lb-legend") === 0; })[0];
  assert(legend, "expected a legend");
  const keys = findAll(legend, function (n) { return n.className === "lb-key"; });
  assert.strictEqual(keys.length, 2);
  const text = collectText(legend).join(" | ");
  assert(text.includes("#1 ChatGPT / Codex → gpt-6.1-terra"));
  assert(text.includes("#2 OpenCode Go → mimo-v2.6-flash"));
});

check("leader vs challenger compares the top two with honest costs", function () {
  assert(lbText.includes("Leader vs challenger"));
  const table = lbFindTable(lbRendered, "Share of window tokens");
  assert(table, "expected the comparison table");
  assert(lbText.includes("#1 gpt-6.1-terra (ChatGPT / Codex)"));
  assert(lbText.includes("#2 mimo-v2.6-flash (OpenCode Go)"));
  assert(lbText.includes("98.75%"), "the leader's share at two decimals");
  assert(lbText.includes("1.25%"), "the challenger's share at two decimals");
  assert(lbText.includes("no published price"), "the unpriced leader keeps its honesty label in the comparison");
});

check("the fallback panel renders the rate, reasons and per-alias rows", function () {
  assert(lbText.includes("Fallback rate"));
  assert(lbText.includes("37.5%"), "the overall fallback rate must reach the leaderboard");
  assert(lbText.includes("Local cooldown held the route"));
  assert(lbText.includes("Primary route not eligible"));
  assert(lbText.includes("By alias"));
  assert(lbText.includes("Metadata only; no prompts, responses, tokens or secrets are stored."));
});

check("an analytics-less state degrades the fallback panel without inventing data", function () {
  const view = fixtureState();
  view.analytics = { available: false, unavailable_reason: "durable_rollup_disabled" };
  const text = collectText(helpers.renderLeaderboardForTest(fixtureLeaderboard(), view)).join("\n");
  assert(text.includes("request-ledger persistence is off"));
  assert(!text.includes("37.5%"));
});

check("the methodology states the window, UTC, token kinds and price origin", function () {
  assert(lbText.includes("Methodology"));
  assert(lbText.includes("Days are UTC boundaries"));
  assert(lbText.includes("Recorded history begins 2026-10-27T01:00:00Z"));
  assert(lbText.includes("The gateway does not tokenize; it reports observed counters."));
  assert(lbText.includes("stronger than list-price estimation"));
  assert(lbText.includes("never as $0.00"), "the methodology must state the cost-honesty rule");
  assert(lbText.includes("A price is never transferred across providers"));
  assert(lbText.includes("no user, session or geo dimensions"));
  assert(lbText.includes("Gaps in this window: 2026-10-26"));
  assert(lbText.includes("Price sources cited in this window"));
  assert(lbText.includes(LB_PRICE_SOURCE + " · observed 2026-10-29"));
});

check("a payload with no priced model cites no price source instead of inventing one", function () {
  const payload = fixtureLeaderboard();
  payload.ranked.forEach(function (entry) { entry.cost.source = null; entry.cost.observed_at = null; });
  const text = collectText(helpers.renderLeaderboardForTest(payload, fixtureState())).join("\n");
  assert(text.includes("No model in this window has a published price, so no price source is cited — and none is invented."));
});

check("null window totals render as absent, never as zero", function () {
  const payload = fixtureLeaderboard();
  payload.totals = {
    requests: null, input_tokens: null, output_tokens: null, cached_tokens: null,
    fallbacks: null, errors: null, total_tokens: null, models: 0, models_without_price: 0,
    known_amount_usd: null, cost_incomplete: null, unknown_cost_requests: null,
    unknown_cost_input_tokens: null, unknown_cost_output_tokens: null
  };
  const text = collectText(helpers.renderLeaderboardForTest(payload, fixtureState())).join("\n");
  assert(text.includes("No recorded rows in this window: totals are absent, not zero."));
  assert(!text.includes("Window totals:"), "absent totals must not be rendered as a totals line");
});

check("an empty window states absence instead of ranking nothing", function () {
  const payload = fixtureLeaderboard();
  payload.ranked = [];
  payload.matrix = { models: [], days: fixtureLeaderboard().matrix.days, cells: [] };
  const root = helpers.renderLeaderboardForTest(payload, fixtureState());
  const text = collectText(root).join("\n");
  assert(text.includes("No models recorded in this window"));
  assert(text.includes("That is absent data, not zero usage."));
  // The fallback panel is state-driven, so its tables may still render;
  // what must be absent are the payload-driven ranking and matrix tables.
  assert.strictEqual(lbFindTable(root, "Tokens in / out / cached"), undefined);
  assert.strictEqual(lbFindTable(root, "Day (UTC)"), undefined);
});

check("an unavailable payload renders an empty state and no tables", function () {
  const root = helpers.renderLeaderboardForTest({
    available: false, unavailable_reason: "durable_rollup_disabled",
    ranked: [], matrix: { models: [], days: [], cells: [] }, totals: {}
  }, fixtureState());
  const text = collectText(root).join("\n");
  assert(text.includes("Leaderboard unavailable: durable_rollup_disabled"));
  assert(text.includes("Missing data is reported, never filled in."));
  assert.strictEqual(findAll(root, function (n) { return n.tag === "table"; }).length, 0);
  assert(!text.includes("$0"));
});

check("the leaderboard payload is metadata only", function () {
  assert(!lbText.includes("req_"));
  assert(!/\bsk-[A-Za-z0-9_\-]{16,}/.test(lbText));
});

if (failures.length) {
  console.error("\n" + failures.length + " check(s) failed");
  process.exit(1);
}
console.log("\nall checks passed");
