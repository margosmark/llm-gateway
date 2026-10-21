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
| Owner console and configuration | `http://10.10.11.109:8765/owner/` | Trusted LAN | Full configuration, credential enrollment, provider actions, and status; no login |
| API | `http://10.10.11.109:8765/v1/` | LAN | `GET /v1/models`, `POST /v1/chat/completions`, `POST /v1/responses` |
| Release identity | `http://10.10.11.109:8765/version` | LAN, anonymous | Fixed four-key metadata |

Trusted LAN admin mode: any device that can reach this address can configure
and use the gateway, including replacing provider credentials. There is no
owner password, login/session bootstrap, separate setup listener, or
per-device administrative boundary. Use this only on a LAN you trust.

The gateway binds an explicit private address. A wildcard bind is refused at
startup. It never creates a firewall or router rule, and it is never publicly
exposed. All owner configuration and client APIs share the one listener.
Canonical same-origin browser mutations work without cookies, passwords,
sessions, or CSRF bootstrap values; cross-origin browser mutations, hostile
Host values, and forwarded headers remain rejected. Credentials are entered
only into write-only form fields and are never read back.

The gateway port is configurable with `--port` or `GATEWAY_PORT`, except port
`8766`, which is reserved and rejected because the retired setup listener must
not be reused.

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

Then, from a trusted LAN device:

1. Open <http://10.10.11.109:8765/owner/>. No login or bootstrap step exists.
2. Connect a provider.
3. Test the connection.
4. Refresh the model catalogue.
5. Enable the models you want to expose.
6. Create an alias, pick a primary, publish it.
7. Point a LAN client at the alias.

The console opens on **Set up your gateway** with that checklist. The two
supported provider *cards* are visible so the owner can choose one, but a
fresh install has zero provider credentials, zero discovered models, zero
profiles, zero aliases and zero published models.

---

## Provider setup

### ChatGPT / Codex

`Providers → ChatGPT → Connect ChatGPT`.

The console shows the official verification URL
(`https://auth.openai.com/codex/device`), the one-time code, a countdown, and
an anti-phishing warning. You enter the code in your own browser; the
gateway never sees your account password.

Two things the console tells you before it starts:

* **Device-code sign-in must be enabled** for your account — ChatGPT security
  settings for a personal account, workspace permissions for a workspace
  account. The flow cannot work otherwise.
* Device-code authorisation is a **beta** path.

A ChatGPT subscription is **not** a guaranteed API or custom-gateway
entitlement. What your account can actually reach depends on your plan, and
the console says so rather than implying otherwise.

### OpenCode Go

`Providers → OpenCode Go → Connect OpenCode Go`.

Paste the API key into the write-only form and choose **Save and test**. The
explicit action stores the key and then validates it against
`https://opencode.ai/inference/go/v1/models`; ordinary page loads still never
call a provider. The key is stored in a private credential store,
separately from any other provider, and is **never displayed again**.

---

## Models and routes

Discovery never auto-publishes. A discovered model is *staged*; it appears in
`/v1/models` only after you enable it, give it an alias, and publish that
alias.

Dialect is a property of the **model**, not of the provider. OpenCode Go
serves four protocols under one account, so the same model id can mean
different things at different providers. The console always labels the
provider next to the model id.

Your client asks for the **alias**; the provider only knows its own model
ids. The gateway substitutes the upstream model id for the chosen route and
never sends the alias upstream, and the request ledger records both the
alias you asked for and the provider and model that actually served it.

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

## Context limits

A provider publishes a **working context window** and a **ceiling for
config overrides** (`max_context_window`), and reserves a share of the
window for its own system prompt, tool overhead and output. The number a
client can rely on is the smallest of those constraints:

```text
gpt-6-luna:  context_window 272,000
              max_context_window 872,000   ← a ceiling for overrides, not a competing limit
              effective limit 258,400       ← 272,000 less the provider's 5% headroom
```

At the **alias** level the same rule applies, and it matters more: an alias
may be served by a fallback, so its limit is the **smallest limit among the
routes that could serve it**. If any eligible route's limit is not
published, the alias's limit is **Unknown** - a bound that cannot be
verified is not a bound. An alias that mixes a ChatGPT route with an
OpenCode Go route therefore reports Unknown, because the Go provider
publishes no context windows.

The limit is **reported, not enforced**. The gateway has no tokenizer, so
it does not count a prompt's tokens; the provider is the authority on what
fits, and a prompt that is too long fails upstream as a malformed request.

## Profiles and clients

LAN API invocation is anonymous by design. On a fresh zero-profile installation,
`GET /v1/models` returns OpenAI-compatible `{ "object": "list", "data": [] }`.
A chat request for an unpublished or unknown route returns a JSON 4xx error and
never calls a provider. Profiles and optional caller tokens are configuration/future-attribution
features, not access control: any reachable LAN peer can configure the gateway
and invoke every published route.

---

## Sensitive data and observability

* It does **not** store prompts, responses, or response content.
* Provider credentials, bearer tokens, device codes, and caller tokens are
  stored only when required for operation, in a private credential store.
  They are never returned by a normal state or status response, ledger,
  export, URL, or routine log. A caller token is shown only in the direct
  response that issues it.
* `/version` exposes no arbitrary environment value: it has only its fixed
  four-key release schema.

Status, analytics and exports are metadata only. A request ledger records
which alias was asked for, which provider and model actually served it, how
long it took and how it ended — never what it said.

Usage and limits carry a **source** and an **as-of time**. A limit the
gateway cannot observe stays **Unknown**; it is never rendered as zero and
never as unlimited.

See [the architecture map](docs/ARCHITECTURE.md),
[development guide](docs/DEVELOPMENT.md), and
[current codebase review](docs/REVIEW.md) for implementation boundaries,
verification commands, and known delivery blockers.

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
  auth.py           client profiles and caller-token authorization
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

The gate runs against **one exact candidate image** and never rebuilds or
substitutes the artifact it checks. It verifies candidate-level release
identity, wildcard refusal, forwarded-header / `Host` / same-origin `Origin`
probes, direct owner-console use, and that no credential material is in the image.
It is not a substitute for a Compose-equivalent deployment check: state-volume
ownership and production `/version` identity still require explicit proof.

---

## Provider notes

Both upstreams have quirks that are not obvious from their documentation.
They are recorded in full in [`docs/PROVIDER_CONTRACTS.md`](docs/PROVIDER_CONTRACTS.md),
with the traps that cost the most time:

* **OpenCode Go requires an `x-opencode-session` header** and refuses a
  request without it. The gateway derives one server-side, because ordinary
  OpenAI-compatible clients send none.
* **The ChatGPT / Codex upstream is streaming only** and expects the same
  request fields OpenAI's own client sends, including a client version. A
  client that wants a single, non-streamed object is served by consuming the
  upstream stream.
* **The Codex model catalogue has its own shape** — a list under `models`,
  with each model identified by its `slug`, not an `id`.
* **SSE frames must be terminated.** A frame is its data lines plus the
  blank line that ends it; a stream without it is read by a strict client as
  one event with no finish reason.

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
* **ChatGPT/Codex onboarding is not ready for use yet.** The current UI cannot
  complete device-code polling, and ChatGPT catalogue refresh does not satisfy
  the common model-metadata contract. These release blockers are R1 and R8 in
  `docs/REVIEW.md`.
* **Cross-dialect buffered replies are not ready for use yet.** A request can
  be converted to an upstream dialect while its non-streaming response is
  returned unchanged. This is R9 in `docs/REVIEW.md`; use only matching
  dialects until it is fixed.
* **The device-code flow is a beta path** implemented from provider
  integration behaviour, not from a published third-party contract. OpenAI
  does not endorse this gateway.
* **No TLS.** The gateway is HTTP-only and bearer tokens and request content
  require a trusted LAN. Do not place it behind a TLS terminator or proxy
  until an explicit proxy-trust/forwarded-header design is implemented; the
  current server rejects forwarded headers.
* **Trusted LAN administration has no authentication boundary.** Any reachable
  peer can configure the gateway, replace credentials, and invoke published
  models. The network must be trusted; the gateway does not protect against an
  on-path LAN attacker.
* **The bridge is verified against a recorded corpus**, not yet against live
  provider captures. The corpus and its round-trip runner are in
  `tests/fixtures/bridge_corpus.json`.
* **ChatGPT limits are Unknown.** OpenAI publishes per-plan *estimates* and
  a web dashboard, but no machine-readable allowance API for a personal
  plan, and the gateway calls no undocumented endpoint. Published estimates
  are shown with their source and date.
* **OpenCode Go publishes no context windows**, so an alias with an
  OpenCode Go route reports an unknown context limit. That is the honest
  answer: the Go service's own limit is what matters, and it does not
  publish it.
* **The device-code path is a beta path.** OpenAI documents it; OpenAI does
  not endorse this gateway's use of it.

---

## Licence

Private and unlicensed. All rights reserved.
