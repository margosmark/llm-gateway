"use strict";
// Real Chromium render against isolated server's bundled /owner/ and state API.
// Invoked by acceptance_isolated_server.py; see that file for run requirements.
const assert = require("node:assert/strict");

const SAFE_URL_MESSAGE = "base URL must be http://localhost:<port> or http://127.0.0.1:<port>";
function validateBaseUrl(input) {
  // Match the raw authority, not merely URL.hostname: WHATWG URL canonicalizes
  // shorthand/octal IPv4 and accepts userinfo, both of which are unsafe here.
  if (typeof input !== "string") throw new Error(SAFE_URL_MESSAGE);
  const match = /^http:\/\/(localhost|127\.0\.0\.1):([0-9]+)\/?$/.exec(input);
  if (!match) throw new Error(SAFE_URL_MESSAGE);
  const port = Number(match[2]);
  if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error(SAFE_URL_MESSAGE);
  return `http://${match[1]}:${port}`;
}

async function run(input, chromium) {
  const base = validateBaseUrl(input); // MUST precede browser creation/navigation/fetch.
  const browser = await chromium.launch({executablePath: process.env.GATEWAY_CHROMIUM || "/usr/bin/chromium", args: ["--no-sandbox", "--disable-dev-shm-usage"]});
  try {
    const page = await browser.newPage();
    await page.goto(base + "/owner/", {waitUntil: "networkidle"});
    const state = await page.evaluate(async () => (await fetch("/owner/api/state")).json());
    assert.ok(state.fallback_policy_warnings.length > 0, "fixture must produce policy warnings");
    const warning = page.getByRole("alert").filter({hasText: "Policy gap"});
    await warning.first().waitFor({state: "visible"});
    const visible = await warning.allInnerTexts();
    assert.ok(visible.some(text => text.includes("route_ineligible")), JSON.stringify(visible));
    assert.ok(visible.some(text => text.includes("Policy gap")), JSON.stringify(visible));
    console.log("incomplete policy visible warning:", visible.join(" | "));

    // Correct the actual persisted alias through the live API. The rendered
    // page must then clear its warning; no mocked component/state injection.
    const alias = state.aliases.find(item => item.name === "phase-test");
    assert.ok(alias, "fixture alias absent");
    const triggers = state.fallback_triggers;
    const response = await page.evaluate(async ({alias, triggers}) => fetch("/owner/api/alias", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({action: "save", name: alias.name, primary: alias.primary,
        fallbacks: alias.fallbacks, fallback_triggers: triggers,
        fallback_authorized: alias.fallback_authorized,
        fallback_confirmed_providers: alias.fallback_confirmed_providers,
        fallback_error_classes: alias.fallback_error_classes,
        cooldown_seconds: alias.cooldown_seconds, enabled: alias.enabled,
        phase_routing: alias.phase_routing})
    }).then(r => ({status: r.status, body: r.json()})), {alias, triggers});
    assert.equal(response.status, 200, JSON.stringify(await response.body));
    await page.reload({waitUntil: "networkidle"});
    await page.waitForTimeout(150);
    const healthyWarnings = await page.getByRole("alert").filter({hasText: "Policy gap"}).count();
    assert.equal(healthyWarnings, 0, "healthy policy rendered a false gap");
    console.log("healthy policy: no Policy gap alerts");
  } finally {
    await browser.close();
  }
}

module.exports = {SAFE_URL_MESSAGE, validateBaseUrl, run};
if (require.main === module) {
  const input = process.argv[2];
  try {
    // Validate before importing or launching browser transport.
    validateBaseUrl(input);
  } catch {
    console.error(SAFE_URL_MESSAGE);
    process.exit(2);
  }
  const { chromium } = require("playwright");
  run(input, chromium).catch(error => { console.error(error); process.exitCode = 1; });
}
