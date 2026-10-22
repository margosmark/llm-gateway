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
same listener. `cli.py` refuses wildcard and non-private binds. No setup
listener or setup-port setting exists. Forwarded headers are rejected rather
than trusted.
The gateway port is configurable, but `8766` is reserved and rejected so the
retired setup port cannot be reused.

## State boundaries

`gateway.config.ConfigStore` persists the non-secret configuration as
`config.json`, with atomic replace, mode `0600`, and a monotonic revision.
It contains provider state, model metadata, aliases/routes, profile metadata,
and access mode. It must never contain a credential or caller-token hash.

`gateway.credentials.CredentialStore` persists separate JSON files under a
private `credentials/` directory. Provider adapters receive a handle scoped to
one provider. Caller-token hashes also use that store. A read of a credential
is available only to internal adapter/auth code; console state renders presence,
not values.

The following state is intentionally in-memory only and disappears on restart:

* in-flight ChatGPT/Codex device authorization state;
* request ledger events and totals;
* limit snapshots.

This means the Status page is operationally useful but is **not** a durable
audit or billing system.

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

* a **hard requirement** - wire dialect, streaming, function tools. A route
  that cannot satisfy it is excluded, because dropping a tool would change
  the API contract.
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
overhead and output. At the alias level it is the smallest limit among the
routes that could serve the request, and **Unknown** if any eligible route's
limit is not published - a bound that cannot be verified is not a bound.

The limit is reported, not enforced. The gateway has no tokenizer, so it does
not count a prompt's tokens; the provider is the authority.

### The OpenCode selector catalogue

`GET /v1/alias-catalog` exposes that same alias-level rule to a client that
needs it before it can offer a model: the context limit (or its Unknown
reason), the primary route's published reasoning levels, and whether every
route supports function tools. It carries no provider inventory, no ledger,
no credential and no unpublished route, and it makes no provider call.

`integrations/opencode/` is an OpenCode V2 provider plugin that reads it and
publishes the aliases into `/models`. Where the gateway reports Unknown, the
plugin applies an **owner-chosen 200,000-token client-side budget** and says
so in the model's display name; that budget is OpenCode's own context
management input, not a claim about any provider's window, and the gateway's
answer stays Unknown.

## Provider seam

The routing core knows aliases, error classes, model capabilities, and fallback
policy. Provider-specific behaviour lives in `gateway.adapters`.

* **ChatGPT / Codex** uses the device-code flow and the Responses dialect.
  Its upstream is **streaming only** and expects the same request fields
  OpenAI's own client sends. See `CHATGPT_CODEX_CONTRACT.md` and
  `PROVIDER_CONTRACTS.md` for the exact contract.
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

## Release identity

`GET /version` returns exactly four keys: `service`, `source_revision`,
`source_tree`, and `image_revision`. The first two revisions are build inputs;
the image revision is read from `release.json` in the data directory when a
release step has written a digest-shaped value. The endpoint must never become
a generic environment or configuration inspector.

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
