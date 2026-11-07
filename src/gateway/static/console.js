/* Personal LLM Gateway console.
 *
 * One page, no framework, no build step. It talks directly to the same-origin
 * trusted-LAN console; browser mutations are protected by canonical Origin checks.
 *
 * Rules the UI keeps:
 *   - a provider is always labelled next to a model id;
 *   - unknown limits are shown as Unknown, with a source and an as-of time;
 *   - a caller token is shown once, as the direct result of issuing it;
 *   - a saved credential is never asked for again and never displayed.
 */
(function (global) {
  "use strict";

  var BASE = (global.GATEWAY_BASE || "");
  var app = null;
  var state = null;
  var tab = "status";
  var busy = false;
  var editingAlias = null;
  // Which window the usage table shows: the live 24-hour ring, or one UTC
  // day from the durable daily rollup. Kept across redraws so switching
  // sections does not silently reset the owner's choice.
  var usageDay = "live";
  var statisticsWindow = "day";
  // The leaderboard lives on its own endpoint (GET <mount>/api/leaderboard)
  // and is fetched only when its tab is open, so the state poll keeps its
  // sub-2s bar (R56). "idle" means "no payload yet": the tab's first draw
  // triggers the fetch, and a completed fetch redraws the tab.
  var leaderboardWindow = 7;      // days requested from /api/leaderboard
  var leaderboardPayload = null;  // last successful payload
  var leaderboardPhase = "idle";  // idle | loading | ready | error
  var leaderboardError = null;
  // Routing health renders one /api/state snapshot. The fetch time is
  // tracked so every card can show an as-of time, the tab can refetch when
  // it regains focus, and no card may claim Healthy once the snapshot is
  // older than the staleness threshold (fail-closed on stale data).
  var STALE_AFTER_MS = 60000;
  var lastFetchAt = null;
  var refreshUnavailable = false;

  // --- tiny helpers -----------------------------------------------------

  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (key) {
        if (key === "class") node.className = attrs[key];
        else if (key === "text") node.textContent = attrs[key];
        else if (key === "html") node.innerHTML = attrs[key];
        else if (key.slice(0, 2) === "on") node.addEventListener(key.slice(2), attrs[key]);
        else if (attrs[key] === true) node.setAttribute(key, "");
        else if (attrs[key] === false) node.removeAttribute(key);
        else if (attrs[key] !== null && attrs[key] !== undefined) node.setAttribute(key, attrs[key]);
      });
    }
    (children || []).forEach(function (child) {
      if (child === null || child === undefined) return;
      node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
    });
    return node;
  }

  function api(path, options) {
    options = options || {};
    var headers = options.headers || {};
    if (options.body !== undefined) headers["Content-Type"] = "application/json";
    return fetch(path, {
      method: options.method || "GET",
      headers: headers,
      body: options.body === undefined ? undefined : JSON.stringify(options.body)
    }).then(function (response) {
      var type = response.headers.get("content-type") || "";
      if (type.indexOf("application/json") === -1) {
        return response.text().then(function (text) {
          if (!response.ok) throw new Error(text || ("request failed: " + response.status));
          return text;
        });
      }
      return response.json().then(function (data) {
        if (!response.ok) {
          var serverError = data && data.error;
          var message = (serverError && serverError.message) || ("request failed: " + response.status);
          if (serverError && serverError.field) message += " (field: " + serverError.field + ")";
          // A 429 carries its wait only in Retry-After; the owner is told to
          // wait, so the number belongs in the message. Parsed, never echoed
          // as free text, so a stubbed header can never leak into a message.
          if (response.status === 429 && response.headers && response.headers.get) {
            var retryAfter = response.headers.get("retry-after");
            if (retryAfter && /^\d+$/.test(String(retryAfter))) {
              message += " — retry in " + String(retryAfter) + "s";
            }
          }
          var err = new Error(message);
          err.payload = data;
          throw err;
        }
        return data;
      });
    });
  }

  // The toast is the console's feedback channel, so it is a live region:
  // appended outside #app, it must still announce (WCAG 4.1.3).
  function toast(message, kind) {
    var host = document.getElementById("toast");
    if (!host) {
      host = el("div", { id: "toast", role: "status", "aria-live": "polite", "aria-atomic": "false" });
      document.body.appendChild(host);
    }
    var node = el("div", { class: "msg " + (kind || "") }, [message]);
    host.appendChild(node);
    setTimeout(function () { node.remove(); }, kind === "bad" ? 9000 : 5000);
  }

  function setBusy(flag) {
    busy = flag;
    if (!document.body) return;
    if (flag) document.body.setAttribute("aria-busy", "true");
    else document.body.removeAttribute("aria-busy");
  }

  function run(promise, successMessage) {
    if (busy) {
      // A dropped click has to say so: no request is sent, and the owner
      // learns the previous one is still in flight.
      toast("Still working on the previous action — nothing new was sent.", "warn");
      return Promise.resolve();
    }
    setBusy(true);
    return promise
      .then(function (result) {
        if (successMessage) toast(successMessage, "ok");
        return result;
      })
      .catch(function (error) {
        toast(error.message || "something went wrong", "bad");
        throw error;
      })
      .finally(function () { setBusy(false); });
  }

  // --- formatting -------------------------------------------------------

  // Dates arrive in two shapes: ISO strings (fixture/tests) and epoch
  // seconds from /owner/api/machine-keys. A bare number below 1e12 is
  // seconds, not milliseconds — feeding it to new Date() is what painted
  // 1/21/1970 on every key's "Last used".
  function when(value) {
    if (!value) return "Unknown";
    try {
      var stamp = value;
      if (typeof value === "number" && value < 1e12) stamp = value * 1000;
      var date = new Date(stamp);
      if (isNaN(date.getTime())) return value;
      return date.toLocaleString();
    } catch (e) {
      return value;
    }
  }

  function tokens(value) {
    if (value === null || value === undefined) return "Unknown";
    if (value >= 1000000) return (value / 1000000).toFixed(value % 1000000 === 0 ? 0 : 2) + "M";
    if (value >= 1000) return Math.round(value / 1000) + "K";
    return String(value);
  }

  function dataAgeMs() {
    return lastFetchAt == null ? null : Date.now() - lastFetchAt;
  }

  function ageText(ms) {
    var total = Math.max(0, Math.round(ms / 1000));
    if (total < 90) return total + "s";
    var minutes = Math.round(total / 60);
    if (minutes < 90) return minutes + "m";
    return Math.round(minutes / 60) + "h";
  }

  // The freshness line every routing-health card carries: the as-of time of
  // the snapshot, what keeps it honest (an automatic refetch when the tab
  // regains focus) and, once stale, the age of the data being shown.
  function freshnessText() {
    if (lastFetchAt == null) {
      return "As of Unknown — this view has not been fetched from the console API; routing health is unavailable.";
    }
    var asOf = new Date(lastFetchAt).toLocaleTimeString();
    var age = dataAgeMs();
    if (refreshUnavailable) {
      return "As of " + asOf + " · refresh unavailable — showing the last known state; routing health is stale.";
    }
    if (age > STALE_AFTER_MS) {
      return "As of " + asOf + " · " + ageText(age) + " old — showing the last known state; it refetches when this tab regains focus.";
    }
    return "As of " + asOf + " · refetches when this tab regains focus.";
  }

  function money(value) {
    if (value === null || value === undefined) return "Unknown";
    return "$" + Number(value).toFixed(2);
  }

  function limitCell(entry) {
    if (!entry) return "Unknown";
    var text = entry.monthly_limit_usd === null || entry.monthly_limit_usd === undefined
      ? "Unknown"
      : money(entry.monthly_limit_usd);
    var source = entry.limits_source || "unknown";
    return el("div", {}, [
      el("div", { text: text }),
      el("div", { class: "small muted", text: "reference only, not enforced · source: " + source })
    ]);
  }

  function providerLabel(providerId) {
    if (providerId === "anthropic") return "Anthropic";
    if (providerId === "chatgpt_codex") return "ChatGPT / Codex";
    if (providerId === "opencode_go") return "OpenCode Go";
    if (providerId === "opencode_console") return "OpenCode Console Jev / System One";
    if (providerId === "ollama") return "Ollama (local)";
    if (providerId === "llama_cpp") return "llama.cpp server (local)";
    if (providerId === "lm_studio") return "LM Studio (local)";
    return providerId;
  }

  // The three providers whose destination the owner types themselves. They
  // share one card layout: an endpoint field, an optional token, and the
  // local-only warnings that a cloud provider card has no reason to carry.
  var LOCAL_PROVIDER_IDS = { ollama: true, llama_cpp: true, lm_studio: true };

  function isLocalProvider(providerId) {
    return Object.prototype.hasOwnProperty.call(LOCAL_PROVIDER_IDS, providerId);
  }

  // Bytes for the durability store figures the status card shows: the store
  // of record, its row count and the journal tail's size are the numbers
  // that prove the retention window is real, so they are rendered rather
  // than left as an inference.
  function fmtBytes(value) {
    if (typeof value !== "number" || !isFinite(value)) return "";
    if (value >= 1048576) return (value / 1048576).toFixed(1) + " MiB";
    if (value >= 1024) return Math.round(value / 1024) + " KiB";
    return value + " B";
  }

  function ledgerStoreDetail(durability) {
    var store = durability && durability.store;
    if (!store) return "";
    var parts = [];
    if (typeof store.bytes === "number") parts.push(fmtBytes(store.bytes) + " store");
    if (typeof store.rows === "number") parts.push(store.rows.toLocaleString() + " rows");
    if (store.oldest_row_ts) parts.push("oldest row " + store.oldest_row_ts);
    if (typeof store.journal_bytes === "number") parts.push(fmtBytes(store.journal_bytes) + " journal tail");
    if (store.error) parts.push("store error: " + store.error);
    return parts.length ? " On disk: " + parts.join(" · ") + "." : "";
  }

  function statePill(value) {
    var map = {
      connected: "ok",
      failed: "bad",
      configured_untested: "warn",
      not_configured: ""
    };
    return el("span", { class: "pill " + (map[value] || ""), text: value.replace(/_/g, " ") });
  }

  function dialectName(value) {
    if (value === "openai_responses") return "Responses";
    if (value === "openai_chat") return "Chat Completions";
    if (value === "anthropic_messages") return "Anthropic Messages";
    if (value === "google_v1beta") return "Google v1beta";
    return value || "Unknown";
  }

  function emptyState(title, detail) {
    return el("div", { class: "empty-state", role: "status", "aria-live": "polite" }, [
      el("strong", { text: title }),
      el("p", { class: "small", text: detail })
    ]);
  }

  // --- shell ------------------------------------------------------------

  function ownerName() {
    return (state && state.owner && state.owner.username) || "owner";
  }

  // The client-key state the server reports: presence and transition only,
  // never a hash and never the value (the value is not stored in a form the
  // server could give back).
  function clientKeyState() {
    return (state && state.access && state.access.client_key) || {};
  }

  // The two boundaries, stated plainly and separately: the console needs an
  // owner sign-in; the model API is keyless unless a client key is set.
  function accessNotice() {
    var key = clientKeyState();
    if (key.enabled) {
      return "Owner sign-in protects this console (signed in as " + ownerName() + "). " +
        "The model API requires the client API key" + (key.display_id ? " (id " + key.display_id + ")" : "") +
        ", sent as an Authorization: Bearer header.";
    }
    return "Owner sign-in protects this console (signed in as " + ownerName() + "). " +
      "The model API is keyless by default — any device on the LAN can call /v1 until you set " +
      "a client API key in Settings → Access.";
  }

  function signOut() {
    api(BASE + "/api/auth/logout", { method: "POST", body: {} })
      .then(function () {
        state = null;
        renderAuthView({ authenticated: false });
      })
      .catch(function (error) {
        toast(error.message || "sign-out failed", "bad");
      });
  }

  var TABS = [
    { id: "status", label: "Operate now" },
    { id: "providers", label: "Providers" },
    { id: "models", label: "Models & Routes" },
    { id: "profiles", label: "Profiles" },
    { id: "leaderboard", label: "Leaderboard" },
    { id: "settings", label: "Settings" },
    { id: "backup", label: "Backup" }
  ];

  function renderShell() {
    app.innerHTML = "";
    var header = el("header", { class: "top" }, [
      el("h1", { text: "Personal LLM Gateway" })
    ]);
    var nav = el("nav", { class: "tabs", "aria-label": "Sections" });
    TABS.forEach(function (item) {
      nav.appendChild(el("button", {
        type: "button",
        "aria-current": tab === item.id ? "page" : null,
        text: item.label,
        onclick: function () {
          tab = item.id;
          // The machine-key list is read on entry (and after each of its
          // own mutations), never from the state snapshot: key metadata
          // deliberately does not travel in /owner/api/state.
          if (item.id === "settings") fetchMachineKeys();
          draw();
        }
      }));
    });
    header.appendChild(nav);
    header.appendChild(el("span", { class: "spacer" }));
    if (state) {
      header.appendChild(el("span", { class: "pill warn", text: "signed in as " + ownerName() }));
      header.appendChild(el("span", { class: "small muted", text: "rev " + state.revision }));
      header.appendChild(el("button", { type: "button", text: "Sign out", onclick: signOut }));
    }
    app.appendChild(header);
    app.appendChild(el("aside", { class: "card", role: "note", text: accessNotice() }));
    // One grouped, single-announcement card instead of one role=alert card
    // per gap: six gaps used to be six live-region announcements per redraw.
    var gaps = (state && state.fallback_policy_warnings) || [];
    if (gaps.length) {
      var gapList = el("ul");
      gaps.forEach(function (warning) { gapList.appendChild(el("li", { text: String(warning) })); });
      app.appendChild(el("aside", {
        class: "card warn policy-gaps",
        role: "alert",
        "aria-label": "Policy gaps"
      }, [
        el("strong", { text: "Policy gap: " + (gaps.length === 1 ? "1 open gap" : gaps.length + " open gaps") }),
        gapList
      ]));
    }

    var main = el("main", { id: "main" });
    app.appendChild(main);
  }

  // A redraw rebuilds the whole DOM, so without this the focused element
  // falls back to <body> after every save, mint or tab switch and the next
  // Tab starts again from the top of the document. The key is the element's
  // id where it has one, otherwise its tag plus its visible label.
  function focusKeyOf(node) {
    if (!node || node === document.body) return null;
    var id = node.id || (node.getAttribute && node.getAttribute("id"));
    if (id) return { id: String(id) };
    var tag = node.tag || node.tagName;
    var text = String(node.textContent || "").replace(/\s+/g, " ").trim().slice(0, 80);
    if (!tag || !text) return null;
    return { tag: String(tag).toLowerCase(), text: text };
  }

  function restoreFocus(key) {
    if (!key || !document) return;
    var target = null;
    if (key.id && document.getElementById) target = document.getElementById(key.id);
    if (!target && key.tag && document.querySelectorAll) {
      var nodes = document.querySelectorAll(key.tag);
      Array.prototype.forEach.call(nodes, function (node) {
        if (target) return;
        if (focusKeyOf(node) && focusKeyOf(node).text === key.text) target = node;
      });
    }
    if (target && typeof target.focus === "function") target.focus();
  }

  function draw() {
    var focusKey = focusKeyOf(document.activeElement);
    renderShell();
    var main = document.getElementById("main");
    if (!state) {
      main.appendChild(el("p", { class: "muted", text: "Loading…" }));
      restoreFocus(focusKey);
      return;
    }

    var renderer = {
      status: renderStatus,
      providers: renderProviders,
      models: renderModels,
      profiles: renderProfiles,
      leaderboard: renderLeaderboard,
      settings: renderSettings,
      backup: renderBackup
    }[tab] || renderStatus;
    main.appendChild(renderer());
    restoreFocus(focusKey);
  }


  // --- status -----------------------------------------------------------

  function renderStatus() {
    var frag = document.createDocumentFragment();
    var aliases = Array.isArray(state.aliases) ? state.aliases.filter(function (alias) { return alias.enabled; }) : [];
    // The empty-state sentence lives once, in routingHealthSummary() below;
    // this header line only points at what to do next.
    var healthIntro = aliases.length
      ? "Read-only view of published aliases and their configured fallback policy."
      : "Nothing published yet — create and publish an alias in Models & Routes and its routing health appears here.";
    var health = el("section", { class: "routing-health", "aria-labelledby": "routing-health-title" }, [
      el("div", { class: "card" }, [
        el("h2", { id: "routing-health-title", text: "Operate now · Routing health" }),
        el("p", { class: "small muted", text: healthIntro }),
        routingHealthSummary(aliases)
      ])
    ]);
    frag.appendChild(health);
    var details = el("details", { class: "status-details", open: !state.initialized }, [
      el("summary", { text: "More status details · setup, providers, history and cost" })
    ]);

    if (!state.initialized) {
      details.appendChild(el("section", {}, [
        el("div", { class: "card" }, [
          el("h2", { text: "Set up your gateway" }),
          el("p", {
            class: "small muted",
            text: "Initialize gateway configuration, then work through the checklist. Nothing here needs a shell command, a file edit, or a JSON edit."
          }),
          renderChecklist()
        ])
      ]));
    } else {
      details.appendChild(el("section", {}, [
        el("div", { class: "card" }, [el("h2", { text: "Gateway status" }), renderChecklist()])
      ]));
    }

    var release = state.release || {};
    details.appendChild(el("section", {}, [
      el("div", { class: "card" }, [
        el("h2", { text: "Release identity" }),
        el("dl", { class: "kv" }, [
          el("dt", { text: "Service" }), el("dd", { text: release.service || "personal-llm-gateway" }),
          el("dt", { text: "Source revision" }), el("dd", { text: release.source_revision || "unknown" }),
          el("dt", { text: "Source tree" }), el("dd", { text: release.source_tree || "unknown" }),
          el("dt", { text: "Image revision" }), el("dd", { text: release.image_revision || "unknown" })
        ]),
        el("p", { class: "small muted", text: "The same identity is served at " + (state.base_url || "/version") + "/version." })
      ])
    ]));

    details.appendChild(el("section", {}, [
      el("div", { class: "card" }, [
        el("h2", { text: "Access mode" }),
        el("p", { class: "small", text: state.access_mode_description || "" }),
        el("dl", { class: "kv" }, [
          el("dt", { text: "Active listener" }), el("dd", { text: state.base_url || "unknown" }),
          el("dt", { text: "Surface" }), el("dd", { text: state.surface === "setup" ? "Setup UI (loopback only)" : "Owner console (LAN)" }),
          el("dt", { text: "Published models" }), el("dd", { text: state.published_models.length ? state.published_models.join(", ") : "none" })
        ]),
        state.published_models.length === 0
          ? el("div", { class: "notice warn" }, [
              el("strong", { text: "/v1/models is empty. " }),
              el("span", { text: "It stays empty until at least one enabled alias is published." })
            ])
          : null
      ])
    ]));

    var providers = el("div", { class: "card" }, [el("h2", { text: "Providers" })]);
    if (!state.providers.length) {
      providers.appendChild(el("p", { class: "muted", text: "No providers configured." }));
    }
    state.providers.forEach(function (provider) {
      providers.appendChild(el("div", { class: "route" }, [
        el("div", { class: "head" }, [
          el("strong", { text: providerLabel(provider.provider_id) }),
          statePill(provider.state)
        ]),
        el("div", {
          class: "small muted",
          text: "credential: " + (provider.credential_present ? "stored" : "not stored") +
            " · last test: " + when(provider.last_test_at) +
            (provider.last_error ? " · " + provider.last_error : "")
        })
      ]));
    });
    details.appendChild(el("section", {}, [providers]));

    var healthCard = el("div", { class: "card" }, [el("h2", { text: "Provider health" })]);
    (state.provider_health || []).forEach(function (health) {
      healthCard.appendChild(el("div", { class: "route" }, [
        el("div", { class: "head" }, [
          el("strong", { text: providerLabel(health.provider_id) }),
          statePill(health.connection_state || "unknown")
        ]),
        el("div", { class: "small", text: "Last test: " + (health.last_test_outcome || "unknown") +
          " · catalogue: " + when(health.catalog_observed_at) +
          " · models: " + (health.model_count === undefined ? "Unknown" : health.model_count) +
          " · cooldown: " + (health.cooldown_remaining_seconds || 0) + "s" }),
        el("div", { class: "small muted", text: "Recent outcomes: " + (health.recent_success_rate === null || health.recent_success_rate === undefined ? "Unknown" : Math.round(health.recent_success_rate * 100) + "% success") +
          " / " + (health.recent_failure_rate === null || health.recent_failure_rate === undefined ? "Unknown" : Math.round(health.recent_failure_rate * 100) + "% failure") +
          " (" + (health.recent_requests || 0) + " requests)" }),
        health.last_error ? el("div", { class: "small muted", text: "Last error: " + health.last_error }) : null
      ]));
    });
    if (!state.provider_health || !state.provider_health.length) {
      healthCard.appendChild(el("p", { class: "muted", text: "Provider health is Unknown until providers are configured." }));
    }
    frag.appendChild(el("section", {}, [healthCard]));

    var totals = state.totals || {};
    var ledger = state.ledger || [];
    var ledgerCard = el("div", { class: "card" }, [
      el("h2", { text: "Recent request outcomes" }),
      el("a", { class: "button", href: BASE + "/api/ledger/export", text: "Download CSV" }),
      el("a", { class: "button secondary", href: BASE + "/api/ledger/export?format=json", text: "Download JSON" }),
      el("p", { class: "small muted", text: "Metadata only. Prompts, responses, tokens and secrets are never stored." }),
      el("p", { class: "small muted", text: (state.ledger_durability && state.ledger_durability.enabled ? "Recent outcomes are retained on the state volume for up to 30 days, bound by age alone - never by file size; the in-memory ring is the hot view. Persistence is asynchronous and may lag briefly." + ledgerStoreDetail(state.ledger_durability) + " This metadata ledger is not a billing system." : "Durable ledger storage is not enabled; only the bounded in-memory ring is available and is lost when the gateway restarts. This metadata ledger is not a billing system.") }),
      el("div", { class: "row" }, [
        statBox("Requests", totals.requests || 0),
        statBox("Errors", totals.errors || 0),
        statBox("Fallbacks", totals.fallbacks || 0),
        statBox("Rate limited", totals.rate_limited || 0),
        statBox("Exhausted", totals.exhausted || 0)
      ])
    ]);
    if (ledger.length) {
      var table = el("table", {}, [
        el("thead", {}, [el("tr", {}, [
          el("th", { text: "When" }), el("th", { text: "Alias" }),
          el("th", { text: "Served by" }), el("th", { text: "Outcome" }),
          el("th", { text: "Cost estimate" }), el("th", { text: "Note" })
        ])])
      ]);
      var tbody = el("tbody");
      ledger.forEach(function (event) {
        tbody.appendChild(el("tr", {}, [
          el("td", { class: "small", text: when(event.timestamp) }),
          el("td", { text: event.requested_alias }),
          el("td", { class: "small", text: event.serving_provider ? providerLabel(event.serving_provider) + " → " + event.serving_model : "—" }),
          el("td", { class: "small", text: event.error_class ? event.error_class : ("status " + (event.status || "")) + (event.fallback_used ? " (fallback)" : "") }),
          el("td", { class: "small", text: event.cost && event.cost.amount_usd != null ? "$" + Number(event.cost.amount_usd).toFixed(6) + " · " + event.cost.source + " · " + event.cost.observed_at : (event.cost && event.cost.reason === "missing_cached_tokens" ? "Unknown - cached-token count not reported" : (event.cost && event.cost.reason === "no_published_price" ? "Unknown - no published actual/list price" : "Unknown")) }),
          el("td", { class: "small muted", text: ledgerNote(event) })
        ]));
      });
      table.appendChild(tbody);
      ledgerCard.appendChild(table);
    }
    details.appendChild(el("section", {}, [ledgerCard]));

    var costs = state.usage_costs || { window_hours: 24, alias: {}, provider: {}, model: {} };
    var analytics = state.analytics || {
      available: false, unavailable_reason: "state_has_no_analytics", series: [], usage: {}, gaps: [], fallback: {}
    };
    // A selected day only stays selected while the payload still carries it;
    // a stale selection falls back to the live ring rather than silently
    // showing nothing.
    var daySelected = usageDay !== "live";
    var dayKnown = daySelected && analytics.available &&
      Object.prototype.hasOwnProperty.call(analytics.usage || {}, usageDay);
    if (daySelected && !dayKnown) usageDay = "live";
    var usageGroups = dayKnown
      ? analytics.usage[usageDay]
      : { alias: costs.alias, provider: costs.provider, model: costs.model };

    var costsCard = el("div", { class: "card" }, [el("h2", { text: "Local usage and estimated cost" })]);
    if (dayKnown) {
      costsCard.appendChild(el("p", { class: "small muted", text: "UTC day " + usageDay + ", from the durable daily rollup (30-day retention, rebuilt from the request store on every start). Prices are applied at read time from the published catalogue; " + (analytics.coverage && analytics.coverage.incomplete ? "the rollup's coverage is incomplete, so this day may be partial." : "every retained row for the day is included.") + " Estimates are not provider bills." }));
    } else {
      costsCard.appendChild(el("p", { class: "small muted", text: "Last " + costs.window_hours + " hours. Based on reported token counts and published prices only; estimates are not provider bills." }));
      costsCard.appendChild(el("p", { class: "small muted", text: (costs.coverage && costs.coverage.incomplete ? "The bounded recent-event ring may omit older events from this window; affected cost estimates are Unknown." : "Aggregates cover the available recent-event ring for the last " + costs.window_hours + " hours.") + " " + (state.ledger_durability && state.ledger_durability.enabled ? "The metadata ledger is persisted asynchronously with an age-based 30-day retention window (no size cap)." : "Durable ledger storage is not enabled; the in-memory ring is lost when the gateway restarts.") + " This is not a billing system." }));
    }
    if (analytics.available && (analytics.series || []).length) {
      costsCard.appendChild(el("div", { class: "row" }, [
        el("span", { class: "small muted", text: "Window" }),
        usageDaySelector(costs, analytics, dayKnown)
      ]));
    }
    if (dayKnown && !usageGroups) {
      costsCard.appendChild(emptyState(
        "No rows recorded for " + usageDay,
        "The rollup has no rows for this day. That is a gap in the record, not zero usage."
      ));
    }
    if (usageGroups) {
      [ ["Alias", usageGroups.alias], ["Provider", usageGroups.provider], ["Model", usageGroups.model] ].forEach(function (group) {
        var entries = Object.keys(group[1] || {}).sort();
        if (!entries.length) return;
        costsCard.appendChild(el("h3", { text: group[0] }));
        var headerCells = [group[0], "Requests", "Input tokens", "Output tokens"];
        if (dayKnown) headerCells.push("Fallbacks");
        headerCells.push("Estimated USD", "Price source / observed");
        var costTable = el("table", {}, [el("thead", {}, [el("tr", {}, headerCells.map(function (title) {
          return el("th", { text: title });
        }))])]);
        var costBody = el("tbody");
        entries.forEach(function (key) {
          var row = group[1][key];
          var cells = [
            el("td", { text: key }), el("td", { text: row.requests }),
            el("td", { text: row.input_tokens }), el("td", { text: row.output_tokens })
          ];
          if (dayKnown) cells.push(el("td", { text: row.fallbacks }));
          cells.push(costCell(row));
          cells.push(el("td", { class: "small", text: priceSourceCell(row) }));
          costBody.appendChild(el("tr", {}, cells));
        });
        costTable.appendChild(costBody);
        costsCard.appendChild(costTable);
      });
    }
    details.appendChild(el("section", {}, [costsCard]));
    details.appendChild(el("section", {}, [renderFallbackCard(analytics)]));


    var statisticWindows = state.request_statistics || {};
    var statistics = statisticWindows[statisticsWindow] || { alias: {}, provider: {}, model: {} };
    var statisticsCard = el("div", { class: "card" }, [
      el("h2", { text: "Request statistics" }),
      el("p", { class: "small muted", text: "Metadata only · retained ledger history, up to 30 days. Percentiles use recorded latencies; token and cost values are Unknown when unavailable." }),
      el("label", { class: "small", text: "Window " }, [el("select", { "aria-label": "Statistics window", onchange: function (event) { statisticsWindow = event.target.value; draw(); } }, [
        el("option", { value: "hour", selected: statisticsWindow === "hour" }, ["Last hour"]),
        el("option", { value: "day", selected: statisticsWindow === "day" }, ["Last day"]),
        el("option", { value: "month", selected: statisticsWindow === "month" }, ["Last month"])
      ])])
    ]);
    [["Alias", statistics.alias], ["Provider", statistics.provider], ["Model", statistics.model]].forEach(function (group) {
      var keys = Object.keys(group[1] || {}).sort();
      if (!keys.length) return;
      statisticsCard.appendChild(el("h3", { text: group[0] }));
      var statsTable = el("table", {}, [el("thead", {}, [el("tr", {}, [
        el("th", { text: group[0] }), el("th", { text: "Requests / errors" }), el("th", { text: "Error classes" }),
        el("th", { text: "Fallback rate" }), el("th", { text: "Latency p50 / p95 / p99 ms" }),
        el("th", { text: "Tokens in / out" }), el("th", { text: "Estimated USD" })
      ])])]);
      var statsBody = el("tbody");
      keys.forEach(function (key) {
        var row = group[1][key];
        var latency = row.latency_ms || {};
        var percentile = function (value) { return value == null ? "Unknown" : value; };
        statsBody.appendChild(el("tr", {}, [
          el("td", { text: key }), el("td", { text: row.requests + " / " + row.errors }),
          el("td", { class: "small", text: Object.keys(row.error_classes || {}).sort().map(function (name) { return name + ": " + row.error_classes[name]; }).join(", ") || "—" }),
          el("td", { text: (Number(row.fallback_rate || 0) * 100).toFixed(1) + "% (" + row.fallbacks + ")" }),
          el("td", { text: percentile(latency.p50) + " / " + percentile(latency.p95) + " / " + percentile(latency.p99) }),
          el("td", { text: row.input_tokens == null || row.output_tokens == null ? "Unknown" : row.input_tokens + " / " + row.output_tokens }),
          el("td", { text: row.estimated_cost_usd == null ? "Unknown" : "$" + Number(row.estimated_cost_usd).toFixed(6) })
        ]));
      });
      statsTable.appendChild(statsBody);
      statisticsCard.appendChild(statsTable);
    });
    frag.appendChild(el("section", {}, [statisticsCard]));

    var limitsCard = el("div", { class: "card" }, [
      el("h2", { text: "Usage and limits" }),
      el("p", {
        class: "small muted",
        text: "Enforceable spend caps observed from providers. Catalogue fields such as monthly_limit_usd are reference metadata, not configured budgets, and are never shown as limits here."
      })
    ]);
    if (!state.limits || !state.limits.length) {
      limitsCard.appendChild(el("p", {
        class: "small muted",
        text: "No cost limit is configured: no enforceable provider spend cap has been observed. Nothing is inferred from catalogue metadata."
      }));
    } else {
      var limitTable = el("table", {}, [
        el("thead", {}, [el("tr", {}, [
          el("th", { text: "Provider" }), el("th", { text: "Window" }), el("th", { text: "Value" }),
          el("th", { text: "Source" }), el("th", { text: "Observed" })
        ])])
      ]);
      var limitBody = el("tbody");
      state.limits.forEach(function (entry) {
        limitBody.appendChild(el("tr", {}, [
          el("td", { text: providerLabel(entry.provider) }),
          el("td", { text: entry.window }),
          el("td", { text: entry.value === null || entry.value === undefined ? "Unknown" : String(entry.value) }),
          el("td", { class: "small", text: entry.source + " · " + entry.confidence }),
          el("td", { class: "small", text: when(entry.observed_at) })
        ]));
      });
      limitTable.appendChild(limitBody);
      limitsCard.appendChild(limitTable);
    }
    details.appendChild(el("section", {}, [limitsCard]));

    frag.appendChild(details);
    return frag;
  }

  var REQUIRED_TRIGGER_LABELS = {
    exhausted: "Exhausted",
    rate_limited: "Rate limited",
    upstream_unavailable: "Upstream unavailable",
    explicit_cooldown: "Explicit cooldown",
    route_ineligible: "Route ineligible",
    probe_contention: "Probe contention"
  };

  // The provider states the console knows how to judge. Anything else — a
  // future enum value or a typo — is Unknown, never a fall-through to
  // Healthy (fail-closed allow-list, not a deny-list).
  var KNOWN_PROVIDER_STATES = {
    connected: true,
    configured_untested: true,
    failed: true,
    not_configured: true
  };

  function routeHealthState(alias, providers, cooldowns, required) {
    if (!alias || !alias.primary || !Array.isArray(alias.fallbacks) || !Array.isArray(alias.fallback_triggers)) return "Unknown";
    // An absent policy list or absent cooldown details is Unknown, never an
    // empty pass: "no data" must not read as "nothing wrong".
    if (!Array.isArray(required)) return "Unknown";
    if (cooldowns == null) return "Unknown";
    if (alias.fallbacks.length === 0 || alias.fallback_authorized !== true) return "Policy gap";
    var disabled = required.filter(function (trigger) { return alias.fallback_triggers.indexOf(trigger) === -1; });
    if (disabled.length) return "Policy gap";
    var routes = [alias.primary].concat(alias.fallbacks);
    var providerStates = routes.map(function (route) { return providers[route.provider]; });
    if (providerStates.some(function (provider) {
      return !provider || !provider.state ||
        !Object.prototype.hasOwnProperty.call(KNOWN_PROVIDER_STATES, provider.state) ||
        provider.state === "configured_untested";
    })) return "Unknown";
    var unavailable = providerStates.some(function (provider) {
      return provider.state === "failed" || provider.state === "not_configured";
    });
    var cooling = Object.keys(cooldowns).some(function (key) {
      var entry = cooldowns[key] || {};
      if (entry.armed === false || entry.remaining_seconds <= 0 && entry.state !== "half_open") return false;
      return routes.some(function (route) {
        return route.provider === entry.provider && (entry.scope === "provider" || route.model === entry.model);
      });
    });
    if (cooling) return "Cooldown";
    if (unavailable) return "Degraded";
    var unconfirmed = alias.fallbacks.some(function (route) {
      return (alias.fallback_confirmed_providers || []).indexOf(route.provider) === -1;
    });
    if (unconfirmed) return "Policy gap";
    return "Healthy";
  }

  function routingHealthSummary(aliases) {
    var providers = {};
    (state.providers || []).forEach(function (provider) { providers[provider.provider_id] = provider; });
    // Absent lists stay absent: routeHealthState turns them into Unknown
    // verdicts instead of treating "missing" as "empty and healthy".
    var required = state.fallback_triggers;
    var policyKnown = Array.isArray(required);
    var cooldowns = state.cooldown_details;
    var cooldownsKnown = cooldowns != null;
    if (!cooldownsKnown) cooldowns = {};
    var ageMs = dataAgeMs();
    var stale = refreshUnavailable || ageMs == null || ageMs > STALE_AFTER_MS;
    var cards = aliases.map(function (alias) {
      var fallbackRoutes = Array.isArray(alias.fallbacks) ? alias.fallbacks : [];
      var base = routeHealthState(alias, providers, cooldownsKnown ? cooldowns : null, required);
      // Stale-beyond-threshold data must never claim Healthy: it renders as
      // Stale with the snapshot's age beside it. Every other verdict is
      // fail-safe or config-derived and is kept as computed.
      var stateText = stale && base === "Healthy" ? "Stale" : base;
      var triggerList = el("ul", { class: "trigger-list", "aria-label": "Fallback trigger status" });
      Object.keys(REQUIRED_TRIGGER_LABELS).forEach(function (key) {
        var enabled = policyKnown && required.indexOf(key) !== -1 && (alias.fallback_triggers || []).indexOf(key) !== -1;
        triggerList.appendChild(el("li", { text: REQUIRED_TRIGGER_LABELS[key] + ": " + (policyKnown ? (enabled ? "enabled" : "disabled") : "unknown") }));
      });
      var routeText = function (route) { return route ? providerLabel(route.provider) + " / " + route.model : "None configured"; };
      var active = Object.keys(cooldowns).filter(function (key) {
        var entry = cooldowns[key] || {};
        if (entry.armed === false || (entry.remaining_seconds <= 0 && entry.state !== "half_open")) return false;
        return [alias.primary].concat(fallbackRoutes).some(function (route) {
          return route && route.provider === entry.provider && (entry.scope === "provider" || route.model === entry.model);
        });
      }).map(function (key) { return cooldowns[key]; });
      var cooldownText = !cooldownsKnown
        ? "Unknown — the console state carried no cooldown details, so an open circuit cannot be ruled out."
        : (active.length ? active.map(function (entry) {
          return entry.provider + "/" + (entry.model || "all models") + " · " + entry.state + " · " + entry.remaining_seconds + "s remaining";
        }).join("; ") : "No active cooldown on this alias’s routes.");
      var confirmed = (alias.fallback_confirmed_providers || []).filter(function (provider) { return fallbackRoutes.some(function (route) { return route.provider === provider; }); });
      return el("article", { class: "route-health-card", "aria-label": alias.name + " routing health" }, [
        el("h3", { text: alias.name + " · " + stateText, "data-alias": alias.name, "data-verdict": base }),
        el("p", { class: "small muted health-freshness", text: freshnessText() }),
        el("dl", { class: "health-routes" }, [
          el("dt", { text: "Primary" }), el("dd", { text: routeText(alias.primary) }),
          el("dt", { text: "Fallback 1" }), el("dd", { text: routeText(fallbackRoutes[0]) }),
          el("dt", { text: "Fallback 2" }), el("dd", { text: routeText(fallbackRoutes[1]) })
        ]),
        el("p", { class: "small", text: "Fallback authorization: " + (alias.fallback_authorized === true ? "enabled" : "disabled") + " · Confirmed providers: " + (confirmed.length ? confirmed.map(providerLabel).join(", ") : "none") }),
        triggerList,
        el("p", { class: "small", text: "Applicable cooldown: " + cooldownText })
      ]);
    });
    if (!aliases.length) return emptyState("No published aliases", "No published aliases yet. Configure and publish an alias to see routing health.");
    return el("div", { class: "routing-health-list" }, cards);
  }

  function statBox(label, value) {
    return el("div", { class: "card", style: "margin:8px 0" }, [
      el("div", { class: "small muted", text: label }),
      el("div", { style: "font-size:22px", text: String(value) })
    ]);
  }

  // --- fallback analytics ------------------------------------------------

  // A recorded fallback reason is a sentence that changes by the second, so
  // the API folds reasons onto a closed vocabulary; the console renders the
  // human label for each key it is given.
  var FALLBACK_REASON_LABELS = {
    cooldown: "Local cooldown held the route",
    route_ineligible: "Primary route not eligible",
    upstream_failure: "Upstream failure trigger",
    other: "Other reason",
    unknown: "Unknown reason"
  };

  // What each day's record state means. A gap is never drawn as a zero.
  var DAY_STATUS_TEXT = {
    ok: "recorded",
    open: "in progress",
    no_rows: "no rows yet today",
    gap: "no data — gap, not zero",
    outside_coverage: "before recorded history",
    unknown: "unavailable"
  };

  function fallbackReasonLabel(key) {
    return FALLBACK_REASON_LABELS[key] || key;
  }

  function percentText(rate) {
    if (rate === null || rate === undefined) return "Unknown";
    return (Number(rate) * 100).toFixed(1) + "%";
  }

  function dayStatusText(status) {
    return DAY_STATUS_TEXT[status] || String(status);
  }

  function priceSourceCell(row) {
    if (row.source) {
      var note = "";
      if (row.cost_reason === "missing_cached_tokens") {
        note = " · cached-token count missing on some requests; those are excluded from the subtotal";
      } else if (row.cost_reason === "missing_cached_price") {
        note = " · cached reads unpriced; those requests excluded";
      } else if (row.cost_reason === "no_published_price" && row.unknown_cost_requests) {
        note = " · " + row.unknown_cost_requests + " request(s) have no published price and are excluded";
      }
      return row.source + " · " + (row.observed_at || "date unknown") + note;
    }
    if (row.cost_reason === "missing_cached_price") {
      return "Price published, but not for cached reads - amount withheld";
    }
    if (row.cost_reason === "missing_cached_tokens") {
      return "Cached-token count not reported - amount withheld for those requests";
    }
    if (row.cost_reason === "no_published_price") {
      return "No published actual/list price for this provider and model - never inferred";
    }
    return "Unknown price source";
  }

  // A group keeps its known dollar subtotal even when some constituent
  // requests have no price; the unknown coverage is stated next to it so a
  // partial total can never read as a complete bill.
  function costCell(row) {
    var parts = [];
    // The rollup's completeness marker, carried through the API: how many
    // requests in this group never reported a cached-read count.
    var cachedNote = row.cached_tokens_unreported
      ? " · cached count missing on " + row.cached_tokens_unreported + " request(s)"
      : "";
    if (row.amount_usd == null) {
      parts.push(el("div", { text: "Unknown" }));
      if (row.unknown_cost_requests) {
        parts.push(el("div", {
          class: "small muted",
          text: "0 of " + row.requests + " requests priced; " +
            row.unknown_cost_requests + " unknown (" +
            tokens(row.unknown_cost_input_tokens) + " in / " +
            tokens(row.unknown_cost_output_tokens) + " out)" + cachedNote
        }));
      }
    } else {
      parts.push(el("div", { text: "$" + Number(row.amount_usd).toFixed(6) }));
      if (row.cost_incomplete && row.unknown_cost_requests) {
        parts.push(el("div", {
          class: "small muted",
          text: "known subtotal; " + row.unknown_cost_requests + " of " + row.requests +
            " requests unpriced (" + tokens(row.unknown_cost_input_tokens) + " in / " +
            tokens(row.unknown_cost_output_tokens) + " out tokens)" + cachedNote
        }));
      } else if (row.cost_incomplete) {
        parts.push(el("div", { class: "small muted", text: "known subtotal; coverage incomplete" + cachedNote }));
      }
    }
    return el("td", {}, parts);
  }

  // The rate with a proportional bar, styled inline so no new stylesheet
  // rule is needed: currentColor adapts to the console's theme.
  function rateCell(rate) {
    if (rate === null || rate === undefined) return el("td", { class: "small muted", text: "Unknown" });
    var percent = Number(rate) * 100;
    var width = Math.max(1, Math.min(100, percent));
    return el("td", {}, [
      el("div", { text: percent.toFixed(1) + "%" }),
      el("div", { style: "height:6px;width:" + width.toFixed(1) + "%;background:currentColor;opacity:.45" })
    ]);
  }

  // The window selector on the usage table: the live ring plus every day the
  // analytics payload carries, newest first.
  function usageDaySelector(costs, analytics, dayKnown) {
    var selector = el("select", {
      id: "usage-day",
      "aria-label": "Usage window",
      onchange: function () { usageDay = selector.value; draw(); }
    });
    selector.appendChild(el("option", {
      value: "live",
      text: "Last " + costs.window_hours + " hours (live ring)",
      selected: !dayKnown
    }));
    (analytics.series || []).slice().reverse().forEach(function (entry) {
      var label = entry.day;
      if (entry.status === "open") label += " (today, in progress)";
      else if (entry.status === "gap") label += " — no data";
      else if (entry.status === "outside_coverage") label += " — before recorded history";
      else if (entry.status === "no_rows") label += " — no rows yet";
      selector.appendChild(el("option", {
        value: entry.day, text: label, selected: dayKnown && usageDay === entry.day
      }));
    });
    selector.value = dayKnown ? usageDay : "live";
    return selector;
  }

  // The fallback card: the overall rate, why fallbacks happened, where they
  // happened, and how the rate moved day by day. Every number comes from the
  // durable rollup the API reported; nothing here is recomputed or inferred.
  function renderFallbackCard(analytics) {
    var card = el("div", { class: "card" }, [
      el("h2", { text: "Fallback analytics" }),
      el("p", { class: "small muted", text: "Metadata only. Prompts, responses, tokens and secrets are never stored." })
    ]);
    if (!analytics.available) {
      card.appendChild(el("p", {
        class: "small muted",
        text: analytics.unavailable_reason === "durable_rollup_disabled"
          ? "Durable fallback analytics are unavailable: request-ledger persistence is off, so there is no daily rollup to read. The live totals above still count this process only."
          : "Durable fallback analytics are unavailable: " + (analytics.unavailable_reason || "unknown reason") + ". Missing data is reported, never filled in."
      }));
      return card;
    }
    var range = analytics.window || {};
    var coverage = analytics.coverage || {};
    var fallback = analytics.fallback || {};
    var overall = fallback.overall || { requests: 0, fallbacks: 0, fallback_rate: null, fallback_reasons: [] };
    card.appendChild(el("p", {
      class: "small muted",
      text: "Window " + (range.from || "unknown") + " to " + (range.to || "unknown") +
        " (" + (analytics.days || 0) + " days, capped at 30) from the durable rollup. " +
        (coverage.incomplete ? "Coverage is incomplete: some ledger rows could not be represented." : "Every retained row in the window is represented.") +
        ((analytics.gaps || []).length
          ? " No recorded rows on " + analytics.gaps.join(", ") + " - shown as a gap, never as zero."
          : "")
    }));
    card.appendChild(el("div", { class: "row" }, [
      statBox("Requests", overall.requests || 0),
      statBox("Fallbacks", overall.fallbacks || 0),
      statBox("Fallback rate", percentText(overall.fallback_rate))
    ]));

    card.appendChild(el("h3", { text: "By reason" }));
    var reasons = overall.fallback_reasons || [];
    if (!reasons.length) {
      card.appendChild(el("p", { class: "small muted", text: "No fallbacks were recorded in this window." }));
    } else {
      var reasonTable = el("table", {}, [el("thead", {}, [el("tr", {}, [
        el("th", { text: "Reason" }), el("th", { text: "Fallbacks" }), el("th", { text: "Share of fallbacks" })
      ])])]);
      var reasonBody = el("tbody");
      reasons.forEach(function (entry) {
        reasonBody.appendChild(el("tr", {}, [
          el("td", { text: fallbackReasonLabel(entry.reason) }),
          el("td", { text: String(entry.count) }),
          el("td", { text: entry.share == null ? "Unknown" : (Number(entry.share) * 100).toFixed(1) + "%" })
        ]));
      });
      reasonTable.appendChild(reasonBody);
      card.appendChild(reasonTable);
    }

    [["Alias", fallback.alias], ["Model", fallback.model], ["Provider", fallback.provider]].forEach(function (group) {
      var entries = Object.keys(group[1] || {}).sort();
      if (!entries.length) return;
      card.appendChild(el("h3", { text: "By " + group[0].toLowerCase() }));
      var table = el("table", {}, [el("thead", {}, [el("tr", {}, [
        el("th", { text: group[0] }), el("th", { text: "Requests" }),
        el("th", { text: "Fallbacks" }), el("th", { text: "Fallback rate" })
      ])])]);
      var body = el("tbody");
      entries.forEach(function (key) {
        var row = group[1][key];
        body.appendChild(el("tr", {}, [
          el("td", { text: key }),
          el("td", { text: String(row.requests) }),
          el("td", { text: String(row.fallbacks) }),
          rateCell(row.fallback_rate)
        ]));
      });
      table.appendChild(body);
      card.appendChild(table);
    });

    card.appendChild(el("h3", { text: "Daily trend" }));
    var series = analytics.series || [];
    if (!series.length) {
      card.appendChild(el("p", { class: "small muted", text: "No days to show." }));
    } else {
      var trend = el("table", {}, [el("thead", {}, [el("tr", {}, [
        el("th", { text: "Day" }), el("th", { text: "Requests" }), el("th", { text: "Input tokens" }),
        el("th", { text: "Output tokens" }), el("th", { text: "Fallbacks" }), el("th", { text: "Fallback rate" }),
        el("th", { text: "Errors" }), el("th", { text: "Record" })
      ])])]);
      var trendBody = el("tbody");
      series.forEach(function (entry) {
        var known = entry.status === "ok" || entry.status === "open";
        function blank() { return el("td", { class: "small muted", text: "—" }); }
        trendBody.appendChild(el("tr", {}, [
          el("td", { class: "small", text: entry.day }),
          known ? el("td", { text: String(entry.requests) }) : blank(),
          known ? el("td", { text: tokens(entry.input_tokens) }) : blank(),
          known ? el("td", { text: tokens(entry.output_tokens) }) : blank(),
          known ? el("td", { text: String(entry.fallbacks) }) : blank(),
          known ? rateCell(entry.fallback_rate) : blank(),
          known ? el("td", { text: String(entry.errors) }) : blank(),
          el("td", { class: "small" + (known ? "" : " muted"), text: dayStatusText(entry.status) })
        ]));
      });
      trend.appendChild(trendBody);
      card.appendChild(trend);
    }
    return card;
  }

  // The note column names *why* a request was not served by its primary:
  // a configured phase route picked a different primary (not a fallback),
  // a local circuit held a route, or the upstream failed. Each reason is
  // labelled so a circuit fallback is never read as a phase choice.
  function ledgerNote(event) {
    var parts = [];
    (event.degradations || []).forEach(function (d) { parts.push(d.reason); });
    if (event.phase) {
      parts.push("phase route: " + event.phase + (event.phase_source ? " (" + event.phase_source + ")" : ""));
    }
    if (event.fallback_reason) {
      parts.push(event.fallback_used ? "fallback: " + event.fallback_reason : event.fallback_reason);
    }
    (event.skipped || []).forEach(function (s) {
      var why = s.reason || "";
      var kind = why.indexOf("cooldown") !== -1 || why.indexOf("recovery probe") !== -1
        ? "held by local circuit"
        : "not eligible";
      parts.push("skipped " + s.provider + "/" + s.model + " (" + kind + "): " + why);
    });
    return parts.join("; ");
  }

  function cooldownPolicyText() {
    var policy = state.cooldown_policy || {};
    if (policy.exhausted_max_seconds == null) return "unknown";
    var text = "a confirmed provider exhaustion holds for at most " + policy.exhausted_max_seconds +
      "s (floor " + policy.exhausted_base_seconds + "s); when the window ends exactly one recovery" +
      " probe is let through, and a concurrent request falls back until it answers";
    if (policy.transient_max_seconds != null) {
      text += "; a transient failure holds at most " + policy.transient_max_seconds +
        "s however many failures accumulate";
    }
    return text;
  }

  // The one line a cooldown clear reports back: how many circuits went
  // and which keys they had. Keys are provider/model metadata only -
  // never a credential, a prompt or a completion.
  function cooldownClearedMessage(result) {
    var count = result.cleared_count || 0;
    var keys = result.keys || [];
    var message = "Cleared " + count + " cooldown(s)";
    if (result.scope === "all") message += " across every provider";
    else if (result.provider) message += " for " + providerLabel(result.provider);
    if (keys.length) message += ": " + keys.join(", ");
    return message + ".";
  }

  // Every clear in this table is confirmed first, then reported with the
  // keys it removed and followed by a state refresh, so the table shows
  // the post-clear truth instead of the snapshot it was rendered from.
  function clearCooldowns(body, confirmText) {
    if (!global.confirm(confirmText)) return;
    run(api(BASE + "/api/provider", { method: "POST", body: body }))
      .then(function (result) {
        toast(cooldownClearedMessage(result), "ok");
        return refresh();
      })
      .catch(function () {});
  }

  // The cooldown table is the operator's view of a held provider: when the
  // exhaustion was seen, how long the hold runs, when the next automatic
  // probe may run, and whether one is in flight right now. Each held
  // circuit carries its own clear control, and the whole table can be
  // emptied behind one counted confirmation.
  function cooldownTable() {
    var details = state.cooldown_details || {};
    var keys = Object.keys(details).sort();
    var card = el("div", {}, [el("h3", { text: "Local cooldowns" })]);
    if (!keys.length) {
      card.appendChild(el("p", { class: "small muted", text: "No local cooldowns are open. A fallback may still be caused by a configured phase route; the request ledger below names the reason for every request." }));
      return card;
    }
    var table = el("table", {}, [
      el("thead", {}, [el("tr", {}, [
        el("th", { text: "Route" }), el("th", { text: "Scope" }),
        el("th", { text: "Failure" }), el("th", { text: "Failures" }),
        el("th", { text: "Opened" }), el("th", { text: "Hold left" }),
        el("th", { text: "Next probe" }), el("th", { text: "State" }),
        el("th", { text: "Why" }), el("th", { text: "Clear" })
      ])])
    ]);
    var tbody = el("tbody");
    keys.forEach(function (key) {
      var entry = details[key] || {};
      var providerWide = entry.scope === "provider";
      // A row is cleared at its own scope: one route for a route row,
      // the provider and its routes for a provider-wide row (there is no
      // single-entry clear for the wildcard circuit).
      var clearButton = el("button", {
        type: "button", text: "Clear",
        // Two rows both say "Clear": the accessible name names the target.
        "aria-label": "Clear the cooldown on " + providerLabel(entry.provider) +
          (providerWide ? " (whole provider)" : " → " + (entry.model || key)),
        onclick: function () {
          var body = providerWide
            ? { action: "clear_cooldowns", provider_id: entry.provider, scope: "provider" }
            : { action: "clear_cooldowns", provider_id: entry.provider, model: entry.model, scope: "route" };
          clearCooldowns(
            body,
            "Clear " + (providerWide
              ? "the whole-provider cooldown on " + providerLabel(entry.provider) + "?\n\nThis also clears the route cooldowns held for that provider."
              : "the cooldown on " + providerLabel(entry.provider) + " → " + (entry.model || key) + "?") +
            "\n\nOnly do this after you have confirmed the subscription or reset window has reopened."
          );
        }
      });
      tbody.appendChild(el("tr", {}, [
        el("td", { class: "small", text: key }),
        el("td", { class: "small", text: entry.scope === "provider" ? "whole provider" : "this route only" }),
        el("td", { class: "small", text: entry.error_class || "unknown" }),
        el("td", { class: "small", text: String(entry.failures || 0) }),
        el("td", { class: "small", text: when(entry.opened_at ? new Date(entry.opened_at * 1000).toISOString() : null) }),
        el("td", { class: "small", text: (entry.remaining_seconds || 0) + "s" }),
        el("td", { class: "small", text: entry.next_probe_at ? when(new Date(entry.next_probe_at * 1000).toISOString()) : "Unknown" }),
        el("td", { class: "small", text: (entry.state || "unknown") + (entry.probe_in_flight ? " · probe in flight" : "") }),
        el("td", { class: "small muted", text: (entry.reason || "") + (entry.request_id ? " · event " + entry.request_id : "") }),
        el("td", {}, [clearButton])
      ]));
    });
    table.appendChild(tbody);
    card.appendChild(table);
    // The mass clear lives next to the table it empties, and it names the
    // exact number of entries before anything is removed. The server
    // refuses it unless the payload itself says scope: "all".
    card.appendChild(el("div", { class: "small" }, [
      el("button", {
        type: "button", text: "Clear all cooldowns",
        class: "danger",
        "aria-label": "Clear all " + keys.length + " local cooldowns",
        onclick: function () {
          clearCooldowns(
            { action: "clear_cooldowns", scope: "all" },
            "Clear all " + keys.length + " local cooldown(s) held by this gateway?\n\n" +
            "This removes every provider and route cooldown, for every provider. " +
            "Only do this after you have confirmed every subscription or reset window has reopened."
          );
        }
      })
    ]));
    return card;
  }

  function renderChecklist() {
    var list = el("ul", { class: "checklist" });
    (state.checklist || []).forEach(function (item) {
      list.appendChild(el("li", {}, [
        el("span", { class: item.done ? "done" : "todo", text: (item.done ? "✓ " : "○ ") + item.step + ". " + item.title })
      ]));
    });
    return list;
  }


  // --- leaderboard ------------------------------------------------------
  //
  // The private, opencode.ai/data-style view of the same durable rollup the
  // status page reads: models ranked by input + output tokens, a stacked
  // daily usage chart, a date x model matrix, a leader-vs-challenger
  // comparison, the fallback panel and a plain-language methodology. It is
  // fetched from its own read-only route so the state poll keeps its
  // sub-2s bar (R56). The three rules it never breaks:
  //   - cost honesty: amount_usd null renders as "no published price",
  //     never $0.00, and a price never crosses a provider boundary;
  //   - absent is not zero: a day with no recorded rows renders as "no
  //     data"; only a recorded day carries a measured 0;
  //   - no invented dimensions: model, provider, alias, fallback reason and
  //     cost are all this single-owner appliance actually has.

  var LB_WINDOW_OPTIONS = [7, 14, 30];

  // Stable colours for the stacked chart, indexed by rank position so one
  // model keeps its colour across every day in the window.
  var LB_COLORS = ["#4c8dff", "#3fb950", "#d29922", "#f85149", "#a371f7", "#56d4dd", "#e0912f", "#8b949e"];

  function lbColor(index) {
    return LB_COLORS[index % LB_COLORS.length];
  }

  function lbShareText(share) {
    if (share === null || share === undefined) return "Unknown";
    return (Number(share) * 100).toFixed(2) + "%";
  }

  // How a day with no recorded rows describes itself. Every variant leads
  // with "no data" so an absent day can never be read as a measured zero,
  // and none of them contains a digit that could pass for a count.
  var LB_ABSENT_TEXT = {
    gap: "no data — gap in the record, not zero",
    outside_coverage: "no data — before recorded history",
    no_rows: "no data — no rows recorded yet today",
    unknown: "no data — record status unavailable"
  };

  function lbAbsentText(status) {
    return LB_ABSENT_TEXT[status] || ("no data — " + status);
  }

  // Cost honesty, leaderboard edition. The headline depends on the reason,
  // so "no published price" (a fact about the provider catalogue) never
  // reads as "amount withheld" (a fact about incomplete data). Unknown
  // coverage is always stated beside whatever subtotal exists.
  var LB_COST_HEADLINE = {
    no_published_price: "no published price",
    no_usage_reported: "no usage to price",
    missing_cached_tokens: "amount withheld",
    missing_cached_price: "amount withheld",
    invalid_usage: "amount withheld"
  };

  var LB_COST_DETAIL = {
    no_published_price: "no published actual/list price for this provider and model — never inferred",
    no_usage_reported: "the rollup recorded no priced usage for this model in the window",
    missing_cached_tokens: "cached-token count not reported on some requests — amount withheld for those",
    missing_cached_price: "price published, but not for cached reads — amount withheld",
    invalid_usage: "recorded usage is not valid to price — amount withheld"
  };

  function lbCostCell(entry) {
    var cost = entry.cost || {};
    var parts = [];
    if (cost.amount_usd === null || cost.amount_usd === undefined) {
      var headline = LB_COST_HEADLINE[cost.cost_reason] || "no published price";
      parts.push(el("div", { text: headline }));
      parts.push(el("div", { class: "small muted", text: LB_COST_DETAIL[cost.cost_reason] || "no price recorded for this provider and model" }));
      if (cost.unknown_cost_requests) {
        parts.push(el("div", {
          class: "small muted",
          text: "0 of " + entry.requests + " requests priced; " + cost.unknown_cost_requests +
            " unknown (" + tokens(cost.unknown_cost_input_tokens) + " in / " +
            tokens(cost.unknown_cost_output_tokens) + " out)"
        }));
      }
    } else {
      parts.push(el("div", { text: "$" + Number(cost.amount_usd).toFixed(6) }));
      if (cost.cost_incomplete) {
        parts.push(el("div", {
          class: "small muted",
          text: (cost.unknown_cost_requests
            ? "known subtotal; " + cost.unknown_cost_requests + " of " + entry.requests +
              " requests unpriced (" + tokens(cost.unknown_cost_input_tokens) + " in / " +
              tokens(cost.unknown_cost_output_tokens) + " out tokens)"
            : "known subtotal; coverage incomplete")
        }));
      }
    }
    if (cost.cached_tokens_unreported) {
      parts.push(el("div", { class: "small muted", text: "cached count missing on " + cost.cached_tokens_unreported + " request(s)" }));
    }
    if (cost.source) {
      parts.push(el("div", {
        class: "small muted",
        text: "price source " + cost.source + " · observed " + (cost.observed_at || "date unknown")
      }));
    }
    return el("td", {}, parts);
  }

  // The window-totals line under the ranking. Null counts are rendered as
  // absent, never as zero; a known subtotal is always accompanied by its
  // unpriced coverage and the number of models with no published price.
  function lbTotalsText(totals) {
    if (!totals || totals.requests === null || totals.requests === undefined) {
      return "No recorded rows in this window: totals are absent, not zero.";
    }
    var parts = [
      "Window totals: " + totals.requests + " requests · " +
        tokens(totals.total_tokens) + " tokens (" + tokens(totals.input_tokens) +
        " in / " + tokens(totals.output_tokens) + " out · " + tokens(totals.cached_tokens) +
        " cached reported) · " + totals.models + " models · " + totals.fallbacks +
        " fallbacks · " + totals.errors + " errors."
    ];
    if (totals.known_amount_usd === null || totals.known_amount_usd === undefined) {
      parts.push("Known cost: nothing in this window has a published price" +
        (totals.models_without_price
          ? " (" + totals.models_without_price + " of " + totals.models + " models unpriced)"
          : "") + ".");
    } else {
      parts.push("Known cost: $" + Number(totals.known_amount_usd).toFixed(6) +
        (totals.cost_incomplete
          ? " — incomplete: " + totals.unknown_cost_requests + " request(s) unpriced (" +
            tokens(totals.unknown_cost_input_tokens) + " in / " +
            tokens(totals.unknown_cost_output_tokens) + " out tokens)."
          : "."));
    }
    if (totals.models_without_price) {
      parts.push(totals.models_without_price + " model(s) have no published price and are never estimated.");
    }
    return parts.join(" ");
  }

  // The ranked table: one row per (provider, model) — the same model id on
  // two providers is two rows, because a price never transfers.
  function renderLeaderboardRanking(payload) {
    var metric = payload.primary_metric || {};
    var card = el("div", { class: "card" }, [
      el("h2", { text: "Model leaderboard" }),
      el("p", {
        class: "small muted",
        text: "Ranked by " + (metric.label || "input + output tokens") + " over " +
          payload.days + " UTC days (" + payload.window.from + " to " + payload.window.to +
          ", " + payload.timezone + "). " + (metric.definition || "")
      })
    ]);
    if (!payload.ranked.length) {
      card.appendChild(emptyState(
        "No models recorded in this window",
        "The rollup holds no rows for these days. That is absent data, not zero usage."
      ));
      return card;
    }
    var table = el("table", {}, [el("thead", {}, [el("tr", {}, [
      el("th", { text: "Rank" }), el("th", { text: "Model" }), el("th", { text: "Provider" }),
      el("th", { text: "Share" }), el("th", { text: "Tokens in / out / cached" }),
      el("th", { text: "Requests" }), el("th", { text: "Fallbacks" }), el("th", { text: "Errors" }),
      el("th", { text: "Cost" })
    ])])]);
    var tbody = el("tbody");
    payload.ranked.forEach(function (entry) {
      tbody.appendChild(el("tr", {}, [
        el("td", { text: "#" + entry.rank }),
        el("td", {}, [
          el("div", { text: entry.model }),
          entry.rank === 1 ? el("div", { class: "small muted", text: "leader" }) : null
        ]),
        el("td", { class: "small", text: providerLabel(entry.provider) }),
        rateCell(entry.share),
        el("td", { class: "small", text: tokens(entry.input_tokens) + " / " +
          tokens(entry.output_tokens) + " / " + tokens(entry.cached_tokens) }),
        el("td", { text: String(entry.requests) }),
        el("td", { text: String(entry.fallbacks) }),
        el("td", { text: String(entry.errors) }),
        lbCostCell(entry)
      ]));
    });
    table.appendChild(tbody);
    card.appendChild(el("div", { class: "table-wrap" }, [table]));
    card.appendChild(el("p", { class: "small muted", text: lbTotalsText(payload.totals) }));
    return card;
  }

  // The stacked daily usage chart. Columns are UTC days, segments are
  // models, heights are proportional to input + output tokens. A day with
  // no recorded data is a hatched column labelled "no data" — never an
  // empty column that would read as a measured zero.
  function renderLeaderboardChart(payload) {
    var metric = payload.primary_metric || {};
    var models = payload.matrix.models;
    var cells = payload.matrix.cells;
    var card = el("div", { class: "card" }, [
      el("h2", { text: "Daily usage by model" }),
      el("p", {
        class: "small muted",
        text: "Stacked " + (metric.label || "input + output tokens") +
          " per UTC day, proportional within this window. A hatched column means the day carries no recorded data — it is not a zero-usage day."
      })
    ]);
    if (!models.length) {
      card.appendChild(el("p", { class: "muted", text: "No models recorded in this window." }));
      return card;
    }
    var dayTotals = cells.map(function (cell) {
      if (!cell.values) return null;
      return cell.values.reduce(function (sum, value) {
        return sum + (value ? value.input_tokens + value.output_tokens : 0);
      }, 0);
    });
    var maxTotal = 0;
    dayTotals.forEach(function (total) { if (total && total > maxTotal) maxTotal = total; });
    var chart = el("div", {
      class: "lb-chart", role: "img",
      "aria-label": "Stacked daily " + (metric.label || "input + output tokens") + " by model"
    });
    cells.forEach(function (cell, dayIndex) {
      var known = cell.status === "ok" || cell.status === "open";
      var column = el("div", { class: "lb-col" });
      var area = el("div", { class: "lb-area" });
      if (!known) {
        area.appendChild(el("div", { class: "lb-nodata", title: lbAbsentText(cell.status) }));
      } else if (!dayTotals[dayIndex]) {
        // A recorded day where every model had no requests: a measured
        // zero, drawn as a flat marker, never as a hatched column.
        area.appendChild(el("div", { class: "lb-zero", text: "0" }));
      } else {
        cell.values.forEach(function (value, modelIndex) {
          if (!value) return;
          var modelTokens = value.input_tokens + value.output_tokens;
          if (!modelTokens) return;
          var entry = payload.ranked[modelIndex];
          area.appendChild(el("div", {
            class: "lb-seg",
            style: "height:" + ((modelTokens / maxTotal) * 100).toFixed(2) + "%;background:" + lbColor(modelIndex),
            title: providerLabel(entry.provider) + " → " + entry.model + ": " + modelTokens +
              " tokens (" + value.requests + " requests)"
          }));
        });
      }
      column.appendChild(area);
      column.appendChild(el("div", { class: "lb-day small", text: cell.day.slice(5) }));
      column.appendChild(el("div", { class: "lb-day small muted", text: known ? (cell.status === "open" ? "in progress" : "") : "no data" }));
      chart.appendChild(column);
    });
    card.appendChild(chart);
    var legend = el("div", { class: "lb-legend small" });
    models.forEach(function (model, index) {
      legend.appendChild(el("span", { class: "lb-key" }, [
        el("span", { class: "lb-swatch", style: "background:" + lbColor(index) }),
        el("span", { text: "#" + model.rank + " " + providerLabel(model.provider) + " → " + model.model })
      ]));
    });
    card.appendChild(legend);
    return card;
  }

  // The date x model matrix: the leaderboard's core artifact. A covered
  // day renders its measured values (0 included); a day with no recorded
  // rows renders one explicit "no data" span — the two claims can never be
  // confused.
  function renderLeaderboardMatrix(payload) {
    var models = payload.matrix.models;
    var card = el("div", { class: "card" }, [
      el("h2", { text: "Date × model matrix" }),
      el("p", {
        class: "small muted",
        text: (payload.primary_metric.label || "input + output tokens") +
          " per UTC day, with request counts. 0 = measured zero on a recorded day; \"no data\" = the day carries no recorded rows — a different claim, drawn differently."
      })
    ]);
    if (!models.length) {
      card.appendChild(el("p", { class: "muted", text: "No models recorded in this window." }));
      return card;
    }
    var header = [el("th", { text: "Day (UTC)" }), el("th", { text: "Record" })];
    models.forEach(function (model) {
      header.push(el("th", {
        text: "#" + model.rank + " " + model.model,
        title: providerLabel(model.provider)
      }));
    });
    header.push(el("th", { text: "Day total" }));
    var table = el("table", {}, [el("thead", {}, [el("tr", {}, header)])]);
    var body = el("tbody");
    payload.matrix.cells.forEach(function (cell) {
      var known = cell.status === "ok" || cell.status === "open";
      if (!known) {
        body.appendChild(el("tr", { class: "lb-absent" }, [
          el("td", { class: "small", text: cell.day }),
          el("td", {
            class: "small", colspan: String(models.length + 2),
            text: lbAbsentText(cell.status) + " · nothing recorded for this day, not zero"
          })
        ]));
        return;
      }
      var rowCells = [
        el("td", { class: "small", text: cell.day }),
        el("td", { class: "small", text: cell.status === "open" ? "in progress" : "recorded" })
      ];
      var dayTotal = 0;
      cell.values.forEach(function (value) {
        if (!value) {
          rowCells.push(el("td", { class: "small muted", text: "—" }));
          return;
        }
        var modelTokens = value.input_tokens + value.output_tokens;
        dayTotal += modelTokens;
        rowCells.push(el("td", {}, [
          el("div", { text: tokens(modelTokens) }),
          el("div", { class: "small muted", text: value.requests + " req" })
        ]));
      });
      rowCells.push(el("td", { text: tokens(dayTotal) }));
      body.appendChild(el("tr", {}, rowCells));
    });
    table.appendChild(body);
    card.appendChild(el("div", { class: "table-wrap" }, [table]));
    return card;
  }

  // Leader vs challenger: the top two rows side by side. Both cost cells
  // keep the full honesty treatment — an unpriced challenger never looks
  // free.
  function renderLeaderboardCompare(payload) {
    var card = el("div", { class: "card" }, [el("h2", { text: "Leader vs challenger" })]);
    if (payload.ranked.length < 2) {
      card.appendChild(el("p", { class: "small muted", text: "At least two ranked models are needed for a comparison; this window has " + payload.ranked.length + "." }));
      return card;
    }
    var leader = payload.ranked[0];
    var challenger = payload.ranked[1];
    var table = el("table", {}, [el("thead", {}, [el("tr", {}, [
      el("th", { text: "Metric" }),
      el("th", { text: "#1 " + leader.model + " (" + providerLabel(leader.provider) + ")" }),
      el("th", { text: "#2 " + challenger.model + " (" + providerLabel(challenger.provider) + ")" })
    ])])]);
    var tbody = el("tbody");
    function compareRow(label, a, b) {
      tbody.appendChild(el("tr", {}, [
        el("td", { text: label }), el("td", { text: a }), el("td", { text: b })
      ]));
    }
    compareRow("Share of window tokens", lbShareText(leader.share), lbShareText(challenger.share));
    compareRow("Tokens (in / out)",
      tokens(leader.input_tokens) + " / " + tokens(leader.output_tokens),
      tokens(challenger.input_tokens) + " / " + tokens(challenger.output_tokens));
    compareRow("Requests", String(leader.requests), String(challenger.requests));
    compareRow("Fallbacks", String(leader.fallbacks), String(challenger.fallbacks));
    compareRow("Errors", String(leader.errors), String(challenger.errors));
    tbody.appendChild(el("tr", {}, [
      el("td", { text: "Cost" }), lbCostCell(leader), lbCostCell(challenger)
    ]));
    table.appendChild(tbody);
    card.appendChild(el("div", { class: "table-wrap" }, [table]));
    return card;
  }

  // The fallback panel, read from the same durable rollup the status page's
  // analytics card uses (no second fetch, no recomputation): the overall
  // rate, why fallbacks happened, and where they happened per alias.
  function renderLeaderboardFallback() {
    var card = el("div", { class: "card" }, [el("h2", { text: "Fallback rate" }),
      el("p", { class: "small muted", text: "From the same durable rollup as the ranking above. Metadata only; no prompts, responses, tokens or secrets are stored. The full breakdown lives on the Status page." })
    ]);
    var analytics = state && state.analytics;
    if (!analytics || !analytics.available) {
      card.appendChild(el("p", {
        class: "small muted",
        text: (analytics && analytics.unavailable_reason === "durable_rollup_disabled")
          ? "Durable fallback analytics are unavailable: request-ledger persistence is off, so there is no daily rollup to read."
          : "Durable fallback analytics are unavailable: " + ((analytics && analytics.unavailable_reason) || "the state payload has no analytics") + ". Missing data is reported, never filled in."
      }));
      return card;
    }
    var fallback = analytics.fallback || {};
    var overall = fallback.overall || { requests: 0, fallbacks: 0, fallback_rate: null, fallback_reasons: [] };
    card.appendChild(el("p", {
      class: "small muted",
      text: "Rollup window " + ((analytics.window && analytics.window.from) || "unknown") +
        " to " + ((analytics.window && analytics.window.to) || "unknown") +
        " (" + (analytics.days || 0) + " days, UTC)."
    }));
    card.appendChild(el("div", { class: "row" }, [
      statBox("Requests", overall.requests || 0),
      statBox("Fallbacks", overall.fallbacks || 0),
      statBox("Fallback rate", percentText(overall.fallback_rate))
    ]));
    var reasons = overall.fallback_reasons || [];
    if (!reasons.length) {
      card.appendChild(el("p", { class: "small muted", text: "No fallbacks were recorded in this window." }));
      return card;
    }
    card.appendChild(el("h3", { text: "By reason" }));
    var reasonTable = el("table", {}, [el("thead", {}, [el("tr", {}, [
      el("th", { text: "Reason" }), el("th", { text: "Fallbacks" }), el("th", { text: "Share of fallbacks" })
    ])])]);
    var reasonBody = el("tbody");
    reasons.forEach(function (entry) {
      reasonBody.appendChild(el("tr", {}, [
        el("td", { text: fallbackReasonLabel(entry.reason) }),
        el("td", { text: String(entry.count) }),
        el("td", { text: entry.share == null ? "Unknown" : (Number(entry.share) * 100).toFixed(1) + "%" })
      ]));
    });
    reasonTable.appendChild(reasonBody);
    card.appendChild(reasonTable);
    var aliasKeys = Object.keys(fallback.alias || {}).sort();
    if (aliasKeys.length) {
      card.appendChild(el("h3", { text: "By alias" }));
      var aliasTable = el("table", {}, [el("thead", {}, [el("tr", {}, [
        el("th", { text: "Alias" }), el("th", { text: "Requests" }),
        el("th", { text: "Fallbacks" }), el("th", { text: "Fallback rate" })
      ])])]);
      var aliasBody = el("tbody");
      aliasKeys.forEach(function (key) {
        var row = fallback.alias[key];
        aliasBody.appendChild(el("tr", {}, [
          el("td", { text: key }),
          el("td", { text: String(row.requests) }),
          el("td", { text: String(row.fallbacks) }),
          rateCell(row.fallback_rate)
        ]));
      });
      aliasTable.appendChild(aliasBody);
      card.appendChild(aliasTable);
    }
    return card;
  }

  // The methodology panel: the window, the UTC convention, which token
  // kinds are counted, where the prices come from, and how absent data and
  // unpriced models are treated. It cites the real price-source URLs the
  // payload carries — observed counters plus published sources, which is a
  // stronger claim than opencode.ai/data's list-price estimates.
  function renderLeaderboardMethodology(payload) {
    var card = el("div", { class: "card" }, [el("h2", { text: "Methodology" })]);
    var metric = payload.primary_metric || {};
    var coverage = payload.coverage || {};
    var bullets = [
      "Window: the last " + payload.days + " UTC days, " + payload.window.from +
        " to " + payload.window.to + ". Days are UTC boundaries; the final day is " +
        (payload.matrix.days.length && payload.matrix.days[payload.matrix.days.length - 1].status === "open"
          ? "in progress and will keep changing."
          : "complete."),
      "Source: the durable daily rollup (" + (payload.schema || "daily-rollup/v1") +
        ", 30-day retention, rebuilt from the durable request store on every start), " +
        "aggregated from (day, alias, provider, model) rows onto (day, provider, model). " +
        "Rollup generated at " + (payload.generated_at || "unknown") + ".",
      "Ranking metric: " + (metric.label || "input + output tokens") + ". " + (metric.definition || ""),
      "Tokens counted: input, output and cached exactly as the provider reported them. " +
        "The gateway does not tokenize; it reports observed counters. Cached tokens are a " +
        "subset of input tokens and are never counted a second time. No separate reasoning-" +
        "token counter exists in the ledger, and none is invented here.",
      "Cost: dollars are computed at read time from published catalogue prices, each with a " +
        "real source URL and observation date (cited below), applied to the observed token " +
        "counters. That is stronger than list-price estimation, but it is still an estimate " +
        "of gateway spend — not a provider bill.",
      "A model with no published price renders as \"no published price\" and is excluded from " +
        "known subtotals — never as $0.00. A price is never transferred across providers: the " +
        "same model id on two providers is two rows with two independent prices.",
      "Absent data: every day carries a record status. A day outside recorded coverage, or " +
        "with no rows, renders as \"no data\"; only a recorded day where a model genuinely had " +
        "no requests carries a measured 0. The two are drawn differently on purpose.",
      "Fallbacks are counted from the router's recorded fallback events, with reasons folded " +
        "to a closed vocabulary (cooldown, route_ineligible, upstream_failure, other, unknown). " +
        "A configured phase route is a choice, not a fallback, and is never counted as one.",
      "Dimensions: model, provider, alias, fallback reason and cost. This is a single-owner " +
        "private appliance — there are no user, session or geo dimensions to show."
    ];
    if (coverage.first_row_ts) {
      bullets.splice(1, 0, "Recorded history begins " + coverage.first_row_ts +
        (coverage.incomplete
          ? "; coverage is incomplete — some ledger rows could not be represented."
          : "; every retained row in the window is represented."));
    }
    var gaps = payload.gaps || [];
    if (gaps.length) {
      bullets.push("Gaps in this window: " + gaps.join(", ") +
        " — drawn as no data, never as zero usage.");
    }
    var list = el("ul", { class: "lb-methodology small" });
    bullets.forEach(function (text) {
      list.appendChild(el("li", { text: text }));
    });
    card.appendChild(list);
    var sources = {};
    (payload.ranked || []).forEach(function (entry) {
      var cost = entry.cost || {};
      if (cost.source) sources[cost.source + "|" + (cost.observed_at || "")] = cost;
    });
    var sourceKeys = Object.keys(sources).sort();
    card.appendChild(el("h3", { text: "Price sources cited in this window" }));
    if (!sourceKeys.length) {
      card.appendChild(el("p", {
        class: "small muted",
        text: "No model in this window has a published price, so no price source is cited — and none is invented."
      }));
    } else {
      sourceKeys.forEach(function (key) {
        card.appendChild(el("p", { class: "small", text: sources[key].source + " · observed " + (sources[key].observed_at || "date unknown") }));
      });
    }
    return card;
  }

  function loadLeaderboard() {
    if (leaderboardPhase === "loading") return;
    leaderboardPhase = "loading";
    leaderboardError = null;
    api(BASE + "/api/leaderboard?days=" + encodeURIComponent(leaderboardWindow))
      .then(function (payload) {
        leaderboardPayload = payload;
        leaderboardPhase = "ready";
        if (tab === "leaderboard") draw();
      })
      .catch(function (error) {
        leaderboardPhase = "error";
        leaderboardError = error.message || String(error);
        if (tab === "leaderboard") draw();
      });
  }

  function renderLeaderboard() {
    var frag = document.createDocumentFragment();
    // Tab title, not a second copy of the ranking card's "Model leaderboard".
    var header = el("div", { class: "card" }, [el("h2", { text: "Leaderboard" })]);
    var selector = el("select", {
      "aria-label": "Leaderboard window (UTC days)",
      onchange: function () {
        leaderboardWindow = Number(selector.value);
        leaderboardPayload = null;
        leaderboardPhase = "idle";
        draw();
      }
    });
    LB_WINDOW_OPTIONS.forEach(function (days) {
      selector.appendChild(el("option", {
        value: String(days),
        selected: leaderboardWindow === days,
        text: "Last " + days + " days (UTC)"
      }));
    });
    header.appendChild(el("div", { class: "row" }, [
      el("span", { class: "small muted", text: "Window" }),
      el("div", { class: "narrow" }, [selector]),
      el("button", {
        type: "button",
        onclick: function () { leaderboardPayload = null; leaderboardPhase = "idle"; draw(); }
      }, ["Refresh"])
    ]));
    header.appendChild(el("p", {
      class: "small muted",
      text: "Private owner view, served from this console's own /api/leaderboard route. " +
        "Aggregates only — no request identifiers, prompts, responses or credentials."
    }));
    frag.appendChild(el("section", {}, [header]));

    if (leaderboardPhase === "idle") loadLeaderboard();
    if (!leaderboardPayload || leaderboardPhase === "idle" || leaderboardPhase === "loading") {
      frag.appendChild(el("section", {}, [el("p", { class: "muted", text: "Loading leaderboard…" })]));
      return frag;
    }
    if (leaderboardPhase === "error") {
      frag.appendChild(el("section", {}, [el("div", { class: "card" }, [
        el("h2", { text: "Leaderboard unavailable" }),
        el("p", { class: "small", text: "The leaderboard endpoint did not answer: " + leaderboardError }),
        el("button", {
          type: "button",
          onclick: function () { leaderboardPhase = "idle"; draw(); }
        }, ["Retry"])
      ])]));
      return frag;
    }
    var payload = leaderboardPayload;
    if (!payload.available) {
      frag.appendChild(el("section", {}, [emptyState(
        "Leaderboard unavailable: " + (payload.unavailable_reason || "unknown reason"),
        "The durable rollup is not readable right now, so nothing is ranked. Missing data is reported, never filled in. The Status page's live totals still reflect this process."
      )]));
      return frag;
    }
    frag.appendChild(el("section", {}, [renderLeaderboardRanking(payload)]));
    frag.appendChild(el("section", {}, [renderLeaderboardChart(payload)]));
    frag.appendChild(el("section", {}, [renderLeaderboardMatrix(payload)]));
    frag.appendChild(el("section", {}, [renderLeaderboardCompare(payload)]));
    frag.appendChild(el("section", {}, [renderLeaderboardFallback()]));
    frag.appendChild(el("section", {}, [renderLeaderboardMethodology(payload)]));
    return frag;
  }

  // --- providers --------------------------------------------------------

  var pendingDevice = {};

  function renderProviders() {
    var frag = document.createDocumentFragment();


    state.providers.forEach(function (provider) {
      frag.appendChild(renderProvider(provider));
    });
    return frag;
  }

  function renderProvider(provider) {
    var card = el("div", { class: "card" }, [
      el("div", { class: "row" }, [
        el("div", {}, [el("h2", { text: providerLabel(provider.provider_id) })]),
        el("div", { class: "narrow" }, [statePill(provider.state)]),
        el("div", { class: "narrow" }, [
          el("button", {
            type: "button", class: "secondary", text: "Test connection",
            onclick: function () {
              run(api(BASE + "/api/provider", { method: "POST", body: { action: "test", provider_id: provider.provider_id } }))
                .then(function (result) {
                  toast(result.message, result.ok ? "ok" : "bad");
                  return refresh();
                })
                .catch(function () {});
            }
          })
        ]),
        el("div", { class: "narrow" }, [
          el("button", {
            type: "button", class: "secondary", text: "Refresh model catalog",
            onclick: function () {
              run(api(BASE + "/api/provider", { method: "POST", body: { action: "refresh", provider_id: provider.provider_id } }))
                .then(function (result) {
                  toast("Catalog refreshed: " + result.model_count + " models.", "ok");
                  return refresh();
                })
                .catch(function () {});
            }
          })
        ]),
        el("div", { class: "narrow" }, [
          el("button", {
            type: "button", class: "danger", text: "Clear cooldowns",
            onclick: function () {
              // A deliberate recovery action: the owner confirms the
              // subscription/reset window is open before the local
              // circuits for this provider are cleared.
              if (!window.confirm(
                "Clear the local cooldowns for " + providerLabel(provider.provider_id) + "?\n\n" +
                "Only do this after you have confirmed the subscription or reset window has reopened. " +
                "The next request to this provider will be the single recovery probe."
              )) return;
              run(api(BASE + "/api/provider", { method: "POST", body: { action: "clear_cooldowns", provider_id: provider.provider_id } }))
                .then(function (result) {
                  var message = "Cleared " + result.cleared_count + " cooldown(s) for " + providerLabel(provider.provider_id) + ".";
                  if (result.probe) {
                    message += result.probe.ok
                      ? " Recovery probe served by " + providerLabel(result.probe.serving_provider) + " → " + result.probe.serving_model + "."
                      : " Recovery probe failed: " + (result.probe.error && result.probe.error.message ? result.probe.error.message : "unknown error") + ".";
                  }
                  toast(message, result.probe && !result.probe.ok ? "bad" : "ok");
                  return refresh();
                })
                .catch(function () {});
            }
          })
        ])
      ]),
      el("div", {
        class: "small muted",
        text: "credential: " + (provider.credential_present ? "stored" : "not stored") +
          " · last test: " + when(provider.last_test_at) +
          (provider.last_error ? " · " + provider.last_error : "")
      })
    ]);

    // Scan structure: everything under this label enrols or tests a credential.
    card.appendChild(el("div", { class: "eyebrow", text: "Credential" }));
    if (provider.provider_id === "chatgpt_codex") {
      card.appendChild(renderDeviceCode(provider));
    } else if (isLocalProvider(provider.provider_id)) {
      card.appendChild(renderLocalEndpoint(provider));
      card.appendChild(renderKeyForm(provider, "Optional local token"));
      card.appendChild(el("p", { class: "small muted", text: LOCAL_PRIVACY_TEXT }));
    } else {
      card.appendChild(renderKeyForm(provider));
      if (provider.provider_id === "opencode_console") {
        card.appendChild(el("p", { class: "small muted", text: "Jev evaluates the state and questions sent to the native /v1/systemone endpoint. It is a structured-decision provider, not a chat-completion model." }));
      }
    }
    card.appendChild(renderConcurrencyControl(provider));
    return card;
  }

  // Per-provider in-flight cap. The server sends both numbers - the saved
  // override (or null) and the one that applies right now - so the owner
  // sees the effect of the cloud/local split without knowing the
  // defaults, and never needs a code change to move this knob again.
  var CONCURRENCY_HELP =
    "Over the cap the gateway refuses the request immediately with HTTP 429 " +
    "(\"local concurrency limit reached for this provider; retry later\"). It does not " +
    "queue, and this is a local protection - never a limit the provider published. " +
    "Cloud providers default to 32 requests in flight; the local servers (Ollama, " +
    "llama.cpp, LM Studio) default to 4 because they share this machine's memory and " +
    "GPU, where more in-flight streams mean out-of-memory risk rather than more " +
    "speed. Saving a number overrides the default for this provider only and takes " +
    "effect on the next request, with no restart. Save an empty value to return to " +
    "the default.";

  function renderConcurrencyControl(provider) {
    var effective = typeof provider.max_in_flight_effective === "number"
      ? provider.max_in_flight_effective
      : null;
    var overridden = typeof provider.max_in_flight === "number";
    var input = el("input", {
      type: "number", min: "1", step: "1", autocomplete: "off",
      placeholder: "default", "aria-label": "Maximum requests in flight"
    });
    if (overridden) input.value = String(provider.max_in_flight);

    function save(value) {
      run(api(BASE + "/api/provider", {
        method: "POST",
        body: { action: "set_max_in_flight", provider_id: provider.provider_id, max_in_flight: value }
      }))
        .then(function (result) {
          toast(value === null
            ? "Limit returned to the default (" + result.max_in_flight_effective + " in flight)."
            : "Limit saved: " + result.max_in_flight_effective + " in flight.", "ok");
          return refresh();
        })
        .catch(function () {});
    }

    var saveButton = el("button", {
      class: "secondary", type: "button", text: "Save limit",
      onclick: function () {
        var raw = String(input.value || "").trim();
        if (raw === "") { save(null); return; }
        if (!/^[0-9]+$/.test(raw) || Number(raw) < 1) {
          // The server refuses the same values with 400; say it here so
          // the owner is not left reading a toast about a number they
          // can see is not a whole one.
          toast("The in-flight limit must be a positive whole number.", "bad");
          return;
        }
        save(Number(raw));
      }
    });
    var clearButton = el("button", {
      type: "button", text: "Use default",
      onclick: function () { save(null); }
    });

    return el("div", {}, [
      el("h3", { text: "Concurrency" }),
      el("p", { class: "small", text:
        "In flight at once: " + (effective === null ? "default" : effective) +
        (overridden ? " (owner override " + provider.max_in_flight + ")" : " (default for this provider)") }),
      el("div", { class: "row" }, [
        el("label", {}, [el("span", { text: "Maximum in flight" }), input]),
        el("div", { class: "narrow" }, [saveButton]),
        el("div", { class: "narrow" }, [clearButton])
      ]),
      el("p", { class: "small muted", text: CONCURRENCY_HELP })
    ]);
  }

  // What a local endpoint means, stated where the owner types it: that the
  // field takes a root origin and the gateway appends the OpenAI paths, the
  // URL shape each of the three servers expects, which addresses are
  // accepted, what loopback means under this deployment's host networking,
  // and what enrolling a token does.
  var LOCAL_ENDPOINT_HELP =
    "Enter the server's ROOT origin, not a full API path: the gateway appends " +
    "/v1/chat/completions and /v1/models itself (and /api/show on Ollama), and a " +
    "trailing /v1 is stripped. Shapes: Ollama is http://HOST:11434; llama.cpp " +
    "(llama-server) is http://HOST:PORT for whatever --port you passed — current " +
    "upstream builds default to 9931, older builds and the official Docker images " +
    "to 8080, and --host defaults to 127.0.0.1 so the server must be started " +
    "LAN-facing to accept another machine; LM Studio is http://HOST:PORT from its " +
    "local-network port display. Requests go only to this address. It must be a " +
    "private or loopback address " +
    "(127.0.0.0/8, 10/8, 172.16/12, 192.168/16, fc00::/7); public addresses and " +
    "link-local addresses such as 169.254.169.254 are refused, and the address is " +
    "checked again against the actual connection before any request byte is sent. " +
    "This deployment uses host networking, so 127.0.0.1 reaches a server on this " +
    "machine's own loopback and a LAN address such as 192.168.x.x reaches another " +
    "machine; run bridge-networked instead and 127.0.0.1 would be the container itself. " +
    "Saving a different endpoint clears the models discovered from the previous one, " +
    "so refresh the catalogue afterwards. The gateway never downloads a model.";

  var LOCAL_AUTH_TEXT =
    "All three local servers are keyless by default, so no token is needed. If you " +
    "enabled one on the server itself (llama-server --api-key, LM Studio Require " +
    "Auth), enrol it here or the connection test fails with an authentication error. " +
    "Ollama ignores any Authorization value it receives, so enrolling a token for " +
    "Ollama changes nothing; the gateway sends no credential unless you enrol one.";

  var LOCAL_PRIVACY_TEXT =
    "Requests to this provider stay on this LAN. Falling back from it to a cloud " +
    "provider sends the prompt off this machine, and that switch is refused unless " +
    "fallback is authorized and the provider being fallen back to is explicitly confirmed for the alias.";

  function renderLocalEndpoint(provider) {
    var input = el("input", { type: "text", autocomplete: "off", spellcheck: "false", placeholder: "http://127.0.0.1:11434" });
    input.value = provider.base_url || "";
    var save = el("button", {
      class: "secondary", type: "button", text: "Save endpoint",
      onclick: function () {
        var url = input.value.trim();
        run(api(BASE + "/api/provider", { method: "POST", body: { action: "set_base_url", provider_id: provider.provider_id, base_url: url } }))
          .then(function (result) {
            toast(url ? "Endpoint saved: " + (result.base_url || url) : "Endpoint cleared.", "ok");
            return refresh();
          })
          .catch(function () {});
      }
    });
    var clear = el("button", {
      type: "button", text: "Clear endpoint",
      onclick: function () {
        if (!window.confirm("Clear the saved endpoint for " + providerLabel(provider.provider_id) + "?\n\nModels discovered from it are removed, and the provider returns to not configured.")) return;
        run(api(BASE + "/api/provider", { method: "POST", body: { action: "set_base_url", provider_id: provider.provider_id, base_url: "" } }))
          .then(function () {
            input.value = "";
            toast("Endpoint cleared.", "ok");
            return refresh();
          })
          .catch(function () {});
      }
    });
    return el("div", {}, [
      el("h3", { text: "Local endpoint" }),
      el("p", { class: "small", text: provider.base_url ? "Saved endpoint: " + provider.base_url : "No endpoint saved. This provider stays not configured until one is saved." }),
      el("div", { class: "row" }, [el("label", {}, [el("span", { text: "Server base URL" }), input]), el("div", { class: "narrow" }, [save]), el("div", { class: "narrow" }, [clear])]),
      el("p", { class: "small muted", text: LOCAL_ENDPOINT_HELP }),
      el("p", { class: "small muted", text: LOCAL_AUTH_TEXT })
    ]);
  }

  function renderKeyForm(provider, heading) {
    var input = el("input", { type: "password", autocomplete: "off", placeholder: "provider API key" });
    var wrap = el("div", { class: "row" }, [
      el("label", {}, [el("span", { text: "API key" }), input]),
      el("div", { class: "narrow" }, [
        el("button", {
          class: "primary", type: "button", text: "Save and test",

          onclick: function () {
            var key = input.value.trim();
            if (!key) {
              toast("enter the API key first", "bad");
              return;
            }
            run(api(BASE + "/api/provider", { method: "POST", body: { action: "enroll_key", provider_id: provider.provider_id, api_key: key } }))
              .then(function () {
                input.value = "";
                toast("Key stored. It will not be shown again. Testing the connection...", "ok");
                // The button says "Save and test", so it tests.
                return api(BASE + "/api/provider", { method: "POST", body: { action: "test", provider_id: provider.provider_id } });
              })
              .then(function (result) {
                toast(result.message, result.ok ? "ok" : "bad");
                return refresh();
              })
              .catch(function () {});
          }
        })
      ])
    ]);
    var block = el("div", {}, [
      // The form must name the provider whose key is being pasted: the old
      // fallback titled every unlabelled card "Connect OpenCode Go".
      el("h3", { text: heading || ("Connect " + providerLabel(provider.provider_id)) }),
      el("p", { class: "small muted", text: "The key is stored in an owner-only credential store, separately from any other provider, and is never displayed again." }),
      wrap
    ]);

    return block;
  }

  function renderDeviceCode(provider) {
    var info = state.device_code || {};
    var block = el("div", {}, [
      el("h3", { text: "Connect ChatGPT" }),
      el("div", { class: "notice warn" }, [
        el("strong", { text: "Before you start: " }),
        el("span", { text: info.prerequisite || "" })
      ])
    ]);

    var startButton = el("button", {
      class: "primary", type: "button", text: "Connect ChatGPT",
      disabled: !state.can_mutate,
      onclick: function () {
        run(api(BASE + "/api/provider", { method: "POST", body: { action: "device_start", provider_id: provider.provider_id } }))
          .then(function (result) {
            pendingDevice[provider.provider_id] = result;
            draw();
            scheduleDevicePoll(provider.provider_id);
          })
          .catch(function () {});
      }
    });
    block.appendChild(startButton);

    var pending = pendingDevice[provider.provider_id];
    if (pending && pending.state === "pending") {
      block.appendChild(el("div", { class: "card", style: "margin-top:12px" }, [
        el("h3", { text: "Enter this code" }),
        el("p", {}, [el("a", { href: pending.verification_url, target: "_blank", rel: "noreferrer noopener", text: pending.verification_url })]),
        el("div", { style: "font-size:26px;letter-spacing:3px;margin:8px 0", class: "mono", text: pending.user_code }),
        el("div", { class: "notice warn" }, [el("span", { text: info.anti_phishing || "" })]),
        el("p", { class: "small muted", text: "This page checks the status automatically. The code expires in about 15 minutes." }),
        el("button", {
          type: "button", text: "Check the status now",
          onclick: function () {
            pollDevice(provider.provider_id);
          }
        })
      ]));
    }
    return block;
  }

  /* Poll the pending device authorisation at the interval the provider
   * asked for. The handle is the gateway's own opaque value; the provider's
   * device auth id never reaches the browser. */
  function scheduleDevicePoll(providerId) {
    var pending = pendingDevice[providerId];
    if (!pending || pending.state !== "pending") return;
    var interval = Math.max(2, Number(pending.interval) || 5) * 1000;
    window.setTimeout(function () {
      pollDevice(providerId);
    }, interval);
  }

  function pollDevice(providerId) {
    var pending = pendingDevice[providerId];
    if (!pending || pending.state !== "pending") return;
    return api(BASE + "/api/provider", {
      method: "POST",
      body: { action: "device_poll", provider_id: providerId, poll_handle: pending.poll_handle }
    })
      .then(function (result) {
        pending.state = result.state;
        pending.error = result.error;
        pendingDevice[providerId] = pending;
        if (result.state === "approved") {
          toast("Connected. The credential is stored and will not be shown again.", "ok");
          return refresh();
        }
        if (result.state === "denied") {
          toast(result.error || "The authorisation was denied.", "bad");
          return refresh();
        }
        if (result.state === "expired") {
          toast(result.error || "The code expired.", "bad");
          return refresh();
        }
        // Still pending: keep checking at the provider's interval.
        scheduleDevicePoll(providerId);
        return null;
      })
      .catch(function (error) {
        toast(error.message || "could not check the authorisation status", "bad");
        return null;
      });
  }

  // --- models and routes ------------------------------------------------

  // One honest price line for every preview state. A published tariff is
  // never rendered as "Unknown": known rates print with their source and
  // observation date even when another gate blocks the run, a stale tariff
  // says it is stale, and only a model the catalogue does not price reads
  // "no published price". `amount` is the worst-case run *estimate* and only
  // exists when a protocol supplied the request bounds.
  function probePriceText(price) {
    if (!price || price.status === "unknown" || price.status === undefined) return "no published price";
    var tariff = "";
    if (typeof price.input_per_m === "number" && typeof price.output_per_m === "number") {
      tariff = "tariff " + price.input_per_m + "/" + price.output_per_m + " USD per 1M in/out";
      if (typeof price.cached_read_per_m === "number") tariff += " (cached read " + price.cached_read_per_m + ")";
    }
    var provenance = (price.source ? " from " + price.source : "") + (price.as_of ? ", observed " + price.as_of : "");
    if (typeof price.amount === "number") {
      return "worst-case estimate " + price.amount + " " + price.currency + (tariff ? " (" + tariff + ")" : "") + provenance;
    }
    if (price.status === "stale") {
      return "stale published price" + (tariff ? " (" + tariff + ")" : "") + provenance + "; stale pricing blocks paid probes";
    }
    return (tariff || "published price") + provenance;
  }

  function renderCapabilityPanel(provider, model, panel) {
    var target = { provider_id: provider.provider_id, model_id: model.model_id };
    var readUrl = BASE + "/api/capabilities?provider_id=" + encodeURIComponent(target.provider_id) + "&model_id=" + encodeURIComponent(target.model_id);
    panel.appendChild(el("p", { class: "small muted", text: "Optional synthetic capability discovery. No probes run on page load or catalogue refresh. Every signed-in owner can run these probes; their results are evidence about providers, not guarantees." }));
    panel.appendChild(el("p", { class: "small muted", text: "Probe results are evidence, not guarantees. Unknown price or unsupported probe protocol blocks execution; estimates are not a bill ceiling. Fixed suite mvp-1: exact bounded synthetic text marker; locally generated inline shape-count image; harmless forced function schema whose returned call is never executed." }));
    // Probe-registry coverage, rendered from the evidence read so the owner
    // sees the opt-in allowlist before clicking anything: a refusal on an
    // unregistered pair is policy, not a malfunction.
    var registryLine = el("p", { class: "small muted" });
    panel.appendChild(registryLine);
    // Numbered flow on the only surface that spends money: evidence first,
    // authorisation second. The evidence region wears the cool rule.
    panel.appendChild(el("div", { class: "step-label" }, [
      el("span", { class: "step-no", text: "1" }),
      el("span", { class: "small muted", text: "Evidence" })
    ]));
    var evidence = el("div", { class: "capability-evidence", role: "status", "aria-live": "polite" });
    panel.appendChild(evidence);
    api(readUrl).then(function (data) {
      var registry = data.probe_registry;
      if (registry) {
        var scope = typeof registry.staged_model_count === "number"
          ? " of " + registry.staged_model_count + " staged model(s)"
          : "";
        registryLine.textContent = "Probe registry: " + registry.registered_count + " pair(s) registered for paid probing" + scope +
          " — an opt-in allowlist, so an unregistered pair refuses before any request. This pair: " +
          (registry.this_pair_registered ? "registered." : "not registered.");
      } else {
        registryLine.textContent = "Probe registry coverage not reported; probing remains an opt-in allowlist.";
      }
      (data.runs || []).forEach(function (runRecord) {
        (runRecord.results || []).forEach(function (result) {
          evidence.appendChild(el("p", { class: "small", text: result.capability + ": " + result.status + " · " + (result.method || "method unknown") + " · " + when(runRecord.finished_at) + " (observed evidence; not a guarantee)" }));
        });
      });
      (data.approvals || []).forEach(function (approval) {
        evidence.appendChild(el("p", { class: "small", text: "Approved metadata provenance: " + JSON.stringify(approval) }));
      });
      (data.proposals || []).forEach(function (proposal) {
        if (["pending", "partially_approved"].indexOf(proposal.status) === -1) return;
        var proposalBox = el("div", { class: "route" }, [el("h4", { text: "Review proposed metadata changes · " + proposal.status })]);
        var choices = {};
        (proposal.fields || []).forEach(function (field) {
          var check = el("input", { type: "checkbox" });
          check.checked = false;
          choices[field.field] = check;
          proposalBox.appendChild(el("label", { class: "capability-field" }, [check, el("span", { text: field.field + ": " + String(field.current_value) + " → " + String(field.proposed_value) + " · " + field.classification + " evidence · " + (field.routing_consequence || "Routing impact unknown; no automatic policy change") })]));
        });
        function decide(value) {
          var decisions = {};
          Object.keys(choices).forEach(function (field) { if (choices[field].checked) decisions[field] = value; });
          if (!Object.keys(decisions).length) { toast("Select at least one field; no changes were made.", "bad"); return; }
          var fingerprint = proposal.base_model_fingerprint || proposal.expected_fingerprint;
          var expectedRevision = proposal.base_revision;
          if (!global.confirm("Explicitly " + value + " selected metadata fields for " + target.provider_id + " / " + target.model_id + "? No route or alias settings are changed.")) return;
          api(BASE + "/api/capabilities/decision", { method: "POST", body: { proposal_id: proposal.proposal_id, decision_id: "ui-" + Date.now() + "-" + Math.random().toString(36).slice(2), decisions: decisions, expected_revision: expectedRevision, expected_fingerprint: fingerprint } })
            .then(function () { toast("Decision saved.", "ok"); return refresh(); })
            .catch(function (error) { toast(error.message + (error.payload && error.payload.error && error.payload.error.type === "conflict" ? " — refresh and review a new diff." : ""), "bad"); });
        }
        proposalBox.appendChild(el("button", { type: "button", text: "Approve selected changes", disabled: !state.can_mutate, onclick: function () { decide("approved"); } }));
        proposalBox.appendChild(el("button", { type: "button", class: "secondary", text: "Reject selected changes", disabled: !state.can_mutate, onclick: function () { decide("rejected"); } }));
        evidence.appendChild(proposalBox);
      });
      if (!(data.runs || []).length && !(data.proposals || []).length) evidence.appendChild(el("p", { class: "small muted", text: "No capability evidence or pending proposal for this exact provider/model." }));
    }).catch(function () { evidence.appendChild(el("p", { class: "small muted", text: "Capability evidence could not be loaded. No provider call was made." })); });

    var choices = {};
    var probeNames = [["text_generation", "Basic text response"], ["image_input", "Synthetic image understanding"], ["function_tools", "Function tool call (never executed)"]];
    var probeList = el("div", { class: "row" });
    probeNames.forEach(function (item) {
      var input = el("input", { type: "checkbox", value: item[0], onchange: function () { syncPreviewControl(); } });
      input.checked = false;
      choices[item[0]] = input;
      probeList.appendChild(el("label", {}, [input, el("span", { text: item[1] })]));
    });
    panel.appendChild(probeList);

    // The owner's billing-route authorisation. It is captured here from an
    // explicit, owner-facing interaction and held in this panel's memory
    // only: nothing is written to config, storage or the server, so a page
    // reload or a closed panel requires a fresh authorisation. The backend
    // owner gate still refuses any run without it, and without the
    // single-use confirmation issued by the matching preview.
    var authorisedOwner = null;
    // Captured at render time: click handlers must not read the live state
    // (a test render restores it), and refresh() re-renders this panel.
    var canMutate = !!state.can_mutate;
    var authHost = el("div", { class: "capability-authorisation" });
    panel.appendChild(authHost);

    function renderAuthorisationPrompt() {
      authHost.textContent = "";
      authHost.appendChild(el("div", { class: "step-label" }, [
        el("span", { class: "step-no", text: "2" }),
        el("span", { class: "small muted", text: "Authorise" })
      ]));
      authHost.appendChild(el("h4", { text: "Authorise a paid probe run" }));
      authHost.appendChild(el("p", { class: "small", text: "Probes are paid inference requests sent to the configured OpenCode Go account for " + target.provider_id + " / " + target.model_id + ", billed through that account as the billing route. OpenCode Go publishes no request-level max-charge control, so no hard dollar ceiling can be enforced: the price shown is a worst-case estimate for this exact run, not a cap. The spend ceiling below bounds the estimate the gateway will accept before sending — it is not a provider-enforced limit." }));
      var ceilingInput = el("input", { type: "number", min: "0.01", step: "any", value: "0.1" });
      authHost.appendChild(el("label", { class: "capability-field" }, [el("span", { text: "Maximum estimated spend for this run (USD)" }), ceilingInput]));
      authHost.appendChild(el("button", { type: "button", text: "Authorise billing route", disabled: !canMutate, onclick: function () {
        var ceiling = Number(ceilingInput.value);
        if (!isFinite(ceiling) || ceiling <= 0) { toast("The spend ceiling must be a number greater than zero; nothing was sent.", "bad"); return; }
        authorisedOwner = { authorize_billing_route: true, spend_policy: "estimate_accepted", max_spend_estimate_usd: ceiling };
        renderAuthorisationGranted(ceiling);
        syncPreviewControl();
      } }));
      authHost.appendChild(el("button", { type: "button", class: "secondary", text: "Cancel", disabled: !canMutate, onclick: function () {
        authorisedOwner = null;
        syncPreviewControl();
        authHost.textContent = "";
        authHost.appendChild(el("p", { class: "small muted", text: "No billing route authorised. Nothing was sent to the provider." }));
        authHost.appendChild(el("button", { type: "button", text: "Authorise a paid probe run", disabled: !canMutate, onclick: function () { renderAuthorisationPrompt(); } }));
      } }));
    }

    function renderAuthorisationGranted(ceiling) {
      authHost.textContent = "";
      authHost.appendChild(el("p", { class: "small", text: "Billing route authorised for this panel: " + target.provider_id + " / " + target.model_id + " · spend ceiling " + String(ceiling) + " USD · estimate policy (not a hard cap). Held in this page only — a reload or a closed panel requires authorising again." }));
      authHost.appendChild(el("button", { type: "button", class: "secondary", text: "Withdraw authorisation", onclick: function () {
        authorisedOwner = null;
        syncPreviewControl();
        renderAuthorisationPrompt();
      } }));
    }

    renderAuthorisationPrompt();

    // The result must land BELOW the button, in the owner's line of sight.
    // Rendering it into an initially-empty div above the button hides it
    // exactly where the click happened: the whole loading cycle flashes past
    // in under a millisecond on a LAN and the owner never sees it, which
    // reads as "the button does nothing". The button also states why it
    // cannot be used instead of sitting dead with no reason shown.
    var previewHost = el("div", { class: "capability-preview", role: "status", "aria-live": "polite" });
    var previewHintId = "capability-preview-hint-" + target.provider_id + "-" + target.model_id;
    var previewHint = el("p", { class: "small muted", id: previewHintId });
    var previewBusy = false;

    function selectedProbes() {
      return Object.keys(choices).filter(function (key) { return choices[key].checked; });
    }

    // The single source of truth for whether preview may run, so the button,
    // its label and the line under it can never disagree with each other.
    function previewGateReason() {
      if (!canMutate) return "Read-only session: authorising a paid run and previewing are unavailable.";
      if (!selectedProbes().length) return "Select at least one probe above to enable preview.";
      if (!authorisedOwner) return "Authorise the billing route above to enable preview; previewing itself sends no provider request.";
      return "";
    }

    function syncPreviewControl() {
      var reason = previewBusy ? "" : previewGateReason();
      previewButton.disabled = previewBusy || !!reason;
      previewButton.textContent = previewBusy ? "Previewing…" : "Preview selected probes";
      previewHint.textContent = reason;
    }

    // The output sits directly under the button, so it is normally already on
    // screen; this only moves the page when the result would otherwise land
    // below the fold.
    function revealPreview() {
      if (typeof previewHost.scrollIntoView === "function") previewHost.scrollIntoView({ block: "nearest" });
    }

    var previewButton = el("button", { type: "button", text: "Preview selected probes", "aria-describedby": previewHintId, disabled: true, onclick: function () {
      var selected = selectedProbes();
      if (!selected.length) { toast("Select at least one probe; nothing was sent.", "bad"); return; }
      if (!authorisedOwner) { toast("Authorise the billing route first; nothing was sent.", "bad"); return; }
      // Feedback in the same tick as the click: the button changes state and
      // the result region below it fills, before any network round trip.
      previewBusy = true;
      syncPreviewControl();
      previewHost.textContent = "";
      previewHost.appendChild(el("p", { class: "small", text: "Loading preview… reading the published bounds for " + target.provider_id + " / " + target.model_id + "; no provider request is sent." }));
      revealPreview();
      api(BASE + "/api/capabilities/preview", { method: "POST", body: { provider_id: target.provider_id, model_id: target.model_id, probes: selected, owner: authorisedOwner } })
        .then(function (preview) {
          previewBusy = false;
          syncPreviewControl();
          previewHost.textContent = "";
          previewHost.appendChild(el("h4", { text: preview.available ? "Preview available" : "Preview unavailable" }));
          previewHost.appendChild(el("p", { class: "small", text: "Target: " + preview.provider_id + " / " + preview.model_id + " · selected: " + preview.probes.join(", ") + " · suite " + preview.suite_version + " · max " + preview.maximum_requests + " request(s), output " + preview.max_output_tokens + " tokens, " + preview.timeout_seconds + "s, concurrency " + preview.concurrency + ", retries " + preview.retries + "." }));
          previewHost.appendChild(el("p", { class: "notice warn", text: preview.reason + " Price: " + probePriceText(preview.price) + ". No provider request has been sent." }));
          previewHost.appendChild(el("p", { class: "small muted", text: "Estimate is not a bill guarantee: worst-case tariff arithmetic on the published bounds; no provider-enforced hard ceiling exists for this route." }));
          if (preview.available) {
            previewHost.appendChild(el("button", { type: "button", text: "Run selected probes", disabled: !canMutate, onclick: function () {
              if (!global.confirm("Send only these synthetic probes to " + target.provider_id + " / " + target.model_id + "? The provider will receive synthetic data. Price estimates are not guaranteed billing caps.")) return;
              api(BASE + "/api/capabilities/run", { method: "POST", body: { provider_id: target.provider_id, model_id: target.model_id, probes: selected, owner: authorisedOwner, base_revision: preview.base_revision, confirmation: preview.confirmation, confirmed: true } }).then(function () { return refresh(); }).catch(function (error) { toast(error.message + " No result is treated as unsupported unless the evidence is an unambiguous capability-specific refusal.", "bad"); });
            } }));
          } else {
            previewHost.appendChild(el("p", { class: "small muted", text: "Unavailable: this probe cannot be run for the reason above; Unknown is not false." }));
          }
          revealPreview();
        }).catch(function (error) {
          var unavailable = error.payload && typeof error.payload.available === "boolean" ? error.payload : null;
          previewBusy = false;
          syncPreviewControl();
          previewHost.textContent = "";
          if (unavailable) {
            previewHost.appendChild(el("h4", { text: "Preview unavailable" }));
            previewHost.appendChild(el("p", { class: "small", text: "Target: " + unavailable.provider_id + " / " + unavailable.model_id + " · selected: " + (unavailable.probes || []).join(", ") + " · suite " + unavailable.suite_version + " · maximum " + unavailable.maximum_requests + " request(s), output " + unavailable.max_output_tokens + " tokens, timeout " + unavailable.timeout_seconds + "s." }));
            previewHost.appendChild(el("p", { class: "notice warn", text: unavailable.reason + " Price: " + probePriceText(unavailable.price) + ". No provider request has been sent." }));
            previewHost.appendChild(el("p", { class: "small muted", text: "Unavailable; Unknown is not false. No probe was executed." }));
          } else {
            previewHost.appendChild(el("h4", { text: "Preview refused" }));
            previewHost.appendChild(el("p", { class: "notice bad", text: error.message + ". No provider request was sent." }));
          }
          revealPreview();
        });
    } });
    // Button first, output last: everything the click produces is rendered
    // after the button that produced it.
    panel.appendChild(previewButton);
    panel.appendChild(previewHint);
    panel.appendChild(previewHost);
    syncPreviewControl();
  }

  function renderModels() {
    var frag = document.createDocumentFragment();
    // Exactly one discovery panel may be open at a time, tracked per render.
    // Each panel's own text is derived from its target at render time, but two
    // open panels put two different targets' "Authorise a paid probe run"
    // paragraphs on the page at once — the reader sees the older one first —
    // and the owner must never read a paid-run confirmation that describes a
    // different provider/model than the row whose panel would be billed.
    var openCapabilityPanel = null; // { row, button } of the open panel

    var modelsCard = el("div", { class: "card" }, [
      el("h2", { text: "Provider models" }),
      el("p", { class: "small muted", text: "Discovered models are staged. A model appears in /v1/models only after it is enabled and published through an alias." })
    ]);
    var hasDiscoveredModels = state.providers.some(function (provider) { return provider.models.length > 0; });
    if (!hasDiscoveredModels) {
      modelsCard.appendChild(emptyState(
        "No discovered models yet",
        "Next: open Providers and explicitly choose Refresh catalogue for a connected provider. This page does not contact providers automatically."
      ));
    }
    state.providers.forEach(function (provider) {
      if (!provider.models.length) return;
      modelsCard.appendChild(el("h3", { text: providerLabel(provider.provider_id) }));
      var table = el("table", {}, [
        el("thead", {}, [el("tr", {}, [
          el("th", { text: "Model" }), el("th", { text: "Dialect" }), el("th", { text: "Limit" }),
          el("th", { text: "Context" }), el("th", { text: "Retention" }), el("th", { text: "" })
        ])])
      ]);
      var tbody = el("tbody");
      provider.models.forEach(function (model) {
        tbody.appendChild(el("tr", {}, [
          el("td", {}, [
            el("div", { class: "mono", text: model.model_id }),
            el("div", { class: "small muted", text: model.source + (model.notes ? " · " + model.notes : "") })
          ]),
          el("td", { class: "small", text: dialectName(model.dialect) }),
          el("td", {}, [limitCell(model)]),
          el("td", { class: "small", text: tokens(model.context_limit) }),
          el("td", { class: "small", text: model.retention_class || "unknown" }),
          el("td", { class: "actions" }, [
            el("button", {
              type: "button", class: "secondary", text: "Discover capabilities",
              disabled: !state.can_mutate || provider.state !== "connected",
              "aria-expanded": "false",
              onclick: function (event) {
                var button = event.currentTarget;
                var row = button.closest("tr");
                var existing = row.nextElementSibling;
                if (existing && existing.classList.contains("capability-detail-row")) {
                  existing.remove();
                  button.setAttribute("aria-expanded", "false");
                  openCapabilityPanel = null;
                  return;
                }
                // Close any other open panel first: two open panels show two
                // different targets' paid-run confirmations at once, and the
                // owner can read the stale one while operating the new one.
                if (openCapabilityPanel) {
                  openCapabilityPanel.row.remove();
                  openCapabilityPanel.button.setAttribute("aria-expanded", "false");
                  openCapabilityPanel = null;
                }
                var detailRow = document.createElement("tr");
                detailRow.className = "capability-detail-row";
                var detailCell = document.createElement("td");
                detailCell.colSpan = 6;
                var panel = el("div", { class: "capability-panel" });
                renderCapabilityPanel(provider, model, panel);
                detailCell.appendChild(panel);
                detailRow.appendChild(detailCell);
                row.parentNode.insertBefore(detailRow, row.nextSibling);
                button.setAttribute("aria-expanded", "true");
                openCapabilityPanel = { row: detailRow, button: button };
              }
            }),
            el("button", {
              type: "button",
              text: model.enabled ? "Disable" : "Enable",
              onclick: function () {
                run(api(BASE + "/api/model", {
                  method: "POST",
                  body: { action: model.enabled ? "disable" : "enable", provider_id: provider.provider_id, model_id: model.model_id }
                })).then(function () { return refresh(); }).catch(function () {});
              }
            })
          ])
        ]));
      });
      table.appendChild(tbody);
      modelsCard.appendChild(el("div", { class: "table-wrap" }, [table]));
    });

    var manualProvider = el("select", {});
    state.providers.forEach(function (provider) {
      manualProvider.appendChild(el("option", { value: provider.provider_id, text: providerLabel(provider.provider_id) }));
    });
    var manualModel = el("input", { placeholder: "model id" });
    var manualDialect = el("select", {}, [
      el("option", { value: "openai_responses", text: "Responses" }),
      el("option", { value: "openai_chat", text: "Chat Completions" })
    ]);
    modelsCard.appendChild(el("h3", { text: "Enter a model id manually" }));
    modelsCard.appendChild(el("p", {
      class: "small muted",
      text: "Use this when discovery is unavailable or incomplete. An undocumented model has an unknown dialect and is shown as Unknown rather than guessed."
    }));
    modelsCard.appendChild(el("div", { class: "row" }, [
      el("label", {}, [el("span", { text: "Provider" }), manualProvider]),
      el("label", {}, [el("span", { text: "Model id" }), manualModel]),
      el("label", {}, [el("span", { text: "Dialect" }), manualDialect]),
      el("div", { class: "narrow" }, [
        el("button", {
          type: "button", text: "Add model",
          disabled: !state.can_mutate,
          onclick: function () {
            run(api(BASE + "/api/model", {
              method: "POST",
              body: { action: "manual", provider_id: manualProvider.value, model_id: manualModel.value, dialect: manualDialect.value }
            })).then(function () { manualModel.value = ""; return refresh(); }).catch(function () {});
          }
        })
      ])
    ]));
    frag.appendChild(el("section", {}, [modelsCard]));

    frag.appendChild(el("section", {}, [renderAliasEditor()]));
    return frag;
  }

  // A pure summary of one save payload against the saved alias (null for a
  // new alias): the alias name, the primary and fallback order, and the
  // trigger/authorization impact, so the owner confirms exactly what will
  // change before anything is written. Nothing here touches the network.
  function savePreviewLines(model, previous) {
    var routeText = function (route) {
      return route && route.provider ? providerLabel(route.provider) + " → " + route.model : "none";
    };
    var list = [];
    list.push(previous
      ? "Alias: " + model.name + " (editing the saved alias)"
      : "Alias: " + model.name + " (new — nothing with this name is saved yet)");
    if (!previous) {
      list.push("Primary: " + routeText(model.primary));
      list.push("Fallback order: " + (model.fallbacks.length ? model.fallbacks.map(routeText).join(" · ") : "none"));
      list.push("Triggers: " + (model.triggers.length ? model.triggers.join(", ") : "none"));
      list.push("Cross-provider authorization: " + (model.authorized ? "enabled" : "disabled") +
        " · confirmed providers: " + (model.confirmed.length ? model.confirmed.join(", ") : "none"));
      list.push("Cooldown: " + model.cooldown + "s · publish state: not published (publish separately)");
      list.push("Phase policy: " + (model.phaseRouting ? "enabled" : "none"));
      return list;
    }
    var oldPrimary = routeText(previous.primary);
    var newPrimary = routeText(model.primary);
    list.push(oldPrimary === newPrimary
      ? "Primary: " + newPrimary + " (unchanged)"
      : "Primary: " + oldPrimary + " → " + newPrimary);
    var oldFallbacks = (previous.fallbacks || []).map(routeText);
    var newFallbacks = model.fallbacks.map(routeText);
    if (oldFallbacks.join(" · ") === newFallbacks.join(" · ")) {
      list.push("Fallback order: " + (newFallbacks.length ? newFallbacks.join(" · ") : "none") + " (unchanged)");
    } else {
      list.push("Fallback order — was: " + (oldFallbacks.length ? oldFallbacks.join(" · ") : "none"));
      list.push("Fallback order — now: " + (newFallbacks.length ? newFallbacks.join(" · ") : "none"));
    }
    var oldTriggers = previous.fallback_triggers || [];
    var added = model.triggers.filter(function (trigger) { return oldTriggers.indexOf(trigger) === -1; });
    var removed = oldTriggers.filter(function (trigger) { return model.triggers.indexOf(trigger) === -1; });
    list.push("Trigger impact: " + (!added.length && !removed.length
      ? "no trigger changes"
      : (added.length ? "enable " + added.join(", ") : "") +
        (added.length && removed.length ? "; " : "") +
        (removed.length ? "disable " + removed.join(", ") : "")));
    var oldAuthorized = previous.fallback_authorized === true;
    list.push("Cross-provider authorization: " + (oldAuthorized === model.authorized
      ? (model.authorized ? "enabled (unchanged)" : "disabled (unchanged)")
      : (oldAuthorized ? "enabled → disabled" : "disabled → enabled")));
    var oldConfirmed = previous.fallback_confirmed_providers || [];
    var confirmedSame = oldConfirmed.length === model.confirmed.length &&
      model.confirmed.every(function (providerId) { return oldConfirmed.indexOf(providerId) !== -1; });
    list.push("Confirmed providers: " + (confirmedSame
      ? (model.confirmed.length ? model.confirmed.join(", ") : "none") + " (unchanged)"
      : "was " + (oldConfirmed.length ? oldConfirmed.join(", ") : "none") +
        " → now " + (model.confirmed.length ? model.confirmed.join(", ") : "none")));
    var oldCooldown = Number(previous.cooldown_seconds);
    list.push(oldCooldown === Number(model.cooldown)
      ? "Cooldown: " + model.cooldown + "s (unchanged)"
      : "Cooldown: " + previous.cooldown_seconds + "s → " + model.cooldown + "s");
    var oldPhase = previous.phase_routing || null;
    list.push("Phase policy: " + (!oldPhase && !model.phaseRouting
      ? "none"
      : !oldPhase ? "added"
      : !model.phaseRouting ? "removed"
      : "updated"));
    list.push("Publish state: " + (previous.enabled
      ? "published — stays published"
      : "not published — stays unpublished"));
    return list;
  }

  // --- cross-provider fallback: the retention class of the route selected --
  // ANCHOR t_fd37033b "retention-class-warning"
  //
  // The alias editor's fallback warning used to say only that a fallback
  // goes to "a different data-retention regime". Retention is a published
  // per-model fact (`retention_class`, the same row the models table shows),
  // so the warning can name the classes of the routes actually selected -
  // primary first, then each fallback row. Two routes of one alias routinely
  // disagree: `claude-haiku-5-5` is `30_days` while the other four
  // Anthropic-dialect models are `0_days`, so which regime a prompt moves to
  // depends on the route that serves it, not on "Anthropic" as a provider.
  var RETENTION_WORDS = {
    "30_days": "30-day retention (abuse monitoring)",
    "0_days": "0-day retention",
    "training_permitted": "prompts may be used for training",
    "unknown": "retention unknown"
  };

  function modelRetentionClass(state, providerId, modelId) {
    var providers = (state && state.providers) || [];
    for (var i = 0; i < providers.length; i += 1) {
      if (providers[i].provider_id !== providerId) continue;
      var models = providers[i].models || [];
      for (var j = 0; j < models.length; j += 1) {
        if (models[j].model_id === modelId) return models[j].retention_class || "unknown";
      }
      // The provider is configured but does not list this model: unknown is
      // the honest answer, never a guess from the provider name.
      return "unknown";
    }
    return "unknown";
  }

  function retentionWarningText(state, routes) {
    var seen = [];
    var parts = (routes || []).map(function (route) {
      var cls = modelRetentionClass(state, route.provider, route.model);
      if (seen.indexOf(cls) === -1) seen.push(cls);
      var name = route.model || "no model selected";
      if (route.provider_label) name += " (" + route.provider_label + ")";
      return route.role + " " + name + " = " + cls;
    });
    if (!parts.length) return "";
    var text = "Retention of the routes selected here: " + parts.join("; ") + ".";
    if (seen.length > 1) {
      text += " These classes differ, so a fallback moves the prompt from " +
        (RETENTION_WORDS[seen[0]] || seen[0]) + " to " +
        (RETENTION_WORDS[seen[seen.length - 1]] || seen[seen.length - 1]) +
        "; the class that applied is the one on the route that actually served " +
        "the request, which the request ledger names per request.";
    }
    return text;
  }

  function renderAliasEditor() {
    var card = el("div", { class: "card" }, [el("h2", { text: "Aliases and routes" })]);
    var editing = editingAlias && state.aliases.filter(function (alias) { return alias.name === editingAlias; })[0];
    if (editingAlias && !editing) editingAlias = null;

    // RH-05: the first click never mutates. A preview of exactly what will
    // change is shown with an explicit "Save routing changes"/"Cancel"
    // choice; a cancel or a rejected save leaves the configuration
    // untouched, says so, and keeps the form draft for correction.
    var unchangedBanner = el("div", { class: "notice warn save-unchanged", role: "alert", tabindex: "-1", hidden: true });
    card.appendChild(unchangedBanner);

    function showUnchangedBanner(detailText) {
      unchangedBanner.innerHTML = "";
      unchangedBanner.appendChild(el("strong", { text: "Changes not saved. Current configuration is unchanged." }));
      if (detailText) {
        var detail = el("p", { class: "small save-error", tabindex: "-1", text: detailText });
        unchangedBanner.appendChild(detail);
        unchangedBanner.appendChild(el("button", {
          type: "button", text: "Review errors",
          onclick: function () { detail.focus(); }
        }));
      }
      unchangedBanner.removeAttribute("hidden");
      unchangedBanner.focus();
    }

    var nameInput = el("input", { placeholder: "coding", value: editing ? editing.name : "", disabled: !!editing });
    var tagInput = el("input", { placeholder: "coding", value: editing ? (editing.tag || "") : "" });
    var primaryProvider = el("select", {});
    var primaryModel = el("select", {});
    var fallbacks = [];
    var fallbackList = el("div");

    function refreshModelSelects() {
      var provider = state.providers.filter(function (p) { return p.provider_id === primaryProvider.value; })[0];
      primaryModel.innerHTML = "";
      (provider ? provider.models.filter(function (m) { return m.enabled; }) : []).forEach(function (model) {
        primaryModel.appendChild(el("option", { value: model.model_id, text: model.model_id }));
      });
    }
    state.providers.forEach(function (provider) {
      primaryProvider.appendChild(el("option", { value: provider.provider_id, text: providerLabel(provider.provider_id) }));
    });
    primaryProvider.addEventListener("change", refreshModelSelects);
    if (editing && editing.primary) primaryProvider.value = editing.primary.provider;
    refreshModelSelects();
    if (editing && editing.primary) primaryModel.value = editing.primary.model;

    function addFallbackRow(route) {
      var provider = el("select", {});
      var model = el("select", {});
      state.providers.forEach(function (p) {
        provider.appendChild(el("option", { value: p.provider_id, text: providerLabel(p.provider_id) }));
      });
      function fillModels() {
        model.innerHTML = "";
        var found = state.providers.filter(function (p) { return p.provider_id === provider.value; })[0];
        (found ? found.models.filter(function (m) { return m.enabled; }) : []).forEach(function (m) {
          model.appendChild(el("option", { value: m.model_id, text: m.model_id }));
        });
      }
      provider.addEventListener("change", fillModels);
      if (route) provider.value = route.provider;
      fillModels();
      if (route) model.value = route.model;
      var entry = { provider: provider, model: model };
      var row = el("div", { class: "row" }, [
        el("label", {}, [el("span", { text: "Provider" }), provider]),
        el("label", {}, [el("span", { text: "Model" }), model]),
        el("div", { class: "narrow" }, [
          el("button", {
            type: "button",
            text: "Remove",
            onclick: function () {
              // The row and the saved list must stay in step: removing the
              // row and leaving the entry behind silently restored the
              // fallback on the next save.
              var at = fallbacks.indexOf(entry);
              if (at !== -1) fallbacks.splice(at, 1);
              row.remove();
            }
          })
        ])
      ]);
      fallbacks.push(entry);
      fallbackList.appendChild(row);
    }
    if (editing) (editing.fallbacks || []).forEach(addFallbackRow);

    var triggerBoxes = {};
    var triggersWrap = el("div", { class: "row" });
    (state.fallback_triggers || []).forEach(function (trigger) {
      var box = el("input", { type: "checkbox", value: trigger });
      box.checked = !!(editing && (editing.fallback_triggers || []).indexOf(trigger) !== -1);
      triggerBoxes[trigger] = box;
      triggersWrap.appendChild(el("label", { style: "flex:0 0 auto" }, [
        el("span", { text: trigger.replace(/_/g, " ") }), box
      ]));
    });

    var authorizeBox = el("input", { type: "checkbox" });
    authorizeBox.checked = !!(editing && editing.fallback_authorized);
    var confirmProviders = el("div");

    function renderConfirmProviders() {
      confirmProviders.innerHTML = "";
      state.providers.forEach(function (provider) {
        var box = el("input", { type: "checkbox", value: provider.provider_id });
        box.checked = !!(editing && (editing.fallback_confirmed_providers || []).indexOf(provider.provider_id) !== -1);
        confirmProviders.appendChild(el("label", { style: "flex:0 0 auto" }, [
          el("span", { text: "may receive requests: " + providerLabel(provider.provider_id) }), box
        ]));
      });
    }
    renderConfirmProviders();

    var cooldown = el("input", { type: "number", value: String(editing ? editing.cooldown_seconds : 300), min: "0" });
    var budget = el("input", { type: "number", value: editing && editing.budget != null ? String(editing.budget) : "", min: "0.01", step: "0.01", placeholder: "No cap" });

    var phasePolicy = editing && editing.phase_routing;
    var phaseEnabled = el("input", { type: "checkbox" });
    phaseEnabled.checked = !!(phasePolicy && phasePolicy.enabled);
    var removePhasePolicy = el("input", { type: "checkbox" });
    var phaseRows = {};

    function routeEditor(route, title) {
      var provider = el("select", {});
      var model = el("select", {});
      state.providers.forEach(function (p) {
        provider.appendChild(el("option", { value: p.provider_id, text: providerLabel(p.provider_id) }));
      });
      function fillModels() {
        model.innerHTML = "";
        var found = state.providers.filter(function (p) { return p.provider_id === provider.value; })[0];
        (found ? found.models.filter(function (m) { return m.enabled; }) : []).forEach(function (m) {
          model.appendChild(el("option", { value: m.model_id, text: m.model_id }));
        });
      }
      provider.addEventListener("change", fillModels);
      if (route && route.provider) provider.value = route.provider;
      fillModels();
      if (route && route.model) model.value = route.model;
      return { provider: provider, model: model, row: el("div", { class: "row" }, [
        el("strong", { text: title }),
        el("label", {}, [el("span", { text: "Provider" }), provider]),
        el("label", {}, [el("span", { text: "Model" }), model])
      ]) };
    }

    var phaseRoutes = (phasePolicy && phasePolicy.routes) || {};
    ["plan", "implement", "validate"].forEach(function (phase) {
      var saved = phaseRoutes[phase];
      var route = routeEditor(saved ? saved.route : null, phase);
      var include = el("input", { type: "checkbox" });
      include.checked = !!saved;
      var effort = el("input", { placeholder: "optional effort", value: saved && saved.effort || "" });
      route.row.insertBefore(el("label", {}, [el("span", { text: "Route this phase" }), include]), route.row.children[1]);
      route.row.appendChild(el("label", {}, [el("span", { text: "Effort (optional)" }), effort]));
      phaseRows[phase] = { include: include, route: route, effort: effort };
    });

    var defaultRoute = routeEditor(phasePolicy && phasePolicy.default_route, "Default route");
    var hasDefault = el("input", { type: "checkbox" });
    hasDefault.checked = !!(phasePolicy && phasePolicy.default_route);
    defaultRoute.row.insertBefore(el("label", {}, [el("span", { text: "Use default route" }), hasDefault]), defaultRoute.row.children[1]);

    var inferenceData = (phasePolicy && phasePolicy.inference) || {};
    var inferenceEnabled = el("input", { type: "checkbox" });
    inferenceEnabled.checked = !!inferenceData.enabled;
    var inferenceRoute = routeEditor({ provider: inferenceData.provider || "opencode_console", model: inferenceData.model || "" }, "");
    var confidence = el("input", { type: "number", min: "0", max: "1", step: "0.01", value: String(inferenceData.minimum_confidence === undefined ? 0.8 : inferenceData.minimum_confidence) });
    var onUncertain = el("select", {}, [el("option", { value: "default", text: "Use default route" })]);
    onUncertain.value = inferenceData.on_uncertain || "default";

    card.appendChild(el("h3", { text: "Per-alias phase routing" }));
    card.appendChild(el("label", {}, [el("span", { text: "Enable phase routing" }), phaseEnabled]));
    if (phasePolicy) card.appendChild(el("label", {}, [el("span", { text: "Remove phase policy" }), removePhasePolicy]));
    ["plan", "implement", "validate"].forEach(function (phase) { card.appendChild(phaseRows[phase].route.row); });
    card.appendChild(el("div", { class: "small muted" }, [
      el("span", { text: "A per-phase effort is stored with the policy and round-trips unchanged, but the gateway does not apply it to the upstream request - the request's own reasoning effort is used instead." })
    ]));
    card.appendChild(defaultRoute.row);
    card.appendChild(el("h3", { text: "Jev phase inference" }));
    card.appendChild(el("label", {}, [el("span", { text: "Enable Jev inference" }), inferenceEnabled]));
    card.appendChild(el("div", { class: "row" }, [
      inferenceRoute.row,
      el("label", {}, [el("span", { text: "Minimum confidence (0–1)" }), confidence]),
      el("label", {}, [el("span", { text: "When uncertain" }), onUncertain])
    ]));

    card.appendChild(el("div", { class: "row" }, [
      el("label", {}, [el("span", { text: "Alias name" }), nameInput]),
      el("label", {}, [el("span", { text: "Group tag (optional)" }), tagInput]),
      el("label", {}, [el("span", { text: "Primary provider" }), primaryProvider]),
      el("label", {}, [el("span", { text: "Primary model" }), primaryModel])
    ]));
    card.appendChild(el("h3", { text: "Fallback routes (in order)" }));
    card.appendChild(el("div", { class: "small muted" }, [
      el("span", { text: "Cross-provider fallback is disabled by default. It sends a prompt to a different provider, and therefore to a different data-retention regime." })
    ]));
    // ANCHOR t_fd37033b "retention-class-warning": the same warning, made
    // specific to the routes selected in this editor. Purely additive - the
    // generic sentence above is left exactly as it was.
    var retentionSpan = el("span", { text: "" });
    function selectedRoutes() {
      var routes = [{
        role: "primary",
        provider: primaryProvider.value,
        model: primaryModel.value,
        provider_label: providerLabel(primaryProvider.value)
      }];
      fallbacks.forEach(function (row, index) {
        routes.push({
          role: "fallback " + (index + 1),
          provider: row.provider.value,
          model: row.model.value,
          provider_label: providerLabel(row.provider.value)
        });
      });
      return routes;
    }
    function renderRetentionWarning() {
      retentionSpan.textContent = retentionWarningText(state, selectedRoutes());
    }
    card.appendChild(el("div", { class: "small muted" }, [retentionSpan]));
    // The events bubble from every select (and from a fallback row's
    // Remove click, and from the Add fallback button) to the card, so the
    // classes shown are the ones on screen when Save is pressed.
    card.addEventListener("change", renderRetentionWarning);
    card.addEventListener("click", renderRetentionWarning);
    renderRetentionWarning();
    card.appendChild(fallbackList);
    card.appendChild(el("button", { type: "button", text: "Add fallback route", onclick: addFallbackRow }));
    card.appendChild(el("h3", { text: "When may a fallback run?" }));
    card.appendChild(triggersWrap);
    card.appendChild(el("h3", { text: "Cross-provider fallback authorisation" }));
    card.appendChild(el("label", { style: "flex:1 1 auto" }, [
      el("span", { text: "I authorise requests assigned to this alias to be sent to the providers named below" }), authorizeBox
    ]));
    card.appendChild(confirmProviders);

    var previewHost = el("div", { class: "save-preview", hidden: true });
    card.appendChild(previewHost);

    function openPreview() {
      var triggers = Object.keys(triggerBoxes).filter(function (key) { return triggerBoxes[key].checked; });
      var confirmed = Array.prototype.slice.call(confirmProviders.querySelectorAll("input:checked"))
        .map(function (box) { return box.value; });
      var phaseRouting = null;
      // Removal is an explicit owner action and takes precedence over
      // the enabled toggle. Otherwise retain an existing disabled policy
      // so it can be re-enabled without rebuilding its configuration.
      if (!removePhasePolicy.checked && (phaseEnabled.checked || phasePolicy)) {
        var routes = {};
        Object.keys(phaseRows).forEach(function (phase) {
          var item = phaseRows[phase];
          if (!item.include.checked) return;
          routes[phase] = {
            provider: item.route.provider.value,
            model: item.route.model.value
          };
          if (item.effort.value.trim()) routes[phase].effort = item.effort.value.trim();
        });
        phaseRouting = {
          enabled: phaseEnabled.checked,
          routes: routes,
          default_route: hasDefault.checked
            ? { provider: defaultRoute.provider.value, model: defaultRoute.model.value }
            : null,
          inference: {
            enabled: inferenceEnabled.checked,
            provider: inferenceRoute.provider.value,
            model: inferenceRoute.model.value,
            minimum_confidence: confidence.value === "" ? 0.8 : Number(confidence.value),
            on_uncertain: onUncertain.value
          }
        };
      }
      var payload = {
        action: "save",
        name: nameInput.value,
        tag: tagInput.value,
        primary: { provider: primaryProvider.value, model: primaryModel.value },
        fallbacks: fallbacks.map(function (row) {
          return { provider: row.provider.value, model: row.model.value };
        }),
        fallback_triggers: triggers,
        fallback_authorized: authorizeBox.checked,
        fallback_confirmed_providers: confirmed,
        cooldown_seconds: parseInt(cooldown.value, 10) || 0,
        budget: budget.value === "" ? null : Number(budget.value),
        enabled: editing ? editing.enabled : false,
        phase_routing: phaseRouting
      };
      previewHost.innerHTML = "";
      var title = el("h4", { id: "save-preview-title", tabindex: "-1", text: "Preview — nothing has been saved" });
      previewHost.appendChild(title);
      previewHost.appendChild(el("p", {
        class: "small",
        text: "Review the routing changes below. The configuration is written only when you choose Save routing changes."
      }));
      var list = el("ul", { class: "preview-lines" });
      savePreviewLines({
        name: payload.name,
        primary: payload.primary,
        fallbacks: payload.fallbacks,
        triggers: payload.fallback_triggers,
        authorized: payload.fallback_authorized,
        confirmed: payload.fallback_confirmed_providers,
        cooldown: payload.cooldown_seconds,
        phaseRouting: payload.phase_routing
      }, editing || null).forEach(function (line) {
        list.appendChild(el("li", { text: line }));
      });
      // tag and budget are written by this same save, so the preview must
      // show them too — a review that omits a field it will persist is not
      // an honest review.
      var previousTag = editing ? (editing.tag || "") : null;
      list.appendChild(el("li", {
        text: "Tag: " + (payload.tag || "none") +
          (previousTag !== null
            ? (previousTag === payload.tag ? " (unchanged)" : " — was: " + (previousTag || "none"))
            : "")
      }));
      var previousBudget = editing && editing.budget != null ? editing.budget : null;
      list.appendChild(el("li", {
        text: "Monthly budget: " + (payload.budget === null ? "no cap" : "$" + payload.budget) +
          (editing
            ? (previousBudget === payload.budget
                ? " (unchanged)"
                : " — was: " + (previousBudget === null ? "no cap" : "$" + previousBudget))
            : "")
      }));
      previewHost.appendChild(list);
      previewHost.appendChild(el("div", { class: "row" }, [
        el("button", {
          class: "primary", type: "button", text: "Save routing changes",
          disabled: !state.can_mutate,
          onclick: function () {
            run(api(BASE + "/api/alias", { method: "POST", body: payload })).then(function () {
              editingAlias = null;
              toast(editing ? "Alias updated." : "Alias saved.", "ok");
              return refresh();
            }).catch(function (error) {
              // The save was refused: nothing was written. Keep the draft
              // in place and give the owner the error plus a focus target.
              showUnchangedBanner(error.message || "the console API refused the save.");
            });
          }
        }),
        el("button", {
          type: "button", text: "Cancel",
          onclick: function () {
            previewHost.setAttribute("hidden", "");
            showUnchangedBanner(null);
          }
        })
      ]));
      previewHost.removeAttribute("hidden");
      title.focus();
    }

    card.appendChild(el("div", { class: "row" }, [
      el("label", {}, [el("span", { text: "Local cooldown (seconds)" }), cooldown]),
      el("label", {}, [el("span", { text: "Monthly budget (USD; soft cap)" }), budget]),
      el("div", { class: "narrow" }, [
        el("button", {
          class: "primary", type: "button", text: editing ? "Save changes" : "Save alias",
          disabled: !state.can_mutate,
          onclick: openPreview
        }),
        editing ? el("button", {
          type: "button", text: "Cancel edit",
          onclick: function () { editingAlias = null; draw(); }
        }) : null
      ])
    ]));

    var existing = el("div");
    state.aliases.forEach(function (alias) {
      var card2 = el("div", { class: "route" }, [
        el("div", { class: "head" }, [
          el("strong", { text: alias.name }),
          el("span", { class: "pill " + (alias.enabled ? "ok" : ""), text: alias.enabled ? "published" : "not published" })
        ]),
        el("div", {
          class: "small",
          text: "Group: " + (alias.tag || "none") + " · Primary: " + (alias.primary ? providerLabel(alias.primary.provider) + " → " + alias.primary.model : "none")
        }),
        el("div", {
          class: "small muted",
          text: "Context: " + (alias.context_limit && alias.context_limit.known
            ? tokens(alias.context_limit.limit) + " of input"
            : "Unknown - " + (alias.context_limit ? (alias.context_limit.reason || "not published") : "not published"))
        }),
        el("div", {
          class: "small muted",
          text: "Fallbacks: " + (alias.fallbacks.length
            ? alias.fallbacks.map(function (f) { return providerLabel(f.provider) + " → " + f.model; }).join(", ")
            : "none") +
            " · triggers: " + (alias.fallback_triggers.join(", ") || "none") +
            " · cooldown: " + alias.cooldown_seconds + "s" +
            " · budget: " + (alias.budget_status && alias.budget_status.capped
              ? money(alias.budget_status.spend_usd) + " / " + money(alias.budget) + " this month (soft; resets " + alias.budget_status.reset_at + ")"
              : alias.budget == null ? "uncapped" : "budget uncapped: spend Unknown (soft; resets " + (alias.budget_status ? alias.budget_status.reset_at : "UTC month boundary") + ")")
        }),
        el("div", { class: "actions" }, [
          el("button", {
            type: "button", text: "Edit",
            disabled: !state.can_mutate,
            onclick: function () { editingAlias = alias.name; draw(); }
          }),
          el("button", {
            type: "button", text: alias.enabled ? "Unpublish" : "Publish",
            disabled: !state.can_mutate,
            onclick: function () {
              run(api(BASE + "/api/alias", { method: "POST", body: { action: "publish", name: alias.name, enabled: !alias.enabled } }))
                .then(function () { return refresh(); }).catch(function () {});
            }
          }),
          el("button", {
            type: "button", text: "Test alias",
            disabled: !state.can_mutate,
            onclick: function () {
              run(api(BASE + "/api/alias", { method: "POST", body: { action: "test", name: alias.name } }))
                .then(function (result) {
                  toast("Served by " + providerLabel(result.serving_provider) + " → " + result.serving_model, "ok");
                  return refresh();
                })
                .catch(function () {});
            }
          }),
          el("button", {
            class: "danger", type: "button", text: "Delete",
            disabled: !state.can_mutate,
            onclick: function () {
              run(api(BASE + "/api/alias", { method: "POST", body: { action: "delete", name: alias.name } }))
                .then(function () { return refresh(); }).catch(function () {});
            }
          })
        ])
      ]);
      existing.appendChild(card2);
    });
    card.appendChild(el("h3", { text: "Existing aliases" }));
    var groupTags = Array.from(new Set(state.aliases.map(function (alias) { return alias.tag; }).filter(Boolean))).sort();
    groupTags.forEach(function (tag) {
      var groupAliases = state.aliases.filter(function (alias) { return alias.tag === tag; });
      existing.appendChild(el("div", { class: "route" }, [
        el("div", { class: "head" }, [el("strong", { text: "Group: " + tag }), el("span", { class: "small muted", text: groupAliases.length + " aliases" })]),
        el("div", { class: "actions" }, [
          el("button", { type: "button", text: "Publish group", disabled: !state.can_mutate || groupAliases.some(function (alias) { return !alias.primary; }), onclick: function () {
            run(api(BASE + "/api/alias-group", { method: "POST", body: { action: "publish", tag: tag } }))
              .then(function () { return refresh(); }).catch(function () {});
          } }),
          el("button", { type: "button", text: "Disable group", disabled: !state.can_mutate, onclick: function () {
            run(api(BASE + "/api/alias-group", { method: "POST", body: { action: "disable", tag: tag } }))
              .then(function () { return refresh(); }).catch(function () {});
          } })
        ])
      ]));
    });
    if (!state.aliases.length) {
      existing.appendChild(emptyState(
        "No aliases or routes yet",
        "To add one, enter an alias name above, choose a primary provider and enabled model, then select Save alias. This will not create a route until you save."
      ));
    }
    card.appendChild(existing);
    return card;
  }

  // --- profiles ---------------------------------------------------------

  function renderProfiles() {
    var frag = document.createDocumentFragment();

    frag.appendChild(el("div", { class: "notice" }, [
      el("span", { text: state.access_mode_description })
    ]));

    frag.appendChild(el("div", { class: "notice warn" }, [
      el("strong", { text: "Profiles do not provide access control. " }),
      el("span", { text: "All reachable LAN peers share the same published routes; caller tokens are retained only as configuration/future attribution data." })
    ]));

    var card = el("div", { class: "card" }, [el("h2", { text: "Client profiles" })]);
    state.profiles.forEach(function (profile) {
      var aliasesBox = el("input", { value: (profile.allowed_aliases || []).join(", ") });
      var nameInput = el("input", { value: profile.name });
      var allAliasesBox = el("input", { type: "checkbox" });
      allAliasesBox.checked = !!profile.all_aliases;
      var tokenBox = el("div");
      card.appendChild(el("div", { class: "route" }, [
        el("div", { class: "row" }, [
          el("label", {}, [el("span", { text: "Name" }), nameInput]),
          el("label", {}, [el("span", { text: "Allowed aliases (comma separated)" }), aliasesBox]),
          el("div", { class: "narrow" }, [
            el("button", {
              type: "button", text: "Save",
              disabled: !state.can_mutate,
              onclick: function () {
                run(api(BASE + "/api/profile", {
                  method: "POST",
                  body: {
                    action: "save",
                    profile_id: profile.profile_id,
                    name: nameInput.value,
                    enabled: profile.enabled,
                    allowed_aliases: aliasesBox.value.split(",").map(function (s) { return s.trim(); }).filter(Boolean),
                    all_aliases: allAliasesBox.checked
                  }
                })).then(function () { return refresh(); }).catch(function () {});
              }
            })
          ]),
          el("div", { class: "narrow" }, [
            el("button", {
              type: "button", text: profile.has_token ? "Rotate token" : "Issue token",
              disabled: true,
              onclick: function () {
                run(api(BASE + "/api/profile", { method: "POST", body: { action: "issue_token", profile_id: profile.profile_id } }))
                  .then(function (result) {
                    tokenBox.innerHTML = "";
                    tokenBox.appendChild(el("div", { class: "notice warn" }, [
                      el("strong", { text: "Copy this token now. It is shown once and never again. " }),
                      el("pre", { class: "token", text: result.token })
                    ]));
                    return refresh();
                  })
                  .catch(function () {});
              }
            })
          ]),
          el("div", { class: "narrow" }, [
            el("button", {
              class: "danger", type: "button", text: "Revoke token",
              disabled: true,
              onclick: function () {
                run(api(BASE + "/api/profile", { method: "POST", body: { action: "revoke_token", profile_id: profile.profile_id } }))
                  .then(function () { return refresh(); }).catch(function () {});
              }
            })
          ])
        ]),
        el("div", { class: "small muted", text: profile.profile_id + " · " + (profile.anonymous ? "anonymous LAN profile" : profile.has_token ? "caller token issued " + when(profile.token_created_at) : "no caller token") }),
        el("label", { style: "flex:1 1 auto" }, [
          el("span", { text: "Allow every published alias" }), allAliasesBox
        ]),
        el("div", { class: "small muted", text: profile.all_aliases ? "May use every published alias." : (profile.allowed_aliases || []).length ? "May use: " + (profile.allowed_aliases || []).join(", ") : "May use no alias yet. A profile denies until access is granted." }),
        tokenBox
      ]));
    });

    var newId = el("input", { placeholder: "laptop" });
    var newName = el("input", { placeholder: "Laptop" });
    card.appendChild(el("h3", { text: "Add a profile" }));
    card.appendChild(el("div", { class: "row" }, [
      el("label", {}, [el("span", { text: "Profile id" }), newId]),
      el("label", {}, [el("span", { text: "Name" }), newName]),
      el("div", { class: "narrow" }, [
        el("button", {
          type: "button", text: "Create",
          disabled: !state.can_mutate,
          onclick: function () {
            run(api(BASE + "/api/profile", {
              method: "POST",
              body: { action: "save", profile_id: newId.value, name: newName.value || newId.value, enabled: true, allowed_aliases: [] }
            })).then(function () { return refresh(); }).catch(function () {});
          }
        })
      ])
    ]));
    frag.appendChild(el("section", {}, [card]));
    return frag;
  }

  // --- settings ---------------------------------------------------------

  function renderBackup() {
    var frag = document.createDocumentFragment();
    var interval = state.snapshot_interval_seconds || 0;
    var importInput = el("input", { type: "file", accept: "application/json", "aria-label": "Configuration backup JSON" });
    var intervalSelect = el("select", { "aria-label": "Snapshot interval" }, [
      el("option", { value: "0", text: "Off (default)" }),
      el("option", { value: "3600", text: "Every hour" }),
      el("option", { value: "21600", text: "Every 6 hours" }),
      el("option", { value: "86400", text: "Every 24 hours" })
    ]);
    intervalSelect.value = String(interval);
    frag.appendChild(el("section", {}, [el("div", { class: "card" }, [
      el("h2", { text: "Configuration backup" }),
      el("p", { class: "small", text: "Exports contain configuration only. Credentials, caller-token hashes and secret-adjacent diagnostics are excluded. Import replaces the current configuration; provider credentials remain untouched." }),
      el("button", { type: "button", text: "Download sanitised export", onclick: function () {
        api(BASE + "/api/backup/export").then(function (result) {
          var blob = new Blob([JSON.stringify(result.export, null, 2) + "\\n"], { type: "application/json" });
          var link = document.createElement("a"); link.href = URL.createObjectURL(blob); link.download = "gateway-config-backup.json"; link.click();
          setTimeout(function () { URL.revokeObjectURL(link.href); }, 1000);
        }).catch(function (error) { toast(error.message, "bad"); });
      } }),
      importInput,
      el("button", { type: "button", text: "Validate and import", onclick: function () {
        if (!importInput.files || !importInput.files[0]) { toast("Choose a backup JSON file first", "bad"); return; }
        importInput.files[0].text().then(function (text) {
          // Parse before anything else: a JSON typo is refused here, never
          // POSTed, and never shown as a raw engine SyntaxError.
          var payload;
          try {
            payload = JSON.parse(text);
          } catch (parseError) {
            toast("That file is not valid JSON (" +
              (parseError && parseError.message ? parseError.message : "parse failed") +
              "). Nothing was imported.", "bad");
            return null;
          }
          if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
            toast("That file is not a gateway configuration object. Nothing was imported.", "bad");
            return null;
          }
          // Import replaces the configuration wholesale, so it confirms like
          // every other destructive write in this console.
          var confirmed = global.confirm(
            "Import this configuration backup?\n\n" +
            "The import replaces the current configuration with this file's contents. " +
            "Credentials are not part of the export and are left untouched.\n\n" +
            "Only continue once you have checked what the file contains."
          );
          if (!confirmed) return null;
          return api(BASE + "/api/backup/import", { method: "POST", body: payload })
            .then(function () { return refresh(); })
            .then(function () { toast("Configuration imported", "ok"); draw(); });
        }).catch(function (error) { toast(error.message || "Invalid backup", "bad"); });
      } }),
      el("label", { class: "small", text: "Scheduled snapshot interval (stored in the state volume)" }),
      intervalSelect,
      el("button", { type: "button", text: "Save snapshot interval", onclick: function () {
        api(BASE + "/api/settings", { method: "POST", body: { action: "snapshot_interval", seconds: Number(intervalSelect.value) } })
          .then(function () { return refresh(); }).then(draw).catch(function (error) { toast(error.message, "bad"); });
      } }),
      el("p", { class: "small muted", text: "Snapshots are disabled by default. Enabling creates timestamped config.json copies in the state volume; no credentials or provider data are included." })
    ])]));
    return frag;
  }

  function renderSettings() {
    var frag = document.createDocumentFragment();

    var release = state.release || {};
    frag.appendChild(renderAccess());
    frag.appendChild(renderMachineKeys());

    frag.appendChild(el("section", {}, [
      el("div", { class: "card" }, [
        el("h2", { text: "Deployed release" }),
        el("dl", { class: "kv" }, [
          el("dt", { text: "Gateway URL" }), el("dd", { text: state.base_url || "unknown" }),
          el("dt", { text: "Source revision" }), el("dd", { text: release.source_revision || "unknown" }),
          el("dt", { text: "Source tree" }), el("dd", { text: release.source_tree || "unknown" }),
          el("dt", { text: "Image revision" }), el("dd", { text: release.image_revision || "unknown" }),
          el("dt", { text: "Configuration revision" }), el("dd", { text: String(state.revision) }),
          el("dt", { text: "Active listener" }), el("dd", { text: "Trusted LAN owner console and API" })
        ])
      ])
    ]));

    frag.appendChild(el("section", {}, [
      el("div", { class: "card" }, [
        el("h2", { text: "Host deployment controls" }),
        el("p", { class: "small muted", text: "These are read-only in this release. Changing them requires an operator action and a restart." }),
        el("dl", { class: "kv" }, [
          el("dt", { text: "Bind IP and port" }), el("dd", { text: (state.base_url || "unknown") + " — an explicit private LAN address; wildcard binding is refused at startup" }),
          el("dt", { text: "Docker networking" }), el("dd", { text: "read-only — set by the compose file; an operator action and a restart are required to change it" }),
          el("dt", { text: "Persistent runtime mount" }), el("dd", { text: "read-only — holds the configuration and the owner-only credential store" }),
          el("dt", { text: "Firewall and router state" }), el("dd", { text: "read-only — this gateway never creates a firewall or router rule" }),
          el("dt", { text: "Public exposure" }), el("dd", { text: "none — the gateway binds a private address only" })
        ])
      ])
    ]));

    frag.appendChild(el("section", {}, [
      el("div", { class: "card" }, [
        el("h2", { text: "Runtime" }),
        el("dl", { class: "kv" }, [
          el("dt", { text: "Request timeouts" }), el("dd", { text: "read-only safe runtime limits" }),
          el("dt", { text: "Local cooldown policy" }), el("dd", { text: cooldownPolicyText() })
        ]),
        cooldownTable()
      ])
    ]));

    return frag;
  }

  // --- access: owner account and client API key -------------------------

  function inputValue(id) {
    var node = global.document.getElementById(id);
    return node ? String(node.value || "") : "";
  }

  function clearInputs(ids) {
    ids.forEach(function (id) {
      var node = global.document.getElementById(id);
      if (node) node.value = "";
    });
  }

  function field(id, label, type, value) {
    return el("label", {}, [
      el("span", { text: label }),
      // value is opt-in: only a field that has a value to restore passes one,
      // so a fresh render still starts empty.
      el("input", {
        id: id, type: type, autocomplete: "off",
        value: value === undefined || value === null ? null : String(value)
      })
    ]);
  }

  // The key is generated in the browser and posted once: the server stores
  // only a hash plus a random id, so it has no value it could ever echo
  // back in a response, a log line, or this UI.
  function generateClientKey() {
    var bytes = new Uint8Array(32);
    global.crypto.getRandomValues(bytes);
    var value = Array.prototype.map.call(bytes, function (byte) {
      return ("0" + byte.toString(16)).slice(-2);
    }).join("");
    var node = global.document.getElementById("client-key-value");
    if (node) node.value = value;
    return value;
  }

  function changePassword() {
    var current = inputValue("password-current");
    var next = inputValue("password-next");
    var confirmation = inputValue("password-confirm");
    if (!next || next !== confirmation) {
      toast("new password and its confirmation must match", "bad");
      return;
    }
    run(
      api(BASE + "/api/auth/password", {
        method: "POST",
        body: { current_password: current, new_password: next }
      }).then(function (result) {
        clearInputs(["password-current", "password-next", "password-confirm"]);
        refresh();
        return result;
      }),
      "Password changed — every other session was signed out"
    );
  }

  function enableClientKey() {
    var value = inputValue("client-key-value");
    if (value.length < 32) {
      toast("client key must be at least 32 characters — use Generate", "bad");
      return;
    }
    var confirmed = global.confirm(
      "Enable the client API key?\n\nFrom now on every model client must send an Authorization: Bearer header, " +
      "and clients without it are refused. This console never edits an agent or client configuration for you — " +
      "update each one yourself."
    );
    if (!confirmed) return;
    api(BASE + "/api/auth/client-key", { method: "POST", body: { action: "set", key: value } })
      .then(function (result) {
        clearInputs(["client-key-value"]);
        refresh();
        toast(result.impact || "Client API key enabled", "ok");
      })
      .catch(function (error) { toast(error.message || "could not enable the key", "bad"); });
  }

  function rotateClientKey() {
    var value = inputValue("client-key-value");
    if (value.length < 32) {
      toast("client key must be at least 32 characters — use Generate", "bad");
      return;
    }
    var confirmed = global.confirm(
      "Rotate the client API key?\n\nThe new key is required immediately. The previous key keeps working for " +
      (CLIENT_KEY_GRACE_LABEL) +
      " so you can update clients without downtime. This console never edits an agent or client configuration for you."
    );
    if (!confirmed) return;
    api(BASE + "/api/auth/client-key", { method: "POST", body: { action: "rotate", key: value } })
      .then(function (result) {
        clearInputs(["client-key-value"]);
        refresh();
        toast(result.impact || "Client API key rotated", "ok");
      })
      .catch(function (error) { toast(error.message || "could not rotate the key", "bad"); });
  }

  function clearClientKey() {
    var confirmed = global.confirm(
      "Disable the client API key?\n\nThe model API becomes keyless again: any device that can reach this " +
      "address can call it without a header, exactly as before a key was set."
    );
    if (!confirmed) return;
    api(BASE + "/api/auth/client-key", { method: "POST", body: { action: "clear" } })
      .then(function (result) {
        refresh();
        toast(result.impact || "Client API key cleared — keyless access restored", "ok");
      })
      .catch(function (error) { toast(error.message || "could not clear the key", "bad"); });
  }

  function renderAccess() {
    var key = clientKeyState();
    var accountRows = [
      el("dt", { text: "Signed in as" }), el("dd", { text: ownerName() }),
      el("dt", { text: "Console boundary" }),
      el("dd", { text: "owner username and password, server-side session in an HttpOnly, SameSite=Strict cookie (Secure when served over HTTPS)" }),
      el("dt", { text: "Password recovery" }),
      el("dd", { text: "run python -m gateway --reset-owner-password on the gateway host; it reads the new password from stdin and revokes every session" })
    ];
    var keyRows = key.enabled
      ? [
          el("dt", { text: "Status" }), el("dd", { text: "enabled — every /v1 request must carry the key" }),
          el("dt", { text: "Key id" }), el("dd", { text: key.display_id || "unknown" }),
          el("dt", { text: "Set" }), el("dd", { text: when(key.created_at) }),
          el("dt", { text: "Rotated" }), el("dd", { text: key.rotated_at ? when(key.rotated_at) : "never" }),
          el("dt", { text: "Previous key" }),
          el("dd", { text: key.previous_key_valid_for_seconds
            ? "still accepted for " + key.previous_key_valid_for_seconds + " more seconds"
            : "no grace window open" })
        ]
      : [
          el("dt", { text: "Status" }),
          el("dd", { text: "disabled — keyless: every client works without an Authorization header" })
        ];
    var actions = key.enabled
      ? [
          field("client-key-value", "New key value (or press Generate)", "text"),
          el("div", { class: "row" }, [
            el("button", { type: "button", text: "Generate", onclick: function () { generateClientKey(); } }),
            el("button", { type: "button", text: "Rotate key", onclick: rotateClientKey })
          ]),
          el("button", { type: "button", class: "danger", text: "Disable and restore keyless access", onclick: clearClientKey })
        ]
      : [
          field("client-key-value", "Key value (32+ characters)", "text"),
          el("div", { class: "row" }, [
            el("button", { type: "button", text: "Generate", onclick: function () { generateClientKey(); } }),
            el("button", { type: "button", class: "primary", text: "Enable client key", onclick: enableClientKey })
          ])
        ];
    return el("section", {}, [
      el("div", { class: "card" }, [
        el("h2", { text: "Access" }),
        el("h3", { text: "Owner account" }),
        el("dl", { class: "kv" }, accountRows),
        el("div", { class: "row" }, [
          field("password-current", "Current password", "password"),
          field("password-next", "New password", "password"),
          field("password-confirm", "Repeat new password", "password")
        ]),
        el("button", { type: "button", text: "Change password", onclick: changePassword }),
        el("h3", { text: "Model client API key" }),
        el("p", { class: "small muted", text: "Owner sign-in and this key are separate boundaries: the key only gates the model API, never this console." }),
        el("dl", { class: "kv" }, keyRows),
        el("div", {}, actions),
        el("p", { class: "small muted", text: "Changing or clearing the key never edits an agent or client configuration — no client changes silently." })
      ])
    ]);
  }

  // --- machine keys (MCP / automation) ---------------------------------
  // The third credential: console-generated, shown once, scoped read or
  // write, revocable at once. The list carries display id, name, scope
  // and last-used only - the store keeps a hash, so there is no value
  // for any read path to hand back, and key metadata never travels in
  // the /owner/api/state snapshot either.

  var machineKeys = { phase: "idle", rows: [], error: null, reveal: null };

  function fetchMachineKeys() {
    machineKeys.phase = "loading";
    return api(BASE + "/api/machine-keys").then(function (payload) {
      machineKeys.phase = "ready";
      machineKeys.rows = (payload && payload.machine_keys) || [];
      machineKeys.error = null;
      if (tab === "settings") draw();
      return payload;
    }).catch(function (error) {
      machineKeys.phase = "error";
      machineKeys.error = error.message || "could not load machine keys";
      if (tab === "settings") draw();
      return null;
    });
  }

  function postMachineKey(body) {
    return api(BASE + "/api/machine-keys", { method: "POST", body: body });
  }

  function createMachineKey() {
    var name = inputValue("machine-key-name");
    var scope = inputValue("machine-key-scope") || "read";
    if (!name) {
      toast("give the key a name so you can recognise it later", "bad");
      return;
    }
    postMachineKey({ action: "create", name: name, scope: scope })
      .then(function (result) {
        clearInputs(["machine-key-name"]);
        // Shown exactly once: held only in this render, then gone.
        machineKeys.reveal = (result && result.machine_key) || null;
        return fetchMachineKeys();
      })
      .then(function () {
        // The one-time reveal is on screen now: focus its Copy button
        // instead of leaving focus on <body> at the top of the page.
        var copy = document.getElementById("machine-key-copy");
        if (copy && typeof copy.focus === "function") copy.focus();
      })      .catch(function (error) { toast(error.message || "could not create the key", "bad"); });
  }

  function rotateMachineKey(row) {
    var confirmed = global.confirm(
      "Rotate machine key '" + row.name + "' (" + row.display_id + ")?\n\n" +
      "The new value is shown once. The current value stops working immediately, so " +
      "update every automation that uses it before you close the dialog."
    );
    if (!confirmed) return;
    postMachineKey({ action: "rotate", display_id: row.display_id })
      .then(function (result) {
        machineKeys.reveal = (result && result.machine_key) || null;
        return fetchMachineKeys();
      })
      .then(function () {
        // The one-time reveal is on screen now: focus its Copy button
        // instead of leaving focus on <body> at the top of the page.
        var copy = document.getElementById("machine-key-copy");
        if (copy && typeof copy.focus === "function") copy.focus();
      })      .catch(function (error) { toast(error.message || "could not rotate the key", "bad"); });
  }

  function revokeMachineKey(row) {
    var confirmed = global.confirm(
      "Revoke machine key '" + row.name + "' (" + row.display_id + ")?\n\n" +
      "It stops working at once and cannot be restored. Create a new key if the " +
      "automation still needs one."
    );
    if (!confirmed) return;
    postMachineKey({ action: "revoke", display_id: row.display_id })
      .then(function () { return fetchMachineKeys(); })
      .catch(function (error) { toast(error.message || "could not revoke the key", "bad"); });
  }

  function copyMachineKeyReveal() {
    var node = global.document.getElementById("machine-key-reveal");
    var value = node ? String(node.value || "") : "";
    if (!value) return;
    if (global.navigator && global.navigator.clipboard && global.navigator.clipboard.writeText) {
      global.navigator.clipboard.writeText(value).then(function () {
        toast("key copied to the clipboard", "ok");
      }, function () {
        toast("could not copy — select the value and copy it by hand", "bad");
      });
      return;
    }
    if (node && typeof node.select === "function") node.select();
    toast("key selected — press Ctrl+C (or Cmd+C) to copy it", "ok");
  }

  function machineKeyRevealCard() {
    var reveal = machineKeys.reveal;
    if (!reveal) return null;
    return el("div", { class: "card warn", role: "alert" }, [
      el("h3", { text: "Copy this key now — it is shown exactly once" }),
      el("dl", { class: "kv" }, [
        el("dt", { text: "Name" }), el("dd", { text: String(reveal.name || "") }),
        el("dt", { text: "Key id" }), el("dd", { text: String(reveal.display_id || "") }),
        el("dt", { text: "Scope" }),
        el("dd", { text: reveal.scope === "write" ? "read + write" : "read only" })
      ]),
      el("input", {
        id: "machine-key-reveal",
        type: "text",
        readonly: true,
        autocomplete: "off",
        value: String(reveal.key || "")
      }),
      el("div", { class: "row" }, [
        el("button", { id: "machine-key-copy", type: "button", text: "Copy key", onclick: copyMachineKeyReveal }),
        el("button", {
          type: "button",
          text: "I have stored it",
          onclick: function () { machineKeys.reveal = null; draw(); }
        })
      ]),
      el("p", {
        class: "small muted",
        text: "The gateway keeps only a hash of this value: it cannot be shown again, " +
          "and it never appears in state, status, logs, errors or any URL."
      })
    ]);
  }

  function machineKeyTable() {
    if (machineKeys.phase === "error") {
      return el("p", {
        class: "small bad",
        text: "Machine keys could not be loaded: " + (machineKeys.error || "unknown error")
      });
    }
    if (machineKeys.phase !== "ready") {
      return el("p", { class: "small muted", text: "Loading machine keys…" });
    }
    var rows = machineKeys.rows || [];
    if (!rows.length) {
      return emptyState(
        "No machine keys yet.",
        "Create one to let an MCP client or another automation read or change gateway state with its own revocable credential."
      );
    }
    var table = el("table", {}, [
      el("thead", {}, [el("tr", {}, [
        el("th", { text: "Key id" }),
        el("th", { text: "Name" }),
        el("th", { text: "Scope" }),
        el("th", { text: "Last used" }),
        el("th", { text: "Actions" })
      ])])
    ]);
    var tbody = el("tbody");
    rows.forEach(function (row) {
      tbody.appendChild(el("tr", {}, [
        el("td", { class: "small", text: String(row.display_id || "") }),
        el("td", { class: "small", text: String(row.name || "") }),
        el("td", { class: "small", text: row.scope === "write" ? "read + write" : "read only" }),
        el("td", { class: "small", text: row.last_used_at ? when(row.last_used_at) : "never" }),
        el("td", {}, [
          el("button", {
            type: "button",
            text: "Rotate",
            onclick: function () { rotateMachineKey(row); }
          }),
          el("button", {
            type: "button",
            class: "danger",
            text: "Revoke",
            onclick: function () { revokeMachineKey(row); }
          })
        ])
      ]));
    });
    table.appendChild(tbody);
    return table;
  }

  function renderMachineKeys() {
    var scopeSelect = el("select", { id: "machine-key-scope" }, [
      el("option", { value: "read", text: "read — owner state only" }),
      el("option", { value: "write", text: "write — read plus configuration changes" })
    ]);
    var children = [
      el("h2", { text: "Machine keys (MCP / automation)" }),
      el("p", {
        class: "small muted",
        text: "A machine key is a third, separate credential: not your owner password or " +
          "session, and not the model client API key. Automation sends it as an " +
          "Authorization: Bearer header with no browser Origin. Read keys can only read " +
          "owner state; write keys can also change configuration. Each value is shown " +
          "once, at creation and at rotation, and can never be retrieved again."
      })
    ];
    var reveal = machineKeyRevealCard();
    if (reveal) children.push(reveal);
    children.push(machineKeyTable());
    children.push(el("h3", { text: "Create a machine key" }));
    children.push(el("div", { class: "row" }, [
      field("machine-key-name", "Name (for example: laptop-mcp)", "text"),
      el("label", {}, [el("span", { text: "Scope" }), scopeSelect])
    ]));
    children.push(el("button", {
      type: "button",
      class: "primary",
      text: "Create machine key",
      onclick: createMachineKey
    }));
    children.push(el("p", {
      class: "small muted",
      text: "Credential minting stays in this console: a machine key can never list, " +
        "create, rotate or revoke keys, and it can never enrol a provider credential."
    }));
    return el("section", {}, [el("div", { class: "card" }, children)]);
  }

  // --- sign-in and first-run setup --------------------------------------

  var authSession = {};

  // How long a rotated-out client key keeps working. Kept in step with
  // gateway.owner_auth.CLIENT_KEY_GRACE_SECONDS (600s); the server sends
  // the exact remaining window in every rotation response.
  var CLIENT_KEY_GRACE_LABEL = "10 minutes";

  function buildAuthView(session) {
    session = session || {};
    var setupRequired = !!session.setup_required;
    // Banner first (the console's honesty signature), then the sign-in card
    // on the same grid as the interior screens.
    var banner = el("aside", { class: "card", role: "note", text: setupRequired
      ? "First-run setup: the one-time setup code is printed by the gateway at startup and kept in the owner_setup_code file on the gateway host. It is single-use and never shown in this console."
      : "Owner sign-in for the console. The model API is a separate boundary: keyless by default, with an optional client API key you can set after signing in." });
    var fields = [];
    if (setupRequired) fields.push(field("setup-code", "One-time setup code", "text"));
    fields.push(field("auth-username", "Username", "text", session.username || ""));
    fields.push(field("auth-password", "Password", "password"));
    if (setupRequired) fields.push(field("auth-password2", "Repeat password", "password"));
    var children = [];
    // Stacked, label above, full width: two cramped columns at 420px was
    // the measured failure of the old row layout.
    children.push(el("div", { class: "auth-fields" }, fields));
    // One reserved alert line under the fields: the server's own reason
    // lands here without shoving the button around. Never a client guess.
    children.push(el("p", {
      id: "auth-error",
      class: "auth-error",
      role: "alert",
      text: session.error ? String(session.error) : ""
    }));
    children.push(el("button", {
      type: "button",
      class: "primary",
      text: setupRequired ? "Create owner account" : "Sign in",
      onclick: submitAuthView
    }));
    var card = [el("h2", { text: setupRequired ? "Set up the owner account" : "Owner sign-in" })].concat(children);
    return el("main", { class: "auth" }, [
      el("div", { class: "login-head" }, [
        el("h1", { class: "login-title", text: "Personal LLM Gateway" }),
        el("span", { class: "login-tag", text: "owner console" })
      ]),
      banner,
      el("div", { class: "card auth-card" + (session.error ? " has-error" : "") }, card)
    ]);
  }

  function renderAuthView(session) {
    authSession = session || {};
    if (!app) return;
    app.innerHTML = "";
    app.appendChild(buildAuthView(authSession));
    // A failed attempt re-renders the view: focus must land back in the
    // form, not on <body>.
    if (authSession.error) {
      var password = document.getElementById("auth-password");
      var target = password || document.getElementById("auth-username");
      if (target && typeof target.focus === "function") target.focus();
    }
  }

  function submitAuthView() {
    if (busy) return;
    var setupRequired = !!authSession.setup_required;
    var username = inputValue("auth-username");
    var password = inputValue("auth-password");
    var path;
    var body;
    if (setupRequired) {
      if (password !== inputValue("auth-password2")) {
        toast("passwords do not match", "bad");
        return;
      }
      path = BASE + "/api/auth/setup";
      body = { username: username, password: password, setup_code: inputValue("setup-code") };
    } else {
      path = BASE + "/api/auth/login";
      body = { username: username, password: password };
    }
    setBusy(true);
    api(path, { method: "POST", body: body })
      .then(function () {
        authSession = {};
        return refresh();
      })
      .catch(function (error) {
        // Show the server's own reason: throttled, wrong setup code, wrong
        // password - never a client-side guess. The typed username is kept
        // (it is not a secret and it is what the owner just typed); the
        // password fields are rebuilt empty, as before.
        var payload = error.payload || {};
        var detail = (payload.error && payload.error.message) || error.message || "sign-in failed";
        renderAuthView({ setup_required: setupRequired, error: detail, username: username });
      })
      .finally(function () { setBusy(false); });
  }

  // --- bootstrap --------------------------------------------------------

  function bootstrap() {
    app = document.getElementById("app");
    // Authentication first: the console decides between first-run setup,
    // sign-in and the normal shell from the server's own answer, never
    // from a client-side guess about whether an account exists.
    api(BASE + "/api/auth/session").then(function (session) {
      if (session && session.authenticated) return refresh();
      renderAuthView(session || {});
      return null;
    }).catch(function (error) {
      renderAuthView({ error: error.message || "cannot reach the gateway" });
      return null;
    });
    // The console is often left open. Refetch whenever the tab is shown
    // again, so the health cards never sit on a snapshot the owner cannot
    // The refetch is skipped while a mutation is in flight. Otherwise it
    // always updates the backing snapshot without redrawing the current tab:
    // a new-alias draft has no editingAlias, but still must survive refresh.
    function refetchIfIdle() {
      if (busy) return;
      refresh({ preserveDraft: true });
    }
    global.addEventListener("focus", refetchIfIdle);
    global.document.addEventListener("visibilitychange", function () {
      if (global.document.visibilityState === "visible") refetchIfIdle();
    });
    // Even without a fetch, a rendered snapshot must stop claiming Healthy
    // once it ages past the threshold; the ticker rewrites only the stale
    // fields in place (a full redraw would collapse open details).
    global.setInterval(function () {
      // Freshness is independent of editing and mutations: a snapshot that
      // ages out must never remain labelled Healthy behind an open draft.
      tickFreshness();
    }, 30000);
  }

  function tickFreshness() {
    var ageMs = dataAgeMs();
    var stale = refreshUnavailable || ageMs == null || ageMs > STALE_AFTER_MS;
    var line = freshnessText();
    var cards = global.document.querySelectorAll(".route-health-card");
    Array.prototype.forEach.call(cards, function (card) {
      var head = card.querySelector("h3[data-verdict]");
      var ageNode = card.querySelector(".health-freshness");
      if (!head || !ageNode) return;
      var base = head.getAttribute("data-verdict");
      var name = head.getAttribute("data-alias");
      head.textContent = name + " · " + (stale && base === "Healthy" ? "Stale" : base);
      ageNode.textContent = line;
    });
  }

  function refresh(options) {
    options = options || {};
    return api(BASE + "/api/state").then(function (full) {
      state = full;
      lastFetchAt = Date.now();
      refreshUnavailable = false;
      if (options.preserveDraft) {
        // A focus/visibility refresh updates the backing snapshot without
        // replacing the editor DOM (including its unsaved fields and preview)
        // or the user's open disclosure state. Refresh only the health view.
        if (tab === "status") {
          var existingHealth = app.querySelector(".routing-health");
          if (existingHealth) {
            var temporary = el("div");
            temporary.appendChild(renderStatus());
            var updatedHealth = temporary.querySelector(".routing-health");
            if (updatedHealth) existingHealth.replaceWith(updatedHealth);
          }
        }
        tickFreshness();
      } else {
        draw();
      }
      return full;
    }).catch(function (error) {
      var payload = error.payload || {};
      var errorType = payload.error && payload.error.type;
      if (errorType === "authentication_required") {
        // The session ended (expired or revoked): drop back to sign-in
        // instead of claiming the console API is unreachable.
        state = null;
        renderAuthView({
          setup_required: !!(payload.error && payload.error.setup_required),
          error: "Your owner session ended. Sign in again."
        });
        return null;
      }
      refreshUnavailable = true;
      if (options.preserveDraft && state) {
        // Keep the draft and preview intact, but fail closed on health.
        tickFreshness();
        toast("Routing health refresh unavailable: " + (error.message || "console API unavailable"), "bad");
        return null;
      }
      app.innerHTML = "";
      app.appendChild(el("main", {}, [
        el("div", { class: "card" }, [
          el("h2", { text: "Cannot reach the console API" }),
          el("p", { class: "small", text: error.message })
        ])
      ]));
      return null;
    });
  }

  // The DOM helper is exported so its behaviour can be tested without a
  // browser. In a browser the console boots normally. The status renderer is
  // exported too, so a test can hand it a fixture state and assert what the
  // owner would actually see.
  if (typeof module !== "undefined" && module.exports) {
    module.exports = {
      el: el,
      emptyState: emptyState,
      savePreviewLines: savePreviewLines,
      STALE_AFTER_MS: STALE_AFTER_MS,
      renderStatusForTest: function (view, day, fetchedAt) {
        var previous = state;
        var previousDay = usageDay;
        var previousFetchAt = lastFetchAt;
        var previousRefreshUnavailable = refreshUnavailable;
        state = view;
        if (day !== undefined) usageDay = day;
        refreshUnavailable = arguments.length > 3 ? !!arguments[3] : false;
        // A test render is a snapshot like any other: default to a fresh
        // fetch, or hand it an explicit fetch time to exercise staleness.
        lastFetchAt = fetchedAt === undefined ? Date.now() : fetchedAt;
        try {
          return renderStatus();
        } finally {
          state = previous;
          usageDay = previousDay;
          lastFetchAt = previousFetchAt;
          refreshUnavailable = previousRefreshUnavailable;
        }
      },
      renderSettingsForTest: function (view) {
        var previous = state;
        state = view;
        try {
          return renderSettings();
        } finally {
          state = previous;
        }
      },
      renderMachineKeysForTest: function (rows, view, reveal) {
        // The machine-key card against a fixture list: the fetch phase is
        // forced to "ready" (or handed a reveal payload) so Node never
        // touches the network, exactly like the leaderboard seam.
        var previousState = state;
        var previousKeys = machineKeys;
        state = view === undefined ? previousState : view;
        machineKeys = {
          phase: "ready",
          rows: rows || [],
          error: null,
          reveal: reveal || null
        };
        try {
          return renderMachineKeys();
        } finally {
          state = previousState;
          machineKeys = previousKeys;
        }
      },
      machineKeysStateForTest: function () {
        // The module's machine-key state after a driven click: phase,
        // loaded rows, and the one-time reveal (if any).
        return {
          phase: machineKeys.phase,
          rows: machineKeys.rows,
          reveal: machineKeys.reveal,
          error: machineKeys.error
        };
      },
      renderCooldownTableForTest: function (view) {
        // The operator's cooldown view with its clear controls, rendered
        // against a fixture state so a test can drive the buttons.
        var previous = state;
        state = view;
        try {
          return cooldownTable();
        } finally {
          state = previous;
        }
      },
      renderModelsForTest: function (view) {
        var previous = state;
        state = view;
        try {
          return renderModels();
        } finally {
          state = previous;
        }
      },
      withStateForTest: function (view, body) {
        // The capability row's click handler renders its panel at click time
        // (reading state.can_mutate there), so a behavioural click-through
        // test needs the fixture state live while it drives the clicks.
        var previous = state;
        state = view;
        try {
          return body();
        } finally {
          state = previous;
        }
      },
      renderCapabilityPanelForTest: function (provider, model, panel, view) {
        // A test hands the panel a fixture state; the panel's evidence read
        // goes through the same api() seam the test stubs.
        var previous = state;
        state = view;
        try {
          return renderCapabilityPanel(provider, model, panel);
        } finally {
          state = previous;
        }
      },
      probePriceTextForTest: function (price) {
        // The preview price line, exercised directly on real payloads so a
        // priced model can never render as "no published price".
        return probePriceText(price);
      },
      renderProviderForTest: function (provider, view) {
        // One provider card, so a test can assert what an owner is told
        // about a local endpoint without a whole console state.
        var previous = state;
        if (view !== undefined) state = view;
        try {
          return renderProvider(provider);
        } finally {
          state = previous;
        }
      },
      renderLeaderboardForTest: function (payload, view) {
        // A test hands the tab a payload directly; the phase is forced to
        // "ready" so rendering never triggers a fetch in Node.
        var previousState = state;
        var previousPayload = leaderboardPayload;
        var previousPhase = leaderboardPhase;
        state = view === undefined ? previousState : view;
        leaderboardPayload = payload;
        leaderboardPhase = "ready";
        try {
          return renderLeaderboard();
        } finally {
          state = previousState;
          leaderboardPayload = previousPayload;
          leaderboardPhase = previousPhase;
        }
      }
    };
    return;
  }

  if (global.document && global.fetch) {
    bootstrap();
  }
})(typeof window !== "undefined" ? window : globalThis);
