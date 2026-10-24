# Architecture

This document describes the code that is in this repository today. It is an
orientation document, not a statement that every future provider flow is
supported or production-ready.

## Runtime topology

The gateway starts one standard-library HTTP server from `gateway.cli`:

| Listener | Default bind | Mounts | Purpose |
|---|---|---|---|
| Trusted LAN | explicit private IP, port `8765` | `/owner/`, `/v1/`, `/health`, `/version` | direct owner console/configuration and anonymous OpenAI-compatible API |

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

The following state is intentionally in-memory only and disappears on restart:

* in-flight ChatGPT/Codex device authorization state;
* limit snapshots.

Request outcomes are also appended asynchronously to `request-ledger.jsonl`
under the state directory. Expired records are physically pruned on load and before rotation; the append-only metadata log is rotated at 5 MiB
with two rotated files (15 MiB maximum) and records older than 30 days are
ignored when loading. The bounded in-memory ring remains the live hot view;
records are restored after restart. This is operational metadata, not a
complete audit trail and **not a billing system**. A full write queue or
storage error may drop a persistence update; the request path does not wait
for disk I/O.

The Status view reports whether persistence is enabled and the declared
30-day retention window. Local usage aggregates cover the last 24 hours and
are computed from token counters and published prices, with source and
observation date; a missing price remains Unknown. Because aggregation uses
the bounded recent-event ring, evictions that may include in-window events
mark cost estimates Unknown rather than presenting a partial sum. This
estimate is not a provider bill. Totals are reconstructed from the restored
ring, not a lifetime accounting source.

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
   `Origin` on browser mutations. Direct requests from a reachable LAN peer are
   administrators by product decision.
3. Same-origin owner mutations require no password, session cookie, or CSRF
   bootstrap token. Cross-origin browser mutations remain refused.
4. The owner console may mutate configuration and enroll credentials on this
   listener. Provider tests and catalogue refreshes are explicit actions; no
   page load or ordinary configuration save calls a provider.

### Client inference

1. A LAN client calls `GET /v1/models`, `GET /v1/alias-catalog`,
   `POST /v1/chat/completions`, or `POST /v1/responses`.
2. `ProfileAuth` resolves every LAN request to a shared anonymous trusted-LAN
   profile. Legacy profile/token metadata does not provide access control.
3. The request model must name a published alias; any reachable peer may invoke
   each published route.
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

### Frame framing

An SSE frame is its data lines **plus** the blank line that ends it. The
upstream reader yields complete frames, and every emitted frame is
re-terminated. A stream without the terminator is read by a strict client as
one event with no finish reason.

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

* **ChatGPT / Codex** uses the device-code flow and the Responses dialect.
  Its upstream is **streaming only** and expects the same request fields
  OpenAI's own client sends. See `CHATGPT_CODEX_CONTRACT.md` and
  `PROVIDER_CONTRACTS.md` for the exact contract.
* **Anthropic** uses the native Messages dialect and a provider-scoped API-key credential file. Model capabilities and limits are not inferred; catalogue refresh and connection tests are explicit actions.
* **OpenCode Go** discovers model IDs from its explicit catalogue endpoint.
  The checked-in `data/go_catalog.json` supplies the dated, published mapping
  from model ID to wire dialect. An undocumented dialect is refused rather
  than guessed.

Adding a provider requires an adapter, model-capability handling, safe error
classification, registry registration, UI/state treatment, tests, and an
explicit credential-isolation review. It is not just a URL plus an API key.

## Routing and fallback policy

Fallback does not happen by default. A fallback requires an alias-level
authorization, confirmation of the next provider, and an enabled trigger.
Tool-bearing requests never fall back. Authentication, policy, and malformed
request failures also fail closed unless the owner explicitly configures a
safe policy for that failure class. A plain HTTP 429 is rate limiting, not a
confirmed allowance exhaustion.

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
* A **confirmed exhaustion** is account-scoped: it opens for at least an
  hour and covers the whole provider, because the account's allowance is
  gone rather than one route's share of it.
* A **rate limit** opens the route for as long as the provider asked.
* A **transient** failure is a blip. It opens the route only after a
  pattern of consecutive failures, and a success resets the count.
* The window **escalates** with consecutive failures, up to a day.
* When a window ends the circuit is **half-open**: exactly one request is
  let through to find out whether the provider recovered. A success closes
  it; a failure reopens it for longer. A probe that never returns is
  reclaimed after five minutes, and a probe is never remembered across a
  restart.
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

## Release identity

`GET /version` returns exactly four keys: `service`, `source_revision`,
`source_tree`, and `image_revision`. The first two revisions are build inputs;
the image revision is read from `release.json` in the data directory when a
release step has written a digest-shaped value. The endpoint must never become
a generic environment or configuration inspector.

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
