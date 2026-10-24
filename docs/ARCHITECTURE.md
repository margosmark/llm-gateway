# Architecture

This document describes the code that is in this repository today. It is an
orientation document, not a statement that every future provider flow is
supported or production-ready.

## Runtime topology

The gateway starts one standard-library HTTP server from `gateway.cli`:

| Listener | Default bind | Mounts | Purpose |
|---|---|---|---|
| Trusted LAN | explicit private IP, port `8765` | `/owner/`, `/v1/`, `/health`, `/version`, `/metrics` | direct owner console/configuration behind the owner session (unless the owner has set `owner_console_auth=false`, which serves it without a session), console-minted machine keys for automation on the same listener, a keyless-by-default OpenAI-compatible API (optional client key), and metadata-only Prometheus exposition |

The owner console, credential forms, provider actions, and client API share the
same listener. `cli.py` refuses wildcard and unsupported binds. Supported classes are
RFC1918 IPv4, IPv6 unique-local (`fc00::/7`), and loopback, subject to the
address being assigned locally. The HTTP server selects an IPv4 or IPv6
listener by address family; IPv6 authorities are bracketed for URLs, `Host`,
and `Origin`. Public (including RFC 5737 TEST-NET),
multicast, link-local, and unspecified addresses are refused. No setup
listener or setup-port setting exists. Forwarded headers are rejected rather
than trusted.
The gateway port is configurable, but `8766` is reserved and rejected so the
retired setup port cannot be reused. HTTP request handling is bounded to 32
worker threads by default; a full pool returns 429 `rate_limited` instead of
creating unbounded workers. A process-local rolling window allows 120 requests
per peer IP per 60 seconds by default. These are local capacity protections,
not an access-control boundary or defense against an on-path LAN attacker.

## Provider concurrency

Inference adapters are wrapped by a process-local concurrency guard with a
**per-provider** limit: cloud providers default to 32 active requests and
the local adapters (`ollama`, `llama_cpp`, `lm_studio`) to 4, and the owner
can override any single provider with `max_in_flight` in the config (set
from the owner console, read live on every acquire). Excess calls fail
promptly with HTTP 429 and the `rate_limited` error class; no queue or
provider probe is introduced. A streamed response retains its slot until
the stream finishes or closes. This is a local protection only, not a
provider guarantee or provider-reported limit. The defaults and overrides
are configuration, and the active counters are independent per process.

## State boundaries

`gateway.config.ConfigStore` persists the non-secret configuration as
`config.json`, with atomic replace, mode `0600`, and a monotonic revision.
It contains provider state, model metadata, aliases/routes, profile metadata,
and access mode. It must never contain a credential or caller-token hash. Owner-console exports use a strict allowlist-style sanitization, and imports build and validate a complete candidate through `gateway.identifiers` and `gateway.routes` before one atomic replace. Optional timestamped snapshots live under the state volume's `snapshots/` directory and are disabled by default; the setting is read live.

`gateway.credentials.CredentialStore` persists separate JSON files under a
private `credentials/` directory. Provider adapters receive a handle scoped to
one provider. Caller-token hashes also use that store. A read of a credential
is available only to internal adapter/auth code; console state renders presence,
not values.

`gateway.owner_auth.MachineKeyStore` persists `machine_keys.json` (mode
`0600` inside the `0700` data directory) beside a **separate**
`machine_key_pepper` file (also `0600`, independent of `owner_auth.json`,
so machine-key state never touches the password or sessions). The store
file holds only a SHA-256 digest per key keyed on that pepper, plus
metadata (display id, name, scope, created/last-used); the plaintext
`gwmk_`-prefixed value exists only in the one create/rotate response
that mints it. The pepper is stat-keyed, so a deleted, replaced or
corrupt pepper stops every machine key on the very next request of a
live store — fail-closed, and restored by restoring the file
(`docs/RUNBOOK.md` §2 incident procedure).

The following state is intentionally in-memory only and disappears on restart:

* in-flight ChatGPT/Codex device authorization state;
* limit snapshots.

Request outcomes are also appended asynchronously to a SQLite store,
`request-ledger.db` under the state directory (WAL, single writer - no lock
on the request hot path). Retention is by age only: records older than 30
days are physically pruned on load and on a daily timer, and there is no
size bound on history, so the published window is reachable at measured
volume. The plaintext `request-ledger.jsonl` journal tail (5 MiB plus two
rotations) remains for debugging and is imported into the store on the
first boot. Boot reads the store through the rollup sidecar plus a bounded
replay and index seed - a full history loads in well under the sub-2 s
state bar (R56) instead of being re-read wholesale. The bounded in-memory
ring remains the live hot view; records are restored after restart. This is operational metadata, not a
complete audit trail and **not a billing system**. A full write queue or
storage error may drop a persistence update; the request path does not wait
for disk I/O.

The same append path maintains `request-ledger-rollup.json`, a derived daily
rollup sidecar: one row per UTC day x alias x provider x model with requests,
input, output and cached tokens, fallbacks broken out by the closed
fallback-reason vocabulary (folded **at ingest** from `gateway.fallback_reasons`,
the same rules the owner analytics reader applies, so a per-second cooldown
sentence never becomes a sidecar key), errors by `error_class` and a status
breakdown, alongside a per-day trend. The store's in-memory request index is
count-capped at 50,000 entries - the only runtime structure that was not
already bounded - and both the cap and what it evicted are reported in
`coverage` (`index_entries` / `index_max_entries` / `index_evicted` /
`index_amendments_dropped`), so totals never depend on unbounded growth and a
late amendment for an evicted request is reported as dropped rather than
counted twice.

The sidecar is rebuilt from the ledger on every start, so aggregated history
survives both a restart and ring eviction, and
`python3 -m gateway.ledger_persistence` regenerates it from the ledger on
demand. Its `coverage` block states the first row it covers, how many rows
could not be represented, and the rebuild status, so an absent day reads as a
gap rather than as zero usage. The sidecar stores aggregates only - no
request identifier, no prompt, no response, and no cost estimate (prices are
applied at read time); it is written 0600 and is not a billing record.

A record written at route time can be amended **in place** when a later
fact changes the outcome: a client disconnect records `client_abort`
(T11), and an upstream that dies mid-answer records the provider's own
class through `gateway.core.abort.classify_late_error` (`docs/REVIEW.md`
R77). Totals move with the amendment, so a truncated or failed request
can no longer read as a success.

The Status view reports whether persistence is enabled and the declared
30-day retention window. Its provider health summary combines configured
connection/test/catalogue metadata, model counts, current local cooldowns,
and success/failure rates from the recent metadata ledger. Rates are based on
requests actually served by each provider; no observations remain Unknown.
The summary performs no provider calls and does not infer external health or
entitlement. Local usage aggregates cover the last 24 hours and
are computed from token counters and published prices, with source and
observation date. Each alias/provider/model row exposes a **known subtotal**
(`known_amount_usd` / `amount_usd`) plus explicit unknown coverage:
`known_cost_requests`, `unknown_cost_requests`,
`unknown_cost_input_tokens`, `unknown_cost_output_tokens`, `cost_incomplete`
and `cost_reason`. One request without a published price (an unpriced
provider such as `chatgpt_codex`) no longer nulls the whole group - its
dollars are excluded from the subtotal and counted as unknown coverage,
while a group with no priced request at all keeps `amount_usd: null` rather
than a fabricated `$0.00`. Cached-token completeness holds at every layer:
a request or rollup row whose cached counter the provider never reported is
Unknown (`missing_cached_tokens`), never a cached count of zero, while an
explicit reported zero stays a priced known zero; no ChatGPT price is ever
inferred from a matching model ID. Because a rollup row holds sums, the
completeness travels beside them: `cached_tokens_unreported` counts the
ledger rows in that row that reported usage without a cached count, and
`requests_without_usage` counts the rows that reported no usage at all
(unreported usage is Unknown, never `$0.00`). The surface prices a row only
where both counts are zero; otherwise the row's dollars are Unknown and the
coverage counters say how much, so an absent cached counter can never become
a reported zero on its way to the console. Because aggregation uses
the bounded recent-event ring, a window that lost rows to eviction is
flagged `coverage.incomplete` instead of being shown as a complete sum: the
amount still prices exactly the tokens the window counts, on the same basis
as the token columns beside it, and the flag is what says the window itself
is partial. This estimate is not a provider bill. Totals are reconstructed
from the restored ring, not a lifetime accounting source.

`/owner/api/state` also carries an `analytics` block assembled from the
in-memory rollup - the poll path never opens the ledger file. It reports a
fallback rate per alias, provider and model with a reason breakdown folded
onto a closed vocabulary (`cooldown`, `route_ineligible`, `upstream_failure`,
`other`, `unknown`), and a daily series of requests, input/output/cached
tokens, fallbacks and errors. The window is `?days=N`, default 7, clamped to
30; a clamped request is echoed back as `requested_days`. A day with no rows
inside the recorded window carries the status `gap` with null values, a day
before the first recorded row is `outside_coverage`, today is `open`, and a
rollup whose schema this build does not recognise is refused rather than
interpreted. A rate with no requests behind it is `null`, never `0.0`. The
console renders this as a fallback card and a day selector on the usage
table; both state plainly that the data is metadata only.

The whole `GET /owner/api/state` render is memoised behind
`gateway.http.state_cache.StateCache`. The rendered body (status,
headers and bytes) is stored under a key derived from every input the
render reads that can change while the process is up: the config
revision plus a stamp of the config file itself
(inode, mtime, size — so a backup import that replaces content without
bumping the revision is caught), each provider credential's `describe()`
digest, the request ledger's version counter (bumped by every record,
amend and clear), circuit **state** (open/closed/expiry, deliberately not
the per-second probe countdown, so an open cooldown does not evict the
entry every second), the limits table, the `?days` window, and the
release identity — the **result** of `release_identity()`, which re-reads
`release.json` on every call, so a release written after the container
started (`scripts/release.py --build`, `scripts/write_release_identity.py`
against a running volume) shows on the very next read instead of waiting
for the background refresh. Process constants the render also reads
(`ledger.durability()`, the cooldown-policy literals) cannot change after
start-up and are not part of the key. Cache
invalidation is therefore by tokenisation rather than by callbacks: a
mutation changes the key, and the immediately following read takes the
miss path and re-renders synchronously — no staleness window can lie
about state the owner just changed. Wall-clock-only drift (usage windows
ageing out, budget month rollover) is bounded separately: a body older
than `refresh_after` (2s) is re-rendered on a background thread after it
is served, and a body older than `hard_max` (300s) is never served at
all — that read re-renders synchronously. The cache sits *outside* the
handler's one-request-one-snapshot structure (R56): a miss still performs
exactly one render from one snapshot of the ledger ring and price
catalogue, so the fix that ended the 46.9s per-consumer re-pull stands
untouched, and a hit performs zero. Acceptance tests live in
`tests/test_state_cache.py` (byte-identity of hits, mutation visibility
on the next read for every mutation class, background refresh, the
hard-max fallback, and the mid-render race guard).

`GET /owner/api/leaderboard` is a second, read-only owner route carrying the
model leaderboard (`gateway.leaderboard.build_leaderboard`). The rollup
already stores one row per (day, alias, provider, model); this endpoint
projects those rows onto **(day, provider, model)** — aliases summed — and
ranks models over the same `?days=N` window by the stated primary metric,
input + output tokens (cached tokens are a subset of input and are never
counted twice), with each entry's `share` of the window total, its requests,
fallbacks, errors and its cost block. It exists as a separate route instead
of a block inside `/owner/api/state` so the console poll keeps its
sub-2s bar (R56); both handlers materialise the in-memory rollup and the
published-price catalogue exactly once per request, and neither opens the
ledger file. Cost runs through `gateway.costs.aggregate_rollup_rows` per
provider slice, so an unpriced model keeps `amount_usd: null` with its
`cost_reason` and unknown token coverage, and a price can never cross a
provider boundary. Every day in the window carries a status
(`ok`/`open`/`no_rows`/`gap`/`outside_coverage`/`unknown`), and a matrix row
for a day with no recorded data is `null` — a measured zero and "no data"
are different claims. The route sits on the same mounts and passes the same
Host/Origin/forwarded-header checks as every other owner route: no new
listener, no public exposure, no auth change.

The owner console's **Leaderboard** tab consumes this route. It is plain
JavaScript in `static/console.js` (no framework, no build step, no CDN
dependency) and fetches `/api/leaderboard` only when the tab is opened, so
the `/owner/api/state` poll keeps its sub-2s bar. It renders the ranked
table, a stacked daily usage chart (CSS-only, palette-indexed by rank), the
date x model matrix, a leader-vs-challenger comparison, a compact
fallback-rate panel fed from the state payload's analytics block, and a
methodology panel. Two rendering invariants are regression-tested in
`tests/console_dom_test.js`: a `null` `amount_usd` renders as "no
published price" and never as `$0.00`, and a day whose matrix row is
`null` renders as a hatched "no data" column in the chart and a single
status span in the matrix — never as a zero bar or a 0-value row.

The owner console can export recent ledger rows as CSV or JSON through a
read-only endpoint; exports are bounded to 1,000 rows, allow-list only the
six ledger-table fields, and pass values through the same redaction rules
as the request log. The endpoint does not create a ledger record.
observation date; a missing price remains Unknown.

Request statistics can be viewed over the last hour, day or 30 days, grouped
by requested alias and serving provider/model. They include errors by class,
fallback rate, recorded latency percentiles, token totals and published-price
estimates. The aggregate is metadata-only, computed over the recent ledger
view (up to 10,000 records), not a lifetime accounting source; missing values
stay Unknown. This estimate is not a provider bill.

The open circuits are the exception: they are written to
`circuits.json` in the state directory, because a gateway that restarts
during a provider outage must not call that provider again. The file holds
**metadata only** - a provider, a model, a failure class, counters and
timestamps - and it is written with the same atomic, owner-only rules as
the rest of the state.

## Request paths

### Browser configuration

1. A browser reaches `/owner/` on the configured canonical listener.
2. `GatewayApp.handle` rejects forwarded headers, invalid `Host`, and invalid
   `Origin` on browser mutations — before authentication runs, so a hostile
   cross-origin request never reaches a handler.
3. The pre-auth surface is exactly: the console shell and its assets,
   `GET /owner/api/auth/session`, `POST /owner/api/auth/setup` and
   `POST /owner/api/auth/login`. Every other `/owner/*` route requires a
   valid owner session cookie (`gw_owner_session`, `HttpOnly`,
   `SameSite=Strict`, `Path=/`, 12-hour TTL, validated server-side) and
   answers `401 authentication_required` before its handler runs — always,
   except while the owner has set `owner_console_auth=false`, which serves
   them without a session (`docs/OWNER_AUTH.md`); with the gate on, a
   streaming endpoint cannot emit a frame and a mutation cannot half-apply
   without one.
4. First run has no account: `ensure_setup_code()` (`src/gateway/owner_auth.py`)
   mints a random one-time setup code, prints it to server stdout and writes
   it to `owner_setup_code` (`0600` inside a `0700` data directory);
   `POST /owner/api/auth/setup` consumes it exactly once and deletes the
   file. No HTTP surface ever returns the code, and there is no default
   credential.
5. CSRF has no bootstrap token: `SameSite=Strict` stops the cookie being
   sent cross-site, and the canonical `Origin` check still runs on every
   cookie-authenticated browser mutation before authentication (the Host
   and forwarded-header checks run on every request, bearer included; a
   machine-key request is exempt from the Origin comparison only — see
   Machine-key automation). Sessions, the password and the setup code are
   never serialised into responses, logs, exports or `/version`.
6. The owner console may mutate configuration and enroll credentials on this
   listener. Provider tests and catalogue refreshes are explicit actions; no
   page load or ordinary configuration save calls a provider.
7. `GET /version`, `GET /health` and `GET /metrics` remain unauthenticated;
   `/v1/*` stays keyless until an owner sets a client API key
   (`POST /owner/api/auth/client-key`, owner session required unless the
   owner has set `owner_console_auth=false`).

**Deployed vs candidate.** The running gateway still serves the previous
boundary — no login, and a reachable LAN peer administers `/owner/*`
directly. The session boundary in steps 3-5 ships only with the
owner-session candidate this document describes; see `docs/OWNER_AUTH.md`.

### Machine-key automation

A machine key is the third credential (`docs/OWNER_AUTH.md`): a bearer
token presented on `/owner/*` for MCP and other automation. The
request path, in order:

1. `check_forwarded_headers`, then `check_host` — every request,
   machine included; there is no bearer exemption.
2. `check_origin` — returns early for any request that carries a
   bearer `Authorization` header, so a machine client (which has no
   browser Origin) skips only the Origin comparison; browser cookie
   mutations keep the exact-Origin check before authentication (the
   CSRF step above).
3. `_authorize` sees the bearer and dispatches to
   `_authorize_machine_key` — **never** to the session fallback:
   `MachineKeyStore.authenticate` resolves the key first (so every
   later refusal is attributable to that key in the access log and no
   refusal path can fall through to a cookie check), then a session
   cookie on the same request → `403 cookie_rejected`, then the
   surface check against `MACHINE_KEY_READ_PATHS` /
   `MACHINE_KEY_WRITE_PATHS` (else `403 machine_key_forbidden`), then
   the scope check (read key on a mutation → `403 scope_required`).
   An unknown key → `401 authentication_required`.
4. The handler sees a machine principal (`machine_key_authenticated`,
   `machine_key_scope`), never an owner session: provider actions
   `enroll_key`, `device_start` and `device_poll` answer `403
   machine_key_forbidden` (credential enrolment stays
   human-session-only), and the machine-key endpoints sit behind
   `_require_signed_in_owner` as a second, handler-level gate (the
   client-key endpoint is gated at the route level instead: without a
   bearer it needs the owner session, and a machine key on it is
   refused `403 machine_key_forbidden`).
5. `/v1/*` is a different surface: `_authorize` routes it to the
   client-key check first, where a *valid* machine key is refused
   with `401 invalid_api_key` — an owner credential and a model
   credential never cross.

State lives in `MachineKeyStore` (state map above): hash-only file,
separate stat-keyed pepper, re-read on change — so a revoke or a
rotate takes effect on the very next request, and a bad pepper fails
closed. The write scope reaches the nine mutation paths in
`MACHINE_KEY_WRITE_PATHS`, which is what makes the deliberate F1
`clear_cooldowns` acceptance (`docs/OWNER_AUTH.md`) reachable at
`scope=all` while every credential-enrolment path stays shut.

### Read-only request preview

`POST /owner/api/preview` accepts an OpenAI Chat Completions request body and
uses the same profile/alias authorization, route eligibility, request bridge,
model substitution, reasoning degradation, and derived session rules as
inference. It returns the primary currently eligible route's upstream payload
and metadata without invoking an adapter. It does not create ledger events;
normal metadata-only access logging records the endpoint path/status, never the
body. Since provider failures cannot be predicted without a provider call,
fallbacks after an upstream error are explicitly not evaluated.

### Client inference

1. A LAN client calls `GET /v1/models`, `GET /v1/alias-catalog`,
   `POST /v1/chat/completions`, or `POST /v1/responses`.
2. `ProfileAuth` resolves every LAN request to a shared anonymous trusted-LAN
   profile. Legacy profile/token metadata does not provide access control.
   This profile resolution is not the model-API key boundary: `/v1/*` is
   keyless until an owner sets a client key, and once one is set all seven
   `/v1` routes require `Authorization: Bearer <key>` before the handler
   runs (the owner session never substitutes for it).
3. The request model must name a published alias; any reachable peer may
   invoke each published route while the surface is keyless.
4. `Router` evaluates the primary route followed by configured fallbacks. A
   candidate must have a configured adapter, an enabled known model, a known
   supported dialect, matching capabilities, and no active local cooldown.
5. The bridge converts between Chat Completions and Responses only when the
   target capability permits it. Unsupported capability/dialect combinations
   fail closed.
6. The adapter makes the provider request. The returned provider/model is
   attributed to the route result. A stream is forwarded incrementally.

### Native Jev / System One

`GET /v1/systemone/models` returns only the owner-enabled models from the
saved OpenCode Console Jev catalogue and never calls OpenCode Console. `POST /v1/systemone`
accepts a JSON-only native System One request: a model, a string/object/array
state, and non-empty named `noul`, `choice`, or `score` questions. It checks
that the model was explicitly enabled, sends the payload to OpenCode Console, and
returns the native structured response. It is intentionally not an alias,
chat-completions, or Responses route.

The native System One boundary accepts JSON only and refuses image assets
and `box` questions. State, questions, and answers are request content: they
are never persisted in configuration, request logs, or the ledger. Separately,
Jev can infer a phase for an alias only when that alias explicitly enables the
phase-routing policy below. This bounded inference uses request text and is
not a general completion router.

### The upstream conversation identifier

Some upstreams require a conversation identifier. OpenCode Go refuses a
request without `x-opencode-session`, and most OpenAI-compatible clients
send no such header, so the gateway cannot pass one through.

`gateway.session` derives it server-side: stable for the same client and
alias, different between clients, reduced to a safe bounded alphabet when
the client supplies a hint, namespaced by provider in the router, and scoped
to the process so a restart ends the session. The derivation lives in the
**router**, not in a handler, so every path that reaches a provider carries
one - the API surface, the alias test, and any future caller.

### Hard requirements and preferences

The eligibility check separates two kinds of mismatch:

* a **hard requirement** - wire dialect, streaming, function tools, and
  **image input**. A route that cannot satisfy it is excluded, because
  dropping a tool would change the API contract, and a model that cannot see
  an image cannot answer a question about it. Image detection reuses the
  bridge's own content parsers, so routing and conversion can never disagree
  about which fields carry an image.
* a **preference** - `reasoning_effort`. A model that cannot produce the
  requested depth of reasoning can still answer, so the route stays
  eligible, the effort is dropped *for that route*, and the substitution is
  recorded in the attribution and the ledger. It is never silent, and only
  the route that actually served the request records it.

Most OpenCode Go models publish no reasoning levels, so treating the effort
as a hard requirement excluded every one of them.

### A streaming-only upstream

The ChatGPT / Codex backend refuses a Responses request that does not set
`stream: true`. A client that asked for a single, non-streamed completion is
therefore served by consuming the upstream stream and presenting the result as
one object.

`gateway.bridge.aggregate` does that: a Responses stream aggregates into the
final Responses object - the terminal `response.completed` event carries the
whole thing - and a Chat Completions stream accumulates into one object. The
result is then converted into the caller's dialect as usual.

### An upstream that dies mid-answer

Routing records the outcome before the answer has been delivered, so a
provider that fails *while the stream is being consumed* would leave a
record that reads success. `gateway.http.app` amends that record through
`gateway.core.abort.classify_late_error` — the class the adapter raised,
or `client_abort` when the caller had already left — so it stays a
member of `ERROR_CLASSES` and circuits and cooldowns consume it. The
streaming client receives exactly one in-band
`data: {"error": {"message": ..., "type": ...}}` frame after the last
delivered frame and then a clean `0\r\n\r\n`, with **no**
`data: [DONE]`: the status really was 200, and `[DONE]` means a complete
answer. Only `AdapterError` is handled — logged at ERROR with its
traceback — so any other exception still reaches
`socketserver.handle_error` untouched.

### Frame framing

An SSE frame is its data lines **plus** the blank line that ends it. The
upstream reader yields complete frames, and every emitted frame is
re-terminated. A stream without the terminator is read by a strict client as
one event with no finish reason.

The HTTP stream writer emits a gateway-owned `: ping` SSE comment after 15
seconds without client-facing output. It wraps the already-converted stream,
so keepalives never enter upstream frames or dialect conversion. Each comment
is sent as its own HTTP chunk and does not alter event framing. The interval
is fixed; clients may ignore SSE comments as usual. Regression coverage is in
`tests/test_sse_keepalive.py`; `tests/test_sse_framing.py` still verifies
upstream frame separators.

### Response segments

A buffered response leaves the process in **one** segment: the status line,
the header block and the body are written together, and each streamed
frame is a single write.

This is not a micro-optimisation. Written as two segments, Nagle's
algorithm holds the body until the header block is acknowledged, and the
client's TCP delays that acknowledgement. Every response of about one MSS
or less - every `/health`, every `/v1/models`, and every short
non-streaming completion - then paid a **41ms stall**, on every request
after the first on a connection. The accepted socket also sets
TCP_NODELAY, so a response never depends on the client's acknowledgement
timing at all. See `REVIEW.md` R28.

### Reported limits

A route's context limit is the **smallest applicable constraint**: the
provider's working window (not the ceiling it publishes for a config
override), less the share it reserves for its own system prompt, tool
overhead and output where the provider publishes one. At the alias level it
is the smallest limit among the routes that could serve the request, and
**Unknown** if any eligible route's limit is not published - a bound that
cannot be verified is not a bound.

OpenCode Go's `/v1/models` response lists IDs only. Its official endpoint
table identifies the exact model behind a documented route, so the snapshot
may use a context window from that *model provider's* official documentation
when both links are established. It currently records MiMo-V2.6-Flash at
1,048,576 tokens (with 131,072 maximum output) and MiniMax M3.1 Flash at
1,000,000 tokens. A Go price tier is not a window, and a model lacking this
evidence remains Unknown.

The limit is reported, not enforced. The gateway has no tokenizer, so it does
not count a prompt's tokens; the provider is the authority.

### The OpenCode selector catalogue

`GET /v1/alias-catalog` exposes that same alias-level rule to a client that
needs it before it can offer a model: the context limit (or its Unknown
reason), the primary route's published reasoning levels, whether every route
supports function tools, and whether any route supports image input. The image
flag uses an any-route rule because the router enforces the capability per
request: for an image request, text-only routes are excluded with a recorded
reason, and the gateway fails closed if no capable route remains. It carries
no provider inventory, no ledger, no credential and no unpublished route, and
it makes no provider call.

`integrations/opencode/` is an OpenCode V2 provider plugin that reads it and
publishes the aliases into `/models`. It uses a documented alias limit when
one exists. Only where the gateway still reports Unknown does it apply an
**owner-chosen 200,000-token client-side budget** and say so in the model's
display name; that budget is OpenCode's own context-management input, not a
claim about a provider's window.

## Provider seam

The routing core knows aliases, error classes, model capabilities, and fallback
policy. Provider-specific behaviour lives in `gateway.adapters`.

Every adapter owns one `UpstreamClient`, which keeps a **bounded keep-alive
connection pool** keyed by origin (`adapters/http_client.py`). A short
non-streaming request therefore reuses the previous connection instead of
paying a fresh TCP and TLS handshake before the provider does any work. The
existing guarantees are unchanged: redirects are refused, a credential never
appears in an exception, and the configured timeout applies to a reused
socket. Timeouts are **two budgets**, not one: the **connect budget** (10s)
bounds establishing a connection - the TCP handshake and, over TLS, the
handshake that follows it - and the head of a streamed response, whose head
precedes generation; the generous **read budget** (120s, per read) bounds
every wait for the provider's own bytes, so a provider that accepts and
goes quiet is abandoned in seconds while a response that is still arriving
is never cut. A connection the peer closed while idle is discarded before the
request is written, so a request is never replayed. On shutdown
`AdapterRegistry.close()` releases the pooled sockets, with an `atexit`
backstop for a process that exits without it.

* **ChatGPT / Codex** uses the device-code flow and the Responses dialect.
  Its upstream is **streaming only** and expects the same request fields
  OpenAI's own client sends. See `CHATGPT_CODEX_CONTRACT.md` and
  `PROVIDER_CONTRACTS.md` for the exact contract.
* **Anthropic** uses the native Messages dialect and a provider-scoped API-key credential file. Model capabilities and limits are not inferred; catalogue refresh and connection tests are explicit actions. The Anthropic request/response bridge serves Anthropic-dialect models through Chat Completions and Responses, and `POST /v1/messages` converts in the reverse direction for OpenAI-dialect routes. Converted fields without a faithful representation are refused.
* **OpenCode Go** discovers model IDs from its explicit catalogue endpoint.
  The checked-in `data/go_catalog.json` supplies the dated, published mapping
  from model ID to wire dialect, context and output windows, token prices,
  monthly allowance and image verdict — each row naming the page it was
  transcribed from and the date it was read. An undocumented dialect is
  refused rather than guessed.
* **Ollama, llama.cpp server and LM Studio** are one generic
  `LocalOpenAIAdapter` (`adapters/local_openai.py`) behind three fixed
  provider ids — `ollama`, `llama_cpp`, `lm_studio` — all speaking the same
  OpenAI-compatible `/v1` surface. They are the gateway's **only
  owner-configured outbound destination**, so they carry an egress boundary
  none of the cloud adapters need (`adapters/local_policy.py`):

  * the endpoint is **read from the config store on every request**, so a
    saved URL takes effect without a restart and nothing is dialed at boot —
    a fresh or endpointless provider is `not_configured` and is never
    contacted;
  * **save-time** normalisation (`normalize_base_url`) refuses anything that
    is not a plain `http`/`https` origin with no userinfo, query or fragment
    and a short plain path, whose host is an IP literal or resolves to
    *nothing but* addresses inside `127.0.0.0/8`, `10/8`, `172.16/12`,
    `192.168/16` or `fc00::/7` — public, link-local/metadata
    (`169.254/16`, `fe80::/10`), multicast, unspecified and broadcast are
    refused, and a mixed public/private DNS answer is refused rather than
    averaged;
  * **connect-time**, the socket's actual peer is read after the TCP
    handshake and **before any byte of the request is written** — the check
    is installed as the connection object's socket factory, which puts it
    before the TLS handshake too, so a refusal on `https://` costs a TCP
    connection and no SNI, no ClientHello and no credential — with a
    second check after `connect()` as the backstop on a Python that does not
    expose the factory hook. The save-time answer can go stale by design
    (DNS changes); the peer check is what makes a rebound hostname safe;
  * **redirects are never followed**, to a private or a public address, so a
    credential is never replayed elsewhere;
  * the endpoint is written only by the owner-only `set_base_url` provider
    action (401 without a session unless the owner has set
    `owner_console_auth=false`), and its refusal reasons are fixed
    strings that never echo the refused value;
  * **keyless by default** — no `Authorization` header at all unless the
    owner enrols an optional local token for a server that demands one — a
    documented decision (`docs/TODO.md` T15);
  * **capabilities and cost fail closed until evidenced**: `/v1/models`
    publishes no capability evidence and no tariff, so tools and image input
    start `False` and move only on owner approval (or Ollama's documented
    `POST /api/show`), and cost is never a number.

Adding a provider requires an adapter, model-capability handling, safe error
classification, registry registration, UI/state treatment, tests, and an
explicit credential-isolation review. It is not just a URL plus an API key.

## Routing and fallback policy

Fallback does not happen by default. A fallback requires an alias-level
authorization, confirmation of the next provider, and an enabled trigger.
A tool-bearing request never falls back from a route that **received** it —
a failed attempt may have executed it. A skipped primary never reached a
provider, so a skip is decided by the owner controls and the skip's trigger
alone. Authentication, policy, and malformed
request failures also fail closed unless the owner explicitly configures a
safe policy for that failure class. A plain HTTP 429 is rate limiting, not a
confirmed allowance exhaustion.

A trigger names *why* the owner permits a switch. The enabled vocabulary is
`exhausted`, `rate_limited`, `upstream_unavailable`, `explicit_cooldown`,
`route_ineligible` and `probe_contention`. The gate covers a **skipped**
primary as well as a failed one: when the primary is excluded while routes
are enumerated, or its invocation-boundary claim is refused, the switch to
a later cross-provider route still needs the authorization, the confirmation
of the next provider, and an enabled trigger. The skip itself contributes
the trigger - a capability or configuration exclusion is `route_ineligible`;
an automatic cooldown keeps the failure trigger that opened it; an
operator-placed cooldown is `explicit_cooldown`; a route held by a live
half-open recovery probe is `probe_contention`, its own class so a manual
cooldown can never be reported as an automatic one. When the gate declines,
the request is refused with `fallback not used: <reason>` and the ledger
records it as served by nobody, without claiming a fallback. The same bounded
refusal reason is retained in `fallback_reason` even when `fallback_used` is
false, so an owner can diagnose a policy refusal from the durable ledger.
Owner state warns loudly when an authorized cross-provider fallback omits any
trigger, and when a newly added trigger is enabled by no alias; the console
renders these warnings as alerts. A phase-selected
route is the primary for its phase, never a fallback, and needs none of this.

The router only decides a fallback before it returns an upstream response. It
does not retry a stream after the caller has begun receiving bytes.

Every terminal path - served, failed, refused conversion - records the same
attribution. `serving_provider` / `serving_model` name the last route tried:
the serving route on a success, the route the failure came from otherwise, so
a 502 is attributable in the durable ledger and in the access-log line. When
a provider rejects a request's shape and names the fields it refused, those
names are carried on the attempt (`rejected_fields`, and the suffix on
`reason`) - and only those names: an upstream body is provider text and can
echo request content, so it is never copied into an error, a log line or the
ledger.

### Cooldowns

A route that fails in a way the owner has enabled a trigger for is put
into a **local cooldown**: it is skipped for a while, and the skip is
recorded with its reason, so a cooldown can never look like a silent
fallback.

A cooldown is a **local retry policy, never evidence that the provider
reset**. There are no background probes and no health checks.

The window is the **longer** of two answers: the alias's own
`cooldown_seconds`, and the provider's `Retry-After` when it sends one.
The provider is the only party that knows how long its own allowance takes
to recover, so its answer is honoured - but it is a value from the
network, so it is bounded to an hour, and a value that is unparseable,
negative or already in the past is ignored in favour of the local policy.
The cooldown records which of the two it used, and the skip says so.

The state lives in `gateway.circuits` and is **remembered on disk**, in
`circuits.json` in the state directory. A gateway that restarts during an
outage stays out of the way, which is exactly when the cooldown is worth
most.

* It is keyed by **route** - provider *and* model - so one broken model
  does not hold the others at the same provider.
* A **confirmed exhaustion** is account-scoped: it covers the whole
  provider, because the account's allowance is gone rather than one
  route's share of it. Its window is **bounded** - a floor of 15 minutes
  and a cap of 30 minutes by default (`EXHAUSTED_BASE_SECONDS` and
  `EXHAUSTED_MAX_SECONDS`, configurable at startup with
  `GATEWAY_EXHAUSTED_COOLDOWN_MAX_SECONDS`) - and it does **not** grow
  exponentially, however many failures have been seen. A subscription
  window reopens on the provider's schedule, so a local hold must never
  strand reopened capacity behind a multi-hour circuit.
* A **rate limit** opens the route for as long as the provider asked.
* A **transient** failure is a blip. It opens the route only after a
  pattern of consecutive failures, and a success resets the count. Its
  exponential window is capped at one hour by default
  (`TRANSIENT_BACKOFF_MAX_SECONDS`, configurable at startup with
  `GATEWAY_TRANSIENT_COOLDOWN_MAX_SECONDS`); the exponential below the cap
  is unchanged.
* The window **escalates** with consecutive failures up to the configured
  transient cap, for every class except confirmed exhaustion (bounded
  separately) and rate limits (which honour an upstream `Retry-After` even
  when it exceeds their local cap).
* When a window ends the circuit is **half-open**: exactly one request is
  let through to find out whether the provider recovered. A success
  closes it; a failure re-arms it within the bounded window and keeps the
  failure class and count. A probe that never returns is reclaimed after
  five minutes, and a probe is never remembered across a restart. The
  probe is **claimed at the invocation boundary**, immediately before a
  provider is called - never while candidates are merely being
  enumerated - and the claim carries an owner token so that only the
  request that took it can release it. A candidate that is skipped
  before invocation (an image the primary cannot see, a phase route
  that was not chosen, a refused conversion) claims nothing.
* The owner may **clear one provider's cooldowns deliberately** from the
  console or `POST /owner/api/provider` with
  `{"action": "clear_cooldowns", "provider_id": "..."}` - a trusted-LAN
  mutation with the same canonical-Origin protection as every other
  owner action. It returns the cleared entries (metadata only) and never
  touches another provider's circuits. A provider call happens only when
  the owner also names a `probe_alias`.
* The owner state (`GET /owner/api/state`) reports, per circuit, the
  original failure timestamp, scope, failure count, current hold,
  next eligible probe, and whether a probe is in flight; the effective
  policy is reported as `cooldown_policy`.
* The stored state is **metadata only**, and it is forgotten after a week.

## Phase routing (opt-in)

An alias may carry an optional, **off-by-default** phase-routing policy. A
phase is one of a closed set - `plan`, `implement`, `validate`. The policy
maps each phase to an owner-configured route and may optionally let the
gateway ask Jev to infer the phase when the caller supplies none.

The precedence is fixed:

1. A **caller-supplied phase** wins. It is read from the gateway-owned
   request field `metadata.personal_gateway.phase`, validated against the
   closed vocabulary, and the **entire `personal_gateway` block is stripped**
   before the request is converted or forwarded - no upstream provider ever
   sees it. An invalid or unknown value is ignored, not refused, so a stale
   hint still gets the ordinary route.
2. Otherwise, **Jev inference** runs only if the alias enabled it *and* the
   request is safe to classify: no tools, not a continuation, and it carries
   text. The gateway sends Jev a single bounded, closed-set `choice` question
   it owns (never caller-authored), using a short, bounded slice of the
   request text as state.
3. Otherwise the ordinary primary (or the policy's configured `default_route`)
   is used.

A Jev choice applies only when it names a **configured phase** and its
confidence meets the alias's `minimum_confidence`. Anything else - low
confidence, an unknown choice, a non-choice answer, a Jev error or timeout -
uses the ordinary route and records why. A Jev failure never fails the
completion request.

A phase-selected route is the **primary for that phase**, not a fallback: it
never sets `fallback_used`, and no fallback authorization is required. Phase
resolution happens before the route loop, so a phase decision never occurs
after a stream has begun or after a tool may have executed.

The request ledger records the phase and its source (`caller`, `jev`, or
`default`) as **metadata only** - never the request content, the Jev question
text, or the answer distribution.

## Request policy (opt-in)

An alias may carry an optional, **off-by-default** request policy: owner
`defaults` for values the client did not specify, `caps` that clamp what
the client did specify, and `deny` rules that refuse or force. The block
is configuration only, validated at parse time, and applied by
`gateway.policy` in the router - one place every provider-reaching path
passes through (the API surface, the alias test, any future caller) -
**before** dialect conversion, in this order:

1. the client's value,
2. the owner's default, if the client gave none,
3. the owner's cap, if it clamps the value now standing,
4. the deny rules.

The owner writes one vocabulary regardless of the dialect a route
serves: `temperature`, `max_output_tokens`, `thinking_level` (the
canonical set in `gateway.thinking`; a level outside it is dropped at
parse time, so the configuration can never claim a second vocabulary)
and `system`. The thinking level is read from and written to the source
dialect's own spelling, so the flat and the nested spelling of one
request can never disagree, and the cap is ordinal in the canonical
order: a level the gateway cannot order does not pass the ceiling. With
no cap configured, an unknown level forwards, as the thinking vocabulary
requires.

Every value the policy changes is recorded in the attribution and the
ledger against the route that **actually served** the request, in the
same form as a dropped reasoning effort, tagged `defaulted`, `clamped`
or `forced`. The `system` instruction is recorded as its length in
characters, never as text: the ledger is metadata.

`deny.tools` refuses the request with a 403 that names the rule,
**before** any provider work happens - never a silently stripped tool
list. `deny.store` forces the retention flag on every upstream payload;
it is re-asserted after conversion because the bridge does not model
`store`, so the force holds on a converted route exactly as it does
same-dialect.

## Release identity

`GET /version` returns exactly five keys: `service`, `source_revision`,
`source_tree`, `image_revision`, and `mcp_contract_version`. The first two
revisions are build inputs;
the image revision is read from `release.json` in the data directory only when
the release step recorded that digest for the container that is currently
running — the record carries the container id, and the running id is resolved
read-only from the process mount table. Anything unbound (an earlier
container's record, no binding, no record) is reported as `unknown`. The
endpoint must never become a generic environment or configuration inspector.

## Alias groups

Aliases may carry an optional validated tag for owner convenience. The owner can publish or disable all aliases with a tag in one configuration transaction. Group operations change only each alias's enabled flag; routes, fallback authorization and chains, and any per-alias policy or budget remain independent. Publishing a group refuses the whole operation if any member lacks a primary route.

The two halves come from different places, so they can disagree: the source
revision is baked into whatever image is running, while the image revision
comes from the state volume, which a release step writes and an ordinary
start does not. After a rollback - an old image started against a released
state volume - `/version` reports the *old* source with the *previous*
release's digest, and the startup warning does not fire, because neither
field says `unknown`. The release identity therefore names an exact image
only when the deployment was made by the release step; after any hand-rolled
start or rollback the operator must re-run it.

## Verification boundaries

The unit suite uses fakes and fixtures; it does not authenticate with a
provider or require credentials. It is not a substitute for probing the real
upstream, because the upstream's request and response *shape* is where the
defects have been. See `PROVIDER_CONTRACTS.md` for the contracts that were
established by probing, and for how to probe safely.

A complete release verification still needs an exact-image gate and a
Compose/runtime proof, especially for the writable state-volume ownership and
listener boundaries. See `DEVELOPMENT.md` and the open findings in
`REVIEW.md`.


## Capability discovery foundation (staged)

The capability discovery API (`GET /owner/api/capabilities`, `POST /owner/api/capabilities/preview`, `/run`, and `/decision`) is opt-in and exact-provider/model scoped. Preview validates a closed MVP probe vocabulary and returns fixed request/output/deadline bounds; it reports the catalogue's published tariff honestly (rates, source and observation date — a priced model is never rendered as "no published price") and refuses paid execution with 422 until a reviewed provider transport contract is registered. No production adapters are enabled for any other pair. Synthetic results create sanitized proposals only in isolated tests.

**One reviewed probe protocol is registered: `opencode_go` / `mimo-v2.6-flash`** (`gateway/capability_probe.py`, `PROBE_PROTOCOLS`); every other provider/model pair keeps the answer above and no request is sent. The run path (`POST /run`) is fail-closed at every gate and names the gate that refused it: reviewed pair, connected provider, a fresh published tariff (unknown or stale pricing fails closed), the owner's explicit route authorization and estimate acceptance (a `hard_cap_required` policy is refused because no provider-enforced ceiling is published), a spend ceiling that covers the worst case, the single-use confirmation issued by the preview, and a one-run concurrency gate. It then executes at most two one-shot, non-streamed, non-retried, never-failed-over requests, bounded by request bytes, image bytes, output tokens, a per-request timeout and a run deadline, cancellable through `POST /probe/cancel`, and never executes a returned tool call. Evidence is written by `DiscoveryStore.record_run` to a closed sanitized shape — capability, status, method, error class, bounded request count, a bounded gateway-authored summary, reported token counts, tariff estimate — as a run plus an **unselected** proposal. A run changes no configuration, routing, cooldown or ledger; the owner's field decision is what writes metadata. Synthetic isolated results remain a test seam labelled `synthetic_isolated_fake`.

Decisions are field-level and require the proposal's base config revision and model fingerprint; approved image/function-tool values and provenance persist in config and catalogue refresh reapplies those values. Exact retries return the saved decision without another config mutation. Partial decisions leave unselected fields pending and rebase the proposal CAS to the resulting model state for the next explicit action. Stale compare-and-swap marks the proposal conflicted. Rejected and unselected fields do not change effective metadata. Any reachable trusted-LAN peer may make these explicit decisions under the product's access-by-reachability administrator policy; this is not individual authentication and Host/Origin checks are not identity. See `MODEL_CAPABILITY_DISCOVERY_SPEC.md` for staged limitations.

### Local alias budgets

Alias budgets are optional monthly USD caps. `gateway.budget` derives spend from metadata-only successful request ledger events and `gateway.costs.calculate_cost`, using published catalogue prices only. Any unpriced/missing usage or incomplete ledger history makes the total Unknown and disables enforcement (never interpreted as zero). The API checks before routing and records actual token usage after completion; concurrent requests may overshoot. The UTC reset is the first instant of the next calendar month.

## Capability discovery foundation (staged)

This section is duplicated above; see [Capability discovery foundation
(staged)](#capability-discovery-foundation-staged) for the current text,
including the one registered probe protocol and the run gates.
