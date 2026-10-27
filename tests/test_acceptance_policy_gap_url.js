"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const {SAFE_URL_MESSAGE, validateBaseUrl, run} = require("./acceptance_policy_gap_browser.js");

test("accepts explicit localhost and IPv4 loopback ports", () => {
  assert.equal(validateBaseUrl("http://localhost:8080"), "http://localhost:8080");
  assert.equal(validateBaseUrl("http://127.0.0.1:65535/"), "http://127.0.0.1:65535");
});

test("rejects unsafe URL classes before browser or mutation transport", async () => {
  const rejected = [
    "http://localhost.evil.example:8080", // hostname lookalike
    "http://[::1]:8080",                 // IPv6, even loopback
    "http://192.168.1.5:8080",            // non-loopback
    "https://localhost:8080",             // non-HTTP scheme
    "https://production.example:443",      // production URL
    "http://user:secret@localhost:8080",   // userinfo credentials
    "http://localhost:8080@evil.example",  // authority/userinfo trick
    "file://localhost:8080",               // non-HTTP scheme
    "http://localhost",                    // absent port
    "http://localhost:0",                  // invalid port
    "http://localhost:65536",              // out-of-range port
    "http://localhost:abc",                // invalid port
    "http://127.1:8080",                   // parser-normalized IPv4 shorthand
    "http://2130706433:8080",              // integer IPv4 shorthand
    "http://localhost:8080/owner",         // not a base origin
    "http://localhost:8080/?x=1",          // query not allowed
  ];
  let launches = 0;
  let navigations = 0;
  let mutations = 0;
  const fakeChromium = {launch: async () => {
    launches++;
    return {newPage: async () => ({goto: async () => { navigations++; }, evaluate: async () => { mutations++; }}), close: async () => {}};
  }};
  for (const value of rejected) {
    assert.throws(() => validateBaseUrl(value), new RegExp(SAFE_URL_MESSAGE.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
    await assert.rejects(run(value, fakeChromium), new RegExp(SAFE_URL_MESSAGE.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
  }
  assert.deepEqual({launches, navigations, mutations}, {launches: 0, navigations: 0, mutations: 0});
});
