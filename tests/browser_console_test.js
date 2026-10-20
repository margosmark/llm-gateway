/* Browser check for the owner console.
 *
 * The spec requires: "A browser test verifies direct console use and the
 * visible trusted-LAN notice at desktop and 390 px viewport widths."
 *
 * This drives a real browser against a running gateway. It asserts that:
 *
 *   - the console opens directly, with no login, passphrase or setup
 *     interstitial;
 *   - the trusted-LAN notice is visible, and still visible at 390 px;
 *   - the provider actions are enabled, not silently disabled;
 *   - the page raises no script errors.
 *
 * Usage: node tests/browser_console_test.js <base-url>
 */
"use strict";

const { chromium } = require("playwright");

const BASE = process.argv[2];
if (!BASE) {
  console.error("usage: browser_console_test.js <base-url>");
  process.exit(2);
}

const failures = [];
function check(name, condition, detail) {
  if (condition) {
    console.log("  ok   " + name);
  } else {
    failures.push(name + (detail ? " - " + detail : ""));
    console.log("  FAIL " + name + (detail ? " - " + detail : ""));
  }
}

(async () => {
  const browser = await chromium.launch({
    executablePath: process.env.GATEWAY_CHROMIUM || "/usr/bin/chromium",
    args: ["--no-sandbox", "--disable-dev-shm-usage"],
  });
  try {
    for (const viewport of [
      { name: "desktop", width: 1280, height: 900 },
      { name: "mobile-390", width: 390, height: 844 },
    ]) {
      console.log("console at " + viewport.name + " (" + viewport.width + "px)");
      const page = await browser.newPage({ viewport: { width: viewport.width, height: viewport.height } });
      const errors = [];
      page.on("pageerror", (error) => errors.push(String(error)));
      page.on("console", (message) => {
        if (message.type() === "error") errors.push("console: " + message.text());
      });

      await page.goto(BASE + "/owner/", { waitUntil: "load" });
      await page.waitForTimeout(1200);

      // Direct use: no login form, no passphrase prompt.
      const passwordInputs = await page.locator('input[type="password"]').count();
      check("no passphrase prompt at " + viewport.name, passwordInputs === 0, passwordInputs + " password inputs");

      // The trusted-LAN notice must be on screen.
      const notice = page.locator("text=Trusted LAN admin mode").first();
      check(
        "the trusted-LAN notice is visible at " + viewport.name,
        (await notice.count()) > 0 && (await notice.isVisible())
      );

      // The console itself is present, with its sections.
      const tabs = await page.locator("nav.tabs button").allInnerTexts();
      check(
        "the console sections render at " + viewport.name,
        ["Status", "Providers", "Models & Routes", "Profiles", "Settings"].every((t) =>
          tabs.some((rendered) => rendered.trim() === t)
        ),
        JSON.stringify(tabs)
      );

      // The provider actions must be enabled.
      await page.getByRole("button", { name: "Providers" }).click();
      await page.waitForTimeout(600);
      const saveAndTest = page.getByRole("button", { name: "Save and test" });
      const saveCount = await saveAndTest.count();
      check(
        '"Save and test" is present at ' + viewport.name,
        saveCount > 0,
        saveCount + " found"
      );
      if (saveCount > 0) {
        check(
          '"Save and test" is enabled at ' + viewport.name,
          !(await saveAndTest.first().isDisabled())
        );
      }
      const connect = page.getByRole("button", { name: "Connect ChatGPT" });
      const connectCount = await connect.count();
      check(
        '"Connect ChatGPT" is present at ' + viewport.name,
        connectCount > 0,
        connectCount + " found"
      );
      if (connectCount > 0) {
        check(
          '"Connect ChatGPT" is enabled at ' + viewport.name,
          !(await connect.first().isDisabled())
        );
      }

      check("no page errors at " + viewport.name, errors.length === 0, errors.join("; "));
      await page.close();
    }
  } finally {
    await browser.close();
  }

  if (failures.length) {
    console.error("\n" + failures.length + " check(s) failed");
    process.exit(1);
  }
  console.log("\nall browser checks passed");
})().catch((error) => {
  console.error("FAILED", error && error.message);
  process.exit(1);
});
