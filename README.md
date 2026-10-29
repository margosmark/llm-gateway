# Personal LLM Gateway

A personal, LAN-only, OpenAI-compatible LLM gateway. It lets one owner manage
their LLM access through a browser — no editing files, no Docker commands, no
environment variables, no JSON.

Two providers are in scope:

| Provider | Credential | Wire dialect |
|---|---|---|
| **ChatGPT / Codex** | user-operated device-code authorisation (beta) | Responses API |
| **OpenCode Go** | API key | Responses, Chat Completions, Anthropic Messages, or Google v1beta — **per model** |
| **OpenCode Console Jev** | API key | Native System One structured decisions (`/v1/systemone`), not chat completions |

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
| Request preview | `POST /owner/api/preview` | Trusted LAN | Shows the converted upstream request without calling a provider; Chat Completions body format |
| Prometheus metrics | `http://10.10.11.109:8765/metrics` | Trusted LAN | Metadata-only operational metrics |

`GET /metrics` exposes Prometheus text-format metrics derived from the retained
request metadata ledger and local cooldown state: request and error counts,
request-latency histogram, fallback counts, and remaining cooldown seconds by
provider route. It contains no prompt or response content and no credentials.
No token-total series are exposed; those are intentionally absent while T2 is
blocked. The endpoint uses the same single-listener trusted-LAN policy as the
owner console and API: any device able to reach the configured private address
can access it. It is not a public monitoring endpoint.

Trusted LAN admin mode: any device that can reach this address can configure.
and use the gateway, including replacing provider credentials. There is no
owner password, login/session bootstrap, separate setup listener, or
per-device administrative boundary. Use this only on a LAN you trust.

The owner API also provides a read-only `POST /owner/api/preview` endpoint.
It returns the request shape for the first currently eligible route without
calling a provider or adding a ledger event. Provider-error fallback cannot be
predicted without a call and is reported as unevaluated. The preview body is
returned only to the LAN caller that submitted it; it is not logged or retained.

The gateway binds an explicit RFC1918 IPv4, IPv6 unique-local (`fc00::/7`),
or loopback address owned by the host. IPv6 literals are supported by the
listener and shown in bracketed URL/Host/Origin form. Public addresses
(including RFC 5737 TEST-NET), multicast, link-local and wildcard binds are
refused at startup. The gateway creates no firewall, router or NAT rule and
cannot enforce one: the operator must ensure the listener is not
port-forwarded or otherwise exposed beyond the trusted LAN. All owner
configuration and client APIs share the one listener.
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

### OpenCode Console Jev / System One

`Providers → OpenCode Console Jev` accepts an OpenCode Console API key in the same write-only
form as other key-backed providers. Use the explicit **Test** and **Refresh**
actions before enabling a discovered Jev model. Normal console reads and model
catalogue reads never contact OpenCode Console.

Jev is a structured-decision provider, not a completion model. Its native API
is available at `POST /v1/systemone`; send a model, a state string/object/array,
and one or more named `noul`, `choice`, or `score` questions. The gateway
returns OpenCode Console's structured response unchanged. This native JSON
endpoint refuses image assets, `box` questions, and multipart bodies; that
restriction is specific to System One and does not mean the chat API is
text-only. Image input is accepted on a route only when that exact model has
published image-capability evidence, and the router excludes incapable routes
for image requests (see the selector-catalogue section below). A direct System
One call sends its supplied state and questions to OpenCode Console; neither is
persisted or written to gateway logs or the request ledger. Separately, Jev
inference is an explicit per-alias opt-in used only for phase selection, as
described below.

### OpenCode Go

`Providers → OpenCode Go → Connect OpenCode Go`.

Paste the API key into the write-only form and choose **Save and test**. The
explicit action stores the key and then validates it against
`https://opencode.ai/inference/go/v1/models`; ordinary page loads still never
call a provider. The key is stored in a private credential store,
separately from any other provider, and is **never displayed again**.

---

## Models and routes

### Capability discovery (staged, synthetic evidence only)

The owner API supports exact-model preview and explicitly selected, provenance-recorded approval of `supports_image_input` and `supports_function_tools`. Approval is per field; fields not selected remain pending and can be decided in a later owner action. The decision endpoint uses revision/fingerprint compare-and-swap, records exact retries idempotently, and rejects stale proposals. Reads and normal configuration operations never probe providers.

**One reviewed probe protocol is enabled, and only one**: `opencode_go` /
`mimo-v2.6-flash`. Every other provider/model pair keeps the old answer —
preview available with HTTP 422 and `probe_unavailable`, and no request is
sent.

A probe is a paid inference request, so the run path fails closed at each
gate and names the gate that refused it:

| Gate | Behaviour |
|---|---|
| Reviewed pair | An unregistered pair is refused; there is no "probe any model" escape hatch. |
| Connected provider | A provider whose connection has not been confirmed is refused. |
| Published tariff | Unknown **or stale** pricing (older than 30 days, no source, no date) is refused, not estimated. |
| Owner selection | The owner must authorize this exact account as the billing route and accept the estimate policy. A `hard_cap_required` policy is refused: OpenCode Go publishes no request-level max-charge control, so no enforceable dollar ceiling can be claimed. |
| Spend ceiling | The owner must state a maximum estimated spend, and it must cover the worst case. |
| Confirmation | A run needs the single-use, expiring confirmation issued by the preview that produced the estimate. |
| Concurrency | One run in flight per gateway. A second run and a second preview are refused while one is running. |

The run itself is at most **two one-shot requests** — one for text, one shared
by image and function tools — non-streamed, non-retried, never failed over,
concurrency 1, each bounded by time, request bytes, image bytes and output
tokens, and cancellable from the console
(`/owner/api/capabilities/probe/cancel`). Returned tool calls are inspected
as data and never executed. A failure, a mismatch, a missing tool call or
absent cost accounting is recorded as `inconclusive` or `not_tested` — never
as `observed_unsupported`.

The estimate is an estimate: it is arithmetic on the published tariff and the
declared bounds, not a bill, and the gateway has no preflight token counter.

A run writes **evidence only** — sanitized metadata in the capability store,
plus an unselected proposal. It changes no configuration, no routing, no
cooldown and no ledger, and the metadata reaches config only through the
owner's own field decision. A synthetic isolated test seam still exists for
exercising approval behaviour; it is not an upstream protocol, and no
capability conclusions may be inferred from it.


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
Metadata-only outcomes are persisted asynchronously on the state volume with
bounded rotation and a 30-day retention window when durable ledger storage is
enabled; otherwise only the bounded in-memory ring is available and is lost
when the gateway restarts. The Status view reports the current durability
mode and marks aggregates Unknown when evictions may omit in-window events.
The same append path maintains `request-ledger-rollup.json`, a derived daily
rollup (one row per day, alias, provider and model, with tokens, fallbacks by
the closed reason vocabulary — `cooldown`, `route_ineligible`,
`upstream_failure`, `other`, `unknown`, folded when the row is written so a
per-second cooldown sentence never becomes a bucket — errors by class and
statuses) that is rebuilt from the ledger on every start, so daily history
survives restarts and ring eviction; its `coverage` block says what it
covers instead of reporting a gap as zero, including the 50,000-entry request
index cap and anything that cap evicted. This is not a billing system.

```text
Alias: coding

Primary
  ChatGPT / Codex → gpt-6-luna

Optional fallbacks
  OpenCode Go → gpt-6-luna
  OpenCode Go → mimo-v2.6-flash
```

### Alias groups

Aliases can share an optional tag in the console. Publish or disable a whole tag from the alias list; this toggles only each member's enabled state. Routes, fallback chains, authorization and any per-alias policy remain independent. A group with an alias missing its primary route is refused as a whole.

### Fallback is opt-in twice

Cross-provider fallback sends a prompt to a *different* provider, and
therefore into a *different* data-retention regime. It is off by default.
The alias editor names the retention class of every route you select
there — primary and each fallback — so a `claude-haiku-5-5` primary
(30 days) with a `qwen3.8-flash` fallback (0 days) says which regime the
prompt moves to before you save, and the request ledger records the class
of the route that actually served. You must:

1. authorise fallback for the alias, **and**
2. confirm each fallback provider by name.

You also choose which triggers may fire it. A confirmed provider-reported
allowance exhaustion is different from a rate limit, and the console keeps
those apart. A local cooldown is a retry policy, not evidence that the
provider reset.

A confirmed exhaustion is held only within a **bounded** window — at most
30 minutes by default, configurable with
`GATEWAY_EXHAUSTED_COOLDOWN_MAX_SECONDS` — so a subscription window that
reopens on the provider's schedule is never stranded behind a multi-hour
local circuit. When the window ends, exactly **one** recovery probe is let
through: a concurrent request falls back (with a truthful skip reason)
until that probe answers. If you have confirmed a reset window yourself,
the console's **Clear cooldowns** button on the provider card — or the
same `clear_cooldowns` action on the owner API — clears exactly that
provider's cooldowns on demand.

The gateway **never** switches provider:

* after streamed output has started;
* after a request with tools or other side effects may have executed — a
  *failed* attempt may have run it; a *skipped* primary never reached a
  provider, so nothing could have executed and that switch is decided by
  the gate below instead;
* after ambiguous delivery;
* for an authentication, authorisation, policy or malformed-request failure,
  unless you have explicitly defined a safe policy for that class;
* without **Authorize fallback**, a confirmed next provider, and an enabled
  trigger — a *skipped* primary (cooldown, capability, or a route held by a
  live recovery probe) passes that same gate before serving elsewhere.

When no eligible route remains, the gateway fails closed and names the
routes it excluded and why. It never silently serves a different model.

### Phase routing is opt-in per alias

An alias may carry an optional **phase-routing** policy that picks a
different primary route depending on what the request is *for*. A phase is
one of a closed set - `plan`, `implement`, `validate`. The policy is off by
default and configured per alias in the console editor. The editor stores the policy as configuration only; it does not inspect or retain request content. The accepted phase vocabulary is closed to `plan`, `implement`, and `validate`. Treat this as owner administration on the trusted LAN, not as a public-client setup instruction.

The precedence is fixed and never silent:

1. A **caller-supplied phase** wins. A client sends it in the gateway-owned
   request field `metadata.personal_gateway.phase` (for example
   `{"metadata": {"personal_gateway": {"phase": "plan"}}}`). The gateway
   validates it against the closed vocabulary and **strips the whole
   `personal_gateway` block** before the request is converted or forwarded,
   so no upstream provider ever sees it. An unknown value is ignored, not
   refused - a stale hint still gets the ordinary route.
2. Otherwise, if you enabled **Jev inference** for the alias, the gateway
   asks Jev to classify the request. This is opt-in, runs only when the
   request is safe to classify (no tools, not a continuation, and it carries
   text), and sends Jev a single bounded, closed-set question the gateway
   owns - never a caller-authored prompt. A Jev choice applies only when it
   names a **configured phase** and its confidence meets your
   `minimum_confidence`. Low confidence, an unknown choice, a Jev error or a
   timeout all fall back to the ordinary route and say why.
3. Otherwise the ordinary primary (or the policy's configured default route)
   is used.

A phase-selected route is the **primary for that phase**, not a fallback:
selecting it never counts as a fallback and needs no fallback authorization.
Phase resolution happens before the route loop, so a phase decision never
occurs after a stream has begun or after a tool may have executed. The
request ledger records the phase and its source (`caller`, `jev`, or
`default`) as metadata only - never the request content, the Jev question,
or the answer distribution.

---

## Using it from OpenCode

The gateway can appear in OpenCode's own `/models` selector, so an alias
published in the console shows up as a selectable model without editing any
OpenCode configuration by hand.

`GET /v1/alias-catalog` publishes, for every published alias, the
alias-level context limit, the primary route's published reasoning levels,
and whether every route supports function tools or whether **any** route
accepts image input. It is metadata only — no provider inventory, no ledger,
no credential, no unpublished route — and it makes no provider call.

An alias is advertised as image-capable when **any** route that could serve
it accepts images. This is deliberately looser than the all-routes rule the
tools flag uses, because the router enforces image capability per request: a
request carrying an image is routed to a route that can see it, and a
text-only route is excluded for that request with the reason recorded —
never silently served an image it cannot read. If no image-capable route
remains eligible, the request fails closed at the gateway rather than at the
provider.

Image support is recorded per exact upstream model ID from official
model-provider documentation, with the source URL kept beside it. A model
with no such evidence is never assumed to accept images — so an alias whose
routes are *all* unevidenced stays text-only in OpenCode until the evidence
exists. This is the same honesty rule the gateway applies to context
windows.

Each provider keeps its **own** evidence map, because the two reach the
capability from different places: OpenCode Go's lives in
`scripts/build_go_catalog_snapshot.py` and is baked into the dated
`data/go_catalog.json`, while ChatGPT/Codex's lives in
`src/gateway/adapters/chatgpt_codex.py` because its catalogue endpoint
publishes no image field to transcribe. Adding a model to one has no effect
on the other. Currently evidenced, per exact slug: `gpt-5.6-terra`
(OpenAI, read 2026-10-23) on the Codex side, and **36 of the 40** Go rows —
24 true and 12 transcribed **false** — each from that model's own data page
(read 2026-10-25), except `minimax-m3.1-flash` (MiniMax documentation, read
2026-10-23) and the four `mimo-*` rows read 2026-10-24. `mimo-v2.5-pro` is
one of the **false** ones, because its page lists text only. The four rows
nobody could read — `omen-alpha`, `iceman-contributor-free`,
`gemini-4-argon-contributor` and `longcat-2.5-preview-free` — stay false
with no source at all.

A verdict that lives only in the catalogue is worth nothing until it
reaches the stored row the router reads, so every boot re-applies each
adapter's evidence to the configured model rows: promotion only, exact slug
only, no provider call, no row added, nothing demoted. See the runbook's
*Hermes profile vision routing* section for what the client side does with
that answer.

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

The gateway's catalogue reports the provider's full working window. The
OpenCode plugin displays a **working input budget** as its context value:
the gateway's limit minus an 8,192-token output reserve. For example, the
ChatGPT/Codex route's 272,000-token working window is shown as 250,208 input
tokens plus 8,192 output tokens. The reserve is an OpenCode-side allocation,
not a claim that the provider publishes a separate output limit.

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

## Local concurrency protection

Inference calls are capped at four in-flight requests per provider, per gateway
process. This local cap rejects excess requests with `rate_limited` and does
not represent a provider guarantee or published provider limit. Streaming
requests hold their slot until their response stream completes.

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
long it took and how it ended — never what it said. Provider health combines
connection/test/catalogue metadata, model counts, local cooldowns and recent
served-request success/failure rates; it performs no provider calls, and rates
remain Unknown without observed requests. Token counters come from
reported provider usage; local cost estimates use published prices only and
are not provider bills. The Status page aggregates usage for the last 24 hours
and displays the price source and observation date, and it can show any
recorded UTC day from the durable daily rollup through the window selector.
A group's dollar figure is the **known subtotal**: requests without a
published price are never priced and never poison the total, so the console
states their coverage beside it — how many requests, and how many input and
output tokens, are unpriced — instead of collapsing the whole group to
Unknown. A group with no priced request at all shows **Unknown** (never
zero) together with that coverage, and an unpriced provider such as
`chatgpt_codex` is labelled as having no published actual/list price; the
gateway never infers one from a matching model ID. Cached-token completeness
is enforced end to end: an absent cached counter is never read as a reported
zero and never prices as one - it is shown as coverage unknown, beside the
count of requests missing it - while an explicit reported zero prices
normally. A request that reported no usage is **Unknown**, never `$0.00`.
Request metadata is
retained on the state volume for up to 30 days; it is not a billing system.

The same state payload carries a **Fallback analytics** block: the fallback
rate overall and per alias, provider and model, the reasons behind it, and a
daily series of requests, tokens, fallbacks and errors. Days are reported
with their own status, so a day with no recorded rows is a gap rather than
zero, and a rate with no requests behind it is unknown rather than `0.0`.
Export the recent ledger from the owner console's read-only
`/owner/api/ledger/export` endpoint; it returns CSV by default or JSON with
`?format=json`, bounded to 1,000 rows (default 500). Only the six metadata
columns shown in the ledger table are included, with the same redaction
rules as the log.
and displays the price source and observation date.

The **Request statistics** table groups request/error counts, error classes,
fallback rates, recorded latency percentiles, reported token totals and local
cost estimates by alias, serving provider and serving model for the last
hour, day or 30 days. It uses the metadata-only ledger history available to
the gateway (bounded to the recent 10,000 in-memory records, with
asynchronously persisted history restored on restart); unavailable usage,
latency or prices remain Unknown.

Aliases may also have a local monthly USD budget. It is a **soft cap** checked before each request; requests already in flight may overshoot. Spend is computed from recorded successful-request token counters and published per-token prices. If any monthly spend cannot be priced or the ledger history is incomplete, spend is **Unknown** and the alias is reported uncapped rather than free. The console shows the UTC month boundary when the budget resets.

Usage and limits carry a **source** and an **as-of time**. A limit the
gateway cannot observe stays **Unknown**; it is never rendered as zero and
never as unlimited. The **Usage and limits** card shows only enforceable
spend caps observed from providers — with `limits: []` it says plainly that
no cost limit is configured. Catalogue fields such as `monthly_limit_usd`
are reference metadata, are never loaded as budget policy, and are labelled
"reference only, not enforced" wherever they appear.

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
  one event with no finish reason. The gateway also emits a `: ping` SSE
  comment after 15 seconds without client-facing output; comments carry no
  response content.

## Known limitations

These are real, and the console reports them honestly rather than hiding
them.

* **Not every catalogued model can be served.** OpenCode Go serves models
  over Anthropic Messages and Google v1beta. Anthropic Messages is
  translated by the native Anthropic provider (T14): once an
  Anthropic-dialect upstream route is configured, it is served through
  the OpenAI-compatible surface and its discovery follows the shared
  adapter contract. Google v1beta is not translated — the owner declined
  that work (T6, `docs/TODO.md`) — and a route the gateway cannot
  translate is refused with a clear reason rather than guessed.
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
* **Local cost is an estimate from published prices, not a bill.** The token
  counters come from the provider's reported usage and the cost is computed
  from the dated catalogue's published per-token prices, with the price's
  source and observation date shown beside it. The provider's own allowance,
  and any credit, discount or plan the owner holds, stay **Unknown**: the
  gateway has no tokenizer and no billing relationship with the provider, and
  a model with no published price reports **Unknown** rather than zero.
* **Anthropic Messages is supported in both directions.** Anthropic-shaped
  clients can use `POST /v1/messages`; Anthropic-dialect upstream models are
  also bridged to the OpenAI Chat Completions and Responses surfaces. The
  supported OpenCode Go Anthropic models include `claude-haiku-5-5`,
  `minimax-m3`, `qwen3.7-plus`, `qwen3.8-flash`, and `qwen3.8-max`.
  Cross-dialect controls that cannot be represented are refused rather than
  silently discarded. Google v1beta remains unsupported.

R1, R8 and R9 are closed in `docs/REVIEW.md`: device-code polling
completes, ChatGPT model refresh follows the shared adapter contract,
and buffered cross-dialect responses are converted. These are covered
by `tests/test_device_flow.py`, `tests/test_adapter_contract.py`, and
`tests/test_buffered_bridge.py` respectively.

The LAN-only HTTP trust model and the absence of a machine-readable
provider allowance API remain genuine limitations; none imply
provider-account or live-deployment verification.

---

## Licence

Private and unlicensed. All rights reserved.
