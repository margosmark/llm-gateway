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
  // Browser-faithful constraint validation for number inputs, so the save
  // guard's checkValidity() call behaves here exactly as it does in a real
  // browser: min/max bounds plus the step grid anchored at `min` (default
  // step 1; step="any" disables the grid). stepMismatch on a valid 0.15 is
  // the review finding this mirrors.
  if (tag === "input") {
    node.checkValidity = function () {
      if (this.value === undefined || this.value === "") return true; // no value, no constraint
      const number = Number(this.value);
      if (!Number.isFinite(number)) return true; // a bad value is reported as no value
      const min = this.attributes.min === undefined ? null : Number(this.attributes.min);
      const max = this.attributes.max === undefined ? null : Number(this.attributes.max);
      if (min !== null && number < min) return false;
      if (max !== null && number > max) return false;
      const step = this.attributes.step === undefined ? 1 : Number(this.attributes.step);
      if (Number.isFinite(step) && step > 0) {
        const base = min === null ? 0 : min;
        const offset = (number - base) / step;
        // Browsers forgive floating-point representation error on the grid.
        if (Math.abs(offset - Math.round(offset)) > 1e-6) return false;
      }
      return true;
    };
  }
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
  assert(source.includes('"Current policy: " + policyText(requestPolicy)'));
});

check("policy save submits explicit policy including deliberate removal and reports server outcome", function () {
  assert(source.includes('text: "Remove request policy"'));
  // Removal is its own branch that nulls the policy: skipping the rebuild
  // while keeping `policy = editing.policy` would resubmit the old block
  // and the server would report "unchanged" (review finding, Medium).
  assert(source.includes("if (removeRequestPolicy.checked) {"));
  assert(source.includes("policy = null;"));
  assert(!source.includes("if (!removeRequestPolicy.checked)"));
  assert(source.includes('policy: policy'));
  assert(source.includes('result && result.policy && result.policy.state'));
  assert(source.includes('" Request policy " + policyState + "."'));
  // A cleared-all rebuild is an explicit clear too: the empty editor must
  // never fall through to the stored policy (review finding, Low).
  assert(source.includes("so the empty rebuild must never fall through to"));
  assert(source.includes("An explicit null is the deliberate clear the"));
  // And a token value that is not plain digits is refused before the wire.
  assert(source.includes("var tokenFields = [defaultTokens, capTokens];"));
  assert(source.includes("!isExactDigits(field.value)"));
  assert(source.includes("no exponent like 1e21"));
});

// --- the alias policy editor, executed against the stub -------------------
// The blocks below are sliced from the real console.js source, so these
// checks exercise the shipped editor and save code, not a hand copy.

const editorSlice = source.slice(
  source.indexOf("var requestPolicy = editing && editing.policy;"),
  source.indexOf("var capTokens =")
);
// The slice starts at production's own prelude - `var policy = editing &&
// editing.policy ? ...` - not a test-only `var policy = null`. A harness
// prelude that differs from production passes while production fails: the
// removal bug shipped exactly there (review finding, Medium).
const rebuildStart = source.indexOf(
  "var policy = editing && editing.policy ? editing.policy : null;"
);
assert(rebuildStart !== -1, "the production policy prelude must be sliceable");
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

function buildPolicyControls(alias) {
  return new Function(
    "editing",
    "el",
    editorSlice +
      "\nreturn { removeRequestPolicy: removeRequestPolicy, defaultTemperature: defaultTemperature," +
      " defaultTokens: defaultTokens, defaultThinking: defaultThinking, defaultSystem: defaultSystem };"
  )(alias, elWithStub);
}

function buildSystemTextarea(alias) {
  return buildPolicyControls(alias).defaultSystem;
}

// Runs the real save-path slice - production's prelude included - against
// the given controls and editing context, exactly as the onclick does.
function rebuildPolicy(controls, editing) {
  return new Function(
    "editing",
    "removeRequestPolicy",
    "defaultTemperature",
    "defaultTokens",
    "defaultThinking",
    "defaultSystem",
    "capTokens",
    "capThinking",
    "denyTools",
    "denyStore",
    "policyInt",
    rebuildSlice + "\nreturn policy;"
  )(
    editing,
    controls.removeRequestPolicy,
    controls.defaultTemperature,
    controls.defaultTokens,
    controls.defaultThinking,
    controls.defaultSystem,
    controls.capTokens,
    controls.capThinking,
    controls.denyTools,
    controls.denyStore,
    helpers.policyInt
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
  }, policiedAlias);
  assert.strictEqual(policy.defaults.system, POLICY_SYSTEM);
  assert.strictEqual(policy.defaults.temperature, 0.4);
});

check("ticking remove request policy submits an explicit policy null", function () {
  // Regression: the save path only skipped the rebuild when the box was
  // ticked, so `policy` kept its prelude value - the existing policy
  // object - and the save resubmitted the old block. The server reported
  // "unchanged" and the policy stayed in force: the control was a no-op.
  // The real save-path slice (production prelude included) is run here
  // with remove ticked and a policied alias in the editor.
  const controls = buildPolicyControls(policiedAlias);
  controls.removeRequestPolicy.checked = true;
  // Even with every policy control populated, removal must win.
  controls.defaultTemperature.value = "0.9";
  const policy = rebuildPolicy({
    removeRequestPolicy: controls.removeRequestPolicy,
    defaultTemperature: controls.defaultTemperature,
    defaultTokens: controls.defaultTokens,
    defaultThinking: controls.defaultThinking,
    defaultSystem: controls.defaultSystem,
    capTokens: { value: "16000" },
    capThinking: { value: "high" },
    denyTools: { checked: true },
    denyStore: { value: "false" },
  }, policiedAlias);
  assert.strictEqual(policy, null, "removal must submit policy: null, got " + JSON.stringify(policy));
  // And the wire body carries the explicit null the endpoint needs to clear.
  const body = helpers.emitJson({ action: "save", name: "coding", policy: policy });
  assert(body.includes('"policy":null'), "the wire body must carry policy: null: " + body);
});

check("the save guard refuses a token value that would serialise exponentially", function () {
  // Regression: checkValidity() accepts "1e21" (a valid floating-point
  // number string), the rebuild routed it through Number(), and the wire
  // carried max_output_tokens:1e+21 - which the server's integer rule
  // (config._policy_tokens) reads as a float and drops while reporting
  // the save a success. The identical value typed as digits is stored
  // exactly, so the loss was client-caused and silent.
  assert.strictEqual(helpers.isExactDigits("32000"), true);
  assert.strictEqual(helpers.isExactDigits("9007199254740993"), true, "an unsafe-integer digit string is digits");
  assert.strictEqual(helpers.isExactDigits("1e21"), false);
  assert.strictEqual(helpers.isExactDigits("1.5"), false);
  assert.strictEqual(helpers.isExactDigits(""), false);

  // The real save guard, sliced from console.js. The stub mirrors browser
  // constraint validation, which reports the exponent form as valid -
  // that is exactly why checkValidity() alone was not enough.
  const exponentField = elWithStub("input", { type: "number", min: "1", step: "1", value: "1e21" });
  assert.strictEqual(exponentField.checkValidity(), true, "the browser accepts 1e21 - that is the trap");
  const guardSlice = source.slice(
    source.indexOf("var policyNumericFields ="),
    source.indexOf("var triggers =")
  );
  const guard = new Function(
    "defaultTemperature",
    "defaultTokens",
    "capTokens",
    "toast",
    "isExactDigits",
    guardSlice + '\nreturn "continued";'
  );
  const blank = { value: "", checkValidity: function () { return true; } };
  const toasts = [];
  const refused = guard(
    { value: "", checkValidity: function () { return true; } },
    exponentField,
    blank,
    function (message) { toasts.push(message); },
    helpers.isExactDigits
  );
  assert.strictEqual(refused, undefined, "the exponent form must stop the save: " + toasts.join("; "));
  assert.strictEqual(toasts.length, 1, "the refusal must be visible: " + toasts.join("; "));
  assert(toasts[0].indexOf("plain digits") !== -1, toasts[0]);

  // The same control in digits - the exact value the exponent named -
  // passes the guard, so nothing server-valid is blocked.
  exponentField.value = "1000000000000000000000";
  const continued = guard(
    { value: "", checkValidity: function () { return true; } },
    exponentField,
    blank,
    function (message) { toasts.push(message); },
    helpers.isExactDigits
  );
  assert.strictEqual(continued, "continued", "plain digits must not be refused: " + toasts.join("; "));
});

check("clearing every policy field submits an explicit clear, not the stored policy", function () {
  // Regression: an empty rebuild fell through to `policy = editing.policy`
  // and resubmitted the old block verbatim. The server reported
  // "unchanged" and every rule the owner had just cleared stayed in
  // force while the editor showed nothing - the visible state never
  // reached the wire.
  const controls = buildPolicyControls(policiedAlias);
  const policy = rebuildPolicy({
    removeRequestPolicy: controls.removeRequestPolicy,
    defaultTemperature: { value: "" },
    defaultTokens: { value: "" },
    defaultThinking: { value: "" },
    defaultSystem: { value: "" },
    capTokens: { value: "" },
    capThinking: { value: "" },
    denyTools: { checked: false },
    denyStore: { value: "" },
  }, policiedAlias);
  assert.strictEqual(
    policy,
    null,
    "an empty editor must clear the policy, not resubmit it: " + JSON.stringify(policy)
  );
  const body = helpers.emitJson({ action: "save", name: "coding", policy: policy });
  assert(body.includes('"policy":null'), "the wire must carry the explicit clear: " + body);
  // A policy that still has one rule is not empty: partial clears keep
  // rebuilding the rest rather than clearing everything.
  const partial = rebuildPolicy({
    removeRequestPolicy: controls.removeRequestPolicy,
    defaultTemperature: { value: "" },
    defaultTokens: { value: "" },
    defaultThinking: { value: "" },
    defaultSystem: { value: "" },
    capTokens: { value: "16000" },
    capThinking: { value: "" },
    denyTools: { checked: false },
    denyStore: { value: "" },
  }, policiedAlias);
  assert.strictEqual(partial.caps.max_output_tokens, 16000, JSON.stringify(partial));
});

check("the policy editor never initialises a textarea from the value attribute", function () {
  assert(source.includes('el("textarea", { maxlength: "4000", rows: "3" })'));
  assert(source.includes("defaultSystem.value = policyDefaults.system || \"\";"));
});

check("the temperature control keeps the server's bounds but adds no tighter step grid", function () {
  // Regression: `step: "0.1"` made checkValidity() report stepMismatch for
  // a stored 0.15, which the server accepts (config._policy_temperature:
  // any numeric temperature in [0, 2]). The save handler refuses the whole
  // save on any invalid policy number, so an unrelated edit was blocked.
  assert(source.includes('min: "0", max: "2", step: "any"'));
  assert(!source.includes('step: "0.1"'));
});

check("an unrelated edit keeps a valid non-tenth temperature through guard and rebuild", function () {
  // Executed against the real editor and save code: an alias configured
  // with temperature 0.15 must load into the control, pass the save
  // guard's checkValidity() sweep, and be rebuilt into the submitted
  // policy while another control is edited.
  const controls = buildPolicyControls({
    policy: { defaults: { temperature: 0.15, system: POLICY_SYSTEM } },
  });
  assert.strictEqual(controls.defaultTemperature.value, "0.15");
  assert.strictEqual(controls.defaultTemperature.checkValidity(), true);

  // The real save guard, sliced from console.js and run against the
  // built controls; "continued" means the guard did not refuse the save.
  const guardSlice = source.slice(
    source.indexOf("var policyNumericFields ="),
    source.indexOf("var triggers =")
  );
  const guard = new Function(
    "defaultTemperature",
    "defaultTokens",
    "capTokens",
    "toast",
    "isExactDigits",
    guardSlice + '\nreturn "continued";'
  );
  const toasts = [];
  const outcome = guard(
    controls.defaultTemperature,
    controls.defaultTokens,
    { value: "", checkValidity: function () { return true; } },
    function (message) { toasts.push(message); },
    helpers.isExactDigits
  );
  assert.strictEqual(
    outcome,
    "continued",
    "the save guard refused a valid temperature of 0.15: " + toasts.join("; ")
  );

  // The rebuilt policy still carries 0.15 while another control changes.
  const policy = rebuildPolicy({
    removeRequestPolicy: controls.removeRequestPolicy,
    defaultTemperature: controls.defaultTemperature,
    defaultTokens: controls.defaultTokens,
    defaultThinking: { value: "high" },
    defaultSystem: controls.defaultSystem,
    capTokens: { value: "32000" },
    capThinking: { value: "" },
    denyTools: { checked: false },
    denyStore: { value: "" },
  }, { policy: { defaults: { temperature: 0.15, system: POLICY_SYSTEM } } });
  assert.strictEqual(policy.defaults.temperature, 0.15);
  assert.strictEqual(policy.defaults.thinking_level, "high");
  assert.strictEqual(policy.defaults.system, POLICY_SYSTEM);
});

// --- exact integers through the console (review finding: Low) ------------
// A server-valid token value above Number.MAX_SAFE_INTEGER used to be
// rounded by JSON parsing before the editor saw it, so saving any
// unrelated edit submitted the rounded replacement.

const UNSAFE_TOKEN = "9007199254740993";
const UNSAFE_ROUNDED = "9007199254740992";

check("the console reads responses and writes bodies without rounding integers", function () {
  assert(source.includes("var data = parseJsonLossless(text);"));
  assert(!source.includes("return response.json()"));
  assert(source.includes("body: options.body === undefined ? undefined : emitJson(options.body)"));
  assert(source.includes("defaults.max_output_tokens = policyInt(defaultTokens.value)"));
  assert(source.includes("caps.max_output_tokens = policyInt(capTokens.value)"));
  assert(!source.includes("Number(defaultTokens.value)"));
  assert(!source.includes("Number(capTokens.value)"));
  assert(source.includes("return parseJsonLossless(text)"));
  assert(source.includes("emitJson(result.export, 2)"));
});

check("an unsafe integer survives the response parser digit for digit", function () {
  const parsed = helpers.parseJsonLossless(
    '{"policy":{"defaults":{"max_output_tokens":' + UNSAFE_TOKEN +
      ',"temperature":0.15},"caps":{"max_output_tokens":32000}}}'
  );
  assert.strictEqual(parsed.policy.defaults.max_output_tokens, UNSAFE_TOKEN);
  assert.strictEqual(parsed.policy.caps.max_output_tokens, 32000); // safe integers stay numbers
  assert.strictEqual(parsed.policy.defaults.temperature, 0.15);
});

check("string literals that contain digits are never rewritten", function () {
  const parsed = helpers.parseJsonLossless(
    '{"system":"' + UNSAFE_TOKEN + '","note":"tokens: ' + UNSAFE_TOKEN +
      ' ok","esc":"say \\"' + UNSAFE_TOKEN + '\\""}'
  );
  assert.strictEqual(typeof parsed.system, "string");
  assert.strictEqual(parsed.system, UNSAFE_TOKEN);
  assert.strictEqual(parsed.note, "tokens: " + UNSAFE_TOKEN + " ok");
  assert.strictEqual(parsed.esc, 'say "' + UNSAFE_TOKEN + '"');
});

check("an unrelated edit resubmits an unsafe token policy with every digit", function () {
  // Load through the real parser, drive the real editor and save guard,
  // rebuild the policy while another control changes, and inspect the
  // exact bytes that leave the console.
  const loaded = helpers.parseJsonLossless(
    '{"policy":{"defaults":{"max_output_tokens":' + UNSAFE_TOKEN +
      ',"system":"' + POLICY_SYSTEM + '"},"caps":{"max_output_tokens":' + UNSAFE_TOKEN + "}}}"
  ).policy;
  const controls = buildPolicyControls({ policy: loaded });
  assert.strictEqual(controls.defaultTokens.value, UNSAFE_TOKEN);
  assert.strictEqual(controls.defaultTokens.checkValidity(), true);

  const guardSlice = source.slice(
    source.indexOf("var policyNumericFields ="),
    source.indexOf("var triggers =")
  );
  const guard = new Function(
    "defaultTemperature",
    "defaultTokens",
    "capTokens",
    "toast",
    "isExactDigits",
    guardSlice + '\nreturn "continued";'
  );
  const toasts = [];
  const outcome = guard(
    controls.defaultTemperature,
    controls.defaultTokens,
    { value: UNSAFE_TOKEN, checkValidity: function () { return true; } },
    function (message) { toasts.push(message); },
    helpers.isExactDigits
  );
  assert.strictEqual(outcome, "continued", "the save guard refused a valid token value: " + toasts.join("; "));

  const policy = rebuildPolicy({
    removeRequestPolicy: controls.removeRequestPolicy,
    defaultTemperature: { value: "" },
    defaultTokens: controls.defaultTokens,
    defaultThinking: { value: "high" },
    defaultSystem: controls.defaultSystem,
    capTokens: { value: UNSAFE_TOKEN },
    capThinking: { value: "" },
    denyTools: { checked: false },
    denyStore: { value: "" },
  }, { policy: loaded });
  assert.strictEqual(policy.defaults.system, POLICY_SYSTEM, "an unrelated edit must not drop other rules");
  assert.strictEqual(policy.defaults.thinking_level, "high");

  const body = helpers.emitJson({ action: "save", name: "coding", policy: policy });
  assert(body.includes('"max_output_tokens":' + UNSAFE_TOKEN), "the exact digits must be on the wire: " + body);
  assert.strictEqual(body.includes(UNSAFE_ROUNDED), false, "a rounded replacement was submitted: " + body);
  assert(JSON.parse(body).name === "coding"); // still a well-formed request body
});

check("serialization keeps digit strings that really are text as strings", function () {
  assert.strictEqual(
    helpers.emitJson({ max_output_tokens: UNSAFE_TOKEN }),
    '{"max_output_tokens":' + UNSAFE_TOKEN + "}"
  );
  assert.strictEqual(helpers.emitJson({ system: UNSAFE_TOKEN }), '{"system":"' + UNSAFE_TOKEN + '"}');
  assert.strictEqual(helpers.emitJson({ name: "123" }), '{"name":"123"}');
  assert.strictEqual(helpers.policyInt("32000"), 32000); // safe values stay plain numbers
  assert.strictEqual(helpers.policyInt("007"), 7);
  // A marker that cannot be spliced is refused rather than sent on.
  assert.throws(
    function () { helpers.emitJson({ max_output_tokens: { __raw_int__: "not-digits" } }); },
    /unspliced/
  );
});

check("text mentioning the marker name serializes; only an unspliced marker is refused", function () {
  // Regression: the refusal used to test the bare substring `__raw_int__`
  // anywhere in the serialized payload, so an alias name or system prompt
  // containing that text made emitJson throw. It runs synchronously inside
  // run(api(...)), so the throw escaped .catch and the save silently
  // no-opped with no toast. The guard is now minted-vs-spliced: only a
  // marker object this file minted counts, and text can neither mint one
  // nor match the splice pattern (string quotes are escaped).
  const withName = helpers.emitJson({ action: "save", name: "a __raw_int__ b" });
  assert(withName.includes('"name":"a __raw_int__ b"'), withName);
  const withPrompt = helpers.emitJson({
    policy: { defaults: { system: "never emit __raw_int__ markers" } },
  });
  assert(withPrompt.includes('"system":"never emit __raw_int__ markers"'), withPrompt);
  // Escaped-quote lookalikes inside text are still text: the payload must
  // serialize and parse back to the exact string, unspliced and unrefused.
  const note = 'text with \\"__raw_int__\\" inside';
  const lookalike = helpers.emitJson({ note: note });
  assert.strictEqual(JSON.parse(lookalike).note, note, lookalike);
  // And a real marker still splices to exact digits while a stray
  // marker-shaped object under a non-integer key counts and splices.
  assert.strictEqual(
    helpers.emitJson({ policy: { defaults: { max_output_tokens: { __raw_int__: UNSAFE_TOKEN } } } }),
    '{"policy":{"defaults":{"max_output_tokens":' + UNSAFE_TOKEN + "}}}"
  );
  // Pretty-printed bodies (backup export) splice too.
  assert.strictEqual(
    helpers.emitJson({ max_output_tokens: { __raw_int__: UNSAFE_TOKEN } }, 2),
    '{\n  "max_output_tokens": ' + UNSAFE_TOKEN + "\n}"
  );
  // A counted marker left unspliced anywhere in the tree is still refused.
  assert.throws(
    function () { helpers.emitJson({ policy: { caps: { max_output_tokens: { __raw_int__: "12x" } } } }); },
    /unspliced/
  );
});

check("the policy summary prints exact token integers, not quotes or rounded digits", function () {
  const policy = helpers.parseJsonLossless(
    '{"defaults":{"max_output_tokens":' + UNSAFE_TOKEN + ',"temperature":0.15,"system":"' +
      POLICY_SYSTEM + '"}}'
  );
  const text = helpers.policyText(policy);
  assert(text.includes('"max_output_tokens": ' + UNSAFE_TOKEN), text);
  assert.strictEqual(text.includes(UNSAFE_ROUNDED), false, text);
  assert.strictEqual(text.includes('"max_output_tokens": "' + UNSAFE_TOKEN + '"'), false, text);
  assert(text.includes('"temperature": 0.15'));
  assert(text.includes('"system": "' + POLICY_SYSTEM + '"'));
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
