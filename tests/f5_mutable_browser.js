/* F5 mutable-isolated routing-health browser repro (test-only, loopback-only).
 *
 * Runs against the isolated candidate gateway started by f5_mutable_fixture.py
 * (F5_BASE, default http://127.0.0.1:55041). It proves, in real Chromium on the
 * exact pinned candidate:
 *
 *  P1  status tab: flash renders Healthy, coding renders Cooldown (seeded open
 *      circuit); past the 60s STALE_AFTER_MS threshold the ticker flips
 *      Healthy -> Stale — no card ever claims Healthy while stale.
 *  P2  server-side mutation through the gateway's own owner API (coding primary
 *      moved to kimi-smoke2b + clear_cooldowns) while the page holds its stale
 *      snapshot (no auto-poll); focus/visibility refresh then renders the
 *      FRESH mutated data (coding -> Healthy with the new primary).
 *  P3  dirty NEW-alias draft + open save preview survive the same >=60s
 *      stale window and focus refresh, while the fresh mutated state
 *      (coding cooldown 300 -> 444) demonstrably arrives (fetch + body).
 *  P4  dirty EXISTING-alias (coding) draft + open preview survive a further
 *      server-side mutation (cooldown 444 -> 333): the DOM keeps the dirty 777
 *      while the fetched state says 333 — fresh data arrived, draft not
 *      overwritten.
 *  P5  status tab again: cards fresh; then a forced refresh failure fail-closes
 *      to "refresh unavailable"/Stale, never Healthy, and recovers on the next
 *      successful focus refresh.
 *
 * Every fetch-count measurement is printed as evidence for the two inherited
 * QA fetch-count assertions (fresh-fixtures/qa_browser2.js lines 405 and 410):
 * the candidate's contract is refetchUnlessBusy + preserveDraft (refetchIfIdle
 * skips only when busy), so a focus event with a draft open DOES fetch, and
 * the guarded property — draft + preview survive — is asserted directly here
 * rather than inferred from a zero-fetch count.
 */
"use strict";
const { chromium } = require("playwright");
const fs = require("fs");
const path = require("path");

const BASE = process.env.F5_BASE || "http://127.0.0.1:55041";
const EVIDENCE = process.env.F5_EVIDENCE_DIR || path.join(__dirname, "..", ".f5", "evidence");
const CHROMIUM = process.env.GATEWAY_CHROMIUM || undefined;
const TRIGGERS = ["exhausted", "rate_limited", "upstream_unavailable",
  "explicit_cooldown", "route_ineligible", "probe_contention"];

fs.mkdirSync(EVIDENCE, { recursive: true });

const failures = [];
let passed = 0;
function check(name, cond, detail) {
  if (cond) { passed++; console.log("  ok   " + name); }
  else { failures.push(name + (detail ? " - " + detail : "")); console.log("  FAIL " + name + (detail ? " - " + detail : "")); }
}
function note(msg) { console.log("       " + msg); }
function shot(page, name) {
  return page.screenshot({ path: path.join(EVIDENCE, name), fullPage: true })
    .then(() => note("screenshot " + name));
}

// In-page readers. NOTE: :has-text is a Playwright-only selector and must NOT
// be used inside page.evaluate()/querySelector — label lookup is done by span
// text here, exactly like the candidate's own DOM.
const cardReader = () => {
  const cards = {};
  document.querySelectorAll(".route-health-card").forEach((card) => {
    const h3 = card.querySelector("h3[data-alias]");
    if (!h3) return;
    cards[h3.getAttribute("data-alias")] = {
      text: h3.textContent,
      verdict: h3.getAttribute("data-verdict"),
      freshness: (card.querySelector(".health-freshness") || {}).textContent || "",
      primary: (card.querySelector(".health-routes dd") || {}).textContent || "",
      cardText: card.textContent,
    };
  });
  return cards;
};
const draftReader = () => {
  const find = (label) => {
    const labels = Array.from(document.querySelectorAll("label"));
    for (const l of labels) {
      const s = l.querySelector("span");
      if (s && s.textContent === label) return l.querySelector("input") || l.querySelector("select");
    }
    return null;
  };
  const name = find("Alias name");
  const cooldown = find("Local cooldown (seconds)");
  const preview = document.querySelector(".save-preview");
  return {
    name: name ? name.value : null,
    nameDisabled: name ? !!name.disabled : null,
    cooldown: cooldown ? cooldown.value : null,
    previewOpen: !!preview && !preview.hasAttribute("hidden"),
    previewTitle: (document.querySelector("#save-preview-title") || {}).textContent || "",
  };
};

async function mutate(page, pathName, payload, label) {
  const result = await page.evaluate(async ({ p, body, origin }) => {
    const resp = await fetch(origin + p, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    return { status: resp.status, body: await resp.json() };
  }, { p: pathName, body: payload, origin: BASE });
  note(`mutation ${label}: HTTP ${result.status} ${JSON.stringify(result.body).slice(0, 220)}`);
  return result;
}

async function fireFocus(page) {
  await page.evaluate(() => {
    window.dispatchEvent(new Event("focus"));
    Object.defineProperty(document, "visibilityState", { value: "visible", configurable: true });
    document.dispatchEvent(new Event("visibilitychange"));
  });
}

(async () => {
  const browser = await chromium.launch({
    executablePath: CHROMIUM,
    args: ["--no-sandbox", "--disable-dev-shm-usage"],
  });
  const page = await browser.newPage({ viewport: { width: 1400, height: 1000 } });

  // Every /owner/api/state the PAGE itself fetches, with response bodies, so
  // "fresh data arrived" is proven from the page's own traffic — not assumed.
  const stateFetches = [];
  page.on("request", (req) => {
    if (req.method() === "GET" && new URL(req.url()).pathname === "/owner/api/state") {
      stateFetches.push({ at: Date.now() });
    }
  });
  page.on("response", async (resp) => {
    if (resp.request().method() === "GET" && new URL(resp.url()).pathname === "/owner/api/state") {
      try {
        const json = await resp.json();
        const last = stateFetches[stateFetches.length - 1];
        if (last && !last.body) {
          last.body = {
            revision: json.revision,
            coding: (json.aliases || []).find((a) => a.name === "coding"),
            cooldown_details_keys: Object.keys(json.cooldown_details || {}),
          };
        }
      } catch (e) { /* body already consumed or non-JSON */ }
    }
  });
  const fetchCount = () => stateFetches.length;
  const lastFetchBody = () => (stateFetches[stateFetches.length - 1] || {}).body;

  try {
    // ---------------- P1: initial render + real-time stale flip -------------
    console.log("P1: initial render and >=60s stale flip (status tab)");
    await page.goto(BASE + "/owner/", { waitUntil: "load" });
    await page.waitForSelector(".route-health-card");
    await page.waitForTimeout(500);
    let cards = await page.evaluate(cardReader);
    note("initial cards: " + JSON.stringify(cards));
    check("P1 flash renders Healthy when fresh", cards.flash && cards.flash.text === "flash · Healthy", cards.flash && cards.flash.text);
    check("P1 coding renders Cooldown from the seeded open circuit",
      cards.coding && cards.coding.text === "coding · Cooldown", cards.coding && cards.coding.text);
    check("P1 coding card names the seeded cooldown on its primary route",
      cards.coding && /Applicable cooldown: opencode_go\/kimi-smoke2 · open · \d+s remaining/.test(cards.coding.cardText),
      cards.coding && (cards.coding.cardText.split("Applicable cooldown: ")[1] || ""));
    check("P1 fresh cards carry the refetch-on-focus as-of line",
      cards.flash && /^As of \d.* · refetches when this tab regains focus\.$/.test(cards.flash.freshness), cards.flash && cards.flash.freshness);
    const initialFetches = fetchCount();
    check("P1 console issued exactly the bootstrap state fetch (no auto-poll loop)",
      initialFetches <= 2, "state fetches after load=" + initialFetches);

    const flipDeadline = Date.now() + 120000;
    let flipped = false;
    while (Date.now() < flipDeadline) {
      cards = await page.evaluate(cardReader);
      if (cards.flash && cards.flash.text === "flash · Stale") { flipped = true; break; }
      await page.waitForTimeout(2000);
    }
    note("stale-window cards: " + JSON.stringify(cards));
    check("P1 flash card flips Healthy -> Stale past STALE_AFTER_MS (60s)", flipped, cards.flash && cards.flash.text);
    check("P1 stale flash card never claims Healthy", cards.flash && !/Healthy/.test(cards.flash.text), cards.flash && cards.flash.text);
    check("P1 stale flash card states the snapshot age and the refetch promise",
      cards.flash && /· \d+(s|m|h) old — showing the last known state; it refetches when this tab regains focus\./.test(cards.flash.freshness), cards.flash && cards.flash.freshness);
    check("P1 stale window did not trigger any state fetch (staleness is a pure client timer)",
      fetchCount() === initialFetches,
      "fetches before=" + initialFetches + " after=" + fetchCount());
    check("P1 no card anywhere claims Healthy while the snapshot is stale",
      Object.values(cards).every((c) => !/· Healthy/.test(c.text)), JSON.stringify(Object.fromEntries(Object.entries(cards).map(([k, v]) => [k, v.text]))));
    await shot(page, "01-stale.png");

    // ------------- P2: server-side mutation + focus refresh -----------------
    console.log("P2: server-side cooldown/primary mutation, then focus refresh");
    const r1 = await mutate(page, "/owner/api/alias", {
      action: "save", name: "coding",
      primary: { provider: "opencode_go", model: "kimi-smoke2b" },
      fallbacks: [{ provider: "anthropic", model: "claude-fixture" }],
      fallback_triggers: TRIGGERS, fallback_authorized: true,
      fallback_confirmed_providers: ["anthropic"],
      cooldown_seconds: 555, enabled: true,
    }, "coding primary -> opencode_go/kimi-smoke2b, cooldown 555");
    check("P2 owner-API alias save accepted", r1.status === 200 && r1.body.ok === true);
    const r2 = await mutate(page, "/owner/api/provider", {
      action: "clear_cooldowns", provider_id: "opencode_go",
    }, "clear_cooldowns opencode_go (removes the seeded circuit)");
    check("P2 owner-API clear_cooldowns accepted and cleared the seeded circuit",
      r2.status === 200 && r2.body.ok === true && r2.body.cleared_count >= 1, JSON.stringify(r2.body));

    await page.waitForTimeout(800);
    cards = await page.evaluate(cardReader);
    note("cards after mutation, before focus: " + JSON.stringify(cards));
    check("P2 page still holds the pre-mutation snapshot after the server changed (no auto-refresh)",
      cards.coding && cards.coding.text === "coding · Cooldown" &&
      cards.coding.primary === "OpenCode Go / kimi-smoke2" &&
      cards.flash && cards.flash.text === "flash · Stale",
      JSON.stringify({ coding: cards.coding && { text: cards.coding.text, primary: cards.coding.primary }, flash: cards.flash && cards.flash.text }));

    await fireFocus(page);
    await page.waitForTimeout(1500);
    cards = await page.evaluate(cardReader);
    note("cards after focus refresh: " + JSON.stringify(cards));
    const freshFetches = fetchCount();
    check("P2 focus/visibility refresh fetched /owner/api/state", freshFetches > initialFetches,
      "fetches " + initialFetches + " -> " + freshFetches);
    const body2 = lastFetchBody();
    check("P2 the fetched state carries the mutated primary (fresh data)",
      body2 && body2.coding && body2.coding.primary && body2.coding.primary.model === "kimi-smoke2b",
      JSON.stringify(body2 && body2.coding && body2.coding.primary));
    check("P2 refreshed coding card renders the mutated primary",
      cards.coding && cards.coding.primary === "OpenCode Go / kimi-smoke2b", cards.coding && cards.coding.primary);
    check("P2 refreshed coding card flips Cooldown -> Healthy (circuit cleared server-side)",
      cards.coding && cards.coding.text === "coding · Healthy", cards.coding && cards.coding.text);
    check("P2 refreshed flash card is Healthy again with a fresh as-of line",
      cards.flash && cards.flash.text === "flash · Healthy" && /^As of \d.* · refetches when this tab regains focus\.$/.test(cards.flash.freshness),
      JSON.stringify(cards.flash));
    await shot(page, "02-fresh-after-focus.png");

    // ------------- P3: dirty NEW-alias draft + preview survive --------------
    console.log("P3: dirty new-alias draft + open preview survive >=60s stale + focus refresh");
    await page.click('nav.tabs button:has-text("Models & Routes")');
    await page.waitForSelector('label:has-text("Alias name") input');
    await page.fill('label:has-text("Alias name") input', "f5_draft");
    await page.selectOption('label:has-text("Primary provider") select', "opencode_go");
    await page.selectOption('label:has-text("Primary model") select', "kimi-smoke2");
    await page.fill('label:has-text("Local cooldown (seconds)") input', "555");
    await page.click('button:text-is("Save alias")');
    await page.waitForSelector(".save-preview:not([hidden])");
    const draftBefore = await page.evaluate(draftReader);
    note("draft+preview before stale window: " + JSON.stringify(draftBefore));
    check("P3 new-alias draft dirty and preview open before the stale window",
      draftBefore.name === "f5_draft" && draftBefore.cooldown === "555" && draftBefore.previewOpen &&
      draftBefore.previewTitle === "Preview — nothing has been saved", JSON.stringify(draftBefore));

    const p3FetchMark = fetchCount();
    const r3 = await mutate(page, "/owner/api/alias", {
      action: "save", name: "coding",
      primary: { provider: "opencode_go", model: "kimi-smoke2b" },
      fallbacks: [{ provider: "anthropic", model: "claude-fixture" }],
      fallback_triggers: TRIGGERS, fallback_authorized: true,
      fallback_confirmed_providers: ["anthropic"],
      cooldown_seconds: 444, enabled: true,
    }, "coding cooldown_seconds -> 444 (server-side, while draft open)");
    check("P3 server-side mutation accepted while the draft is open", r3.status === 200 && r3.body.ok === true);

    note("waiting >=61s with the dirty draft and preview open...");
    await page.waitForTimeout(61000);
    const draftMid = await page.evaluate(draftReader);
    check("P3 draft + preview survive the full >=60s stale window untouched",
      draftMid.name === "f5_draft" && draftMid.cooldown === "555" && draftMid.previewOpen, JSON.stringify(draftMid));
    check("P3 no fetch happened during the stale window (no auto-poll)",
      fetchCount() === p3FetchMark, "fetches " + p3FetchMark + " -> " + fetchCount());

    await fireFocus(page);
    await page.waitForTimeout(1500);
    const draftAfter = await page.evaluate(draftReader);
    const body3 = lastFetchBody();
    note("draft+preview after focus refresh: " + JSON.stringify(draftAfter));
    check("P3 focus refresh with a NEW-alias draft open DID fetch (candidate contract: refetchUnlessBusy + preserveDraft)",
      fetchCount() > p3FetchMark, "fetches " + p3FetchMark + " -> " + fetchCount());
    check("P3 the focus fetch carried the fresh mutated state (coding cooldown 444)",
      body3 && body3.coding && body3.coding.cooldown_seconds === 444, JSON.stringify(body3 && body3.coding && body3.coding.cooldown_seconds));
    check("P3 new-alias draft name survived the refresh", draftAfter.name === "f5_draft", draftAfter.name);
    check("P3 new-alias draft cooldown survived the refresh", draftAfter.cooldown === "555", draftAfter.cooldown);
    check("P3 open save preview survived the refresh",
      draftAfter.previewOpen && draftAfter.previewTitle === "Preview — nothing has been saved", JSON.stringify(draftAfter));
    await shot(page, "03-new-draft-survives.png");

    // --------- P4: dirty EXISTING-alias draft + preview survive -------------
    console.log("P4: dirty existing-alias (coding) draft + preview survive a further server mutation");
    const codingRoute = page.locator(".route").filter({ has: page.locator('strong:text-is("coding")') });
    await codingRoute.locator('button:text-is("Edit")').click();
    await page.waitForSelector('label:has-text("Alias name") input[disabled]');
    await page.fill('label:has-text("Local cooldown (seconds)") input', "777");
    await page.click('button:text-is("Save changes")');
    await page.waitForSelector(".save-preview:not([hidden])");
    const editBefore = await page.evaluate(draftReader);
    note("existing-alias draft+preview before mutation: " + JSON.stringify(editBefore));
    check("P4 existing-alias draft dirty (cooldown 777) with preview open",
      editBefore.name === "coding" && editBefore.nameDisabled && editBefore.cooldown === "777" && editBefore.previewOpen,
      JSON.stringify(editBefore));

    const p4FetchMark = fetchCount();
    const r4 = await mutate(page, "/owner/api/alias", {
      action: "save", name: "coding",
      primary: { provider: "opencode_go", model: "kimi-smoke2b" },
      fallbacks: [{ provider: "anthropic", model: "claude-fixture" }],
      fallback_triggers: TRIGGERS, fallback_authorized: true,
      fallback_confirmed_providers: ["anthropic"],
      cooldown_seconds: 333, enabled: true,
    }, "coding cooldown_seconds -> 333 (server-side, while edit draft open)");
    check("P4 server-side mutation accepted while the edit draft is open", r4.status === 200 && r4.body.ok === true);

    await fireFocus(page);
    await page.waitForTimeout(1500);
    const editAfter = await page.evaluate(draftReader);
    const body4 = lastFetchBody();
    note("existing-alias draft+preview after focus refresh: " + JSON.stringify(editAfter));
    check("P4 focus refresh with an EXISTING-alias draft open DID fetch",
      fetchCount() > p4FetchMark, "fetches " + p4FetchMark + " -> " + fetchCount());
    check("P4 the focus fetch carried the fresh mutated state (coding cooldown 333)",
      body4 && body4.coding && body4.coding.cooldown_seconds === 333, JSON.stringify(body4 && body4.coding && body4.coding.cooldown_seconds));
    check("P4 dirty draft cooldown kept its unsaved 777 — NOT overwritten by the fresh server value 333",
      editAfter.cooldown === "777", "draft=" + editAfter.cooldown + " server=" + (body4 && body4.coding && body4.coding.cooldown_seconds));
    check("P4 edit draft name kept (coding, disabled)", editAfter.name === "coding" && editAfter.nameDisabled, JSON.stringify(editAfter));
    check("P4 open save preview survived the refresh", editAfter.previewOpen, JSON.stringify(editAfter));
    await shot(page, "04-existing-draft-survives.png");

    // ------------- P5: fresh cards, fail-closed refresh, recovery -----------
    console.log("P5: fresh cards again; forced refresh failure fail-closes, then recovers");
    await page.click('button:text-is("Cancel edit")');
    await page.click('nav.tabs button:has-text("Operate now")');
    await page.waitForSelector(".route-health-card");
    cards = await page.evaluate(cardReader);
    note("status tab after drafts: " + JSON.stringify(cards));
    check("P5 status tab shows fresh mutated data (coding Healthy, kimi-smoke2b)",
      cards.coding && cards.coding.text === "coding · Healthy" && cards.coding.primary === "OpenCode Go / kimi-smoke2b",
      JSON.stringify(cards.coding));

    await page.route("**/owner/api/state", (route) => route.abort());
    await fireFocus(page);
    await page.waitForTimeout(1500);
    cards = await page.evaluate(cardReader);
    const toastText = await page.evaluate(() => {
      const host = document.getElementById("toast");
      return host ? host.textContent : "";
    });
    note("cards after forced refresh failure: " + JSON.stringify(cards));
    check("P5 failed focus refresh marks routing health stale/unavailable, never Healthy",
      Object.values(cards).every((c) => !/· Healthy/.test(c.text)) &&
      cards.flash && /refresh unavailable/.test(cards.flash.freshness),
      JSON.stringify(cards.flash));
    check("P5 a bad-refresh toast is raised",
      /Routing health refresh unavailable/.test(toastText), toastText.slice(0, 120));
    await shot(page, "06-refresh-unavailable.png");

    await page.unroute("**/owner/api/state");
    await fireFocus(page);
    await page.waitForTimeout(1500);
    cards = await page.evaluate(cardReader);
    check("P5 next successful focus refresh recovers to fresh Healthy",
      cards.flash && cards.flash.text === "flash · Healthy" && /^As of \d.* · refetches when this tab regains focus\.$/.test(cards.flash.freshness),
      JSON.stringify(cards.flash));
    await shot(page, "07-recovered.png");

    // ------------------- inherited-assertion evidence -----------------------
    console.log("inherited QA fetch-count assertions (qa_browser2.js:405 / :410) — measured on this candidate:");
    note("total page-owned /owner/api/state fetches this run: " + fetchCount());
    stateFetches.forEach((f, i) => note(`  fetch #${i + 1} at +${((f.at - stateFetches[0].at) / 1000).toFixed(1)}s` +
      (f.body ? ` revision=${f.body.revision} coding.cooldown_seconds=${f.body.coding && f.body.coding.cooldown_seconds}` : "")));

    const report = {
      candidate_base: BASE,
      passed,
      failed: failures.length,
      failures,
      total_state_fetches: fetchCount(),
      fetch_log: stateFetches.map((f, i) => ({
        index: i + 1,
        seconds_after_first: Number(((f.at - stateFetches[0].at) / 1000).toFixed(1)),
        revision: f.body && f.body.revision,
        coding_primary: f.body && f.body.coding && f.body.coding.primary,
        coding_cooldown_seconds: f.body && f.body.coding && f.body.coding.cooldown_seconds,
      })),
      generated_at: new Date().toISOString(),
    };
    fs.writeFileSync(path.join(EVIDENCE, "f5-browser-report.json"), JSON.stringify(report, null, 2) + "\n");
    console.log("evidence: " + path.join(EVIDENCE, "f5-browser-report.json"));
  } finally {
    await browser.close();
  }

  console.log(`\n${passed} passed, ${failures.length} failed`);
  if (failures.length) {
    failures.forEach((f) => console.log("FAILED: " + f));
    process.exit(1);
  }
})().catch((e) => { console.error("HARNESS ERROR", e); process.exit(2); });
