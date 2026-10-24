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
  const node = {
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
      // Browser asymmetry: the `value` content attribute is an element's
      // DEFAULT value. An <input> reflects it into .value; a <textarea>'s
      // editable text comes from its child text, so setAttribute("value")
      // never populates it (verified in a real browser - the review
      // finding that motivated this).
      if (key === "value" && tag !== "textarea" && this.value === undefined) {
        this.value = String(value);
      }
    },
    removeAttribute(key) {
      delete this.attributes[key];
    },
    addEventListener(name, handler) {
      this.listeners[name] = handler;
    },
  };
  // A textarea's editable text starts empty, independent of attributes.
  if (tag === "textarea") node.value = "";
  return node;
}

// The helper builds real DOM nodes, so exercise it through a tiny stub.
const domStub = {
  createElement(tag) {
    return makeNode(tag);
  },
  createTextNode(text) {
    return { text: text };
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

check("alias request policy editor exposes the canonical defaults, caps, and deny controls", function () {
  assert(source.includes('text: "Request policy"'));
  assert(source.includes('text: "Default temperature (0–2)"'));
  assert(source.includes('text: "Default max output tokens"'));
  assert(source.includes('text: "Default thinking level"'));
  assert(source.includes('text: "Default system instruction (up to 4000 characters)"'));
  assert(source.includes('text: "Cap max output tokens"'));
  assert(source.includes('text: "Cap thinking level"'));
  assert(source.includes('text: "Deny requests with tools"'));
  assert(source.includes('text: "Force store"'));
  assert(source.includes('["none", "minimal", "low", "medium", "high", "xhigh", "max"]'));
  assert(source.includes('maxlength: "4000"'));
  assert(source.includes('min: "0", max: "2"'));
  assert(source.includes('"Current policy: " + JSON.stringify(requestPolicy)'));
});

check("policy save submits explicit policy including deliberate removal and reports server outcome", function () {
  assert(source.includes('text: "Remove request policy"'));
  assert(source.includes('if (!removeRequestPolicy.checked)'));
  assert(source.includes('policy: policy'));
  assert(source.includes('result && result.policy && result.policy.state'));
  assert(source.includes('" Request policy " + policyState + "."'));
});

// --- the alias policy editor, executed against the stub -------------------
// The blocks below are sliced from the real console.js source, so these
// checks exercise the shipped editor and save code, not a hand copy.

const editorSlice = source.slice(
  source.indexOf("var requestPolicy = editing && editing.policy;"),
  source.indexOf("var capTokens =")
);
const rebuildStart = source.indexOf("if (!removeRequestPolicy.checked) {");
const rebuildSlice = source.slice(
  rebuildStart,
  source.indexOf("if (!removePhasePolicy.checked && (phaseEnabled.checked || phasePolicy))", rebuildStart)
);

const POLICY_SYSTEM = "You are a careful senior engineer.";
const policiedAlias = {
  policy: {
    defaults: { temperature: 0.1, system: POLICY_SYSTEM },
    caps: { max_output_tokens: 32000 },
    deny: { store: false },
  },
};

function buildSystemTextarea(alias) {
  return new Function("editing", "el", editorSlice + "\nreturn defaultSystem;")(alias, elWithStub);
}

function rebuildPolicy(controls) {
  return new Function(
    "removeRequestPolicy",
    "defaultTemperature",
    "defaultTokens",
    "defaultThinking",
    "defaultSystem",
    "capTokens",
    "capThinking",
    "denyTools",
    "denyStore",
    "var policy = null;\n" + rebuildSlice + "\nreturn policy;"
  )(
    controls.removeRequestPolicy,
    controls.defaultTemperature,
    controls.defaultTokens,
    controls.defaultThinking,
    controls.defaultSystem,
    controls.capTokens,
    controls.capThinking,
    controls.denyTools,
    controls.denyStore
  );
}

check("the policy system textarea is editable, not merely attributed", function () {
  // Regression: the textarea was initialised with a `value` attribute,
  // which does not populate a textarea's editable .value - the editor
  // rendered blank while the JSON summary showed the rule.
  const textarea = buildSystemTextarea(policiedAlias);
  assert.strictEqual(textarea.value, POLICY_SYSTEM);
  assert.strictEqual(
    Object.prototype.hasOwnProperty.call(textarea.attributes, "value"),
    false
  );
});

check("editing another policy control and saving keeps the configured system rule", function () {
  // Regression: with a blank textarea the rebuilt policy omitted `system`,
  // and the save endpoint - correctly replacing the policy with what the
  // editor sent - silently deleted the owner's default instruction.
  const textarea = buildSystemTextarea(policiedAlias);
  const policy = rebuildPolicy({
    removeRequestPolicy: { checked: false },
    defaultTemperature: { value: "0.4" },
    defaultTokens: { value: "" },
    defaultThinking: { value: "" },
    defaultSystem: textarea,
    capTokens: { value: "32000" },
    capThinking: { value: "" },
    denyTools: { checked: false },
    denyStore: { value: "" },
  });
  assert.strictEqual(policy.defaults.system, POLICY_SYSTEM);
  assert.strictEqual(policy.defaults.temperature, 0.4);
});

check("the policy editor never initialises a textarea from the value attribute", function () {
  assert(source.includes('el("textarea", { maxlength: "4000", rows: "3" })'));
  assert(source.includes("defaultSystem.value = policyDefaults.system || \"\";"));
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

if (failures.length) {
  console.error("\n" + failures.length + " check(s) failed");
  process.exit(1);
}
console.log("\nall checks passed");
