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

if (failures.length) {
  console.error("\n" + failures.length + " check(s) failed");
  process.exit(1);
}
console.log("\nall checks passed");
