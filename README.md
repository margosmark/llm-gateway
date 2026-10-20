# Personal LLM Gateway

A personal, LAN-only, OpenAI-compatible LLM gateway. It lets one owner manage
their LLM access through a browser — no editing files, no Docker commands, no
environment variables, no JSON.

Two providers are in scope:

| Provider | Credential | Wire dialect |
|---|---|---|
| **ChatGPT / Codex** | user-operated device-code authorisation (beta) | Responses API |
| **OpenCode Go** | API key | Responses, Chat Completions, Anthropic Messages, or Google v1beta — **per model** |

---

## The idea

Your LAN clients talk to one base URL and choose a friendly alias:

```bash
curl http://10.10.11.109:8765/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model": "coding", "messages": [{"role": "user", "content": "hello"}]}'
```

`coding` is an alias you defined in the console. It resolves to a primary
route and, if you explicitly allow it, to ordered fallbacks. Provider churn
never breaks your client's configuration.

---

## Surfaces

| Surface | Address | Who can reach it | What it does |
|---|---|---|---|
| Owner console | `http://10.10.11.109:8765/owner/` | LAN | Safe status without a session. Read-only in LAN no-auth mode; configurable with the owner passphrase in authenticated mode |
| Setup UI | `http://10.10.11.109:8766/setup/` | LAN (or loopback — see below) | Everything: credential enrolment, provider tests, catalogue refresh, and all configuration |
| API | `http://10.10.11.109:8765/v1/` | LAN | `GET /v1/models`, `POST /v1/chat/completions`, `POST /v1/responses` |
| Release identity | `http://10.10.11.109:8765/version` | LAN, anonymous | Fixed four-key metadata |

The setup UI is a **separate listener on its own port**. Where it binds is
your choice:

```sh
# reachable from the network (the default)
GATEWAY_SETUP_BIND=10.10.11.109

# loopback only - the host itself, or an SSH tunnel
GATEWAY_SETUP_BIND=127.0.0.1
```

Either way the **owner passphrase is required** before anything can be
enrolled, and the CSRF, `Host` and `Origin` checks apply to it exactly as
they do to the console. What changes is only who can *reach* the page — if
you expose it to the network, the network is your perimeter and the
passphrase is the only thing standing between a LAN host and your provider
credentials. Tighten it at the firewall if that matters to you.

The gateway binds an **explicit private address**. A wildcard bind is refused
at startup, and there is no flag that enables one. It never creates a
firewall or router rule, and it is never publicly exposed.

### Where the configuration happens

The setup UI and the owner console are the **same application** on two
listeners. The setup UI has its own port, so it can be bound separately
from the console.

* In **LAN no-auth mode** the owner console is read-only, even with the
  passphrase — otherwise any host on the network could reconfigure the
  gateway. All configuration happens in the setup UI.
* In **authenticated mode** the owner console is configurable once you sign
  in, but credential enrolment is still setup-only.

Either way, **a credential can only ever be entered on the setup UI.** The
owner console shows that a credential is stored, never its value.

---

## Quick start

```sh
git clone https://github.com/margosmark/llm-gateway.git
cd llm-gateway

export GATEWAY_BIND=10.10.11.109          # your explicit private LAN address
export SOURCE_REVISION="$(git rev-parse HEAD)"
export SOURCE_TREE="$(git rev-parse HEAD^{tree})"

docker compose up -d --build
```

Then, on the host:

1. Open <http://10.10.11.109:8766/setup/> and create the owner passphrase.
   (Use `http://127.0.0.1:8766/setup/` if you bound the setup listener to
   loopback.)
2. Connect a provider.
3. Test the connection.
4. Refresh the model catalogue.
5. Enable the models you want to expose.
6. Create an alias, pick a primary, publish it.
7. Point a LAN client at the alias.

The console opens on **Set up your gateway** with that checklist. Nothing is
pre-configured: a fresh install has zero providers, zero credentials, zero
profiles, zero aliases and zero published models.

---

## Provider setup

### ChatGPT / Codex

`Providers → ChatGPT → Connect ChatGPT`.

The setup UI shows the official verification URL
(`https://auth.openai.com/codex/device`), the one-time code, a countdown, and
an anti-phishing warning. You enter the code in your own browser; the
gateway never sees your account password.

Two things the setup UI tells you before it starts:

* **Device-code sign-in must be enabled** for your account — ChatGPT security
  settings for a personal account, workspace permissions for a workspace
  account. The flow cannot work otherwise.
* Device-code authorisation is a **beta** path.

A ChatGPT subscription is **not** a guaranteed API or custom-gateway
entitlement. What your account can actually reach depends on your plan, and
the console says so rather than implying otherwise.

### OpenCode Go

`Providers → OpenCode Go → Connect OpenCode Go`.

Paste the API key into the protected form and choose **Save and test**. The
gateway validates the key against `https://opencode.ai/inference/go/v1/models`.
The key is stored in an owner-only credential store, separately from any
other provider, and is **never displayed again**.

---

## Models and routes

Discovery never auto-publishes. A discovered model is *staged*; it appears in
`/v1/models` only after you enable it, give it an alias, and publish that
alias.

Dialect is a property of the **model**, not of the provider. OpenCode Go
serves four protocols under one account, so the same model id can mean
different things at different providers. The console always labels the
provider next to the model id.

```text
Alias: coding

Primary
  ChatGPT / Codex → gpt-6-luna

Optional fallbacks
  OpenCode Go → gpt-6-luna
  OpenCode Go → mimo-v2.6-flash
```

### Fallback is opt-in twice

Cross-provider fallback sends a prompt to a *different* provider, and
therefore into a *different* data-retention regime. It is off by default.
You must:

1. authorise fallback for the alias, **and**
2. confirm each fallback provider by name.

You also choose which triggers may fire it. A confirmed provider-reported
allowance exhaustion is different from a rate limit, and the console keeps
those apart. A local cooldown is a retry policy, not evidence that the
provider reset.

The gateway **never** switches provider:

* after streamed output has started;
* after a request with tools or other side effects may have executed;
* after ambiguous delivery;
* for an authentication, authorisation, policy or malformed-request failure,
  unless you have explicitly defined a safe policy for that class.

When no eligible route remains, the gateway fails closed and names the
routes it excluded and why. It never silently serves a different model.

---

## Profiles and clients

**LAN no-auth mode** (the default) uses one explicit **Anonymous LAN
profile** for every request. It provides no per-device and no per-person
isolation, and the console says so.

**Authenticated mode** adds named client profiles with per-profile allowed
aliases and optional caller tokens. A token is shown **once**, as the direct
result of issuing or rotating it. It is never returned by a later read, and
never appears in a URL, an analytics record, an export or a routine log.

Profiles scope *aliases*, never upstream identity. Every caller shares the
owner's single upstream account.

---

## What the gateway never stores

* prompts, responses, or any response content;
* provider credentials, bearer tokens, device codes, or caller tokens;
* any environment value beyond the four release fields.

Status, analytics and exports are metadata only. A request ledger records
which alias was asked for, which provider and model actually served it, how
long it took and how it ended — never what it said.

Usage and limits carry a **source** and an **as-of time**. A limit the
gateway cannot observe stays **Unknown**; it is never rendered as zero and
never as unlimited.

---

## `/version`

```json
{
  "service": "personal-llm-gateway",
  "source_revision": "<git commit or unknown>",
  "source_tree": "<git tree or unknown>",
  "image_revision": "<sha256 digest or unknown>"
}
```

That is the whole schema. There are no other keys, and the endpoint never
returns configuration, credentials, prompts, completions, provider account
details or environment values.

`image_revision` is written by the release step into the persistent volume,
because an image's own digest is only knowable after the image exists. The
release gate verifies that the running container reports exactly its own
digest.

---

## Development

The project uses the **Python standard library only**. There is no
third-party runtime dependency, nothing to `pip install`, and no build step.

```sh
PYTHONPATH=src python3 -m unittest discover -s tests    # the suite
PYTHONPATH=src python3 -m gateway --help                 # the CLI
python3 scripts/build_go_catalog_snapshot.py              # regenerate the Go catalogue
```

### Layout

```
src/gateway/
  cli.py            entry point; refuses a wildcard bind
  buildinfo.py      the fixed release identity
  config.py         configuration, aliases, profiles
  credentials.py    the owner-only credential store
  auth.py           owner sessions, client profiles, caller tokens
  observability.py  the metadata-only ledger and limit snapshots
  stores.py         atomic, permission-restricted persistence
  http/             the HTTP layer, request hardening, the application
  core/             capabilities and the routing core
  adapters/         the provider seam
  bridge/           Responses <-> Chat Completions, both directions
  static/           the console
data/
  go_catalog.json   the dated OpenCode Go catalogue snapshot
scripts/
  build_go_catalog_snapshot.py
  release_gate.py
```

### The release gate

```sh
docker build --build-arg SOURCE_REVISION="$(git rev-parse HEAD)" \
             --build-arg SOURCE_TREE="$(git rev-parse HEAD^{tree})" \
             -t personal-llm-gateway:candidate .
python3 scripts/release_gate.py personal-llm-gateway:candidate
```

The gate runs against **one exact image digest** and never rebuilds or
substitutes the artifact it checks. It verifies the release identity, the
wildcard refusal, the forwarded-header / `Host` / `Origin` / CSRF probes,
the unauthenticated surface, and that no credential material is in the
image.

---

## Known limitations

These are real, and the console reports them honestly rather than hiding
them.

* **Models on a non-OpenAI dialect cannot be served.** OpenCode Go serves
  some models over Anthropic Messages or Google v1beta. The gateway does not
  translate those dialects, so such a route is refused with a clear reason
  and its dialect is shown as Unknown. It is not guessed.
* **ChatGPT limits are Unknown.** OpenAI publishes per-plan *estimates* and
  a web dashboard, but no machine-readable API for a personal subscription.
  The gateway does not call undocumented endpoints, so actual remaining
  allowance and reset times are Unknown. Published estimates are shown with
  their source and date.
* **The device-code flow is a beta path** implemented from provider
  integration behaviour, not from a published third-party contract. OpenAI
  does not endorse this gateway.
* **No TLS.** The gateway is LAN-only. Put it behind a TLS terminator if you
  need one, and keep it on a private network.
* **The setup UI is reachable from the network by default.** It requires the
  owner passphrase, but a network perimeter is not something the gateway
  provides. Bind it to loopback with `GATEWAY_SETUP_BIND=127.0.0.1` if you
  would rather it were not reachable at all.
* **The bridge is verified against a recorded corpus**, not yet against live
  provider captures. The corpus and its round-trip runner are in
  `tests/fixtures/bridge_corpus.json`.

---

## Licence

Private and unlicensed. All rights reserved.
