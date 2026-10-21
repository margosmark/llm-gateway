# Roadmap and open work

The register of work this project has agreed to do next. Every item is
derived from a fact in this repository — a code path, a catalogue field, a
documented provider contract — and every item says what "done" means
before it is started.

Findings that describe **defects** live in [`REVIEW.md`](REVIEW.md) and are
referenced from here. This file is the **plan**.

## How to read the status column

| Status | Meaning |
|---|---|
| `todo` | Agreed, not started. |
| `doing` | In progress. |
| `blocked` | Waiting on something outside the repository — a provider contract, an owner decision. |
| `done` | Landed, with its regression test and its documentation update. |

## Definitions of done

These are the repository's existing rules, restated because every item
below inherits them:

1. **A regression test for every defect**, and for every new capability a
   refusal test as well as the happy path.
2. **No real credentials, provider accounts or network access** in the test
   suite. Tests use the adapter seam and synthetic values.
3. **Update the README, runbook, architecture/contract docs** when a
   behaviour, endpoint, UI label, security invariant or deployment procedure
   changes.
4. **Close a finding in `REVIEW.md` only with a cited fix and regression
   evidence.**
5. **Never silently drop or invent.** A value the target dialect cannot
   express is refused or dropped *with the substitution recorded* — never
   discarded quietly. This is the rule that R19 turned into a capability /
   preference distinction, and it applies to every conversion.
6. **Unknown is a valid answer.** Provider entitlement, limits, budgets and
   undocumented capabilities stay Unknown unless backed by official
   evidence. Do not fill a gap from a vendor's marketing page or from
   another project.

## Non-negotiable boundaries

These are product decisions, not gaps. Do not close an item below by
adding one of them.

* No owner passphrase, login page, session cookie, setup listener, or
  reserved port `8766`. **Any device that can reach the configured address
  is an administrator and a caller, by decision.**
* No TLS terminator, proxy trust model, forwarded-header acceptance,
  wildcard/public bind, firewall or router rule, or public exposure.
* No implicit provider calls. Saving configuration, loading a page or
  reading status never calls a provider.
* No new runtime dependency, external database, or background probe.
* Profiles stay configuration and attribution objects. They are **not**
  access control, and they must not be presented as if they were.

---

## Tier 1 — functional gaps

### T1 — Refresh the README's Known Limitations — `done`

**Landed.** The three claims that cited R1, R8 and R9 as open release
blockers are gone - all three findings are closed, and the behaviour they
described is implemented and tested. The duplicated bullets are merged, and
the section now carries the limitations that are true today: the
non-OpenAI dialects, the Unknown ChatGPT allowance, the beta device-code
path, no TLS, no authentication boundary, the recorded bridge corpus, the
Go models without a documented window, and the fact that a local cost is an
estimate from published prices rather than a bill (T2).

**Why.** The README states that two features are *not ready* and cites R1,
R8 and R9 as open release blockers. All three are **closed** in
[`REVIEW.md`](REVIEW.md) with passing tests (`tests/test_device_flow.py`,
`tests/test_adapter_contract.py`, `tests/test_buffered_bridge.py`), and the
console does complete the device-code poll (`static/console.js:528-571`).
The README also **duplicates two bullets verbatim** — "ChatGPT limits are
Unknown" and "the device-code path is a beta path".

This matters more than a stale list usually would: the README is the
product contract, and it currently understates what works while pointing a
reader at findings that are already closed.

**Done when.** Every limitation maps to an open finding or to a fact
observed in this repository; no bullet is duplicated; each citation points
at the right finding; and each claim has been re-read against the code that
implements it.

### T2 — Local usage and cost accounting — `done`

**Landed.** The dated Go catalogue snapshot is now read for its published
per-token prices, and every price carries the source it was published at and
the date it was observed. Usage is extracted on both paths — the buffered
response and the streamed one — and amended into the recorded request, so a
request's record carries its own token counts. The Status page shows the
local estimate per request and the last 24 hours aggregated by alias,
provider and model.

**Rules kept.** Metadata only — counts and published prices, never content.
The price's source and as-of date sit next to the number. A model with no
published price reports **Unknown**, never `0` and never "unlimited", and a
price is only applied to the provider that published it. The provider's
allowance is still Unknown: the gateway has no entitlement to it and does
not claim one. The aggregates survive a restart, because T7 landed with it.

**Landed with it.** The request identifier is now unique per request
(`observability.new_request_id`). The old `req_<millisecond>` was not: two
requests inside one millisecond shared one identifier, and the ledger amends
a record by that identifier — so one request's usage could be written onto
another's. The ledger now also refuses to amend an identifier that matches
more than one record. See `docs/REVIEW.md` R38.

**Evidence.** `src/gateway/costs.py`; `tests/test_costs.py` (the price's
provenance against the snapshot itself, the arithmetic, and the
no-published-price case); `tests/test_end_to_end.py` (the counters on the
buffered and the streamed path); `tests/test_request_ids.py` (identifier
uniqueness, concurrency, the durable round trip, and per-request
attribution). Full suite passed (463 tests, 2 skipped) on the rebased tree.

### T3 — Enforce the capabilities that are declared but never checked — `partial`

**Why.** `core/routing.py` used to check `supports_function_tools` alone.
But `supports_image_input`, `supports_builtin_tools` and
`supports_previous_response_id` are carried through the config, the model
entry, the state API and the console (`core/capabilities.py:60-64`) and were
then **never consulted**.

So a route whose model publishes `supports_image_input: false` could be
selected for an image request and fail at the provider with an opaque error,
instead of being excluded at the gateway with the clear reason the router
already gives for every other mismatch. This is the same shape as R19: a
requirement that is not checked is a failure that happens in the wrong place.

**Scope.** Decide, per capability, whether it is a hard requirement (route
excluded, reason recorded) or is refused at the bridge:

| Capability | Reading | Status |
|---|---|---|
| `supports_image_input` | **Hard.** A model that cannot see an image cannot serve an image request. | **done** — excluded in `candidates()`; see R37 in [`REVIEW.md`](REVIEW.md) |
| `supports_builtin_tools` | **Hard**, for the same reason tools are hard. | open |
| `supports_previous_response_id` | The bridge already refuses continuation it cannot serve (`bridge/requests.py`). Decide whether the router should exclude the route earlier, and why. | open |

**Done when.** Each of the three is either enforced in `candidates()` with
its exclusion reason, or is explicitly documented as informational-only with
a cited reason for that choice — and each has a refusal test.

**Landed so far.** `payload_has_image_input()` in `bridge/requests.py`
detects an image with the *same* content parsers conversion uses, so routing
and rendering cannot disagree about what counts as an image. The router
passes `needs_image_input` into `candidates()`, which excludes a route that
cannot accept one with the reason `this model does not support image input`.
A request with no capable route fails closed at the gateway. Evidence:
`tests/test_image_input.py`.

### T4 — Normalised thinking levels — `done`

**Landed.** One canonical vocabulary in `src/gateway/thinking.py`,
reconciled against what providers publish: it is OpenAI's published set
(`none`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`), every level
cites its source, and `ultra` is **not** claimed because no provider this
gateway serves or fronts publishes it. `reasoning_effort`,
`reasoning.effort`, `reasoning.enabled` and the disabled level `none`
parse to that one level on **both** source dialects, and render per
dialect - `reasoning_effort` for Chat Completions, `reasoning: {effort}`
for Responses. "Disabled" is rendered per dialect rather than dropped:
Responses spells it `effort: "none"` (both published Responses models
list it), Chat Completions omits the control (no served chat-dialect
model publishes that level, and OpenAI documents HTTP 400 from a model
that does not support it). A control the target cannot express - an
Anthropic `thinking` budget, a Google `thinkingConfig`, a
`reasoning.summary` on a Chat Completions target, an unknown reasoning
field, a wrong-typed value, or two controls that contradict - is
**refused**, never discarded. A level the selected model does not publish
is still dropped for that route with the substitution recorded, and a
level outside the gateway's vocabulary is forwarded, so the router - not
the vocabulary - decides. A same-dialect payload the gateway does not
model is forwarded untouched, because the provider owns its own dialect.
Evidence: `tests/test_thinking_levels.py` (19 tests),
`tests/test_thinking_controls.py`, and R25/R35 in
[`REVIEW.md`](REVIEW.md). Rendering for the Anthropic and Google dialects
stays with T5/T6: T5 now serves the Anthropic rendering (T6 is declined),
and a spelling those paths cannot render is refused on a converted path
and forwarded on a same-dialect one.

**Why.** "Thinking level" is the one control a client is most likely to
send and the gateway is least able to honour. The state this item was
written against:

* only OpenAI's flat `reasoning_effort` string was understood
  (`bridge/requests.py:265`);
* the newer Chat Completions spelling `reasoning: {"effort": "high"}` was
  **silently dropped** on a converted path, even though
  `_without_disabled_reasoning` read that same field for the disabled case
  (`bridge/requests.py:58`) — the two paths disagreed about one field;
* Anthropic's `thinking: {"type": "enabled", "budget_tokens": N}` was not
  understood at all, and was **silently dropped** on a converted path;
* Google's `thinkingConfig` / `thinkingLevel` were not understood.

Both silent drops broke the bridge's own stated contract
(`bridge/requests.py:7`: "Nothing is silently dropped. If the target
dialect cannot express the request, the request is refused."). They are
filed as **R25**, closed by **R35** and completed here, in
[`REVIEW.md`](REVIEW.md). (An earlier draft of this item named R26
alongside R25; R26 is the unrelated token-accounting finding.)

The published data is thin and must stay honest: only **3 of 40** catalogue
models publish reasoning levels (`gpt-5.6-luna`, `gpt-6-luna`,
`minimax-m3.1-flash`), and the gateway's own vocabulary used to contain
`ultra`, which **no** catalogue model publishes — it has since been removed,
and `minimal` was kept because OpenAI publishes it (`src/gateway/thinking.py`
carries the citations).

**Scope.**

1. **One canonical vocabulary**, reconciled against what providers actually
   publish. Keep the gateway's extra levels only if a provider publishes
   them or the gateway documents them as its own alias.
2. **Parse every inbound spelling** — `reasoning_effort`,
   `reasoning.effort`, `thinking.type`/`thinking.budget_tokens`,
   `thinkingConfig.thinkingLevel` / `thinkingBudget` — and normalise to the
   canonical level.
3. **Render per dialect** — `reasoning: {effort}` for Responses,
   `reasoning_effort` for Chat Completions, `thinking` for Anthropic
   Messages, `thinkingConfig` for Google v1beta.
4. **A level is a preference, not a capability** (the R19 rule). The route
   stays eligible, the level is dropped *for that route*, and the
   substitution is recorded in the attribution and the ledger. Only the
   route that actually served the request records it.
5. **Per-model level and budget tables must be sourced from official
   provider documentation.** No invented budgets. Where a provider publishes
   no table for a model, that model's levels are **Unknown**, the level is
   dropped, and the substitution is recorded.
6. **No silent drops.** A level the target dialect cannot express is
   refused with an actionable message, never discarded.

**Done when.** A client may send any supported spelling and the upstream
receives the dialect-correct form; a level the model does not publish is
dropped *and recorded*; a dialect that cannot express it refuses; and
R25 (closed through R35) carries the fix and its regression evidence.

### T5 — The Anthropic Messages dialect, and a `/v1/messages` surface — **done**

**Why.** Five of the 40 catalogued models are served over Anthropic
Messages and are **permanently refused** by the router
(`core/routing.py:301-306`): `claude-haiku-5-5`, `minimax-m3`,
`qwen3.7-plus`, `qwen3.8-flash`, `qwen3.8-max`. The gateway exposes only
`/v1/models`, `/v1/chat/completions` and `/v1/responses`
(`http/app.py:120-122`), so there is no Anthropic-shaped surface either.

That is about an eighth of the live catalogue (5 of 38) — including the most
recognisable model in it — sitting permanently outside the product.

**Scope.** Two separable pieces; do the first before the second.

1. **Bridge Anthropic Messages to the OpenAI dialects**, so those five
   models become servable through the existing surfaces. The conversion
   must be symmetric and must work on the buffered *and* the streamed path.
2. **Add `/v1/messages`**, an Anthropic-compatible public surface, so
   Anthropic-style clients can call the gateway natively.

**Note.** Retention differs: `claude-haiku-5-5` is 30 days; the other four
are 0 days. The console's cross-provider fallback warning must reflect the
route that is actually selected.

**Done.** Piece 1 landed first: `src/gateway/bridge/anthropic.py` converts
both directions on the buffered and the streamed path, the four additive
anchors (`capabilities._CONVERTIBLE_FROM`, the `routing` import seam,
`opencode_go.SERVABLE_DIALECTS`, the `app.py` response dispatch) make all
five models servable over `/v1/chat/completions` and `/v1/responses`, and
`src/gateway/http/messages.py` adds `POST /v1/messages` behind the same
Host/Origin/forwarded-header policy as the OpenAI surfaces. A control the
target dialect cannot express is refused or recorded as a substitution,
never dropped silently. The console's fallback warning now names the
retention class of each route actually selected (`retention-class-warning`
anchor in `console.js`). Evidence: `tests/test_anthropic_bridge.py`,
`tests/test_anthropic_surface.py`, `tests/test_messages_handler.py`,
`tests/test_messages_routing_streaming.py`, and the retention checks in
`tests/console_dom_test.js`.

### T6 — The Google v1beta dialect — `blocked`

**Why.** Two catalogued models are served over Google v1beta and are
refused today: `gemini-4-argon` (live, transcribed 2026-10-25) and
`gemini-4-argon-contributor` (no longer served; its row is deprecated).
Three rows carry the retention class **`training_permitted`** —
`gemini-4-argon-contributor` and the two `muse-spark-*-contributor` models —
and the contributor row is the refused one with the weakest privacy
posture.

**Decision (owner, 2026-10-23): declined.** `gemini-4-argon-contributor`
stays refused. One model does not justify a third conversion path, and it
is the only `training_permitted` model — the weakest privacy posture in
the catalogue. Do not implement a passthrough unless the owner reverses
this decision explicitly; no worker may self-promote it back to todo.
*(Count note added 2026-10-25: the 2026-10-25 transcription puts three
rows in `training_permitted` — this one and both `muse-spark-*-contributor`
models — and adds a second Google row, `gemini-4-argon`. The decision
stands as written; the "only" above is the count as observed on 2026-10-23.)*

### T16 — A manual model entry must merge, not replace — `done`

**Why.** The `manual` model action rebuilds the whole `ModelEntry`
(`http/app.py:835-850`) instead of merging into it, so the
provider-published fields are silently discarded: `context_window`,
`context_limit`, `reasoning_efforts`, `monthly_limit_usd`,
`limits_source`, `limits_observed_at` and the `supports_*` flags.

Verified: a manual dialect edit on a discovered `gpt-6-luna` takes the
context limit from 258,400 to **None** — so the alias's limit becomes
Unknown — and empties the reasoning levels, so a reasoning request is
dropped as a degradation instead of honoured. The action still returns
`{"ok": true}` and tells the owner nothing.

This is a **documented workflow**: the README's troubleshooting table
sends the owner to "enter it manually and set the dialect" when a model
shows an empty dialect. The fix for a missing dialect must not destroy the
context limit and the reasoning levels of the model it was applied to.

**Scope.** The manual action changes only the fields the owner actually
supplied, and preserves published metadata it has no opinion about. A
manual entry that cannot supply a published fact keeps the published one.
The same merge discipline applies wherever an owner action edits a
discovered model.

**Done when.** A manual dialect edit preserves the context limit, the
reasoning levels and the capability flags; a test covers it; and the
console does not report a limit it does not have.

**Landed.** The `manual` action merges into the existing entry and changes
only what the owner supplied. The published context window, effective limit,
reasoning levels, monthly limit and its source, retention class and the
capability flags all survive; a model that does not exist yet is created
enabled. Evidence: `tests/test_manual_entry_merge.py`, and R27 is closed in
[`REVIEW.md`](REVIEW.md).


### T35 — Continue the fallback chain past a deprecated model — `done`

**Why.** OpenCode Go returned **410 Gone** for a model it had deprecated, and
`410` classified as `unknown`. `unknown` never falls back - by design - so the
chain stopped with a second, perfectly usable fallback unused, and every
request failed with a 502.

The class is neither transient nor a policy failure. It is an **availability
fact about the route**: the upstream no longer serves this model there, and
another route may still serve the request. It must not be retried on the same
route, and it must not bench the provider's other models.

**Done when.** A `410` and the provider's own deprecation code classify as
route gone; the class satisfies the "upstream unavailable" trigger so the
chain continues; it is never retried on the same route; the provider's other
models stay usable; and each has a test.

**Landed.** A `route_gone` class now covers `410` and the provider's
deprecation codes. Evidence: `tests/test_route_gone.py`, and R32 is closed
in [`REVIEW.md`](REVIEW.md).


### T36 — The gateway appears in OpenCode's model selector — `done`

**Why.** The gateway published only bare alias names at `/v1/models`. An
OpenCode client could not list the aliases with their context limit, tool
support and reasoning levels, and a custom provider had to name every alias
by hand in its configuration - so an alias created in the console was
invisible to that client until its configuration was edited again.

**Scope.** `GET /v1/alias-catalog` publishes, per published alias, the
alias-level context limit (the smallest limit among the routes that could
serve it, Unknown when any route's is unpublished), the primary route's
published reasoning levels, and whether every route supports function tools.
`integrations/opencode/` is an OpenCode V2 provider plugin that reads it,
registers one provider and refreshes every 60 seconds.

**Rules.** Metadata only: no provider inventory, no ledger, no credential,
no unpublished route, and no provider call on a read. OpenCode requires a
number, so it uses a documented route window when Go's endpoint table and
the exact model's official documentation establish one (currently
MiMo-V2.6-Flash and MiniMax M3.1 Flash). An Unknown alias gets the
**owner-chosen 200,000-token OpenCode working budget**, labelled
`(200k client budget; upstream unknown)`. That is a client-side budget, not
a provider guarantee - the gateway still reports Unknown. A failed refresh
keeps the last complete inventory rather than erasing the owner's selection.

**Landed.** Evidence: `tests/test_opencode_catalog.py`,
`tests/test_opencode_catalog.mjs`, and R34 in [`REVIEW.md`](REVIEW.md). The
integration was also exercised against a running gateway: the aliases
appeared in `/models` with their real limits, and a real request completed
through the gateway with the fallback chain and attribution recorded.


### T37 — Opt-in per-alias phase routing with optional Jev inference — `done`

**Why.** One alias served every kind of request through one primary route.
An owner who wanted a planning prompt on a reasoning-heavy model and an
implementation prompt on a fast model had to define two aliases and switch
them by hand in the client.

**Scope.** An alias may carry an optional, off-by-default `phase_routing`
policy mapping a closed phase vocabulary (`plan`, `implement`, `validate`)
to owner-configured routes, plus an optional default route. Precedence is
caller-supplied phase, then Jev inference (only when enabled and the request
is safe to classify), then the ordinary primary. The caller's hint lives in
the gateway-owned `metadata.personal_gateway.phase` field; the entire
`personal_gateway` block is stripped before conversion, so no provider ever
sees it.

**Rules.** A phase-selected route is the **primary for that phase**, never a
fallback: it does not set `fallback_used` and needs no fallback
authorization. Jev inference is opt-in per alias, sends a single
gateway-owned closed-set `choice` question with a bounded slice of request
text as state, and applies only on a confident, known choice. Low
confidence, an unknown choice, a non-choice answer, tools, a continuation, or
a Jev failure all use the ordinary route and record why. Every phase route
and the inference model are validated at save time like any other route. The
ledger records only the phase and its source (`caller`/`jev`/`default`) as
metadata - never the request content, the Jev question, or the answer
distribution.

**Landed.** Evidence: `tests/test_phase_routing.py` - caller phase selects
the phase route and is stripped before upstream; invalid phase ignored; Jev
confident choice selects the phase route; low confidence, unknown choice,
non-choice answer and Jev failure use the ordinary route; tools skip
inference; inference disabled never calls Jev; the ledger records phase
metadata only; and every invalid phase route, provider, model and confidence
is refused.


---

## Tier 2 — operational completeness

### T7 — A durable request ledger — `done`

**Landed.** Request outcomes are persisted asynchronously as metadata-only
records on the state volume in `request-ledger.jsonl`, rotated at 5 MiB with
two backups and a 30-day retention window. The in-memory ring remains the hot
view, is restored at startup, and exposes an `amend(request_id, **fields)`
seam for updating safe outcome metadata without exposing storage details. The
Status view reports the persistence mode and explicitly says the ledger is
not a billing system.

**Evidence.** `src/gateway/ledger_persistence.py`,
`tests/test_ledger_persistence.py` (including restart survival and content
refusal), and the Status payload/UI; full suite passed (385 tests, 2 skipped).

### T8 — Backup, export and import in the console — `todo`

**Why.** Configuration backup is currently a manual `docker run … cp`
(`RUNBOOK.md` §5). The owner has to leave the console to protect the
gateway's state.

**Scope.** A sanitised configuration export (never credentials, never
caller-token hashes), an import that validates through `gateway/identifiers.py`
and `gateway/routes.py` before it persists anything, and an optional
scheduled snapshot of `config.json` into the state volume.

### T9 — A `/metrics` endpoint — `todo`

**Why.** Every operational question is currently answered by reading the
Status page in a browser, or by parsing the container's stdout.

**Scope.** Prometheus text format: request counts by outcome, error classes,
a latency histogram, fallback counts, token totals (T2), cooldown state.
Metadata only. No content, no credential, no client identity beyond what
the ledger already holds.

### T10 — A concurrency cap and a per-client rate limit — `todo`

**Why.** The server is a thread-per-request server with no cap
(`http/core.py:326`). The request body is capped at 4 MiB
(`http/core.py:31`), but concurrent requests are not, so one reachable
peer can saturate the host.

**Scope.** A bounded worker pool with a clear `429` and the `rate_limited`
error class; a per-client rolling window keyed on the same client identity
the session derivation already uses (`core/routing.py:461`).

**Note.** This is a *local* protection, and it must be described as such. It
is not an access-control boundary, and the console must not imply it is.

### T11 — Cancel the upstream when the client disconnects — `todo`

**Why.** A broken pipe is caught at the HTTP layer (`http/core.py:289`),
but the upstream request is not cancelled. An abandoned stream keeps
running upstream and keeps consuming the provider's allowance.

**Scope.** Propagate the client disconnect to the adapter, abort the
upstream request, and record the outcome as a client abort rather than a
provider failure.

### T12 — Present cooldowns and skipped routes properly — `todo`

**Why.** Cooldowns are rendered as raw JSON
(`static/console.js:1027`), and the reason a route was skipped is visible
only in the ledger. The owner learns that a route is excluded by reading a
request record, which is the wrong way round: R5 made the record complete
precisely so that the console could be the friendly view of it.

**Scope.** A readable cooldown list with the remaining time and the reason
it was set, and the skipped-route reasons surfaced on the route itself.

---

## Performance

Measured against the running deployment on a real network segment. The
numbers below were taken with the real handler, not a mock.

### T17 — Stop the 41ms stall on every small response — `done`

**Landed.** Closed as **R28**. `disable_nagle_algorithm = True` on the
handler, and a buffered response is written in one segment instead of two.

**Why.** This is the largest latency defect in the gateway, and it is
invisible to a casual look because the first request on a fresh connection
is fast and every later one on the same connection is slow.

`_RequestHandler` does not set `disable_nagle_algorithm`, so TCP_NODELAY
is **not** set on the accepted socket (`http/core.py:225` -
`socketserver.StreamRequestHandler` defaults it to False). The handler
writes a buffered response in **three** separate `send()` calls - the
status line, the header block, then the body (`core.py:292-303`) - and
with `wbufsize = 0` each one is its own segment. Nagle holds the last two
until the first is acknowledged; the client's TCP delays that
acknowledgement.

Measured against `10.10.11.109:8765`, reusing the connection as every
modern client does:

| endpoint | keep-alive | close-per-request |
|---|---|---|
| `/health` | 52.4ms | 6.9ms |
| `/version` | 52.0ms | 7.1ms |
| `/v1/models` | 53.3ms | 8.9ms |
| `/owner/api/state` (29,516 B) | 13.8ms | 15.2ms |

The last row is the control: a body bigger than the MSS is not held. The
threshold is exactly the MSS - **every response of about 1.4KB or less
stalls**:

| body | TCP_NODELAY off | TCP_NODELAY on |
|---|---|---|
| 200 B | 41.08ms | 0.09ms |
| 1400 B | 41.07ms | 0.08ms |
| 2000 B | 0.24ms | 0.08ms |
| streaming | 0.01ms/frame | 0.01ms/frame |

So `/health`, `/version`, `/v1/models` **and every short non-streaming
completion** pay 41ms of pure delay. A *longer* completion is faster than
a shorter one - the signature of the bug. Streaming is unaffected.

**Scope.** Set `disable_nagle_algorithm = True` on the handler, and write
the status line, the header block and the body as a single `send()`. Both
together take a small response from 41ms to 0.06ms.

**Done when.** A buffered response is written in one write, a regression
test asserts it, and the measured stall is gone. Filed as **R28**.

### T18 — Reuse upstream connections to the providers — `done`

**Landed.** `UpstreamClient` now speaks `http.client` behind a bounded
per-origin keep-alive pool. 50 sequential requests to one provider open
**1** TCP connection instead of 50.

**Why.** `UpstreamClient` used `urllib.request` (`adapters/http_client.py`),
which opens a **new connection for every request** and never keeps one
alive. A TLS handshake to a remote provider measures **~55ms** on this
network, so every provider request pays that before the provider does any
work.

For a streaming request the connection is held for the whole generation, so
the handshake is amortised. For a short non-streaming request it is pure
added latency, on top of the provider's own time.

**Scope.** A per-provider connection pool with keep-alive, a bounded idle
pool, and a clean close on shutdown. It must keep the existing guarantees:
**no redirects followed**, no credential in an exception, and the timeout
behaviour unchanged.

**Done.** The client speaks `http.client` behind `_ConnectionPool`, which
holds at most `DEFAULT_IDLE_CONNECTIONS` (4) idle connections per origin.
Connections are keyed by origin, so one provider pointed at two hosts keeps
two pools. The shared SSL context is built once per pool, so a reused
connection does not re-read the CA store.

Pooling buys one failure a connection-per-request client cannot have: the
peer may close an idle connection while the pool still holds it. That is
detected with a non-blocking readiness check **before** the request is
written, so a request is never replayed on the grounds that a pooled
connection turned out to be dead - a side effect is never duplicated. A
connection is returned to the pool only when its body has been fully read
or its stream has ended on its own; a stall, an interrupted read or a
caller that stops consuming all leave bytes in flight, and a connection
with bytes in flight is never handed to another request.

Shutdown releases the sockets rather than leaving them to the operating
system: `AdapterRegistry.close()` is called from `cli.main()`'s existing
`finally` after `server.stop()`, and an `atexit` hook over a weak set of
live clients is the backstop for a process that exits without it.

**Kept.** Redirects are still refused and their connection is dropped
rather than reused; a credential never appears in an exception; the
configured timeout is re-applied to a reused socket, not just to the
attribute; and a non-2xx answer to a streaming request is still buffered,
because an error body is what the adapter turns into the caller's error.

**Note.** This is the one performance item that changes the shape of the
network code, so it wants its own tests: pooled reuse, exhaustion, and
that a redirect is still refused. `tests/test_upstream_http_pool.py`
covers all of those plus the stale-connection refusal and the shutdown
path.

### T19 — Stop deep-copying the whole configuration on every read — `todo`

**Why.** `ConfigStore.snapshot()` rebuilds the entire configuration by
serialising it to a dict and parsing it back
(`config.py:461-464`). It is called **three times per inference request**
- once in `_handle_inference`, once in `Router.resolve_alias` and once in
`Router.candidates` - so every request copies every provider, every model
entry, every alias and every profile.

Measured with a realistic configuration (2 providers, 38 models, 5
aliases): **0.225ms per snapshot, 0.67ms per request**, scaling with the
size of the configuration. It is small today and irrelevant at a few
requests per second, and it becomes waste as the catalogue grows.

**Scope.** An immutable snapshot that is published on mutation and shared
by readers, rather than copied per read. The transaction must still be
atomic and must still persist before it becomes visible.

**Done when.** A read no longer copies the configuration, the mutation
semantics are unchanged, and a test proves a reader never observes a
half-applied transaction.

### T20 — Separate the connect timeout from the read timeout — `done`

**Landed.** A provider that accepts a connection and then goes quiet is
abandoned after the **connect budget** (10s), not after two minutes. A
long-but-progressing stream is judged by the **read budget** (120s, per
read) and runs to completion. Both are tested.

**Why.** `DEFAULT_TIMEOUT = 120.0` was used for both the connect and the
read (`adapters/http_client.py`), so a provider that accepted a
connection and then went quiet held a **gateway thread for two minutes**,
and with no concurrency cap (T10) a few such requests could exhaust the
process.

**Scope.** A short connect timeout and a separate, generous read timeout
for the body. A stream that pauses must be judged by its own rule, not by
the connect timeout.

**Done when.** A provider that accepts and stalls is abandoned after the
connect budget, a long-but-progressing stream is not, and both are tested.

**Done.** `UpstreamClient` carries two budgets, and each phase of a
request is judged by the one that fits it. The connect budget
(`DEFAULT_CONNECT_TIMEOUT`, 10s) bounds establishing a connection - the
TCP handshake and, over TLS, the handshake that follows it - and the head
of a **streamed** response: an SSE provider sends its status line before
it generates anything, so a head that has not arrived within the connect
budget is a stall and not a slow generation. The read budget
(`DEFAULT_TIMEOUT`, 120s) judges every wait for the provider's own
bytes, per read: the buffered head and body, and every frame of a
stream, so a response that is still arriving is never abandoned while it
progresses. A *buffered* head is deliberately judged by the read budget,
because on the buffered inference paths (`opencode_go` with
`stream: false`, the System One native request) the head may follow the
whole generation, and bounding that at the connect budget would refuse
every non-streaming generation longer than 10 seconds. Each phase
re-arms the socket it inherits - including a pooled connection reused
for a later request - so neither budget can leak into the other's phase.
Tests: `tests/test_upstream_timeouts.py` drives a provider that accepts
and stalls (TLS handshake, and a stream head), a stream that runs
several times longer than the connect budget without being cut, a
dribbling buffered body, a pooled connection served under the read
budget, and the credential-non-disclosure refusal on the connect path.

---

## Tier 3 — provider breadth

The provider seam is the strongest part of this codebase: a provider is an
adapter, not a conditional in the routing core
(`core/routing.py:1-21`). Adding one is a module, a registry entry, model
capability handling, safe error classification, UI/state support, tests and
a credential-isolation review — **not** just a URL and an API key.

Each item below also needs: a `list_models()` returning `list[ModelInfo]`,
tested through the **generic** refresh handler; safe error classification
into the shared classes; and no implicit provider calls.

### T13 — A plain OpenAI API-key provider — `todo`

The most requested provider, and the least novel: a standard
`/v1/chat/completions` and `/v1/responses` upstream with a bearer token.

### T14 — A native Anthropic API-key provider — `todo`

A real Anthropic Messages upstream. It also makes T5's `/v1/messages`
surface meaningful for a second reason: the gateway would have an Anthropic
dialect it is natively serving, not only one it converts to.

### T15 — A local provider — `todo`

Ollama or LM Studio, on the LAN, with no credential. It fits this product
better than anything else on this list: a fallback route that keeps the
prompt on the LAN instead of sending it to a third party with a published
retention class. It would also give the gateway a route that is available
when every provider is in cooldown.

---

## Tier 4 — the owner's control plane

These are **new capabilities**, not gap fixes. They share one theme: the
alias today is a routing decision and nothing else, so the owner has no
lever over what a client actually asks the provider for, and no view of
what the alias has been doing.

Two rules apply to every item in this tier, because they are the rules
the rest of the project already lives by:

* **A policy that changes a request is recorded, not silent.** When an
  owner cap overrides a client value, that is a *degradation* in the sense
  R19 established for a dropped reasoning effort: it goes into the
  attribution and the ledger, and it is never invisible.
* **A policy that cannot be honoured refuses.** "Tools denied" on an alias
  is an actionable 4xx, not a silently stripped tool list.

### T21 — A request policy per alias — `todo`

**Why.** `Alias` (`config.py:243-312`) carries a primary route, fallbacks,
a fallback policy and a cooldown. It carries **no parameters**: the client
dictates `temperature`, `max_output_tokens`, the thinking level and the
tools, and the owner has no say. For a single-owner appliance that is the
biggest missing control.

The policy block has two halves:

```yaml
alias: coding
defaults:          # applied when the client did not specify
  temperature: 0.1
  max_output_tokens: 8000
  thinking_level: high
  system: "You are a careful senior engineer."
caps:              # applied over the client
  max_output_tokens: 32000
  thinking_level: medium
deny:              # refused outright
  tools: true
  store: false
```

**Scope.** A `policy` block on the alias, applied in one place, in this
order: the client's value, then the owner's default if the client gave
none, then the owner's cap if it clamps the client's value, then the
deny rules.

* It applies **before** dialect conversion, so the owner thinks in one
  vocabulary ("thinking level: high") rather than per-dialect field names.
* It applies on **every** path that reaches a provider - the API surface,
  the alias test, and any future caller - so it lives beside the session
  derivation in the router, not in a handler.
* A clamped value is recorded as a **degradation**, exactly like a dropped
  reasoning effort, and only the route that actually served the request
  records it.
* A denied capability is refused with an actionable 4xx that names the
  rule.
* A forced value (`store: false`) is applied and recorded.

**Depends on.** **T4**, for the canonical thinking vocabulary and its
per-model levels. Defining an owner-facing "thinking level" before the
canonical model exists would bake in a second vocabulary.

**Done when.** Every clause above is enforced, tested for both the
allowed and the refused case, and the ledger shows what was changed and
why.

### T22 — A budget per alias, enforced locally — `done`

**Landed.** Optional monthly USD alias cap; known spend uses successful ledger token counters and the published catalogue prices. Unknown/incomplete totals are explicitly uncapped. The API checks before routing and returns actionable HTTP 429; the console shows spend, soft-cap caveat and UTC reset boundary. See `gateway/budget.py`, `tests/test_costs.py`, and `tests/test_end_to_end.py`.

**Why.** The provider's allowance is **Unknown** for every provider that
does not publish one, and the gateway calls no undocumented endpoint to
find out. That is the honest answer - but it is not the *only* answer.
The gateway can count what it spends, and a local cap does not need the
provider's permission.

**Scope.** A monthly budget in USD per alias, checked against locally
computed spend.

* Spend comes from **T2** (the recorded token counters and the published
  per-token prices). A model with no published price contributes
  **Unknown**, not zero - and an alias whose spend cannot be computed is
  reported as uncapped, never as free.
* The cap is checked **before** the request and the actual cost is
  recorded **after** it, so the cap is a **soft** one: concurrent requests
  can overshoot it. The console must say so rather than imply a hard
  guarantee.
* Over budget, the alias refuses with an actionable 4xx naming the
  budget, the spend and the reset time - it does not silently serve a
  different model, because that would be a fallback the owner did not
  authorise.
* A reset window and a timezone-free UTC boundary, shown in the console.

**Depends on.** **T2**, and **T23** for the spend view.

### T23 — Statistics per alias — `todo`

**Why.** The Status page reports **global** totals and the last 25 events
(`http/app.py:584-586`). With ten aliases, the owner's questions are
"which one is failing?", "what did `chat` cost this month?" and "is the
fallback firing?" - and none of them can be answered.

**Scope.** Per alias, per provider and per model: request count, error
count by error class, fallback rate, latency percentiles, token totals
and spend, over a selectable window (last hour, day, month). Metadata
only, on the same rules as the ledger.

**Depends on.** **T2** for tokens and spend. **T7** if the history must
survive a restart; without it, the windows are in-memory and the console
must say so.

### T24 — Export the ledger — `done`

**Why.** The data exists; getting it out of the browser is manual.

**Scope.** A metadata-only export of the ledger - CSV or JSON, bounded in
rows, with the same fields the console shows and no content. It is a
read-only surface, it records nothing, and it obeys the same redaction
table as the log.

**Depends on.** **T7** for anything older than the in-memory ring.

### T25 — A dry run: show the upstream payload without sending it — `todo`

**Why.** Every dialect bug in this project's history - R9, R18, R21, R25 -
was "the upstream received something I did not expect". The alias test
(`http/app.py:937-976`) sends a **real** request, costs provider
allowance, and cannot show what was sent. A preview would have caught
every one of them on the day they were found.

**Scope.** A read-only endpoint that takes an alias and a request body and
returns the payload the gateway *would* send: the substituted upstream
model id, the target dialect, the derived session header, the thinking
level, the tool list, and the converted shape of the request.

* It **never calls a provider**. That is the whole point, and it is
  compatible with the rule that provider calls are explicit owner actions
  - a preview is not a provider call at all.
* It never records the preview in the ledger and never writes the body to
  the log. The body is returned to the owner who supplied it.
* It applies the same policy, capability and degradation rules as a real
  request, so the preview is **what would actually happen** - including
  the refusals.

**Done when.** A preview of a request that would be refused says so, and
a preview of a converted request shows the converted shape.

### T26 — Weighted routes within an alias — `todo`

**Why.** An alias is one primary plus ordered fallbacks. Sometimes the
right shape is to *split* traffic across two models at the same provider -
a cheap fast one and a strong one - rather than to treat one as a
failure fallback.

**Scope.** A route may carry a weight. The alias selects among its
weighted routes by weight, **sticky per conversation** so a long exchange
stays on one model (which preserves the provider's cache and the
reasoning continuity).

* A pool and a fallback are **different concepts** and must not be
  overloaded onto one field. A pool is a deliberate distribution; a
  fallback is a failure path.
* A failure inside the pool is a fallback, and it obeys the existing
  opt-in rules: the alias must authorise it, the provider must be
  confirmed, the trigger must be enabled, and it never happens after a
  stream or a tool-bearing request.

**Care.** This is the feature most likely to be over-engineered for a
single owner. It should land only if the simpler shapes have been used.

### T27 — Honour a client-supplied request id — `todo`

**Why.** The gateway invents its own request id
(`observability.new_request_id`). A client that already correlates its own
requests cannot join the two records together.

**Scope.** Accept `X-Request-Id`, validate it - length, character set, no
control characters - and record it beside the gateway's own id. A client
value that fails validation is **rejected, not echoed**, because it reaches
the log line and the ledger.

**Rule.** The gateway's own id stays authoritative. A client id is a
correlation aid, never an identifier the gateway trusts.

### T28 — Keepalive comments on a long stream — `todo`

**Why.** A stream that produces nothing for a while can be dropped by an
idle client or an intermediary. The ChatGPT upstream is streaming-only and
can be slow to produce its first token.

**Scope.** While a response is streaming and nothing has been written for
N seconds, emit an SSE comment (`: ping`) into the **client-facing**
stream. A comment is valid in SSE for every dialect, and it carries no
content.

**Care.** The comment belongs to the gateway's output, never to the
upstream's frames, and it must not disturb the frame framing that
`tests/test_sse_framing.py` checks.

### T29 — A concurrency cap per provider — `todo`

**Why.** The server is thread-per-request with no cap
(`http/core.py:326`). One busy client can saturate a provider account and
earn a rate limit that the owner then has to wait out.

**Scope.** A bounded number of concurrent in-flight requests per
provider. When the cap is reached, the request is refused with the
`rate_limited` error class and a clear message.

**Note.** This is a **local** protection, and it must be described as
such. It is not a guarantee the provider made, and the console must not
imply it is. It is the provider-side counterpart to T10's per-client
cap.

### T30 — Groups of aliases — `todo`

**Why.** An owner with a dozen aliases wants to publish or disable a
family of them together - "all the coding aliases", "everything that
touches the cheap model".

**Scope.** A tag on each alias and a bulk publish/disable over a tag. A
group is a **convenience layer only**: it changes no route, no policy and
no limit, and each alias keeps its own configuration.

**Care.** A group must never imply a shared fallback chain or a shared
budget. Those stay per alias, because a shared fallback would quietly
merge two privacy decisions.

### T31 — One provider health view — `todo`

**Why.** The facts already exist and are scattered: `ProviderState` holds
`state`, `last_test_at`, `last_error` and `catalog_observed_at`
(`config.py:196-201`), and `CooldownState.snapshot()` holds the cooldowns.
There is no single place that answers "which provider is healthy right
now?".

**Scope.** One view: per provider, the connection state, the last test and
its outcome, the last error, when the catalogue was last observed, the
model count, the cooldown countdown, and the recent success and failure
rate from the ledger.

**Depends on.** **T23** for the rates; the rest is available now.

### T32 — A durable circuit breaker, remembered across restarts — `done`

**Landed.** Closed as **R30**. The state is written to `circuits.json` in
the state directory and reloaded on start.

**Why.** The gateway already has a cooldown, and it is the right idea in
the wrong shape. `CooldownState` (`core/routing.py:100-145`) is
**in-memory only**, keyed by **provider**, with a **flat** duration taken
from the alias's `cooldown_seconds`. So:

* A **restart during an outage** wipes the memory and the gateway
  immediately hammers the exhausted provider again. That is exactly the
  moment a cooldown is worth most.
* An **exhausted allowance** and a **single 520 from an edge** get the
  same five minutes. For a monthly allowance, five minutes is worthless;
  for one transient error, five minutes is too aggressive.
* Every failure sets the **same** duration, so repeated failures do not
  back off further, and a success does not reset anything.
* When the cooldown expires, **everything returns at once**, with no
  probe to find out whether the provider recovered.
* The key is the **provider**, so one route's failure benches every model
  at that provider - and two aliases with different `cooldown_seconds`
  fight over the same key, because the map is shared by the whole router.
* The provider's own **`Retry-After` is discarded**: the adapter does not
  keep the response headers, so the one number the provider gives us
  about how long to wait never reaches the router.

**Scope.** Replace the flat timer with a **circuit breaker per route**
(`provider` + `model`), persisted in the state volume.

* **It is a local retry policy, never evidence that the provider reset.**
  That wording is already in the code and must survive.
* **Distinct classes get distinct treatment.** `exhausted` is a sticky
  condition - the allowance is gone - so it opens for a long time. A
  `transient` error is a blip, so it opens briefly and only after
  consecutive failures. `rate_limited` is its own case.
* **Escalating backoff.** Consecutive failures lengthen the open window;
  a success closes the circuit and resets the counter.
* **Half-open.** When the window ends, exactly **one** request is let
  through to test recovery. Success closes the circuit; failure reopens it
  for longer.
* **It survives a restart**, so a gateway that comes back up during an
  outage stays out of the way. The stored state is metadata only -
  provider, model, error class, timestamps - never content.
* **It has a maximum age**, so the store cannot grow without bound, and
  an entry that is older than its maximum is simply forgotten.
* The route is still **recorded as skipped, with its reason**, so a
  cooldown can never look like a silent failover (the rule R5 established).
* When the circuit is open, the alias's **existing fallback policy**
  decides what happens next. Provider-wide behaviour then emerges on its
  own: an exhausted account fails every route at that provider, and each
  one opens its own circuit.

**Resolution.** `gateway.circuits` replaces the flat timer.

* **Durable.** The state is written to `circuits.json` in the state
  directory with the same atomic, owner-only rules as the rest of the
  state, and it is reloaded on start. A gateway that comes back up during
  an outage stays out of the way.
* **Per route.** The key is the provider *and* the model, so one broken
  model does not hold the others at the same provider.
* **Class-driven.** A confirmed **exhaustion** is account-scoped: it opens
  for at least an hour and covers the whole provider, because the
  account's allowance is gone rather than one route's share of it. A
  **rate limit** opens the route for as long as the provider asked. A
  **transient** failure is a blip: it opens the route only after a
  pattern of consecutive failures, and a success resets the count.
* **Escalating.** The window grows with consecutive failures, up to a day.
* **Half-open.** When a window ends, exactly **one** request is let
  through. A success closes the circuit; a failure reopens it for longer.
  A probe that never returns is reclaimed after five minutes, and a probe
  is **never remembered** across a restart - a stale probe must not block
  a route for ever.
* **Bounded and forgotten.** The store keeps at most 512 circuits, the
  oldest first, and a circuit that has not been touched for a week is
  forgotten.
* **Metadata only.** The file holds a provider, a model, a failure class,
  counters and timestamps. No prompt, no completion, no content.
* The route is still **recorded as skipped, with its reason**, so a
  cooldown can never look like a silent fallback.

**Evidence:** `tests/test_circuits.py` - the state survives a restart; the
stored file is metadata only and mode `0600`; a probe is never remembered;
an old entry is forgotten; the store stays bounded; an exhaustion opens
for an hour and covers the provider; a rate limit opens the route only; a
transient failure needs a pattern and a success resets it; the window
escalates and is capped; exactly one probe is let through; a successful
probe closes the circuit; a failed probe reopens it for longer; an
abandoned probe does not block the route; and the router records the skip
with its reason.

### T33 — Honour the provider's `Retry-After` — `done`

**Landed.** The header is read at the single choke point where an
upstream response becomes an error, carried on the `AdapterError`, and
used as the cooldown's window.

**Why.** When a provider rate-limits a request, it usually says how long
to wait. The adapter does not keep the response headers
(`adapters/http_client.py`), so the number is thrown away and the gateway
guesses.

**Scope.** Carry `Retry-After` (both the seconds and the HTTP-date form)
on the `AdapterError`, and use it as the circuit's window when it is
present and sane.

**Rules.** It is a **hint from the provider**, so it is bounded: a
hostile or mistaken value must not bench a route for a week. A value that
is absent, unparseable or absurd falls back to the configured policy, and
the circuit records which of the two it used.

**Resolution.** `parse_retry_after` reads both forms - the seconds form
and the HTTP-date form - and returns nothing for a value that is
unparseable, negative or already in the past. A value longer than
`MAX_RETRY_AFTER_SECONDS` (3600) is **clamped**, not taken literally,
because waiting too long is the safer error. The header is read at
`error_from_response`, the one place where an upstream response becomes
an `AdapterError`, and both adapters pass their response headers to it.

The window is the **longer** of the local policy and the provider's ask:
the gateway never retries earlier than either asks for. The cooldown
records which of the two it used, and the skip says so - *"provider is in
a local cooldown for 3600s (the provider asked us to wait 3600s)"*.

**Evidence:** `tests/test_retry_after.py` - both header forms; unusable
values; the clamp; the longer-window rule in all three directions; the
skip reason; the cooldown details; and a real 429 with a real header
driven through the OpenCode Go adapter.

### T34 — Decide whether a response cache may exist at all — `blocked`

**Why.** "Cache a request" can also mean *cache the response*, so that an
identical request is served without calling the provider. That is a
different feature, and it collides with two decisions this project has
already made.

* **It stores prompts and completions.** The product's promise is that it
  does not store prompts, responses or response content. A disk cache
  breaks that outright; a memory cache still puts the content in RAM and
  in swap.
* **It leaks between callers.** LAN API invocation is anonymous by
  design - any peer may invoke any published alias. A shared cache would
  serve one caller's answer to another. Keying it per client would need
  the client identity the product deliberately does not authenticate.
* **It is wrong for most requests.** A cached answer for a request with
  temperature, tools or reasoning is stale by construction.

**This is a decision for the owner, not a feature to assume.** If it is
wanted, it must be explicitly opt-in, per client, off by default, and
bounded - and the README's storage promise must change to match. The
failure-state cache in T32 has none of these problems, because it stores
no content.

**Decision (owner, 2026-10-23): declined.** No response cache may exist.
The no-storage promise stands unchanged: the gateway does not store
prompts, responses, or response content. The failure-state cache in T32
remains the only caching, and it stores no content.

### T38 — Reset-aware ChatGPT cooldown recovery — `done`

**Why.** On 2026-10-24 a reopened `chatgpt_codex` subscription window sat
behind a four-hour provider-wide circuit: a confirmed exhaustion had a
1-hour floor and grew exponentially with consecutive failures, so the
third failure escalated the persisted hold and every ChatGPT-primary
alias kept falling back to OpenCode. See R41 in
[`REVIEW.md`](REVIEW.md).

**Landed.** The confirmed-exhaustion window is now **bounded**: a floor
of 15 minutes and a cap of 30 minutes by default, configurable at
startup with `GATEWAY_EXHAUSTED_COOLDOWN_MAX_SECONDS` (or
`--exhausted-cooldown-max-seconds`), with no exponential growth for this
class. Rate-limit and transient windows are unchanged. Recovery stays
automatic and stampede-safe: when the window ends, exactly one half-open
probe is let through, concurrent requests are skipped with a truthful
reason, a successful probe clears the provider and route circuits and
the primary serves again with `fallback_used=false`, and a failed probe
re-arms within the cap keeping the failure class and count. The owner
gained a deliberate per-provider **Clear cooldowns** action (console
button and `POST /owner/api/provider` `clear_cooldowns`, canonical
Origin required) that returns the cleared metadata-only entries and
never touches another provider's circuits; an optional `probe_alias`
runs exactly one explicit probe. State and console expose the recovery
telemetry (failure timestamp, scope, failures, hold, next probe,
probe-in-flight) plus the effective `cooldown_policy`, and the ledger
labels `phase route` vs `held by local circuit` vs `fallback` distinctly.

**Evidence.** `tests/test_circuits.py`, `tests/test_cooldown_recovery.py`,
`tests/test_cli.py`; docs updated in README, ARCHITECTURE (cooldowns),
RUNBOOK §8 (recovery procedure) and §9 (retired legacy systemd unit).

---

## Summary

| ID | Item | Tier | Status |
|---|---|---|---|
| T1 | Refresh the README's Known Limitations | 1 | **done** |
| T2 | Local usage and cost accounting | 1 | **done** |
| T3 | Enforce the declared-but-unchecked capabilities | 1 | todo |
| T4 | Normalised thinking levels | 1 | **done** |
| T5 | Anthropic Messages dialect and `/v1/messages` | 1 | **done** |
| T6 | Google v1beta dialect | 1 | declined (owner, 2026-10-23) |
| T7 | A durable request ledger | 2 | **done** |
| T8 | Console backup, export and import | 2 | todo |
| T9 | A `/metrics` endpoint | 2 | todo |
| T10 | Concurrency cap and per-client rate limit | 2 | todo |
| T11 | Cancel the upstream on client disconnect | 2 | todo |
| T12 | Present cooldowns and skipped routes properly | 2 | todo |
| T13 | A plain OpenAI API-key provider | 3 | todo |
| T14 | A native Anthropic API-key provider | 3 | todo |
| T15 | A local provider | 3 | todo |
| T16 | A manual model entry must merge, not replace | 1 | **done** |
| T17 | Stop the 41ms stall on every small response | perf | **done** |
| T18 | Reuse upstream connections to the providers | perf | **done** |
| T19 | Stop deep-copying the configuration on every read | perf | todo |
| T20 | Separate the connect timeout from the read timeout | perf | todo |
| T21 | A request policy per alias | 4 | todo |
| T22 | A budget per alias, enforced locally | 4 | **done** |
| T23 | Statistics per alias | 4 | todo |
| T24 | Export the ledger | 4 | todo |
| T25 | A dry run: show the upstream payload without sending it | 4 | todo |
| T26 | Weighted routes within an alias | 4 | todo |
| T27 | Honour a client-supplied request id | 4 | todo |
| T28 | Keepalive comments on a long stream | 4 | todo |
| T29 | A concurrency cap per provider | 4 | todo |
| T30 | Groups of aliases | 4 | todo |
| T31 | One provider health view | 4 | todo |
| T32 | A durable circuit breaker, remembered across restarts | 4 | **done** |
| T33 | Honour the provider's `Retry-After` | 4 | **done** |
| T34 | Decide whether a response cache may exist at all | 4 | declined (owner, 2026-10-23) |
| T36 | The gateway appears in OpenCode's model selector | 1 | **done** |
| T37 | Opt-in per-alias phase routing with optional Jev inference | 3 | **done** |
| T38 | Reset-aware ChatGPT cooldown recovery (bounded exhaustion hold, single probe, owner clear action) | 2 | **done** |

## Suggested order

1. **T1** — done. The README's Known Limitations no longer cites closed
   findings.
2. **T4** — done. The silent drops (R25) are fixed; **R26** is the one open
   finding and belongs to T2.
3. **T2** — the pieces exist; this is plumbing, and it changes what the
   Status page can honestly claim.
4. **T3** — small, and it closes a class of opaque upstream failures.
5. **T16** — a live defect: a documented owner action silently destroys
   published capability metadata.
6. **T5** — done. A quarter of the catalogue and a second client shape
   are served: Anthropic-dialect models bridge to the OpenAI surfaces and
   `POST /v1/messages` is public on the LAN trust model.
7. **T17** — done. 41ms is off every small response.
8. **T7, T8, T9** — operational durability, then observability.
9. **T10, T11, T12** — robustness and presentation.
10. **T18, T19, T20** — network and CPU efficiency, once the latency
    defect is gone. T18 is done: upstream connections are reused, so a
    short request no longer pays a fresh TLS handshake.
11. **T25** — the dry run. It is the cheapest of the new capabilities and
    the best tool against this project's actual bug class: it would have
    caught R9, R18, R21 and R25 on the day they were found.
12. **T4, then T21** — the canonical thinking vocabulary first, then the
    owner's per-alias thinking controls, so there is only ever one
    vocabulary.
13. **T2, then T22 and T23** — the numbers first, then the budget and
    the per-alias statistics that act on them.
14. **T13–T15** — breadth, once the core is complete.
15. **T26–T31** — the remaining control-plane items, in the order the
    owner finds them useful.
16. **T32, T33** — the failure-state cache. It is metadata only, it
    survives a restart, and it stops the gateway from hammering a provider
    it already knows is exhausted. T33 is the small half.
