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

// A minimal DOM node, enough to observe attribute behaviour. It also models
// the little tree structure the capability row click handler walks: parent
// links, siblings, classList, insertBefore and remove.
function makeNode(tag) {
  const node = {
    tag: tag,
    attributes: {},
    listeners: {},
    children: [],
    parentNode: null,
    _text: "",
    appendChild(child) {
      this.children.push(child);
      if (child && typeof child === "object") child.parentNode = this;
      return child;
    },
    insertBefore(newChild, refChild) {
      if (refChild === null || refChild === undefined) return this.appendChild(newChild);
      const at = this.children.indexOf(refChild);
      if (at === -1) return this.appendChild(newChild);
      this.children.splice(at, 0, newChild);
      if (newChild && typeof newChild === "object") newChild.parentNode = this;
      return newChild;
    },
    remove() {
      if (!this.parentNode) return;
      const at = this.parentNode.children.indexOf(this);
      if (at !== -1) this.parentNode.children.splice(at, 1);
      this.parentNode = null;
    },
    closest(selector) {
      let node = this;
      while (node) {
        if (node.tag === selector) return node;
        node = node.parentNode;
      }
      return null;
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
  // Like the DOM: assigning textContent replaces every child.
  Object.defineProperty(node, "textContent", {
    get() {
      return node._text;
    },
    set(value) {
      node._text = String(value);
      node.children = [];
    },
  });
  // The row handler toggles panels by sibling position; the stub returns the
  // next *element* (text nodes carry no tag) like a browser's nextElementSibling.
  Object.defineProperty(node, "nextElementSibling", {
    get() {
      if (!this.parentNode) return undefined;
      const at = this.parentNode.children.indexOf(this);
      for (let i = at + 1; i < this.parentNode.children.length; i += 1) {
        const sib = this.parentNode.children[i];
        if (sib && sib.tag) return sib;
      }
      return undefined;
    },
  });
  Object.defineProperty(node, "nextSibling", {
    get() {
      if (!this.parentNode) return undefined;
      const at = this.parentNode.children.indexOf(this);
      return at === -1 ? undefined : this.parentNode.children[at + 1];
    },
  });
  Object.defineProperty(node, "classList", {
    get() {
      const classes = String(this.className || "").split(/\s+/).filter(Boolean);
      return { contains: (name) => classes.indexOf(name) !== -1 };
    },
  });
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
  createDocumentFragment() {
    return makeNode("#fragment");
  },
  // toast() looks up its host and falls back to document.body.
  getElementById() {
    return null;
  },
  body: makeNode("body"),
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
  // The refusal line must never hardcode "Price: Unknown": the price block
  // carries whatever the catalogue holds (card t_784e39d5 finding 2).
  assert(source.includes('text: unavailable.reason + " Price: " + probePriceText(unavailable.price) + ". No provider request has been sent."'));
  assert(!source.includes('" Price: Unknown. No provider request has been sent."'));
  assert(source.includes('text: preview.reason + " Price: " + probePriceText(preview.price) + ". No provider request has been sent."'));
  assert(source.includes('"Unavailable; Unknown is not false. No probe was executed."'));
  assert(source.includes('if (preview.available)'));
  assert(source.includes('Price estimates are not guaranteed billing caps.'));
});

check("probePriceText renders a published tariff honestly and only says no published price when there is none", function () {
  const priceFn = source.slice(source.indexOf("function probePriceText"), source.indexOf("function renderCapabilityPanel"));
  assert(priceFn.includes('if (!price || price.status === "unknown" || price.status === undefined) return "no published price"'));
  assert(priceFn.includes('"worst-case estimate "'));
  assert(priceFn.includes('"stale published price"'));
  assert(priceFn.includes("input_per_m"), "known per-million rates must render");
  assert(priceFn.includes("cached_read_per_m"), "the cached-read rate must render when published");
  assert(priceFn.includes("price.source"), "the tariff source must render");
  assert(priceFn.includes("price.as_of"), "the observation date must render");
  // Behaviour, not just strings: the helper is exercised on real payloads.
  const realPrice = { status: "known", amount: null, currency: "USD", input_per_m: 0.3, output_per_m: 1.2, cached_read_per_m: 0.06, source: "https://opencode.ai/data/minimax/minimax-m2-7", as_of: "2026-10-25" };
  const rendered = helpers.probePriceTextForTest(realPrice);
  assert(rendered.indexOf("https://opencode.ai/data/minimax/minimax-m2-7") !== -1, "a priced model must show its source");
  assert(rendered.indexOf("0.3") !== -1 && rendered.indexOf("1.2") !== -1, "a priced model must show its rates");
  assert(rendered.indexOf("no published price") === -1, "a priced model must never read as unpriced");
  assert(helpers.probePriceTextForTest({ status: "unknown" }) === "no published price");
  assert(helpers.probePriceTextForTest(null) === "no published price");
  const stale = helpers.probePriceTextForTest({ status: "stale", input_per_m: 0.14, output_per_m: 0.28, source: "x", as_of: "2026-01-01" });
  assert(stale.indexOf("stale") !== -1 && stale.indexOf("2026-01-01") !== -1, "a stale tariff must say stale with its date");
});

check("the capability panel surfaces probe-registry coverage before any preview", function () {
  assert(source.includes('var registryLine = el("p", { class: "small muted" });'));
  assert(source.includes('"Probe registry: " + registry.registered_count + " pair(s) registered for paid probing"'));
  assert(source.includes('registry.this_pair_registered ? "registered." : "not registered."'));
  assert(source.includes("an opt-in allowlist, so an unregistered pair refuses before any request"));
});

// A synchronous thenable so the panel's fetch chains settle inside a check.
function syncResolve(value) {
  if (value && typeof value.then === "function") return value;
  return {
    then: function (onFulfilled) {
      if (!onFulfilled) return this;
      try { return syncResolve(onFulfilled(value)); } catch (error) { return syncReject(error); }
    },
    catch: function (onRejected) { return this.then(undefined, onRejected); }
  };
}
function syncReject(error) {
  return {
    then: function (onFulfilled, onRejected) {
      if (!onRejected) return this;
      try { return syncResolve(onRejected(error)); } catch (next) { return syncReject(next); }
    },
    catch: function (onRejected) { return this.then(undefined, onRejected); }
  };
}

const AVAILABLE_PREVIEW = {
  available: true, code: "ready",
  provider_id: "opencode_go", model_id: "mimo-v2.6-flash",
  probes: ["text_generation"], suite_version: "mvp-1",
  maximum_requests: 1, max_output_tokens: 128, timeout_seconds: 60,
  concurrency: 1, retries: 0, base_revision: 4, confirmation: "conf-1",
  estimate_is_not_a_bill_guarantee: true, hard_ceiling_available: false,
  reason: "1 bounded synthetic request(s) are ready for explicit confirmation.",
  price: { status: "known", amount: 0.0001, currency: "USD", source: "catalog" }
};

check("the capability panel sends the owner gate fields only from an explicit authorisation, and cancel sends nothing", function () {
  const recorded = [];
  const realFetch = global.fetch;
  const realSetTimeout = global.setTimeout;
  const realConfirm = globalThis.confirm;
  global.setTimeout = function () { return 0; };  // toast cleanup must not outlive the check
  globalThis.confirm = function () { return true; };
  const jsonResponse = (payload) => syncResolve({
    ok: true,
    headers: { get: () => "application/json" },
    json: () => syncResolve(payload),
    text: () => syncResolve(JSON.stringify(payload)),
  });
  global.fetch = function (url, options) {
    options = options || {};
    recorded.push({ url: url, method: options.method || "GET", body: options.body ? JSON.parse(options.body) : null });
    if (url.indexOf("/api/capabilities/run") !== -1) {
      // Avoid the refresh()/draw() full-app path; the run's own .catch toasts.
      return syncReject(new Error("run recorded"));
    }
    if (url.indexOf("/api/capabilities/preview") !== -1) return jsonResponse(AVAILABLE_PREVIEW);
    return jsonResponse({ runs: [], proposals: [], approvals: [] });
  };
  try {
    const panel = domStub.createElement("div");
    helpers.renderCapabilityPanelForTest(
      { provider_id: "opencode_go" },
      { model_id: "mimo-v2.6-flash" },
      panel,
      { can_mutate: true }
    );
    const buttons = () => findAll(panel, (node) => node.tag === "button");
    const buttonText = (text) => buttons().find((b) => b.textContent === text);
    const posts = () => recorded.filter((r) => r.method === "POST");

    // Rendering reads evidence only; nothing was posted and preview is gated.
    assert.strictEqual(recorded.length, 1, "only the evidence GET");
    assert.strictEqual(posts().length, 0);
    assert.strictEqual(buttonText("Preview selected probes").attributes.disabled, "", "preview gated until authorised");

    // A probe is selected, then preview is attempted without authorisation.
    const probeBox = findAll(panel, (node) => node.tag === "input" && node.attributes.type === "checkbox")[0];
    probeBox.checked = true;
    buttonText("Preview selected probes").listeners.click();
    assert.strictEqual(posts().length, 0, "unauthorised preview sends nothing");

    // Cancel at the authorisation step sends nothing.
    buttonText("Cancel").listeners.click();
    assert.strictEqual(posts().length, 0, "cancel sends nothing");
    assert(collectText(panel).join("\n").includes("No billing route authorised. Nothing was sent to the provider."));

    // The prompt is recoverable, and an invalid ceiling is refused
    // client-side without any request.
    buttonText("Authorise a paid probe run").listeners.click();
    const ceilingInput = () => findAll(panel, (node) => node.tag === "input" && node.attributes.type === "number")[0];
    ceilingInput().value = "0";
    buttonText("Authorise billing route").listeners.click();
    ceilingInput().value = "not-a-number";
    buttonText("Authorise billing route").listeners.click();
    assert.strictEqual(posts().length, 0, "an invalid ceiling sends nothing");

    // A valid ceiling authorises in-page and still sends nothing by itself.
    ceilingInput().value = "0.5";
    buttonText("Authorise billing route").listeners.click();
    assert.strictEqual(posts().length, 0, "authorising itself is not a request");
    // The handler flips the live `disabled` property (the stub does not
    // reflect properties back onto attributes the way a browser does).
    assert.strictEqual(buttonText("Preview selected probes").disabled, false, "preview enabled after authorisation");
    assert(collectText(panel).join("\n").includes("Held in this page only"));

    // Preview now sends the exact owner gate fields.
    buttonText("Preview selected probes").listeners.click();
    assert.strictEqual(posts().length, 1, "one preview POST");
    assert.deepStrictEqual(posts()[0].body.owner, {
      authorize_billing_route: true, spend_policy: "estimate_accepted", max_spend_estimate_usd: 0.5
    });

    // The run carries the same owner object plus its confirmation and revision.
    const runButton = buttonText("Run selected probes");
    assert(runButton, "an available preview renders the run button");
    runButton.listeners.click();
    const run = posts()[1];
    assert(run && run.url.indexOf("/api/capabilities/run") !== -1, "one run POST");
    assert.deepStrictEqual(run.body.owner, posts()[0].body.owner, "run re-sends the confirmed owner");
    assert.strictEqual(run.body.confirmation, "conf-1");
    assert.strictEqual(run.body.base_revision, 4);

    // Withdrawing returns the panel to the gated state.
    buttonText("Withdraw authorisation").listeners.click();
    assert.strictEqual(buttonText("Preview selected probes").disabled, true, "preview gated again after withdraw");
  } finally {
    global.fetch = realFetch;
    global.setTimeout = realSetTimeout;
    globalThis.confirm = realConfirm;
  }
});

// --- capability preview placement and feedback --------------------------
// The defect these cover: the preview result used to render into an
// initially-empty div ABOVE the button, so on a fast LAN the whole cycle
// finished before the owner's eye reached it and the control read as inert.

function renderPreviewPanel(fetchImpl) {
  global.fetch = fetchImpl;
  const panel = domStub.createElement("div");
  helpers.renderCapabilityPanelForTest(
    { provider_id: "opencode_go" },
    { model_id: "mimo-v2.6-flash" },
    panel,
    { can_mutate: true }
  );
  return panel;
}

function previewParts(panel) {
  const buttons = findAll(panel, (node) => node.tag === "button");
  return {
    button: buttons.find((b) => b.textContent === "Preview selected probes"),
    output: findAll(panel, (node) => node.className === "capability-preview")[0],
    hint: findAll(panel, (node) => String((node.attributes || {}).id || "").indexOf("capability-preview-hint-") === 0)[0],
  };
}

function selectFirstProbe(panel) {
  const box = findAll(panel, (node) => node.tag === "input" && node.attributes.type === "checkbox")[0];
  box.checked = true;
  if (box.listeners.change) box.listeners.change();
  return box;
}

function authorisePreviewPanel(panel) {
  const ceiling = findAll(panel, (node) => node.tag === "input" && node.attributes.type === "number")[0];
  ceiling.value = "0.1";
  const grant = findAll(panel, (node) => node.tag === "button").find((b) => b.textContent === "Authorise billing route");
  grant.listeners.click();
}

check("the preview output renders below its button, with feedback before the response arrives", function () {
  const realFetch = global.fetch;
  const realSetTimeout = global.setTimeout;
  const realConfirm = globalThis.confirm;
  global.setTimeout = function () { return 0; };  // toast cleanup must not outlive the check
  globalThis.confirm = function () { return true; };
  const jsonResponse = (payload) => syncResolve({
    ok: true,
    headers: { get: () => "application/json" },
    json: () => syncResolve(payload),
    text: () => syncResolve(JSON.stringify(payload)),
  });
  let panel, parts, atRequest;
  global.fetch = function (url) {
    if (url.indexOf("/api/capabilities/preview") !== -1) {
      // fetch() runs synchronously inside the click handler, after the
      // loading state was written and before any response exists: this is
      // the state the owner sees within one frame of clicking.
      atRequest = {
        outputAfterButton: panel.children.indexOf(parts.output) > panel.children.indexOf(parts.button),
        loadingText: collectText(parts.output).join(" "),
        buttonLabel: parts.button.textContent,
        buttonDisabled: parts.button.disabled,
      };
      return jsonResponse(AVAILABLE_PREVIEW);
    }
    return jsonResponse({ runs: [], proposals: [], approvals: [] });
  };
  try {
    panel = renderPreviewPanel(global.fetch);
    parts = previewParts(panel);

    // Before anything is ticked the button is gated and says why.
    assert.strictEqual(parts.button.attributes.disabled, "", "the preview button starts gated");
    assert(
      parts.hint.textContent.indexOf("Select at least one probe") !== -1,
      "a disabled preview button explains itself, got: " + parts.hint.textContent
    );
    assert.strictEqual(parts.button.attributes["aria-describedby"], parts.hint.attributes.id, "the reason line is announced with the button");

    // Ticking a probe moves the reason to the next missing step.
    selectFirstProbe(panel);
    assert(
      parts.hint.textContent.indexOf("Authorise the billing route") !== -1,
      "the reason follows the next missing step, got: " + parts.hint.textContent
    );

    authorisePreviewPanel(panel);
    assert.strictEqual(parts.button.disabled, false, "the button unlocks once authorised with a probe ticked");
    assert.strictEqual(parts.hint.textContent, "", "the reason line clears when the button becomes usable");

    parts.button.listeners.click();

    // Feedback exists before the response, and it is where the click was.
    assert(atRequest, "the preview request was sent");
    assert.strictEqual(atRequest.outputAfterButton, true, "the result region is below the button at click time");
    assert(atRequest.loadingText.indexOf("Loading preview…") !== -1, "loading is visible in the result region, got: " + atRequest.loadingText);
    assert.strictEqual(atRequest.buttonLabel, "Previewing…", "the button reflects the in-flight state");
    assert.strictEqual(atRequest.buttonDisabled, true, "the in-flight button cannot be double-clicked");

    // After the click the result is a later sibling of the button and holds
    // the rendered outcome — never an empty, collapsed region.
    assert(
      panel.children.indexOf(parts.output) > panel.children.indexOf(parts.button),
      "the preview output is a later sibling of the preview button"
    );
    assert.strictEqual(parts.output.children.length > 0, true, "the result region holds content after a successful click");
    const rendered = collectText(parts.output).join("\n");
    assert(rendered.indexOf("Preview available") !== -1, "the outcome is labelled, got: " + rendered);
    assert(rendered.indexOf("Run selected probes") !== -1, "an available preview offers the run button");
    assert.strictEqual(parts.button.textContent, "Preview selected probes", "the button label returns when the request settles");
    assert.strictEqual(parts.button.disabled, false, "the button is usable again after the preview");

    // A region that holds content must never be laid out at zero height.
    const css = fs.readFileSync(path.join(__dirname, "..", "src", "gateway", "static", "console.css"), "utf8");
    assert(
      css.indexOf(".capability-preview:not(:empty)") !== -1,
      "the result region is boxed while it holds content, so it cannot collapse"
    );
  } finally {
    global.fetch = realFetch;
    global.setTimeout = realSetTimeout;
    globalThis.confirm = realConfirm;
  }
});

check("every preview outcome renders a distinct result below the button", function () {
  const realFetch = global.fetch;
  const realSetTimeout = global.setTimeout;
  const realConfirm = globalThis.confirm;
  global.setTimeout = function () { return 0; };
  globalThis.confirm = function () { return true; };
  const jsonResponse = (payload) => syncResolve({
    ok: true,
    headers: { get: () => "application/json" },
    json: () => syncResolve(payload),
    text: () => syncResolve(JSON.stringify(payload)),
  });
  const errorResponse = (payload) => syncResolve({
    ok: false,
    status: 400,
    headers: { get: () => "application/json" },
    json: () => syncResolve(payload),
    text: () => syncResolve(JSON.stringify(payload)),
  });
  function drive(fetchImpl) {
    const panel = renderPreviewPanel(fetchImpl);
    const parts = previewParts(panel);
    selectFirstProbe(panel);
    authorisePreviewPanel(panel);
    parts.button.listeners.click();
    assert(
      panel.children.indexOf(parts.output) > panel.children.indexOf(parts.button),
      "every outcome renders below the button"
    );
    return { parts, text: collectText(parts.output).join("\n"), children: parts.output.children };
  }
  try {
    const available = drive(function (url) {
      if (url.indexOf("/api/capabilities/preview") !== -1) return jsonResponse(AVAILABLE_PREVIEW);
      return jsonResponse({ runs: [], proposals: [], approvals: [] });
    });
    assert(available.text.indexOf("Preview available") !== -1, "available is labelled: " + available.text);
    assert(available.text.indexOf("Run selected probes") !== -1, "available offers the run button");
    assert(available.text.indexOf("No provider request has been sent.") !== -1, "available still states that nothing was sent");

    const unavailablePayload = Object.assign({}, AVAILABLE_PREVIEW, {
      available: false,
      reason: "No published bounds for this probe on this pair.",
    });
    const unavailable = drive(function (url) {
      if (url.indexOf("/api/capabilities/preview") !== -1) return jsonResponse(unavailablePayload);
      return jsonResponse({ runs: [], proposals: [], approvals: [] });
    });
    assert(unavailable.text.indexOf("Preview unavailable") !== -1, "unavailable is labelled: " + unavailable.text);
    assert(unavailable.text.indexOf("Unknown is not false") !== -1, "unavailable keeps the Unknown rule");
    assert(unavailable.text.indexOf("Run selected probes") === -1, "an unavailable preview never offers the run button");

    const refused = drive(function (url) {
      if (url.indexOf("/api/capabilities/preview") !== -1) {
        return errorResponse({ error: { message: "invalid_request", type: "invalid_request" } });
      }
      return jsonResponse({ runs: [], proposals: [], approvals: [] });
    });
    assert(refused.text.indexOf("Preview refused") !== -1, "refused is labelled: " + refused.text);
    assert(refused.text.indexOf("No provider request was sent.") !== -1, "refused states that nothing was sent");
    assert(
      refused.children.some((child) => child.className === "notice bad"),
      "a refusal is rendered as an error, not as a warning"
    );
  } finally {
    global.fetch = realFetch;
    global.setTimeout = realSetTimeout;
    globalThis.confirm = realConfirm;
  }
});

check("the capability panel's authorisation is explicit, honest and never persisted", function () {
  const panel = source.slice(source.indexOf("function renderCapabilityPanel"), source.indexOf("function renderModels()"));
  assert(panel.includes('text: "Authorise a paid probe run"'));
  assert(panel.includes("Probes are paid inference requests"));
  assert(panel.includes("no request-level max-charge control"));
  assert(panel.includes("not a provider-enforced limit"));
  assert(panel.includes("Estimate is not a bill guarantee"));
  assert(panel.includes('spend_policy: "estimate_accepted"'));
  assert(panel.includes("authorize_billing_route: true"));
  assert(panel.includes("max_spend_estimate_usd"));
  assert(panel.includes("Held in this page only"));
  // Nothing may persist the grant: no storage, no cookie, no config write.
  assert(!panel.includes("localStorage"));
  assert(!panel.includes("sessionStorage"));
  assert(!panel.includes("document.cookie"));
});

// The QA repro's shape: two different providers, each with one model, so
// opening both discovery panels back to back (no reload) is exactly the
// sequence that used to leave two paid-run confirmations on the page.
function twoProviderFixture() {
  return {
    initialized: true,
    revision: 4,
    surface: "owner",
    can_mutate: true,
    providers: [
      {
        provider_id: "chatgpt_codex", kind: "chatgpt_codex", state: "connected",
        models: [{ model_id: "codex-auto-review", dialect: "openai_chat", enabled: true, source: "discovered", context_limit: 400000, retention_class: "unknown" }]
      },
      {
        provider_id: "opencode_go", kind: "opencode_go", state: "connected",
        models: [{ model_id: "mimo-v2.6-flash", dialect: "openai_chat", enabled: true, source: "discovered", context_limit: 200000, retention_class: "provider_retains_30_days" }]
      }
    ],
    aliases: [],
    fallback_triggers: ["exhausted", "rate_limited"],
    limits: [],
    ledger: []
  };
}

check("only one capability panel may be open, so a paid-run confirmation always names the row that was opened", function () {
  const realFetch = global.fetch;
  const realSetTimeout = global.setTimeout;
  global.setTimeout = function () { return 0; };  // toast cleanup must not outlive the check
  const jsonResponse = (payload) => syncResolve({
    ok: true,
    headers: { get: () => "application/json" },
    json: () => syncResolve(payload),
    text: () => syncResolve(JSON.stringify(payload)),
  });
  global.fetch = function () { return jsonResponse({ runs: [], proposals: [], approvals: [] }); };
  try {
    const view = twoProviderFixture();
    helpers.withStateForTest(view, function () {
      const fragment = helpers.renderModelsForTest(view);
      const discoverButtons = () => findAll(fragment, (node) => node.tag === "button" && node.textContent === "Discover capabilities");
      const detailRows = () => findAll(fragment, (node) => node.className === "capability-detail-row");
      const authHosts = () => findAll(fragment, (node) => node.className === "capability-authorisation");
      assert.strictEqual(discoverButtons().length, 2, "fixture renders one discover button per model");

      // Row A (chatgpt_codex / codex-auto-review) opens its panel.
      const click = (index) => discoverButtons()[index].listeners.click({ currentTarget: discoverButtons()[index] });
      click(0);
      assert.strictEqual(detailRows().length, 1, "first click opens exactly one panel");
      assert.strictEqual(authHosts().length, 1);
      let confirmation = collectText(authHosts()[0]).join(" ");
      assert(confirmation.includes("chatgpt_codex / codex-auto-review"), "the first panel names its own row");

      // Row B (opencode_go / mimo-v2.6-flash) opened without a reload.
      click(1);
      assert.strictEqual(detailRows().length, 1, "opening a second panel closes the first — never two paid-run confirmations at once");
      assert.strictEqual(authHosts().length, 1, "exactly one authorisation block remains on the page");
      confirmation = collectText(authHosts()[0]).join(" ");
      assert(confirmation.includes("opencode_go / mimo-v2.6-flash"), "the confirmation names the row that was just opened");
      assert(!confirmation.includes("codex-auto-review"), "the stale panel's confirmation is gone, not merely further down the page");
      assert.strictEqual(discoverButtons()[0].attributes["aria-expanded"], "false", "the closed row's toggle is reset");
      assert.strictEqual(discoverButtons()[1].attributes["aria-expanded"], "true", "the open row's toggle is set");

      // Toggle behaviour is unchanged: clicking the open row's button closes it.
      click(1);
      assert.strictEqual(detailRows().length, 0, "clicking the open row's button closes its panel");

      // Reopening row A names row A again — no state leaks between panels.
      click(0);
      assert.strictEqual(detailRows().length, 1);
      confirmation = collectText(authHosts()[0]).join(" ");
      assert(confirmation.includes("chatgpt_codex / codex-auto-review"), "reopening row A names row A again");
    });
  } finally {
    global.fetch = realFetch;
    global.setTimeout = realSetTimeout;
  }
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

check("owner sign-in and the non-guarantee are explicit in discovery UI", function () {
  assert(source.includes("Every signed-in owner can run these probes"));
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

check("the console authenticates before it renders the shell", function () {
  // Bootstrap asks the server whether a session exists and renders
  // first-run setup or sign-in from that answer, never from a guess.
  assert(source.includes('api(BASE + "/api/auth/session")'));
  assert(source.includes("function renderAuthView"));
  assert(source.includes("function submitAuthView"));
  assert(source.includes("Owner sign-in"));
  assert(source.includes("Set up the owner account"));
  // SameSite=Strict is what replaces a CSRF token here: the cookie is
  // never sent cross-site, so no anti-CSRF cookie is needed.
  assert(!source.includes("gw_csrf"));
  assert(!source.includes('"/api/session"'));
});

check("the access card keeps owner auth and the client key apart", function () {
  const access = source.slice(source.indexOf("function renderAccess"), source.indexOf("function buildAuthView"));
  assert(access.includes("Owner account"));
  assert(access.includes("Model client API key"));
  assert(access.includes("Change password"));
  assert(access.includes("Enable client key"));
  assert(access.includes("Rotate key"));
  assert(access.includes("Disable and restore keyless access"));
  assert(access.includes("no client changes silently"));
  // The console never echoes a key back or reads one from a URL.
  assert(!access.includes("location.search"));
  assert(!access.includes("gw_csrf"));
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
    cooldown_policy: { exhausted_base_seconds: 60, exhausted_max_seconds: 900, transient_max_seconds: 3600, probe_timeout_seconds: 5 },
    cooldown_details: {},
    ledger: [],
    ledger_durability: { enabled: true, retention_days: 30, billing_system: false, store: { path: "/var/lib/gateway/request-ledger.db", bytes: 1048576, rows: 12345, requests: 12345, oldest_row_ts: "2026-10-08T05:50:01Z", journal_path: "/var/lib/gateway/request-ledger.jsonl", journal_bytes: 524288, error: null } },
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

check("Operate now renders exactly one read-only health card per published alias before collapsed details", function () {
  const state = fixtureState();
  state.aliases = [
    { name: "coding", enabled: true, primary: { provider: "p", model: "main" }, fallbacks: [{ provider: "q", model: "backup" }], fallback_triggers: ["exhausted", "rate_limited", "upstream_unavailable", "explicit_cooldown", "route_ineligible", "probe_contention"], fallback_authorized: true, fallback_confirmed_providers: ["q"] },
    { name: "hidden", enabled: false, primary: { provider: "p", model: "private" }, fallbacks: [], fallback_triggers: [], fallback_authorized: false, fallback_confirmed_providers: [] }
  ];
  state.fallback_triggers = ["exhausted", "rate_limited", "upstream_unavailable", "explicit_cooldown", "route_ineligible", "probe_contention"];
  state.providers = [{ provider_id: "p", state: "connected" }, { provider_id: "q", state: "connected" }];
  const root = helpers.renderStatusForTest(state);
  const cards = findAll(root, function (node) { return node.tag === "article" && node.className === "route-health-card"; });
  assert.strictEqual(cards.length, 1, "only published alias cards should render");
  const text = collectText(root).join("\n");
  assert(text.indexOf("Operate now · Routing health") < text.indexOf("More status details"));
  assert(text.includes("Primary"));
  assert(text.includes("Fallback 1"));
  assert(text.includes("Fallback 2"));
  assert(text.includes("Exhausted: enabled"));
  assert(text.includes("Probe contention: enabled"));
  assert(text.includes("Healthy"));
  assert.strictEqual(findAll(root, function (node) { return node.tag === "details"; }).length, 1);
});

check("the ledger card states the store's real on-disk figures, not just a retention promise", function () {
  const text = collectText(helpers.renderStatusForTest(fixtureState())).join("\n");
  assert(text.includes("bound by age alone - never by file size"));
  assert(text.includes("On disk: 1.0 MiB store \u00b7 12,345 rows \u00b7 oldest row 2026-10-08T05:50:01Z \u00b7 512 KiB journal tail"));
  assert(!text.includes("bounded rotation"));
  assert(text.includes("an age-based 30-day retention window (no size cap)"));
});

check("a policy gap is explicit and an orphan cooldown cannot affect an alias", function () {
  const state = fixtureState();
  state.aliases = [{ name: "coding", enabled: true, primary: { provider: "p", model: "main" }, fallbacks: [{ provider: "q", model: "backup" }], fallback_triggers: ["exhausted"], fallback_authorized: true, fallback_confirmed_providers: ["q"] }];
  state.fallback_triggers = ["exhausted", "rate_limited", "upstream_unavailable", "explicit_cooldown", "route_ineligible", "probe_contention"];
  state.providers = [{ provider_id: "p", state: "connected" }, { provider_id: "q", state: "connected" }];
  state.cooldown_details = { "other/model": { provider: "other", model: "model", scope: "route", state: "open", armed: true, remaining_seconds: 30 } };
  const text = collectText(helpers.renderStatusForTest(state)).join("\n");
  assert(text.includes("coding · Policy gap"));
  assert(text.includes("Rate limited: disabled"));
  assert(text.includes("No active cooldown on this alias’s routes."));
  assert(!text.includes("other/model"));
});

check("an applicable exact-route cooldown appears only for a configured route", function () {
  const state = fixtureState();
  state.aliases = [{ name: "coding", enabled: true, primary: { provider: "p", model: "main" }, fallbacks: [{ provider: "q", model: "backup" }], fallback_triggers: ["exhausted", "rate_limited", "upstream_unavailable", "explicit_cooldown", "route_ineligible", "probe_contention"], fallback_authorized: true, fallback_confirmed_providers: ["q"] }];
  state.fallback_triggers = ["exhausted", "rate_limited", "upstream_unavailable", "explicit_cooldown", "route_ineligible", "probe_contention"];
  state.providers = [{ provider_id: "p", state: "connected" }, { provider_id: "q", state: "connected" }];
  state.cooldown_details = { "q/backup": { provider: "q", model: "backup", scope: "route", state: "open", armed: true, remaining_seconds: 77 } };
  const text = collectText(helpers.renderStatusForTest(state)).join("\n");
  assert(text.includes("coding · Cooldown"));
  assert(text.includes("q/backup · open · 77s remaining"));
});

// A fresh, fully-healthy alias: the base for the fail-closed staleness and
// missing-payload checks below.
function healthyAliasState() {
  const state = fixtureState();
  const ALL6 = ["exhausted", "rate_limited", "upstream_unavailable", "explicit_cooldown", "route_ineligible", "probe_contention"];
  state.aliases = [{ name: "coding", enabled: true, primary: { provider: "p", model: "main" }, fallbacks: [{ provider: "q", model: "backup" }], fallback_triggers: ALL6, fallback_authorized: true, fallback_confirmed_providers: ["q"] }];
  state.fallback_triggers = ALL6;
  state.providers = [{ provider_id: "p", state: "connected" }, { provider_id: "q", state: "connected" }];
  state.cooldown_details = {};
  return state;
}

check("every routing-health card carries an as-of freshness line", function () {
  const text = collectText(helpers.renderStatusForTest(healthyAliasState())).join("\n");
  assert(/As of \d/.test(text), "expected an as-of timestamp on the card");
  assert(text.includes("refetches when this tab regains focus"),
    "the card must say what keeps it fresh");
});

check("stale-beyond-threshold data never claims Healthy", function () {
  const stale = Date.now() - (helpers.STALE_AFTER_MS + 60000);
  const text = collectText(helpers.renderStatusForTest(healthyAliasState(), undefined, stale)).join("\n");
  assert(text.includes("coding · Stale"), "a stale snapshot must render Stale");
  assert(!text.includes("coding · Healthy"));
  assert(/old — showing the last known state/.test(text), "the stale card must carry the age");
});

check("unavailable refresh never presents stale cached health as Healthy", function () {
  const text = collectText(helpers.renderStatusForTest(healthyAliasState(), undefined, Date.now(), true)).join("\n");
  assert(text.includes("coding · Stale"));
  assert(!text.includes("coding · Healthy"));
  assert(text.includes("refresh unavailable"));
});

check("no successful state fetch is unavailable, not fresh or Healthy", function () {
  const text = collectText(helpers.renderStatusForTest(healthyAliasState(), undefined, null)).join("\n");
  assert(text.includes("coding · Stale"));
  assert(!text.includes("coding · Healthy"));
  assert(text.includes("routing health is unavailable"));
});

check("failed focus refresh preserves the editor and marks health unavailable", function () {
  assert(source.includes("if (options.preserveDraft && state)"));
  assert(source.includes("refreshUnavailable = true;"));
  assert(source.includes("tickFreshness();\n        toast(\"Routing health refresh unavailable:"));
});

check("successful focus refresh updates health without redrawing the alias editor", function () {
  const refresh = source.slice(source.indexOf("function refresh(options)"), source.indexOf("// The DOM helper is exported"));
  assert(refresh.includes("existingHealth.replaceWith(updatedHealth)"));
  assert(refresh.includes("if (options.preserveDraft)"));
  assert(refresh.includes("draw();"), "non-draft refreshes still redraw the console");
});

check("focus refresh preserves new-alias drafts, previews, and open disclosures", function () {
  assert(source.includes("refresh({ preserveDraft: true });"),
    "refresh must preserve current UI even when a new alias has no editingAlias");
  const refresh = source.slice(source.indexOf("function refresh(options)"), source.indexOf("// The DOM helper is exported"));
  assert(refresh.includes("existingHealth.replaceWith(updatedHealth)"),
    "refresh may replace only health content, not editor or disclosure DOM");
  assert(refresh.includes("if (options.preserveDraft && state)"),
    "failed refresh must leave current editor and preview intact");
});

check("freshness ticker runs independently of editing and mutations", function () {
  assert(source.includes("global.setInterval(function () {\n      // Freshness is independent of editing and mutations"));
  assert(source.includes("var stale = refreshUnavailable || ageMs == null || ageMs > STALE_AFTER_MS;"));
});

check("the console refetches when the tab regains focus and records its fetch time", function () {
  assert(typeof helpers.STALE_AFTER_MS === "number" && helpers.STALE_AFTER_MS > 0);
  assert(source.includes("lastFetchAt = Date.now();"), "a successful /api/state fetch must be timestamped");
  assert(source.includes('global.addEventListener("focus", refetchIfIdle)'));
  assert(source.includes('"visibilitychange"'));
  assert(source.includes('visibilityState === "visible"'));
  assert(source.includes("if (busy) return;"));
  assert(source.includes("refresh({ preserveDraft: true });"),
    "focus refresh updates state without replacing an in-progress draft");
  assert(source.includes("existingHealth.replaceWith(updatedHealth)"),
    "only routing health is refreshed while preserving the editor and disclosure DOM");
  assert(source.includes("global.setInterval(function () {\n      // Freshness is independent of editing and mutations"),
    "the independent ticker must continue during editing and mutations");
});

check("missing fallback_triggers renders Unknown, never healthy-from-empty", function () {
  const state = healthyAliasState();
  delete state.fallback_triggers;
  const text = collectText(helpers.renderStatusForTest(state)).join("\n");
  assert(text.includes("coding · Unknown"), "an absent policy list must be Unknown");
  assert(!text.includes("coding · Healthy"));
  assert(text.includes("Exhausted: unknown"), "chips must say unknown, not disabled, when the policy list is missing");
});

check("missing cooldown_details renders Unknown, never no-cooldown", function () {
  const state = healthyAliasState();
  delete state.cooldown_details;
  const text = collectText(helpers.renderStatusForTest(state)).join("\n");
  assert(text.includes("coding · Unknown"), "absent cooldown details must be Unknown");
  assert(text.includes("Unknown — the console state carried no cooldown details"));
  assert(!text.includes("No active cooldown"));
});

check("an unrecognized provider state is Unknown, never a fall-through to Healthy", function () {
  const future = healthyAliasState();
  future.providers[1].state = "state_enum_future_value";
  const text = collectText(helpers.renderStatusForTest(future)).join("\n");
  assert(text.includes("coding · Unknown"), "an allow-list must reject unknown provider states");
  assert(!text.includes("coding · Healthy"));
  const untested = healthyAliasState();
  untested.providers[1].state = "configured_untested";
  const untestedText = collectText(helpers.renderStatusForTest(untested)).join("\n");
  assert(untestedText.includes("coding · Unknown"));
  const hostile = healthyAliasState();
  hostile.providers[0].state = "__proto__";
  const hostileText = collectText(helpers.renderStatusForTest(hostile)).join("\n");
  assert(hostileText.includes("coding · Unknown"), "a prototype-polluting state string must not pass the allow-list");
});

check("the alias save path previews changes before mutating", function () {
  // The first click must not POST: the mutating call lives behind the
  // confirmed "Save routing changes" button, after the preview.
  assert(source.includes("onclick: openPreview"), "the save button must open the preview");
  assert(source.includes('text: "Save routing changes"'));
  assert(source.includes('text: "Cancel"'));
  const confirmIdx = source.indexOf('text: "Save routing changes"');
  const postCall = 'run(api(BASE + "/api/alias", { method: "POST", body: payload }))';
  const postIdx = source.indexOf(postCall);
  assert(postIdx > confirmIdx, "the mutating POST must live behind the confirmed button");
  assert(source.split(postCall).length - 1 === 1, "exactly one mutating save call exists");
  assert(source.includes("Changes not saved. Current configuration is unchanged."));
  assert(source.includes('text: "Review errors"'));
  assert(source.includes("class: \"save-preview\", hidden: true"),
    "the preview starts hidden and holds nothing");
});

check("the save preview names the alias, primary/fallback order and the trigger/authorization impact", function () {
  const lines = helpers.savePreviewLines({
    name: "coding",
    primary: { provider: "q", model: "new-main" },
    fallbacks: [{ provider: "r", model: "b" }],
    triggers: ["exhausted", "rate_limited"],
    authorized: true,
    confirmed: ["r"],
    cooldown: 300,
    phaseRouting: null
  }, {
    name: "coding",
    primary: { provider: "p", model: "main" },
    fallbacks: [{ provider: "q", model: "backup" }],
    fallback_triggers: ["exhausted"],
    fallback_authorized: false,
    fallback_confirmed_providers: [],
    cooldown_seconds: 60,
    enabled: false,
    phase_routing: null
  }).join("\n");
  assert(lines.includes("Alias: coding"), "the preview must name the alias");
  assert(lines.includes("p → main") && lines.includes("q → new-main"),
    "the primary change must be named old → new");
  assert(lines.includes("Fallback order — was:") && lines.includes("Fallback order — now:"),
    "the fallback order change must be shown");
  assert(lines.includes("Trigger impact: enable rate_limited"));
  assert(lines.includes("disabled → enabled"), "the authorization change must be named");
  assert(lines.includes("Cooldown: 60s → 300s"));
});

check("an unchanged save preview says everything is unchanged", function () {
  const saved = {
    name: "coding", primary: { provider: "p", model: "main" },
    fallbacks: [{ provider: "q", model: "backup" }],
    fallback_triggers: ["exhausted"], fallback_authorized: true,
    fallback_confirmed_providers: ["q"], cooldown_seconds: 60, enabled: true, phase_routing: null
  };
  const lines = helpers.savePreviewLines({
    name: saved.name, primary: saved.primary, fallbacks: saved.fallbacks,
    triggers: saved.fallback_triggers, authorized: saved.fallback_authorized,
    confirmed: saved.fallback_confirmed_providers, cooldown: saved.cooldown_seconds,
    phaseRouting: null
  }, saved).join("\n");
  assert(lines.includes("(unchanged)"), "an unchanged save must say so");
  assert(lines.includes("no trigger changes"));
  assert(lines.includes("Fallback order: "));
  assert(lines.includes("published — stays published"));
});

check("a new-alias preview is labelled as new and never mutates", function () {
  const lines = helpers.savePreviewLines({
    name: "fresh", primary: { provider: "p", model: "main" }, fallbacks: [],
    triggers: [], authorized: false, confirmed: [], cooldown: 300, phaseRouting: null
  }, null).join("\n");
  assert(lines.includes("Alias: fresh (new — nothing with this name is saved yet)"));
  assert(lines.includes("Primary: p → main"));
  assert(lines.includes("Triggers: none"));
  assert(lines.includes("not published (publish separately)"));
});

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
  assert(rendered.includes("transient failure holds at most 3600s"), rendered);
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

// --- local providers -----------------------------------------------------

check("a local provider card asks for an endpoint and states the privacy rule", function () {
  const card = helpers.renderProviderForTest({
    provider_id: "ollama", kind: "ollama", state: "not_configured", models: 0, base_url: ""
  });
  const text = collectText(card).join("\n");
  assert(text.includes("Save endpoint"), "the card must offer the endpoint field");
  // The privacy warning the owner has to see next to the field.
  assert(text.includes("stay on this LAN"), "the local privacy rule must be stated");
  assert(text.includes("explicitly confirmed for the alias"), "the fallback consent rule must be stated");
  // N2 reword: the confirmation party is the fallback target, never the
  // card's own (local) provider. The old party name must be gone.
  assert(
    !text.includes("this provider is explicitly confirmed"),
    "the confirmation party must be the fallback target, not this provider"
  );
  // The loopback/bridge-mode caveat, and the no-download rule.
  assert(text.includes("127.0.0.1 reaches a server"), "the host-networking caveat must be stated");
  assert(text.includes("bridge-networked"), "the bridge-mode caveat must be stated");
  assert(text.includes("never downloads a model"), "no automatic model downloads must be stated");
  const endpointInputs = findAll(card, function (node) {
    return node.tag === "input" && String(node.attributes.placeholder || "").includes("11434");
  });
  assert.strictEqual(endpointInputs.length, 1, "exactly one endpoint input");
});

check("a saved endpoint is pre-filled in the owner's own card", function () {
  const card = helpers.renderProviderForTest({
    provider_id: "lm_studio", kind: "lm_studio", state: "configured_untested",
    models: 0, base_url: "http://192.168.1.77:1234"
  });
  const inputs = findAll(card, function (node) {
    return node.tag === "input" && String(node.attributes.placeholder || "").includes("11434");
  });
  assert.strictEqual(inputs.length, 1);
  assert.strictEqual(inputs[0].value, "http://192.168.1.77:1234");
  const text = collectText(card).join("\n");
  assert(text.includes("keyless by default"), "keyless must be the stated default");
});

check("a public provider card has no endpoint field and no local privacy text", function () {
  const card = helpers.renderProviderForTest({
    provider_id: "anthropic", kind: "anthropic", state: "connected", models: 3, base_url: null
  });
  const text = collectText(card).join("\n");
  assert(!text.includes("Save endpoint"), "only local providers configure an endpoint");
  assert(!text.includes("stay on this LAN"));
  assert(!text.includes("11434"));
  const endpointInputs = findAll(card, function (node) {
    return node.tag === "input" && String(node.attributes.placeholder || "").includes("11434");
  });
  assert.strictEqual(endpointInputs.length, 0);
});

// --- per-provider concurrency (card t_59fd2408) --------------------------

check("every provider card states its in-flight limit and the cloud/local split", function () {
  const cloud = helpers.renderProviderForTest({
    provider_id: "anthropic", kind: "anthropic", state: "connected", models: 3,
    base_url: null, max_in_flight: null, max_in_flight_effective: 32
  });
  const cloudText = collectText(cloud).join("\n");
  assert(cloudText.includes("In flight at once: 32 (default for this provider)"), "the cloud default is shown");
  assert(cloudText.includes("Cloud providers default to 32"), "the cloud default must be stated");
  assert(cloudText.includes("default to 4"), "the local floor must be stated next to it");
  assert(cloudText.includes("HTTP 429"), "the over-cap answer must be stated");
  assert(cloudText.includes("does not queue"), "no queueing must be stated");

  const local = helpers.renderProviderForTest({
    provider_id: "ollama", kind: "ollama", state: "not_configured", models: 0,
    base_url: "", max_in_flight: null, max_in_flight_effective: 4
  });
  assert(
    collectText(local).join("\n").includes("In flight at once: 4 (default for this provider)"),
    "the local floor is what a local provider actually runs at"
  );

  const overridden = helpers.renderProviderForTest({
    provider_id: "opencode_go", kind: "opencode_go", state: "not_configured", models: 0,
    base_url: null, max_in_flight: 12, max_in_flight_effective: 12
  });
  const overriddenText = collectText(overridden).join("\n");
  assert(overriddenText.includes("In flight at once: 12 (owner override 12)"), "the override is labelled");
  const inputs = findAll(overridden, (node) => node.tag === "input" && node.attributes.type === "number");
  assert.strictEqual(inputs.length, 1, "exactly one limit field");
  assert.strictEqual(inputs[0].value, "12", "the saved override is pre-filled");
});

check("saving an in-flight limit posts the owner's number, and an invalid one posts nothing", function () {
  const recorded = [];
  const realFetch = global.fetch;
  const realSetTimeout = global.setTimeout;
  global.setTimeout = function () { return 0; };  // toast cleanup must not outlive the check
  global.fetch = function (url, options) {
    options = options || {};
    recorded.push({
      url: url, method: options.method || "GET",
      body: options.body ? JSON.parse(options.body) : null
    });
    if (options.method === "POST") {
      return jsonResponse({ ok: true, max_in_flight: 16, max_in_flight_effective: 16 });
    }
    return jsonResponse({ providers: [], aliases: [], profiles: [] });
  };
  try {
    const card = helpers.renderProviderForTest({
      provider_id: "opencode_go", kind: "opencode_go", state: "not_configured", models: 0,
      base_url: null, max_in_flight: null, max_in_flight_effective: 32
    });
    const input = findAll(card, (node) => node.tag === "input" && node.attributes.type === "number")[0];
    assert(input, "the card must offer the limit field");
    const buttons = findAll(card, (node) => node.tag === "button");
    const save = buttons.find((node) => node.textContent === "Save limit");
    const clear = buttons.find((node) => node.textContent === "Use default");
    assert(save && clear, "the card must offer save and clear");

    input.value = "16";
    save.listeners.click();
    let posts = recorded.filter((r) => r.method === "POST");
    assert.strictEqual(posts.length, 1, "a valid number is sent");
    assert.deepStrictEqual(posts[0].body, {
      action: "set_max_in_flight", provider_id: "opencode_go", max_in_flight: 16
    });

    input.value = "0";
    save.listeners.click();
    assert.strictEqual(
      recorded.filter((r) => r.method === "POST").length, 1, "zero is refused before it leaves the browser"
    );

    input.value = "-3";
    save.listeners.click();
    assert.strictEqual(
      recorded.filter((r) => r.method === "POST").length, 1, "a negative is refused before it leaves the browser"
    );

    clear.listeners.click();
    posts = recorded.filter((r) => r.method === "POST");
    assert.strictEqual(posts.length, 2, "clearing sends the clear");
    assert.strictEqual(posts[1].body.max_in_flight, null, "empty means back to the default");
  } finally {
    global.fetch = realFetch;
    global.setTimeout = realSetTimeout;
  }
});

// --- cooldown clear controls (card t_a278c4d4) --------------------------

function cooldownFixtureState() {
  return {
    cooldown_details: {
      "chatgpt_codex/*": {
        provider: "chatgpt_codex", model: null, scope: "provider",
        error_class: "exhausted", failures: 2, opened_at: 1762000000, until: 1762003600,
        remaining_seconds: 3600, next_probe_at: 1762003600, probe_in_flight: false,
        reason: "the provider reported that this account's allowance is exhausted",
        request_id: "evt-1", state: "open"
      },
      "opencode_go/mimo-v2.6-flash": {
        provider: "opencode_go", model: "mimo-v2.6-flash", scope: "route",
        error_class: "rate_limited", failures: 1, opened_at: 1762000100, until: 1762000700,
        remaining_seconds: 600, next_probe_at: 1762000700, probe_in_flight: false,
        reason: "the provider rate-limited this route",
        request_id: "evt-2", state: "open"
      }
    }
  };
}

// A synchronous thenable with finally, so a click handler chained as
// run(api(...)).then(...).catch(...) settles inside a check. The state is
// resolved eagerly: a callback's returned thenable is drained on the spot,
// never re-adopted (that is what blew the stack on the first draft).
function settle(value, failed) {
  const isThenable = (v) => !!v && (typeof v === "object" || typeof v === "function") && typeof v.then === "function";
  return {
    then(onFulfilled, onRejected) {
      const fn = failed ? onRejected : onFulfilled;
      if (typeof fn !== "function") return settle(value, failed);
      let out;
      try {
        const result = fn(value);
        if (isThenable(result)) {
          let settled = false;
          result.then(
            (x) => { if (!settled) { settled = true; out = settle(x, false); } },
            (e) => { if (!settled) { settled = true; out = settle(e, true); } }
          );
          if (!settled) throw new Error("async thenables are not supported by this test harness");
        } else {
          out = settle(result, false);
        }
      } catch (error) {
        out = settle(error, true);
      }
      return out;
    },
    catch(onRejected) { return this.then(undefined, onRejected); },
    finally(onFinally) {
      try { if (typeof onFinally === "function") onFinally(); }
      catch (error) { return settle(error, true); }
      return this;
    },
  };
}

function jsonResponse(value) {
  return settle({
    ok: true,
    headers: { get: function () { return "application/json"; } },
    json: function () { return settle(value, false); },
    text: function () { return settle(JSON.stringify(value), false); }
  }, false);
}

check("the cooldown table offers one clear control per held circuit and a counted clear-all", function () {
  const card = helpers.renderCooldownTableForTest(cooldownFixtureState());
  const headers = findAll(card, (node) => node.tag === "th").map((node) => node.textContent);
  assert(headers.includes("Clear"), "the table needs a clear column");
  const texts = findAll(card, (node) => node.tag === "button").map((node) => node.textContent);
  assert.strictEqual(texts.filter((t) => t === "Clear").length, 2, "one clear button per held circuit");
  assert(texts.includes("Clear all cooldowns"), "the mass clear sits next to the table it empties");
  const empty = helpers.renderCooldownTableForTest({ cooldown_details: {} });
  assert.strictEqual(findAll(empty, (node) => node.tag === "button").length, 0, "nothing held, nothing to clear");
});

check("a route row confirms, posts the route scope, reports the keys and refreshes the table", function () {
  const recorded = [];
  const confirms = [];
  const realFetch = global.fetch;
  const realSetTimeout = global.setTimeout;
  const realConfirm = globalThis.confirm;
  const realWindow = global.window;
  global.setTimeout = function () { return 0; };  // toast cleanup must not outlive the check
  global.window = globalThis;
  globalThis.confirm = function (message) { confirms.push(message); return true; };
  global.fetch = function (url, options) {
    options = options || {};
    recorded.push({ url: url, method: options.method || "GET", body: options.body ? JSON.parse(options.body) : null });
    if (options.method === "POST") {
      return jsonResponse({
        ok: true, scope: "route", provider: "opencode_go", model: "mimo-v2.6-flash",
        cleared: [{ key: "opencode_go/mimo-v2.6-flash" }],
        cleared_count: 1, keys: ["opencode_go/mimo-v2.6-flash"], probe: null
      });
    }
    const after = cooldownFixtureState();
    delete after.cooldown_details["opencode_go/mimo-v2.6-flash"];
    return jsonResponse(after);
  };
  try {
    const toastBefore = document.body.children.length;
    const card = helpers.renderCooldownTableForTest(cooldownFixtureState());
    const routeRow = findAll(card, (node) => node.tag === "tr")
      .find((row) => collectText(row).join(" ").includes("opencode_go/mimo-v2.6-flash"));
    const button = findAll(routeRow, (node) => node.tag === "button")[0];
    button.listeners.click();

    assert.strictEqual(confirms.length, 1, "a row clear must confirm first");
    assert(confirms[0].includes("mimo-v2.6-flash"), "the confirmation names the route");
    const posts = recorded.filter((r) => r.method === "POST");
    assert.strictEqual(posts.length, 1);
    assert.deepStrictEqual(posts[0].body, {
      action: "clear_cooldowns", provider_id: "opencode_go",
      model: "mimo-v2.6-flash", scope: "route"
    }, "a route row clears exactly its route");
    const refreshes = recorded.filter((r) => r.method === "GET" && r.url.endsWith("/api/state"));
    assert.strictEqual(refreshes.length, 1, "the table refreshes from state after the clear");
    const toasts = [];
    for (let i = toastBefore; i < document.body.children.length; i += 1) {
      toasts.push(collectText(document.body.children[i]).join("\n"));
    }
    const message = toasts.join("\n");
    assert(message.includes("Cleared 1 cooldown(s)"), "the report states the count");
    assert(message.includes("opencode_go/mimo-v2.6-flash"), "the report names the cleared key");
  } finally {
    global.fetch = realFetch;
    global.setTimeout = realSetTimeout;
    globalThis.confirm = realConfirm;
    if (realWindow === undefined) delete global.window; else global.window = realWindow;
  }
});

check("a provider-wide row clears the provider scope and says its routes go too", function () {
  const recorded = [];
  const confirms = [];
  const realFetch = global.fetch;
  const realSetTimeout = global.setTimeout;
  const realConfirm = globalThis.confirm;
  const realWindow = global.window;
  global.setTimeout = function () { return 0; };
  global.window = globalThis;
  globalThis.confirm = function (message) { confirms.push(message); return true; };
  global.fetch = function (url, options) {
    options = options || {};
    recorded.push({ url: url, method: options.method || "GET", body: options.body ? JSON.parse(options.body) : null });
    if (options.method === "POST") {
      return jsonResponse({
        ok: true, scope: "provider", provider: "chatgpt_codex", model: null,
        cleared: [{ key: "chatgpt_codex/*" }, { key: "chatgpt_codex/gpt-6-luna" }],
        cleared_count: 2, keys: ["chatgpt_codex/*", "chatgpt_codex/gpt-6-luna"], probe: null
      });
    }
    return jsonResponse(cooldownFixtureState());
  };
  try {
    const card = helpers.renderCooldownTableForTest(cooldownFixtureState());
    const wideRow = findAll(card, (node) => node.tag === "tr")
      .find((row) => collectText(row).join(" ").includes("chatgpt_codex/*"));
    const button = findAll(wideRow, (node) => node.tag === "button")[0];
    button.listeners.click();

    assert(confirms[0].includes("whole-provider cooldown"), "the confirmation names the wider scope");
    assert(confirms[0].includes("route cooldowns"), "the owner is told the routes go too");
    const posts = recorded.filter((r) => r.method === "POST");
    assert.deepStrictEqual(posts[0].body, {
      action: "clear_cooldowns", provider_id: "chatgpt_codex", scope: "provider"
    }, "a provider row never reaches the all scope");
  } finally {
    global.fetch = realFetch;
    global.setTimeout = realSetTimeout;
    globalThis.confirm = realConfirm;
    if (realWindow === undefined) delete global.window; else global.window = realWindow;
  }
});

check("the clear-all control names the entry count and sends only scope: all", function () {
  const recorded = [];
  const confirms = [];
  const realFetch = global.fetch;
  const realSetTimeout = global.setTimeout;
  const realConfirm = globalThis.confirm;
  const realWindow = global.window;
  global.setTimeout = function () { return 0; };
  global.window = globalThis;
  globalThis.confirm = function (message) { confirms.push(message); return true; };
  global.fetch = function (url, options) {
    options = options || {};
    recorded.push({ url: url, method: options.method || "GET", body: options.body ? JSON.parse(options.body) : null });
    if (options.method === "POST") {
      return jsonResponse({
        ok: true, scope: "all", provider: null, model: null,
        cleared: [{ key: "chatgpt_codex/*" }, { key: "chatgpt_codex/gpt-6-luna" }, { key: "opencode_go/*" }, { key: "opencode_go/mimo-v2.6-flash" }],
        cleared_count: 4,
        keys: ["chatgpt_codex/*", "chatgpt_codex/gpt-6-luna", "opencode_go/*", "opencode_go/mimo-v2.6-flash"],
        probe: null
      });
    }
    const after = { cooldown_details: {} };
    return jsonResponse(after);
  };
  try {
    const toastBefore = document.body.children.length;
    const card = helpers.renderCooldownTableForTest(cooldownFixtureState());
    const button = findAll(card, (node) => node.tag === "button")
      .find((node) => node.textContent === "Clear all cooldowns");
    button.listeners.click();

    assert.strictEqual(confirms.length, 1, "the mass clear must confirm first");
    assert(confirms[0].includes("2 local cooldown(s)"), "the confirmation names how many entries go");
    const posts = recorded.filter((r) => r.method === "POST");
    assert.strictEqual(posts.length, 1);
    assert.deepStrictEqual(
      posts[0].body,
      { action: "clear_cooldowns", scope: "all" },
      "the mass clear says scope: all by name and carries no provider to default against"
    );
    assert.strictEqual(Object.prototype.hasOwnProperty.call(posts[0].body, "provider_id"), false);
    const toasts = [];
    for (let i = toastBefore; i < document.body.children.length; i += 1) {
      toasts.push(collectText(document.body.children[i]).join("\n"));
    }
    const message = toasts.join("\n");
    assert(message.includes("Cleared 4 cooldown(s) across every provider"), "the report states the total");
    assert(message.includes("chatgpt_codex/*"), "the report names the cleared keys");
    assert.strictEqual(
      recorded.filter((r) => r.method === "GET" && r.url.endsWith("/api/state")).length, 1,
      "the table refreshes from state after the mass clear"
    );
  } finally {
    global.fetch = realFetch;
    global.setTimeout = realSetTimeout;
    globalThis.confirm = realConfirm;
    if (realWindow === undefined) delete global.window; else global.window = realWindow;
  }
});

check("declining any cooldown confirmation sends nothing", function () {
  const recorded = [];
  const realFetch = global.fetch;
  const realSetTimeout = global.setTimeout;
  const realConfirm = globalThis.confirm;
  const realWindow = global.window;
  global.setTimeout = function () { return 0; };
  global.window = globalThis;
  globalThis.confirm = function () { return false; };
  global.fetch = function (url, options) {
    options = options || {};
    recorded.push({ url: url, method: options.method || "GET" });
    return jsonResponse({});
  };
  try {
    const card = helpers.renderCooldownTableForTest(cooldownFixtureState());
    const buttons = findAll(card, (node) => node.tag === "button");
    buttons.find((node) => node.textContent === "Clear all cooldowns").listeners.click();
    buttons.find((node) => node.textContent === "Clear").listeners.click();
    assert.strictEqual(recorded.length, 0, "a declined confirmation must not touch the API");
  } finally {
    global.fetch = realFetch;
    global.setTimeout = realSetTimeout;
    globalThis.confirm = realConfirm;
    if (realWindow === undefined) delete global.window; else global.window = realWindow;
  }
});

check("the provider-card clear button is unchanged: provider-only payload, original confirmation", function () {
  const recorded = [];
  const confirms = [];
  const realFetch = global.fetch;
  const realSetTimeout = global.setTimeout;
  const realConfirm = globalThis.confirm;
  const realWindow = global.window;
  global.setTimeout = function () { return 0; };
  global.window = globalThis;
  globalThis.confirm = function (message) { confirms.push(message); return true; };
  global.fetch = function (url, options) {
    options = options || {};
    recorded.push({ url: url, method: options.method || "GET", body: options.body ? JSON.parse(options.body) : null });
    if (options.method === "POST") {
      return jsonResponse({
        ok: true, provider: "anthropic", cleared: [], cleared_count: 0,
        scope: "provider", model: null, keys: [], probe: null
      });
    }
    return jsonResponse(cooldownFixtureState());
  };
  try {
    const card = helpers.renderProviderForTest({
      provider_id: "anthropic", kind: "anthropic", state: "connected", models: 3, base_url: null
    });
    const button = findAll(card, (node) => node.tag === "button")
      .find((node) => node.textContent === "Clear cooldowns");
    assert(button, "the provider card keeps its own clear button");
    button.listeners.click();
    assert(confirms[0].includes("single recovery probe"), "the provider-card confirmation wording is unchanged");
    const posts = recorded.filter((r) => r.method === "POST");
    assert.strictEqual(posts.length, 1);
    assert.deepStrictEqual(
      posts[0].body,
      { action: "clear_cooldowns", provider_id: "anthropic" },
      "the provider-card button still posts the provider-only default, with no scope field"
    );
  } finally {
    global.fetch = realFetch;
    global.setTimeout = realSetTimeout;
    globalThis.confirm = realConfirm;
    if (realWindow === undefined) delete global.window; else global.window = realWindow;
  }
});

// --- machine keys (MCP / automation) ------------------------------------

const MACHINE_KEY_ROWS = [
  { display_id: "mk_1a2b3c4d", name: "laptop-mcp", scope: "read", last_used_at: "2026-11-04T10:00:00Z" },
  { display_id: "mk_5e6f7a8b", name: "ci-mcp", scope: "write", last_used_at: null },
];

check("the machine key list shows display id, name, scope and last-used only", function () {
  const card = helpers.renderMachineKeysForTest(MACHINE_KEY_ROWS, fixtureState());
  const text = collectText(card).join("\n");
  assert(text.includes("mk_1a2b3c4d"), "the display id is shown");
  assert(text.includes("laptop-mcp"), "the name is shown");
  assert(text.includes("read only"), "a read scope reads as read only");
  assert(text.includes("read + write"), "a write scope reads as read plus write");
  assert(text.includes("never"), "an unused key says never");
  // The renderer has no value to draw: the list is metadata by contract.
  assert(!text.includes("gwmk_"), "no key material in the list");
  const headers = findAll(card, (n) => n.tag === "th").map((n) => n.textContent);
  assert.deepStrictEqual(
    headers,
    ["Key id", "Name", "Scope", "Last used", "Actions"],
    "the list is exactly display id, name, scope, last-used (+ actions)"
  );
});

check("the machine key card offers read and write scopes and nothing else", function () {
  const card = helpers.renderMachineKeysForTest([], fixtureState());
  const buttons = findAll(card, (n) => n.tag === "button");
  assert(buttons.some((b) => b.textContent === "Create machine key"), "a create control exists");
  const select = findAll(card, (n) => n.tag === "select")
    .find((n) => n.attributes.id === "machine-key-scope");
  assert(select, "a scope select exists");
  assert.deepStrictEqual(
    select.children.map((o) => o.attributes.value),
    ["read", "write"],
    "exactly two scopes - no third is offered"
  );
  const text = collectText(card).join("\n");
  assert(text.includes("No machine keys yet"), "an empty list says so");
  assert(
    text.includes("a machine key can never list, create, rotate or revoke keys"),
    "the console states that a machine key cannot manage keys"
  );
});

check("the one-time reveal shows the value once and never in the list", function () {
  const reveal = {
    display_id: "mk_1a2b3c4d",
    name: "laptop-mcp",
    scope: "read",
    key: "gwmk_freshly-created-value",
  };
  const card = helpers.renderMachineKeysForTest(MACHINE_KEY_ROWS, fixtureState(), reveal);
  const input = findAll(card, (n) => n.tag === "input")
    .find((n) => n.attributes.id === "machine-key-reveal");
  assert(input, "the reveal input exists");
  assert.strictEqual(input.attributes.value, reveal.key, "the value is shown to the owner");
  assert.strictEqual(input.attributes.readonly, "", "the reveal input is read-only");
  const text = collectText(card).join("\n");
  assert(text.includes("shown exactly once"), "the once-only warning is stated");
  const buttons = findAll(card, (n) => n.tag === "button").map((b) => b.textContent);
  assert(buttons.includes("Copy key"), "a copy action exists");
  assert(buttons.includes("I have stored it"), "the owner can dismiss the reveal");
  // The list under the reveal still carries metadata only.
  assert(text.includes("mk_5e6f7a8b"), "the list still renders");
});

check("creating a machine key posts name and scope and holds the value for one reveal", function () {
  const recorded = [];
  const realFetch = global.fetch;
  const realSetTimeout = global.setTimeout;
  const realConfirm = globalThis.confirm;
  const realWindow = global.window;
  const realGetElementById = global.document.getElementById;
  const inputs = {
    "machine-key-name": { value: "laptop-mcp" },
    "machine-key-scope": { value: "write" },
  };
  global.setTimeout = function () { return 0; };
  global.window = globalThis;
  globalThis.confirm = function () { return true; };
  global.document.getElementById = function (id) {
    if (inputs[id]) return inputs[id];
    if (id === "machine-key-reveal") return { value: "", select: function () {} };
    return null;
  };
  const created = {
    ok: true,
    action: "create",
    shown_once: true,
    machine_key: { display_id: "mk_new1", name: "laptop-mcp", scope: "write", key: "gwmk_fresh" },
  };
  global.fetch = function (url, options) {
    options = options || {};
    recorded.push({
      url: url,
      method: options.method || "GET",
      body: options.body ? JSON.parse(options.body) : null,
    });
    if ((options.method || "GET") === "POST") return jsonResponse(created);
    return jsonResponse({
      machine_keys: [{ display_id: "mk_new1", name: "laptop-mcp", scope: "write", last_used_at: null }],
      count: 1,
    });
  };
  try {
    const card = helpers.renderMachineKeysForTest([], fixtureState());
    const button = findAll(card, (n) => n.tag === "button")
      .find((b) => b.textContent === "Create machine key");
    button.listeners.click();

    const posts = recorded.filter((r) => r.method === "POST");
    assert.strictEqual(posts.length, 1, "one create request");
    assert(posts[0].url.endsWith("/api/machine-keys"), posts[0].url);
    assert.deepStrictEqual(
      posts[0].body,
      { action: "create", name: "laptop-mcp", scope: "write" },
      "name and scope are posted, nothing else"
    );
    const state = helpers.machineKeysStateForTest();
    assert.strictEqual(state.reveal && state.reveal.key, "gwmk_fresh", "the value is held for one reveal");
    assert.strictEqual(state.rows.length, 1, "the list reloads after creation");
    assert.strictEqual(inputs["machine-key-name"].value, "", "the name field is cleared");
  } finally {
    global.fetch = realFetch;
    global.setTimeout = realSetTimeout;
    globalThis.confirm = realConfirm;
    if (realWindow === undefined) delete global.window; else global.window = realWindow;
    global.document.getElementById = realGetElementById;
  }
});

check("rotate and revoke confirm first and post the display id", function () {
  const recorded = [];
  const confirms = [];
  const realFetch = global.fetch;
  const realSetTimeout = global.setTimeout;
  const realConfirm = globalThis.confirm;
  const realWindow = global.window;
  global.setTimeout = function () { return 0; };
  global.window = globalThis;
  globalThis.confirm = function (message) { confirms.push(message); return true; };
  global.fetch = function (url, options) {
    options = options || {};
    recorded.push({
      url: url,
      method: options.method || "GET",
      body: options.body ? JSON.parse(options.body) : null,
    });
    if ((options.method || "GET") === "POST") {
      return jsonResponse({ ok: true, action: "rotate", machine_key: { display_id: "mk_1a2b3c4d", key: "gwmk_next" } });
    }
    return jsonResponse({ machine_keys: [], count: 0 });
  };
  try {
    const card = helpers.renderMachineKeysForTest(MACHINE_KEY_ROWS, fixtureState());
    const buttons = findAll(card, (n) => n.tag === "button");
    buttons.find((b) => b.textContent === "Rotate").listeners.click();
    buttons.find((b) => b.textContent === "Revoke").listeners.click();

    assert.strictEqual(confirms.length, 2, "both destructive actions confirm");
    assert(confirms[0].includes("mk_1a2b3c4d"), "the confirmation names the key id");
    assert(confirms[1].includes("stops working at once"), "revocation states the immediacy");
    const posts = recorded.filter((r) => r.method === "POST");
    assert.deepStrictEqual(
      posts.map((p) => p.body),
      [
        { action: "rotate", display_id: "mk_1a2b3c4d" },
        { action: "revoke", display_id: "mk_1a2b3c4d" },
      ],
      "both post the display id and nothing else"
    );
  } finally {
    global.fetch = realFetch;
    global.setTimeout = realSetTimeout;
    globalThis.confirm = realConfirm;
    if (realWindow === undefined) delete global.window; else global.window = realWindow;
  }
});

check("declining a machine key confirmation sends nothing", function () {
  const recorded = [];
  const realFetch = global.fetch;
  const realSetTimeout = global.setTimeout;
  const realConfirm = globalThis.confirm;
  const realWindow = global.window;
  global.setTimeout = function () { return 0; };
  global.window = globalThis;
  globalThis.confirm = function () { return false; };
  global.fetch = function (url, options) {
    options = options || {};
    recorded.push({ url: url, method: options.method || "GET" });
    return jsonResponse({ machine_keys: [], count: 0 });
  };
  try {
    const card = helpers.renderMachineKeysForTest(MACHINE_KEY_ROWS, fixtureState());
    const buttons = findAll(card, (n) => n.tag === "button");
    buttons.find((b) => b.textContent === "Rotate").listeners.click();
    buttons.find((b) => b.textContent === "Revoke").listeners.click();
    assert.strictEqual(
      recorded.filter((r) => r.method === "POST").length,
      0,
      "a declined confirmation must not touch the API"
    );
  } finally {
    global.fetch = realFetch;
    global.setTimeout = realSetTimeout;
    globalThis.confirm = realConfirm;
    if (realWindow === undefined) delete global.window; else global.window = realWindow;
  }
});

// --- reset redemption (card t_571135f9) ------------------------------------
// The Redeem control's three-state contract and its click flow: disabled
// at 0 banked, disabled below full exhaustion with the waste reason
// inline, enabled only when a credit is banked AND a window sits at
// 100% - and then one POST whose outcome is toasted verbatim, with only
// "reset" re-fetching state so the restored windows replace the
// exhausted ones. Everything below runs over the stubbed fetch seam; no
// network exists in this file.

function redeemQuotaFixture(banked, worst) {
  return {
    source: "account_usage_api",
    observed_at: new Date().toISOString(),
    plan_type: "plus",
    unavailable_reason: null,
    resets_banked: banked,
    windows: [
      { label: "Session", used_percent: worst, reset_at: new Date(Date.now() + 3600e3).toISOString() },
      { label: "Weekly", used_percent: worst, reset_at: new Date(Date.now() + 86400e3).toISOString() }
    ]
  };
}

function redeemStateFixture(quota) {
  const state = fixtureState();
  state.providers = [{ provider_id: "chatgpt_codex", state: "connected", credential_present: true, last_test_at: null, last_error: null }];
  state.provider_health = [{
    provider_id: "chatgpt_codex",
    connection_state: "connected",
    last_test_outcome: "ok",
    catalog_observed_at: "2026-11-08T00:00:00Z",
    model_count: 3,
    cooldown_remaining_seconds: 0,
    recent_success_rate: 1,
    recent_failure_rate: 0,
    recent_requests: 9,
    last_error: null,
    account_quota: quota
  }];
  return state;
}

function redeemButton(root) {
  return findAll(root, (n) => n.tag === "button")
    .find((b) => b.textContent === "Redeem banked reset");
}

function redeemErrorJson(status, value) {
  // A fetch that reached the gateway but came back as an error status -
  // the same shape jsonResponse makes, only ok:false.
  return settle({
    ok: false,
    status: status,
    headers: { get: function () { return "application/json"; } },
    json: function () { return settle(value, false); },
    text: function () { return settle(JSON.stringify(value), false); }
  }, false);
}

check("Redeem: disabled at 0 banked, disabled below 100% with the waste reason, enabled only when both hold", function () {
  // AC3 - today's live value first: 0 banked, even at 100% exhaustion.
  const zeroRoot = helpers.renderStatusForTest(redeemStateFixture(redeemQuotaFixture(0, 100)));
  const zeroButton = redeemButton(zeroRoot);
  assert.strictEqual(zeroButton.attributes.disabled, "", "0 banked disables the control");
  const zeroText = collectText(zeroRoot).join("\n");
  assert(zeroText.includes("0 banked"), "the count that blocks it is on the card");
  assert(zeroText.includes("nothing to redeem"), "the reason is inline");
  assert(String(zeroButton.attributes["aria-label"]).includes("disabled"), "the reason is the accessible name");

  // Banked but only 40% through the week: disabled with the waste reason.
  const partialRoot = helpers.renderStatusForTest(redeemStateFixture(redeemQuotaFixture(1, 40)));
  const partialButton = redeemButton(partialRoot);
  assert.strictEqual(partialButton.attributes.disabled, "", "banked-but-not-exhausted disables the control");
  const partialText = collectText(partialRoot).join("\n");
  assert(partialText.includes("redeeming at 40% would waste it"), "the waste reason is inline");
  assert(partialText.includes("FULL 5h + weekly limits"), "the reason says what a reset would restore");

  // Banked AND exhausted: enabled, no reason anywhere.
  const readyRoot = helpers.renderStatusForTest(redeemStateFixture(redeemQuotaFixture(2, 100)));
  const readyButton = redeemButton(readyRoot);
  assert.strictEqual(readyButton.attributes.disabled, undefined, "2 banked at 100% enables the control");
  const readyText = collectText(readyRoot).join("\n");
  assert(!readyText.includes("would waste it"), "no reason is rendered when it may fire");
  assert(readyText.includes("resets banked: 2"), "the banked count it spends from is on the card");

  // Absent account_quota block: no control at all (older state, unchanged).
  const bareState = redeemStateFixture(null);
  assert.strictEqual(redeemButton(helpers.renderStatusForTest(bareState)), undefined,
    "no quota block, no Redeem control - nothing appears for state that cannot decide");
});

check("a banked credit below full exhaustion never leaves the console - even a forced click sends nothing", function () {
  // AC1 (console half): disabled with the reason, and the handler's own
  // guard means a programmatic dispatch of the disabled button posts
  // nothing either - the POST count stays at zero through the spy.
  const recorded = [];
  const realFetch = global.fetch;
  const realSetTimeout = global.setTimeout;
  const realWindow = global.window;
  global.setTimeout = function () { return 0; };
  global.window = globalThis;
  global.fetch = function (url, options) {
    options = options || {};
    recorded.push({ url: url, method: options.method || "GET" });
    return jsonResponse({});
  };
  try {
    const root = helpers.renderStatusForTest(redeemStateFixture(redeemQuotaFixture(1, 40)));
    const button = redeemButton(root);
    assert.strictEqual(button.attributes.disabled, "", "the control is disabled at 40%");
    button.listeners.click();
    assert.strictEqual(recorded.filter((r) => r.method === "POST").length, 0,
      "the guard runs inside the click: no POST may leave the console");
    assert.strictEqual(recorded.length, 0, "no request at all - not even a state read");
  } finally {
    global.fetch = realFetch;
    global.setTimeout = realSetTimeout;
    if (realWindow === undefined) delete global.window; else global.window = realWindow;
  }
});

check("a refusal toast carries the server's guard text verbatim and nothing is re-fetched", function () {
  // The server re-checks and refuses (its guard is the real one): the
  // toast shows its reason, exactly one POST went out, no state re-read.
  const recorded = [];
  const toastsSeen = [];
  const realFetch = global.fetch;
  const realSetTimeout = global.setTimeout;
  const realWindow = global.window;
  global.setTimeout = function () { return 0; };
  global.window = globalThis;
  global.fetch = function (url, options) {
    options = options || {};
    recorded.push({ url: url, method: options.method || "GET", body: options.body ? JSON.parse(options.body) : null });
    if (options.method === "POST") {
      return redeemErrorJson(409, {
        error: {
          type: "redeem_not_exhausted",
          message: "Refused: a reset restores your FULL 5h + weekly limits - redeeming at 40% would waste it"
        }
      });
    }
    return jsonResponse({});
  };
  try {
    const toastBefore = document.body.children.length;
    const root = helpers.renderStatusForTest(redeemStateFixture(redeemQuotaFixture(1, 100)));
    redeemButton(root).listeners.click();
    for (let i = toastBefore; i < document.body.children.length; i += 1) {
      toastsSeen.push(collectText(document.body.children[i]).join("\n"));
    }
    const message = toastsSeen.join("\n");
    assert(message.includes("redeeming at 40% would waste it"), "the guard's reason is toasted verbatim: " + message);
    const posts = recorded.filter((r) => r.method === "POST");
    assert.strictEqual(posts.length, 1, "one guarded POST, no retry");
    assert.deepStrictEqual(posts[0].body, { action: "redeem_reset", provider_id: "chatgpt_codex" },
      "the action names itself and its provider, nothing else");
    assert.strictEqual(recorded.filter((r) => r.method === "GET").length, 0,
      "a refusal never re-fetches state");
  } finally {
    global.fetch = realFetch;
    global.setTimeout = realSetTimeout;
    if (realWindow === undefined) delete global.window; else global.window = realWindow;
  }
});

check("every consume outcome maps to its own console text, and only reset re-fetches the quota", function () {
  // AC2: four outcomes, four texts; exactly one of them (the real spend)
  // re-reads state and renders the restored windows + spent count.
  const realFetch = global.fetch;
  const realSetTimeout = global.setTimeout;
  const realWindow = global.window;
  global.setTimeout = function () { return 0; };
  global.window = globalThis;
  const scenarios = [
    { code: "reset", toast: "Reset redeemed" },
    { code: "nothing_to_reset", toast: "credit NOT spent." },
    { code: "no_credit", toast: "No credit available" },
    { code: "already_redeemed", toast: "Already redeemed" }
  ];
  try {
    for (const scenario of scenarios) {
      const recorded = [];
      const restored = redeemStateFixture(redeemQuotaFixture(scenario.code === "reset" ? 0 : 1, scenario.code === "reset" ? 0 : 100));
      global.fetch = function (url, options) {
        options = options || {};
        recorded.push({ url: url, method: options.method || "GET", body: options.body ? JSON.parse(options.body) : null });
        if (options.method === "POST") {
          return jsonResponse({
            ok: true,
            outcome: scenario.code,
            message: scenario.code === "reset"
              ? "Reset redeemed - your usage limits have been reset. 1 banked resets remaining."
              : scenario.code === "nothing_to_reset"
                ? "Limits not exhausted - credit NOT spent."
                : scenario.code === "no_credit"
                  ? "No credit available - nothing was redeemed."
                  : "Already redeemed - no second credit was spent.",
            windows_reset: scenario.code === "reset" ? 2 : 0,
            resets_remaining: scenario.code === "reset" ? 1 : 2,
            circuit_open: scenario.code === "reset",
            account_quota: restored.provider_health[0].account_quota
          });
        }
        return jsonResponse(restored);
      };
      const toastBefore = document.body.children.length;
      const root = helpers.renderStatusForTest(redeemStateFixture(redeemQuotaFixture(2, 100)));
      const button = redeemButton(root);
      assert.strictEqual(button.attributes.disabled, undefined,
        scenario.code + ": enabled when banked and exhausted");
      button.listeners.click();

      const posts = recorded.filter((r) => r.method === "POST");
      assert.strictEqual(posts.length, 1, scenario.code + ": exactly one POST");
      assert.deepStrictEqual(posts[0].body, { action: "redeem_reset", provider_id: "chatgpt_codex" },
        scenario.code + ": the POST body is the two named keys");
      const toasts = [];
      for (let i = toastBefore; i < document.body.children.length; i += 1) {
        toasts.push(collectText(document.body.children[i]).join("\n"));
      }
      const message = toasts.join("\n");
      assert(message.includes(scenario.toast), scenario.code + ": its own text is toasted - got: " + message);
      const stateFetches = recorded.filter((r) => r.method === "GET" && r.url.endsWith("/api/state"));
      if (scenario.code === "reset") {
        assert.strictEqual(stateFetches.length, 1, "reset re-fetches state once");
        const live = helpers.liveStateForTest();
        assert(live && live.provider_health, "the re-fetch wrote the new state");
        const liveQuota = live.provider_health[0].account_quota;
        assert.strictEqual(liveQuota.resets_banked, 0, "the spent credit is gone from the live state");
        assert.strictEqual(liveQuota.windows[0].used_percent, 0, "the live state carries the restored windows");
        const after = collectText(helpers.renderStatusForTest(live)).join("\n");
        assert(after.includes("resets banked: 0"), "the next render shows the spent credit gone");
        assert(message.includes("never clears cooldowns"),
          "a held cooldown is pointed at the manual clear, never auto-cleared: " + message);
      } else {
        assert.strictEqual(stateFetches.length, 0, scenario.code + ": no re-fetch, nothing changed");
      }
    }
  } finally {
    global.fetch = realFetch;
    global.setTimeout = realSetTimeout;
    if (realWindow === undefined) delete global.window; else global.window = realWindow;
  }
});

if (failures.length) {
  console.error("\n" + failures.length + " check(s) failed");
  process.exit(1);
}
console.log("\nall checks passed");
