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
          var err = new Error(message);
          err.payload = data;
          throw err;
        }
        return data;
      });
    });
  }

  function toast(message, kind) {
    var host = document.getElementById("toast");
    if (!host) {
      host = el("div", { id: "toast" });
      document.body.appendChild(host);
    }
    var node = el("div", { class: "msg " + (kind || "") }, [message]);
    host.appendChild(node);
    setTimeout(function () { node.remove(); }, kind === "bad" ? 9000 : 5000);
  }

  function run(promise, successMessage) {
    if (busy) return Promise.resolve();
    busy = true;
    return promise
      .then(function (result) {
        if (successMessage) toast(successMessage, "ok");
        return result;
      })
      .catch(function (error) {
        toast(error.message || "something went wrong", "bad");
        throw error;
      })
      .finally(function () { busy = false; });
  }

  // --- formatting -------------------------------------------------------

  function when(value) {
    if (!value) return "Unknown";
    try {
      return new Date(value).toLocaleString();
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
    return providerId;
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

  function emptyState(title, message) {
    return el("div", { class: "empty-state", role: "status", "aria-live": "polite" }, [
      el("strong", { text: title }),
      el("p", { class: "small", text: message })
    ]);
  }

  // --- shell ------------------------------------------------------------

  var TABS = [
    { id: "status", label: "Status" },
    { id: "providers", label: "Providers" },
    { id: "models", label: "Models & Routes" },
    { id: "profiles", label: "Profiles" },
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
        onclick: function () { tab = item.id; draw(); }
      }));
    });
    header.appendChild(nav);
    header.appendChild(el("span", { class: "spacer" }));
    if (state) {
      header.appendChild(el("span", { class: "pill warn", text: "trusted LAN admin mode" }));
      header.appendChild(el("span", { class: "small muted", text: "rev " + state.revision }));
    }
    app.appendChild(header);
    app.appendChild(el("aside", { class: "card", role: "note", text: "Trusted LAN admin mode — any device that can reach this address can configure and use this gateway." }));

    var main = el("main", { id: "main" });
    app.appendChild(main);
  }

  function draw() {
    renderShell();
    var main = document.getElementById("main");
    if (!state) {
      main.appendChild(el("p", { class: "muted", text: "Loading…" }));
      return;
    }

    var renderer = {
      status: renderStatus,
      providers: renderProviders,
      models: renderModels,
      profiles: renderProfiles,
      settings: renderSettings,
      backup: renderBackup
    }[tab] || renderStatus;
    main.appendChild(renderer());
  }


  // --- status -----------------------------------------------------------

  function renderStatus() {
    var frag = document.createDocumentFragment();

    if (!state.initialized) {
      frag.appendChild(el("section", {}, [
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
      frag.appendChild(el("section", {}, [
        el("div", { class: "card" }, [el("h2", { text: "Gateway status" }), renderChecklist()])
      ]));
    }

    var release = state.release || {};
    frag.appendChild(el("section", {}, [
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

    frag.appendChild(el("section", {}, [
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
    frag.appendChild(el("section", {}, [providers]));

    var totals = state.totals || {};
    var ledger = state.ledger || [];
    var ledgerCard = el("div", { class: "card" }, [
      el("h2", { text: "Recent request outcomes" }),
      el("p", { class: "small muted", text: "Metadata only. Prompts, responses, tokens and secrets are never stored." }),
      el("p", { class: "small muted", text: (state.ledger_durability && state.ledger_durability.enabled ? "Recent outcomes are retained on the state volume for up to 30 days, with bounded rotation; the in-memory ring is the hot view. Persistence is asynchronous and may lag briefly. This metadata ledger is not a billing system." : "Durable ledger storage is not enabled; only the bounded in-memory ring is available and is lost when the gateway restarts. This metadata ledger is not a billing system.") }),
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
    frag.appendChild(el("section", {}, [ledgerCard]));

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
      costsCard.appendChild(el("p", { class: "small muted", text: "UTC day " + usageDay + ", from the durable daily rollup (30-day retention, rebuilt from the request ledger on every start). Prices are applied at read time from the published catalogue; " + (analytics.coverage && analytics.coverage.incomplete ? "the rollup's coverage is incomplete, so this day may be partial." : "every retained row for the day is included.") + " Estimates are not provider bills." }));
    } else {
      costsCard.appendChild(el("p", { class: "small muted", text: "Last " + costs.window_hours + " hours. Based on reported token counts and published prices only; estimates are not provider bills." }));
      costsCard.appendChild(el("p", { class: "small muted", text: (costs.coverage && costs.coverage.incomplete ? "The bounded recent-event ring may omit older events from this window; affected cost estimates are Unknown." : "Aggregates cover the available recent-event ring for the last " + costs.window_hours + " hours.") + " " + (state.ledger_durability && state.ledger_durability.enabled ? "The metadata ledger is persisted asynchronously with bounded rotation and a 30-day retention window." : "Durable ledger storage is not enabled; the in-memory ring is lost when the gateway restarts.") + " This is not a billing system." }));
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
    frag.appendChild(el("section", {}, [costsCard]));
    frag.appendChild(el("section", {}, [renderFallbackCard(analytics)]));

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
    frag.appendChild(el("section", {}, [limitsCard]));

    return frag;
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
    if (row.amount_usd == null) {
      parts.push(el("div", { text: "Unknown" }));
      if (row.unknown_cost_requests) {
        parts.push(el("div", {
          class: "small muted",
          text: "0 of " + row.requests + " requests priced; " +
            row.unknown_cost_requests + " unknown (" +
            tokens(row.unknown_cost_input_tokens) + " in / " +
            tokens(row.unknown_cost_output_tokens) + " out)"
        }));
      }
    } else {
      parts.push(el("div", { text: "$" + Number(row.amount_usd).toFixed(6) }));
      if (row.cost_incomplete && row.unknown_cost_requests) {
        parts.push(el("div", {
          class: "small muted",
          text: "known subtotal; " + row.unknown_cost_requests + " of " + row.requests +
            " requests unpriced (" + tokens(row.unknown_cost_input_tokens) + " in / " +
            tokens(row.unknown_cost_output_tokens) + " out tokens)"
        }));
      } else if (row.cost_incomplete) {
        parts.push(el("div", { class: "small muted", text: "known subtotal; coverage incomplete" }));
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
    if (event.fallback_used && event.fallback_reason) {
      parts.push("fallback: " + event.fallback_reason);
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
    return "a confirmed provider exhaustion holds for at most " + policy.exhausted_max_seconds +
      "s (floor " + policy.exhausted_base_seconds + "s); when the window ends exactly one recovery" +
      " probe is let through, and a concurrent request falls back until it answers";
  }

  // The cooldown table is the operator's view of a held provider: when the
  // exhaustion was seen, how long the hold runs, when the next automatic
  // probe may run, and whether one is in flight right now.
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
        el("th", { text: "Why" })
      ])])
    ]);
    var tbody = el("tbody");
    keys.forEach(function (key) {
      var entry = details[key] || {};
      tbody.appendChild(el("tr", {}, [
        el("td", { class: "small", text: key }),
        el("td", { class: "small", text: entry.scope === "provider" ? "whole provider" : "this route only" }),
        el("td", { class: "small", text: entry.error_class || "unknown" }),
        el("td", { class: "small", text: String(entry.failures || 0) }),
        el("td", { class: "small", text: when(entry.opened_at ? new Date(entry.opened_at * 1000).toISOString() : null) }),
        el("td", { class: "small", text: (entry.remaining_seconds || 0) + "s" }),
        el("td", { class: "small", text: entry.next_probe_at ? when(new Date(entry.next_probe_at * 1000).toISOString()) : "Unknown" }),
        el("td", { class: "small", text: (entry.state || "unknown") + (entry.probe_in_flight ? " · probe in flight" : "") }),
        el("td", { class: "small muted", text: entry.reason || "" })
      ]));
    });
    table.appendChild(tbody);
    card.appendChild(table);
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
            type: "button", text: "Test connection",
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
            type: "button", text: "Refresh model catalog",
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
            type: "button", text: "Clear cooldowns",
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

    if (provider.provider_id === "chatgpt_codex") {
      card.appendChild(renderDeviceCode(provider));
    } else {
      card.appendChild(renderKeyForm(provider));
      if (provider.provider_id === "opencode_console") {
        card.appendChild(el("p", { class: "small muted", text: "Jev evaluates the state and questions sent to the native /v1/systemone endpoint. It is a structured-decision provider, not a chat-completion model." }));
      }
    }
    return card;
  }

  function renderKeyForm(provider) {
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
      el("h3", { text: "Connect OpenCode Go" }),
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

  function renderModels() {
    var frag = document.createDocumentFragment();

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

  function renderAliasEditor() {
    var card = el("div", { class: "card" }, [el("h2", { text: "Aliases and routes" })]);
    var editing = editingAlias && state.aliases.filter(function (alias) { return alias.name === editingAlias; })[0];
    if (editingAlias && !editing) editingAlias = null;

    var nameInput = el("input", { placeholder: "coding", value: editing ? editing.name : "", disabled: !!editing });
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
      el("label", {}, [el("span", { text: "Primary provider" }), primaryProvider]),
      el("label", {}, [el("span", { text: "Primary model" }), primaryModel])
    ]));
    card.appendChild(el("h3", { text: "Fallback routes (in order)" }));
    card.appendChild(el("div", { class: "small muted" }, [
      el("span", { text: "Cross-provider fallback is disabled by default. It sends a prompt to a different provider, and therefore to a different data-retention regime." })
    ]));
    card.appendChild(fallbackList);
    card.appendChild(el("button", { type: "button", text: "Add fallback route", onclick: addFallbackRow }));
    card.appendChild(el("h3", { text: "When may a fallback run?" }));
    card.appendChild(triggersWrap);
    card.appendChild(el("h3", { text: "Cross-provider fallback authorisation" }));
    card.appendChild(el("label", { style: "flex:1 1 auto" }, [
      el("span", { text: "I authorise requests assigned to this alias to be sent to the providers named below" }), authorizeBox
    ]));
    card.appendChild(confirmProviders);
    card.appendChild(el("div", { class: "row" }, [
      el("label", {}, [el("span", { text: "Local cooldown (seconds)" }), cooldown]),
      el("div", { class: "narrow" }, [
        el("button", {
          class: "primary", type: "button", text: editing ? "Save changes" : "Save alias",
          disabled: !state.can_mutate,
          onclick: function () {
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
            run(api(BASE + "/api/alias", {
              method: "POST",
              body: {
                action: "save",
                name: nameInput.value,
                primary: { provider: primaryProvider.value, model: primaryModel.value },
                fallbacks: fallbacks.map(function (row) {
                  return { provider: row.provider.value, model: row.model.value };
                }),
                fallback_triggers: triggers,
                fallback_authorized: authorizeBox.checked,
                fallback_confirmed_providers: confirmed,
                cooldown_seconds: parseInt(cooldown.value, 10) || 0,
                enabled: editing ? editing.enabled : false,
                phase_routing: phaseRouting
              }
            })).then(function () {
              editingAlias = null;
              toast(editing ? "Alias updated." : "Alias saved.", "ok");
              return refresh();
            }).catch(function () {});
          }
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
          text: "Primary: " + (alias.primary ? providerLabel(alias.primary.provider) + " → " + alias.primary.model : "none")
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
            " · cooldown: " + alias.cooldown_seconds + "s"
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
      el("strong", { text: "Profiles do not provide access control in trusted LAN admin mode. " }),
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
        importInput.files[0].text().then(function (text) { return JSON.parse(text); }).then(function (payload) {
          return api(BASE + "/api/backup/import", { method: "POST", body: payload });
        }).then(function () { return refresh(); }).then(function () { toast("Configuration imported", "ok"); draw(); })
          .catch(function (error) { toast(error.message || "Invalid backup", "bad"); });
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
    frag.appendChild(el("section", {}, [
      el("div", { class: "card" }, [
        el("h2", { text: "Trusted LAN access" }),
        el("p", { class: "small", text: "Any device that can reach this address can configure and use the gateway. This product mode has no password, login, or per-client authorization." })
      ])
    ]));

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

  // --- bootstrap --------------------------------------------------------

  function bootstrap() {
    app = document.getElementById("app");
    refresh();
  }

  function refresh() {
    return api(BASE + "/api/state").then(function (full) {
      state = full;
      draw();
      return full;
    }).catch(function (error) {
      app.innerHTML = "";
      app.appendChild(el("main", {}, [
        el("div", { class: "card" }, [
          el("h2", { text: "Cannot reach the console API" }),
          el("p", { class: "small", text: error.message })
        ])
      ]));
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
      renderStatusForTest: function (view, day) {
        var previous = state;
        var previousDay = usageDay;
        state = view;
        if (day !== undefined) usageDay = day;
        try {
          return renderStatus();
        } finally {
          state = previous;
          usageDay = previousDay;
        }
      }
    };
    return;
  }

  if (global.document && global.fetch) {
    bootstrap();
  }
})(typeof window !== "undefined" ? window : globalThis);
