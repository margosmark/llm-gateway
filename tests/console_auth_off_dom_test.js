/* Regression test for the owner-console session gate (owner_console_auth).
 *
 * The flag opts the console out of owner sign-in. When it is off the console
 * must:
 *   - never render the sign-in view (the server reports console_auth=
 *     "disabled"; the mode and the session are separate facts),
 *   - never claim a session it did not establish (no "signed in as ..." pill
 *     and no Sign-out control when there is no session),
 *   - say the mode out loud on every tab - a silently-open admin console is
 *     the failure this flag creates.
 * And when it is on (the default) nothing about the shell may change.
 *
 * Run: node tests/console_auth_off_dom_test.js
 */
"use strict";

const path = require("path");
const assert = require("assert");

const consoleJs = path.join(__dirname, "..", "src", "gateway", "static", "console.js");

// Require the console first, while global.document is still undefined: that
// keeps the module from bootstrapping (it would fetch the live session probe
// in Node). The stub is installed below, before any render call, because the
// helper resolves `document` at call time.
const helpers = require(consoleJs);

function makeNode(tag) {
  const node = {
    tag: tag,
    attributes: {},
    listeners: {},
    children: [],
    parentNode: null,
    className: "",
    _text: "",
    appendChild(child) {
      this.children.push(child);
      if (child && typeof child === "object") child.parentNode = this;
      return child;
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
  Object.defineProperty(node, "textContent", {
    get() {
      return node._text;
    },
    set(value) {
      node._text = String(value);
      node.children = [];
    },
  });
  Object.defineProperty(node, "innerHTML", {
    get() {
      return node._html || "";
    },
    set(value) {
      node._html = String(value);
      if (node._html === "") node.children = [];
    },
  });
  Object.defineProperty(node, "classList", {
    get() {
      const classes = String(this.className || "").split(/\s+/).filter(Boolean);
      return { contains: (name) => classes.indexOf(name) !== -1 };
    },
  });
  return node;
}

global.document = {
  createElement(tag) {
    return makeNode(tag);
  },
  createTextNode(text) {
    return { text: text };
  },
  createDocumentFragment() {
    return makeNode("#fragment");
  },
  getElementById() {
    return null;
  },
  body: makeNode("body"),
};

// Every string the rendered tree carries, in document order.
function texts(node, out) {
  out = out || [];
  if (!node || typeof node !== "object") return out;
  if (typeof node.textContent === "string" && node.textContent) out.push(node.textContent);
  if (typeof node.text === "string" && node.text) out.push(node.text);
  (node.children || []).forEach((child) => texts(child, out));
  return out;
}

function nodes(node, out) {
  out = out || [];
  if (!node || typeof node !== "object") return out;
  out.push(node);
  (node.children || []).forEach((child) => nodes(child, out));
  return out;
}

function fixture(overrides) {
  const view = {
    revision: 7,
    owner: { username: null },
    access: { client_key: { enabled: false } },
    fallback_policy_warnings: [],
    console_auth: "disabled",
  };
  Object.keys(overrides || {}).forEach((key) => {
    if (overrides[key] === undefined) delete view[key];
    else view[key] = overrides[key];
  });
  return view;
}

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

console.log("owner console session gate (owner_console_auth)");

// --- the bootstrap decision --------------------------------------------

check("gate off + no session -> shell, never the sign-in view", function () {
  assert.strictEqual(
    helpers.authViewDecisionForTest({ console_auth: "disabled", authenticated: false }),
    "shell"
  );
});

check("gate off + a real session -> shell", function () {
  assert.strictEqual(
    helpers.authViewDecisionForTest({ console_auth: "disabled", authenticated: true }),
    "shell"
  );
});

check("gate on + no session -> sign-in view", function () {
  assert.strictEqual(
    helpers.authViewDecisionForTest({ console_auth: "enabled", authenticated: false }),
    "auth"
  );
});

check("gate on + a real session -> shell", function () {
  assert.strictEqual(
    helpers.authViewDecisionForTest({ console_auth: "enabled", authenticated: true }),
    "shell"
  );
});

check("an absent or unknown mode fails closed to the sign-in view", function () {
  assert.strictEqual(helpers.authViewDecisionForTest({}), "auth");
  assert.strictEqual(helpers.authViewDecisionForTest(null), "auth");
  assert.strictEqual(
    helpers.authViewDecisionForTest({ console_auth: "maybe", authenticated: false }),
    "auth"
  );
});

// --- the persistent chrome ---------------------------------------------

check("gate off: the shell carries an unmistakable mode banner", function () {
  const root = helpers.renderShellForTest(fixture());
  const banner = nodes(root).filter(
    (n) => n.attributes && n.attributes.role === "alert" && n.className === "notice bad"
  );
  assert.strictEqual(banner.length, 1, "expected exactly one alert banner, got " + banner.length);
  const text = texts(banner[0]).join(" ");
  assert.ok(text.indexOf("OWNER CONSOLE SIGN-IN IS OFF") !== -1, text);
  assert.ok(text.indexOf("owner_console_auth=false") !== -1, text);
});

check("gate off with no session: no identity is claimed and there is no Sign out", function () {
  const root = helpers.renderShellForTest(fixture());
  const all = texts(root).join(" | ");
  assert.ok(all.indexOf("signed in as") === -1, "must not claim a session: " + all);
  const buttons = nodes(root).filter((n) => n.tag === "button");
  assert.ok(
    buttons.every((b) => texts(b).join(" ").indexOf("Sign out") === -1),
    "no Sign out control without a session"
  );
  const pills = nodes(root).filter((n) => n.className === "pill bad");
  assert.strictEqual(pills.length, 1, "expected the mode pill");
  assert.strictEqual(texts(pills[0]).join(" "), "console sign-in is OFF");
});

check("gate off with a real session: the identity and Sign out stay truthful", function () {
  const root = helpers.renderShellForTest(fixture({ owner: { username: "owner" } }));
  const all = texts(root).join(" | ");
  assert.ok(all.indexOf("signed in as owner") !== -1, all);
  const buttons = nodes(root).filter((n) => n.tag === "button");
  assert.ok(
    buttons.some((b) => texts(b).join(" ").indexOf("Sign out") !== -1),
    "a signed-in owner keeps the Sign out control"
  );
  // The mode banner is still there: being signed in does not make the open
  // listener safe.
  assert.ok(all.indexOf("OWNER CONSOLE SIGN-IN IS OFF") !== -1, all);
});

check("gate off: the access notice states the mode instead of a sign-in that never happened", function () {
  const root = helpers.renderShellForTest(fixture());
  const all = texts(root).join(" | ");
  assert.ok(all.indexOf("Owner console sign-in is OFF") !== -1, all);
  assert.ok(all.indexOf("Owner sign-in protects this console") === -1, all);
});

check("gate on (default): the shell is unchanged - no banner, identity, Sign out", function () {
  const root = helpers.renderShellForTest(
    fixture({ console_auth: undefined, owner: { username: "owner" } })
  );
  const all = texts(root).join(" | ");
  assert.ok(all.indexOf("OWNER CONSOLE SIGN-IN IS OFF") === -1, all);
  assert.ok(all.indexOf("signed in as owner") !== -1, all);
  const buttons = nodes(root).filter((n) => n.tag === "button");
  assert.ok(
    buttons.some((b) => texts(b).join(" ").indexOf("Sign out") !== -1),
    "the Sign out control stays"
  );
  const alerts = nodes(root).filter((n) => n.attributes && n.attributes.role === "alert");
  assert.strictEqual(alerts.length, 0, "no alert banner in the default mode");
  assert.ok(all.indexOf("Owner sign-in protects this console") !== -1, all);
});

if (failures.length) {
  console.log("\n" + failures.length + " failure(s)");
  process.exit(1);
}
console.log("\nall checks passed");
