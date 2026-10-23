# Personal LLM Gateway

A personal, LAN-only, OpenAI-compatible LLM gateway. It lets one owner manage
their LLM access through a browser — no editing files, no Docker commands, no
environment variables, no JSON.

Two providers are in scope:

| Provider | Credential | Wire dialect |
|---|---|---|
| **ChatGPT / Codex** | user-operated device-code authorisation (beta) | Responses API |
| **OpenCode Go** | API key | Responses, Chat Completions, Anthropic Messages, or Google v1beta — **per model** |
| **TypeSafe Jev** | API key | Native System One structured decisions (`/v1/systemone`), not chat completions |

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
| API | `http://10.10.11.109:8765/v1/` | LAN | `GET /v1/models`, `GET /v1/alias-catalog`, `POST /v1/chat/completions`, `POST /v1/responses`, plus native Jev `GET /v1/systemone/models` and `POST /v1/systemone` |
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

python3 scripts/release.py --build --image personal-llm-gateway:candidate
```

That one command builds the image from this checkout, starts the
deployment, publishes the running image's identity into the state volume
and verifies it. **Run it on the machine that owns `GATEWAY_BIND`** - the
gateway refuses to start on an address the host does not hold.

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

### TypeSafe Jev / System One

`Providers → TypeSafe Jev` accepts a TypeSafe API key in the same write-only
form as other key-backed providers. Use the explicit **Test** and **Refresh**
actions before enabling a discovered Jev model. Normal console reads and model
catalogue reads never contact TypeSafe.

Jev is a structured-decision provider, not a completion model. Its native API
is available at `POST /v1/systemone`; send a model, a state string/object/array,
and one or more named `noul`, `choice`, or `score` questions. The gateway
returns TypeSafe's structured response unchanged. Image assets, `box`
questions, multipart requests, and the provider's danger-zone long-context
mode are refused for now rather than silently forwarded. A direct System One
call sends its supplied state and questions to TypeSafe; neither is persisted
or written to gateway logs or the request ledger. Jev is not used to inspect
or route chat requests in this release.

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

## Using it from OpenCode

The gateway can appear in OpenCode's own `/models` selector, so an alias
published in the console shows up as a selectable model without editing any
OpenCode configuration by hand.

`GET /v1/alias-catalog` publishes, for every published alias, the
alias-level context limit, the primary route's published reasoning levels,
and whether every route supports function tools. It is metadata only — no
provider inventory, no ledger, no credential, no unpublished route — and it
makes no provider call.

`integrations/opencode/` is an OpenCode V2 provider plugin that reads that
catalogue, registers one provider (`personal-gateway`) and refreshes every
60 seconds. Install it in `~/.config/opencode/opencode.jsonc`:

```jsonc
{
  "plugins": [
    {
      "package": "/path/to/llm-gateway/integrations/opencode",
      "options": { "baseURL": "http://10.10.11.109:8765/v1" }
    }
  ]
}
```

The base URL must be the gateway's own private address with the `/v1`
prefix; the plugin refuses a public host, an HTTPS host, a path other than
`/v1`, and a URL carrying credentials or a query. After it loads,
`/models` lists `personal-gateway/<alias>`, and each reasoning level the
primary route publishes is offered as a `#variant`.

Two rules the plugin keeps:

* **Documented windows are used; Unknown stays honest.** The Go catalogue
  itself returns only IDs, but its official endpoint table identifies the
  exact model it serves. Where that model's own official documentation
  publishes a window, the gateway records it: MiMo-V2.6-Flash is 1,048,576
  tokens with a 131,072-token maximum completion, and MiniMax M3.1 Flash is
  1,000,000 tokens. An alias uses the smallest documented route limit. A
  model without that evidence still gets the **owner-chosen 200,000-token
  OpenCode working budget**, labelled `(200k client budget; upstream
  unknown)`. That is a client-side fallback, not a provider guarantee.
* **A failed read invents nothing.** If the catalogue cannot be read, the
  plugin warns and lists no gateway aliases; a failed *refresh* keeps the
  last complete inventory rather than erasing the model you had selected.

The reasoning level is a **preference**, exactly as the gateway treats it:
if the route that actually serves the request does not publish the level
you picked, the gateway drops it for that route and records the
substitution in the request ledger — it is never silent, and it never
blocks a fallback that could otherwise serve the request.

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
verified is not a bound. OpenCode Go's own catalogue returns only IDs, but
its published endpoint table names the exact model served. The gateway uses
a model-provider context window only when both pieces of official evidence
exist; its current snapshot records MiMo-V2.6-Flash at 1,048,576 and
MiniMax M3.1 Flash at 1,000,000 tokens. A pricing tier (for example,
`≤ 272K`) is never treated as a context guarantee.

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
python3 scripts/release.py --build --image personal-llm-gateway:candidate
                                                       # build, deploy, publish the identity, verify
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
  ../integrations/opencode/   the OpenCode V2 provider plugin
data/
  go_catalog.json   the dated OpenCode Go catalogue snapshot
scripts/
  build_go_catalog_snapshot.py
  init_state_dir.py    prepares the state volume for the non-root runtime
  release.py           the cutover step: build, deploy, publish identity, verify
  release_gate.py      the gate against one exact candidate image
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

### Deploying a release

**Run these on the machine that owns `GATEWAY_BIND`.** The gateway binds
one explicit address and refuses to start on an address the host does not
hold, so a release run from the wrong machine fails at startup rather than
serving somewhere unexpected.

```sh
export GATEWAY_BIND=10.10.11.109          # an address THIS machine owns

python3 scripts/release.py --build --image personal-llm-gateway:candidate
```

One command. It:

1. builds the image from the checkout, stamping the source revision and
   tree, so the image can say what it was built from;
2. starts the Compose deployment - including the one-shot `state-init`
   step that prepares the state volume for the non-root runtime;
3. writes the running image's identity into the state volume, atomically,
   as the runtime user;
4. verifies from outside the container that `/health` answers, that
   `/version` reports exactly the identity that was published, that the
   running container *is* that image, and that the process listens only on
   the configured address.

Every step is checked. A mismatch stops the release rather than leaving a
deployment that cannot say what it is.

To build and gate without deploying:

```sh
export SOURCE_REVISION="$(git rev-parse HEAD)"
export SOURCE_TREE="$(git rev-parse HEAD^{tree})"

docker build --build-arg SOURCE_REVISION="$SOURCE_REVISION" \
             --build-arg SOURCE_TREE="$SOURCE_TREE" \
             -t personal-llm-gateway:candidate .
python3 scripts/release_gate.py personal-llm-gateway:candidate
```

Then deploy that exact image:

```sh
python3 scripts/release.py --image personal-llm-gateway:candidate
```

**Never rebuild between the gate and the deployment.** The gate inspects
one artifact; the deployment must be that artifact. The image tag is
convenience only - the digest is what identifies it.

**Do not deploy with a bare `docker compose up -d --build`.** Compose
passes `SOURCE_REVISION` through from the environment, so without it the
image is built with `unknown`, and in any case only the release step
publishes the image digest into the state volume. A deployment that
skipped it reports `image_revision: unknown`, and the gateway warns about
that at startup, because a deployment that cannot identify itself cannot
be audited.

To check what is running:

```sh
curl -s http://10.10.11.109:8765/version
```

`/version` has exactly four keys, and `image_revision` must be the digest
of the image that is actually running. The runbook has the rest:
[`RUNBOOK.md`](docs/RUNBOOK.md) covers verification, cutover and rollback.

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
  a web dashboard, but no machine-readable allowance API for a personal
  plan. The gateway calls no undocumented endpoint, so actual remaining
  allowance and reset times are Unknown. Published estimates are shown with
  their source and date.
* **The device-code flow is a beta path.** It is implemented from provider
  integration behaviour, and OpenAI documents the flow but does not endorse
  this gateway's use of it. Device-code sign-in must also be enabled for
  the account, or the provider answers 404.
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
* **OpenCode Go's catalogue itself carries no context field.** A Go route
  gets a context limit only when the Go endpoint table identifies the exact
  model *and* that model's official documentation publishes a window. This
  currently covers MiMo-V2.6-Flash and MiniMax M3.1 Flash; a service-side
  cap not documented by Go would still win, and a model without both sources
  stays Unknown rather than inheriting a price tier or a guess.
* **Usage and cost are not yet recorded.** The bridge reads the provider's
  token counters and the catalogue publishes per-token prices, but the
  request ledger does not store either yet, so the Status page reports
  request counts and timings rather than spend.

---

## Licence

Private and unlicensed. All rights reserved.
