/* Personal LLM Gateway console.
 *
 * One page, no framework, no build step. It talks to the same origin, so
 * every request carries the CSRF token the server handed out in a cookie.
 *
 * Rules the UI keeps:
 *   - a provider is always labelled next to a model id;
 *   - unknown limits are shown as Unknown, with a source and an as-of time;
 *   - a caller token is shown once, as the direct result of issuing it;
 *   - a saved credential is never asked for again and never displayed.
 */
(function () {
  "use strict";

  var BASE = window.GATEWAY_BASE || "";
  var app = document.getElementById("app");
  var state = null;
  var tab = "status";
  var busy = false;

  // --- tiny helpers -----------------------------------------------------

  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (key) {
        if (key === "class") node.className = attrs[key];
        else if (key === "text") node.textContent = attrs[key];
        else if (key === "html") node.innerHTML = attrs[key];
        else if (key.slice(0, 2) === "on") node.addEventListener(key.slice(2), attrs[key]);
        else if (attrs[key] !== null && attrs[key] !== undefined) node.setAttribute(key, attrs[key]);
      });
    }
    (children || []).forEach(function (child) {
      if (child === null || child === undefined) return;
      node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
    });
    return node;
  }

  function cookie(name) {
    var parts = document.cookie.split(";");
    for (var i = 0; i < parts.length; i++) {
      var pair = parts[i].split("=");
      if (pair[0].trim() === name) return decodeURIComponent(pair.slice(1).join("="));
    }
    return null;
  }

  function api(path, options) {
    options = options || {};
    var headers = options.headers || {};
    if (options.body !== undefined) headers["Content-Type"] = "application/json";
    var csrf = cookie("gw_csrf");
    if (csrf) headers["X-Gateway-CSRF"] = csrf;
    return fetch(path, {
      method: options.method || "GET",
      headers: headers,
      credentials: "same-origin",
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
          var message = (data && data.error && data.error.message) || ("request failed: " + response.status);
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
      el("div", { class: "small muted", text: "source: " + source })
    ]);
  }

  function providerLabel(providerId) {
    if (providerId === "chatgpt_codex") return "ChatGPT / Codex";
    if (providerId === "opencode_go") return "OpenCode Go";
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

  // --- shell ------------------------------------------------------------

  var TABS = [
    { id: "status", label: "Status" },
    { id: "providers", label: "Providers" },
    { id: "models", label: "Models & Routes" },
    { id: "profiles", label: "Profiles" },
    { id: "settings", label: "Settings" }
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
      header.appendChild(el("span", {
        class: "pill " + (state.access_mode === "authenticated" ? "ok" : "warn"),
        text: state.access_mode === "authenticated" ? "authenticated mode" : "LAN no-auth mode"
      }));
      header.appendChild(el("span", { class: "small muted", text: "rev " + state.revision }));
    }
    app.appendChild(header);

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
    if (!state.authenticated && state.passphrase_set === false) {
      main.appendChild(renderSetupPassphrase());
      return;
    }
    if (!state.authenticated) {
      main.appendChild(renderLogin());
      return;
    }
    var renderer = {
      status: renderStatus,
      providers: renderProviders,
      models: renderModels,
      profiles: renderProfiles,
      settings: renderSettings
    }[tab] || renderStatus;
    main.appendChild(renderer());
  }

  // --- login ------------------------------------------------------------

  function renderLogin() {
    var input = el("input", { type: "password", id: "passphrase", autocomplete: "current-password" });
    var card = el("div", { class: "login card" }, [
      el("h2", { text: "Sign in" }),
      el("p", { class: "small muted", text: state.access_mode_description || "" }),
      el("label", {}, [el("span", { text: "Owner passphrase" }), input]),
      el("button", {
        class: "primary",
        type: "button",
        text: "Sign in",
        onclick: function () {
          run(api(BASE + "/api/session", { method: "POST", body: { passphrase: input.value } }))
            .then(function () { return refresh(); })
            .catch(function () {});
        }
      })
    ]);
    setTimeout(function () { input.focus(); }, 0);
    return card;
  }

  function renderSetupPassphrase() {
    var input = el("input", { type: "password", id: "passphrase", autocomplete: "new-password" });
    var confirm = el("input", { type: "password", autocomplete: "new-password" });
    return el("div", { class: "login card" }, [
      el("h2", { text: "Initialize gateway configuration" }),
      el("p", {
        class: "small muted",
        text: "Choose an owner passphrase. It protects this console and the setup UI. It is stored as a salted hash and is never shown again."
      }),
      el("label", {}, [el("span", { text: "Passphrase" }), input]),
      el("label", {}, [el("span", { text: "Confirm passphrase" }), confirm]),
      el("button", {
        class: "primary",
        type: "button",
        text: "Initialize gateway configuration",
        onclick: function () {
          if (!input.value || input.value !== confirm.value) {
            toast("the two passphrases do not match", "bad");
            return;
          }
          run(api(BASE + "/api/session", { method: "POST", body: { passphrase: input.value } }))
            .then(function () { return refresh(); })
            .catch(function () {});
        }
      })
    ]);
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
          el("th", { text: "Served by" }), el("th", { text: "Outcome" })
        ])])
      ]);
      var tbody = el("tbody");
      ledger.forEach(function (event) {
        tbody.appendChild(el("tr", {}, [
          el("td", { class: "small", text: when(event.timestamp) }),
          el("td", { text: event.requested_alias }),
          el("td", { class: "small", text: event.serving_provider ? providerLabel(event.serving_provider) + " → " + event.serving_model : "—" }),
          el("td", { class: "small", text: event.error_class ? event.error_class : ("status " + (event.status || "")) + (event.fallback_used ? " (fallback)" : "") })
        ]));
      });
      table.appendChild(tbody);
      ledgerCard.appendChild(table);
    }
    frag.appendChild(el("section", {}, [ledgerCard]));

    var limitsCard = el("div", { class: "card" }, [el("h2", { text: "Usage and limits" })]);
    if (!state.limits || !state.limits.length) {
      limitsCard.appendChild(el("p", {
        class: "small muted",
        text: "Unknown. No limit snapshot has been observed yet. Nothing is inferred."
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

    if (!state.can_enroll_credentials) {
      frag.appendChild(el("div", { class: "notice warn" }, [
        el("strong", { text: "Credential enrollment is not available on this surface. " }),
        el("span", { text: "Open the setup UI on the host itself (http://127.0.0.1:<setup port>/setup/) or through an SSH local tunnel." })
      ]));
    }

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
          disabled: !state.can_enroll_credentials,
          onclick: function () {
            run(api(BASE + "/api/provider", { method: "POST", body: { action: "enroll_key", provider_id: provider.provider_id, api_key: input.value } }))
              .then(function () {
                input.value = "";
                toast("Key stored. It will not be shown again.", "ok");
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
    if (!state.can_enroll_credentials) {
      block.appendChild(el("div", { class: "notice" }, [
        el("span", { text: "Credential entry is only available on the setup UI, which is reachable from the host itself or through an SSH local tunnel." })
      ]));
    }
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
        el("button", {
          type: "button", text: "I have entered the code — check status",
          onclick: function () {
            run(api(BASE + "/api/provider", { method: "POST", body: { action: "device_poll", provider_id: provider.provider_id, device_auth_id: pending.device_auth_id } }))
              .then(function (result) {
                pending.state = result.state;
                pendingDevice[provider.provider_id] = pending;
                if (result.state === "approved") toast("Connected. The credential is stored and will not be shown again.", "ok");
                else if (result.state === "denied") toast(result.error || "The authorisation was denied.", "bad");
                else if (result.state === "expired") toast(result.error || "The code expired.", "bad");
                return refresh();
              })
              .catch(function () {});
          }
        })
      ]));
    }
    return block;
  }

  // --- models and routes ------------------------------------------------

  function renderModels() {
    var frag = document.createDocumentFragment();

    var modelsCard = el("div", { class: "card" }, [
      el("h2", { text: "Provider models" }),
      el("p", { class: "small muted", text: "Discovered models are staged. A model appears in /v1/models only after it is enabled and published through an alias." })
    ]);
    state.providers.forEach(function (provider) {
      if (!provider.models.length) return;
      modelsCard.appendChild(el("h3", { text: providerLabel(provider.provider_id) }));
      var table = el("table", {}, [
        el("thead", {}, [el("tr", {}, [
          el("th", { text: "Model" }), el("th", { text: "Dialect" }), el("th", { text: "Limit" }),
          el("th", { text: "Retention" }), el("th", { text: "" })
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
      modelsCard.appendChild(table);
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

    var nameInput = el("input", { placeholder: "coding" });
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
    refreshModelSelects();

    function addFallbackRow() {
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
      fillModels();
      var row = el("div", { class: "row" }, [
        el("label", {}, [el("span", { text: "Provider" }), provider]),
        el("label", {}, [el("span", { text: "Model" }), model]),
        el("div", { class: "narrow" }, [
          el("button", { type: "button", text: "Remove", onclick: function () { row.remove(); } })
        ])
      ]);
      fallbacks.push({ provider: provider, model: model });
      fallbackList.appendChild(row);
    }

    var triggerBoxes = {};
    var triggersWrap = el("div", { class: "row" });
    (state.fallback_triggers || []).forEach(function (trigger) {
      var box = el("input", { type: "checkbox", value: trigger });
      box.checked = false;
      triggerBoxes[trigger] = box;
      triggersWrap.appendChild(el("label", { style: "flex:0 0 auto" }, [
        el("span", { text: trigger.replace(/_/g, " ") }), box
      ]));
    });

    var authorizeBox = el("input", { type: "checkbox" });
    var confirmProviders = el("div");

    function renderConfirmProviders() {
      confirmProviders.innerHTML = "";
      state.providers.forEach(function (provider) {
        var box = el("input", { type: "checkbox", value: provider.provider_id });
        confirmProviders.appendChild(el("label", { style: "flex:0 0 auto" }, [
          el("span", { text: "may receive requests: " + providerLabel(provider.provider_id) }), box
        ]));
      });
    }
    renderConfirmProviders();

    var cooldown = el("input", { type: "number", value: "300", min: "0" });

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
          class: "primary", type: "button", text: "Save alias",
          disabled: !state.can_mutate,
          onclick: function () {
            var triggers = Object.keys(triggerBoxes).filter(function (key) { return triggerBoxes[key].checked; });
            var confirmed = Array.prototype.slice.call(confirmProviders.querySelectorAll("input:checked"))
              .map(function (box) { return box.value; });
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
                cooldown_seconds: parseInt(cooldown.value, 10) || 0
              }
            })).then(function () { toast("Alias saved.", "ok"); return refresh(); }).catch(function () {});
          }
        })
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
          text: "Fallbacks: " + (alias.fallbacks.length
            ? alias.fallbacks.map(function (f) { return providerLabel(f.provider) + " → " + f.model; }).join(", ")
            : "none") +
            " · triggers: " + (alias.fallback_triggers.join(", ") || "none") +
            " · cooldown: " + alias.cooldown_seconds + "s"
        }),
        el("div", { class: "actions" }, [
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
    card.appendChild(existing);
    return card;
  }

  // --- profiles ---------------------------------------------------------

  function renderProfiles() {
    var frag = document.createDocumentFragment();

    frag.appendChild(el("div", { class: "notice" }, [
      el("span", { text: state.access_mode_description })
    ]));

    if (state.access_mode !== "authenticated") {
      frag.appendChild(el("div", { class: "notice warn" }, [
        el("strong", { text: "Caller tokens are only available in authenticated mode. " }),
        el("span", { text: "Switch the access mode in Settings, or use the Anonymous LAN profile." })
      ]));
    }

    var card = el("div", { class: "card" }, [el("h2", { text: "Client profiles" })]);
    state.profiles.forEach(function (profile) {
      var aliasesBox = el("input", { value: (profile.allowed_aliases || []).join(", ") });
      var nameInput = el("input", { value: profile.name });
      var tokenBox = el("div");
      card.appendChild(el("div", { class: "route" }, [
        el("div", { class: "row" }, [
          el("label", {}, [el("span", { text: "Name" }), nameInput]),
          el("label", {}, [el("span", { text: "Allowed aliases (comma separated, empty means all)" }), aliasesBox]),
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
                    allowed_aliases: aliasesBox.value.split(",").map(function (s) { return s.trim(); }).filter(Boolean)
                  }
                })).then(function () { return refresh(); }).catch(function () {});
              }
            })
          ]),
          el("div", { class: "narrow" }, [
            el("button", {
              type: "button", text: profile.has_token ? "Rotate token" : "Issue token",
              disabled: !state.can_mutate || state.access_mode !== "authenticated" || profile.anonymous,
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
              disabled: !state.can_mutate || !profile.has_token,
              onclick: function () {
                run(api(BASE + "/api/profile", { method: "POST", body: { action: "revoke_token", profile_id: profile.profile_id } }))
                  .then(function () { return refresh(); }).catch(function () {});
              }
            })
          ])
        ]),
        el("div", { class: "small muted", text: profile.profile_id + " · " + (profile.anonymous ? "anonymous LAN profile" : profile.has_token ? "caller token issued " + when(profile.token_created_at) : "no caller token") }),
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

  function renderSettings() {
    var frag = document.createDocumentFragment();

    var release = state.release || {};
    frag.appendChild(el("section", {}, [
      el("div", { class: "card" }, [
        el("h2", { text: "Access mode" }),
        el("p", { class: "small", text: state.access_mode_description || "" }),
        el("div", { class: "row" }, [
          el("div", { class: "narrow" }, [
            el("button", {
              type: "button", text: "LAN no-auth mode", disabled: !state.can_mutate,
              onclick: function () {
                run(api(BASE + "/api/settings", { method: "POST", body: { action: "access_mode", access_mode: "lan_noauth" } }))
                  .then(function () { return refresh(); }).catch(function () {});
              }
            })
          ]),
          el("div", { class: "narrow" }, [
            el("button", {
              type: "button", text: "Authenticated mode", disabled: !state.can_mutate,
              onclick: function () {
                run(api(BASE + "/api/settings", { method: "POST", body: { action: "access_mode", access_mode: "authenticated" } }))
                  .then(function () { return refresh(); }).catch(function () {});
              }
            })
          ])
        ])
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
          el("dt", { text: "Active listener" }), el("dd", { text: state.surface === "setup" ? "Setup UI (loopback only)" : "Owner console (LAN)" })
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
          el("dt", { text: "Local cooldowns" }), el("dd", { text: JSON.stringify(state.cooldowns || {}) })
        ]),
        el("button", {
          type: "button", text: "Sign out",
          onclick: function () {
            run(api(BASE + "/api/session", { method: "DELETE" })).then(function () {
              state = null;
              draw();
            }).catch(function () {});
          }
        })
      ])
    ]));

    return frag;
  }

  // --- bootstrap --------------------------------------------------------

  function refresh() {
    return api(BASE + "/api/state").then(function (data) {
      state = data;
      draw();
      return data;
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

  api(BASE + "/api/session").then(function (session) {
    return api(BASE + "/api/state").then(function (data) {
      state = data;
      state.passphrase_set = session.passphrase_set;
      state.authenticated = session.authenticated;
      draw();
    });
  }).catch(function () {
    app.innerHTML = "";
    app.appendChild(el("main", {}, [
      el("div", { class: "card" }, [
        el("h2", { text: "Cannot reach the console API" }),
        el("p", { class: "small", text: "The gateway did not answer on this origin." })
      ])
    ]));
  });
})();
