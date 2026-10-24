# Codebase review

## Scope and evidence

This review covers the repository through `c577b50` plus the documentation
changes listed below. It is a static code review plus local, credential-free
verification; it is **not** a provider-account or deployed-container test.

Verified locally:

```text
PYTHONPATH=src python3 -m unittest discover -s tests -q
Ran 197 tests ... OK

python3 -m compileall -q src tests
OK
```

The review environment did not have Docker installed, so Docker build, release
-gate, Compose, volume-permission, and live-listener claims are not marked as
runtime-verified here.

## What is solid today

* The project has a clear provider seam, a shared router/error vocabulary, and
  explicit capability checks rather than silently guessing model dialects.
* Credential/config separation is structurally visible in code. Config is
  atomically persisted; provider credential files are separate and intended to
  be owner-only.
* Browser controls are layered: canonical Host, forwarded-header rejection,
  and origin validation for browser mutations. **Deployed:** there is no
  owner session and no CSRF bootstrap, by product decision. **Owner-session
  candidate:** one `HttpOnly`, `SameSite=Strict` session cookie guards every
  `/owner/*` route except the console shell/assets and the three pre-auth
  auth endpoints (always — except while the owner has set
  `owner_console_auth=false`, which serves them without a session; see the
  `owner_console_auth` register entry below); there is still no CSRF
  bootstrap token — `SameSite=Strict`
  plus the canonical `Origin`/`Host` checks carry that.
* There is one listener. The owner console, the configuration API and the
  OpenAI-compatible API share it. **Deployed:** any peer that can reach it is
  an administrator by product decision. **Owner-session candidate:**
  reaching `/owner/*` is no longer administration — a valid owner session is
  required (unless the owner has set `owner_console_auth=false`) — while
  `/v1/*` stays keyless until the owner sets a client key,
  so any reachable peer is still a caller on the model API.
* Cross-provider fallback is opt-in and has hard stops for tools and unsafe
  classes. The test suite covers most of those safety decisions.
* A capability and a preference are treated differently: dialect, streaming
  and tools are hard requirements, while a reasoning effort is dropped for a
  route that cannot honour it - and the substitution is recorded.
* `/version` has a deliberately fixed, metadata-only schema, and a
  deployment that cannot identify itself warns about it at startup.

These are code/test observations, not a claim that a provider account or a
container deployment has been exercised.

## Findings requiring implementation work

### R0 — A LAN client can claim the first owner passphrase — **closed**
**Severity: release blocker for LAN-reachable first boot.**

Setup now binds to the private LAN address by default (`src/gateway/cli.py:159-
162` and `compose.yaml:30-36`). When no passphrase exists,
`handle_login` accepts any request reaching the setup surface and immediately
stores the submitted passphrase (`src/gateway/http/app.py:467-490`). There is
no prior owner session by definition and no loopback/bootstrap check in that
branch. A different LAN host can therefore become the owner before the
intended owner visits setup.

**Required fix:** make initial passphrase creation loopback-only even if later
setup is LAN-reachable, or require an explicit out-of-band bootstrap secret.
Add a LAN-peer refusal test for uninitialized setup plus a successful
loopback/bootstrap test. Until then, bind setup to `127.0.0.1` for first boot
and switch only after the passphrase exists.

**Resolution.** Closed in two steps, taking the finding's second option
(explicit out-of-band bootstrap secret) rather than the loopback-only one.
First, the trusted-LAN admin-mode change removed passphrase creation and
owner sessions altogether (see "Trusted LAN admin-mode candidate update"
below), so the offending branch — an unauthenticated `handle_login` storing
any submitted passphrase — no longer existed. Second, the owner-session
candidate reintroduces first-run setup **gated by a one-time setup code**:
`ensure_setup_code()` (`src/gateway/owner_auth.py`) mints a 128-bit
`secrets.token_urlsafe(16)` code only while no account exists, returns the
plaintext solely for server-side stdout, and writes it to `owner_setup_code`
(`0600` inside a `0700` data directory); `POST /owner/api/auth/setup`
re-derives its hash and compares with `hmac.compare_digest`, consumes it
exactly once and deletes the file. No HTTP response, log line or access-log
record ever carries the code, and there is no default credential — so a LAN
peer that can reach the listener still cannot claim first ownership without
reading the code off the gateway host, which is the privilege the finding
was about.

Regression evidence: `tests/test_owner_auth.py` —
`test_a_fresh_gateway_has_no_account_and_no_default_credential`,
`test_the_setup_code_lives_on_the_host_and_never_in_a_response`,
`test_setup_is_single_use_and_a_wrong_code_is_refused`,
`test_owner_endpoints_require_a_session_while_the_shell_does_not` — and
`tests/live_owner_auth_smoke.py` (34 checks driving setup, sign-in,
throttling, key set/rotate/clear and secret non-exposure over real HTTP
against a throwaway gateway). Claimed by the security review of `154971e`
(card `t_e4577180`, not re-run here): 8 concurrent setup attempts produced
exactly one winner with the code single-use, and the code was absent from
every public response and access-log record. The loopback-only variant of
the required fix is deliberately not implemented: the approved policy keeps
one listener on the private bind with no separate setup surface
(`docs/TRUSTED_LAN_ADMIN_MODE.md`).

### R1 — ChatGPT/Codex device flow cannot complete in the current UI — **closed**
**Severity: release blocker for the ChatGPT/Codex setup flow.** *(closed)*

`DeviceAuthorization.to_dict()` deliberately omits `device_auth_id`
(`src/gateway/adapters/chatgpt_codex.py:115-123`). The browser stores only
that response (`src/gateway/static/console.js:573-577`) and later submits
`pending.device_auth_id` to the poll action (`console.js:593-594`). The value
is absent, so the server receives an empty ID and returns
`unknown_authorization` (`src/gateway/http/app.py:767-772`).

**Required fix:** return a short-lived opaque poll handle that the setup UI can
present, bind it to the in-memory pending authorization, and add an end-to-end
browser/API test covering start → poll → approved. Do not expose stored tokens
or a device code through a later read endpoint.

**Resolution.** The start action now returns an opaque `poll_handle` minted by
the gateway. The provider's `device_auth_id` never leaves the process. The
pending record is keyed by that handle, is in memory only, and is pruned when
it expires. The one-time code is returned only by the start action; the poll
response carries the handle, state, interval and expiry only. The console
polls at the interval the provider asked for, and keeps polling until the
state is terminal.

**Evidence:** `tests/test_device_flow.py` — start returns an opaque handle;
the provider identifier is absent from the response; poll with the handle
reaches `approved` and stores the credential; the one-time code is absent from
the poll response; an unknown, missing or expired handle is refused with
`unknown_authorization`; the flow is unreachable from the API surface. The
wire contract was also exercised against the live OpenAI endpoint: the start
request issues a code and the poll reports `pending` until the code is
approved.

### R2 — Compose deployment has no verified writable-volume initialization — **closed**
**Severity: release blocker until exercised and fixed if necessary.** *(closed)*

The image runs as UID/GID `10001` (`Dockerfile:48-49`), and Compose mounts a
new named volume at `/var/lib/gateway` (`compose.yaml:36-38`). A Docker named
volume is normally root-owned when first mounted; the image-layer ownership
does not initialize the mounted volume. Startup calls `os.chmod(data_dir,
0o700)` without recovery (`src/gateway/cli.py:99-103`) and must then create
files beneath it. The release gate starts a disposable container without the
Compose volume (`scripts/release_gate.py:203-219`), so it cannot prove this
production path.

**Required fix:** use a narrowly scoped, audited initialization strategy that
creates/chowns only the gateway state directory before the non-root process
starts, or provision the volume ownership out of band. Add a Compose-level
smoke test which verifies first boot, a persisted config write, and restart.
Preserve the non-root runtime, read-only root filesystem, and no-new-
privileges controls.

**Resolution.** A one-shot `state-init` service runs before the gateway and
executes `scripts/init_state_dir.py`, which touches **exactly one path** - the
gateway state directory - creates it if missing, sets mode `0700`, and sets
its owner to the runtime UID/GID. It is the only part of the deployment that
runs as root, it runs with `no-new-privileges`, `cap_drop: ALL` and only
`CHOWN`, `FOWNER` and `DAC_OVERRIDE`, and it is idempotent. The gateway
itself still runs as UID/GID 10001 with a read-only root filesystem.

The Compose deployment is now exercised by a test, not by assumption:
`tests/test_compose_deployment.py` builds the image, brings the real
deployment up on its own project and volume, and proves that the volume is
owned by `10001:10001`, that the gateway writes its configuration as that
user, that a configuration change **survives a restart**, that the process
listens only on the configured address with no wildcard and no `:8766`, and
that the release step publishes an identity that `/version` then reports
exactly.

**Evidence:** `tests/test_compose_deployment.py`, and
`scripts/init_state_dir.py` for the narrowly scoped initialiser.

### R3 — Release gate listener probe does not reliably inspect bind addresses — **closed**
**Severity: high.** *(closed)*

The gate reads `/proc/net/tcp` as raw strings and indexes character positions
rather than parsing the local address/state fields
(`scripts/release_gate.py:325-345`). It then compares that output to textual
`0.0.0.0`/`::`, even though proc entries are hexadecimal. This cannot prove
that a wildcard listener is absent. It also does not inspect IPv6.

**Required fix:** parse `/proc/net/tcp` and `/proc/net/tcp6` correctly, or use
a small standard-library socket/proc parser with unit fixtures. Add negative
fixtures for wildcard IPv4 and IPv6 plus the expected explicit loopback/LAN
binds.

**Resolution.** The gate now decodes the hexadecimal local address of both
`/proc/net/tcp` and `/proc/net/tcp6`, and it scopes the result to the
**gateway process's own sockets** by matching the socket inodes in
`/proc/<pid>/fd`. That scoping matters: a container with host networking
shares the host's network namespace, so an unscoped probe reports every
socket on the machine, including unrelated ones, and cannot attribute a
listener to the gateway.

The gate additionally asserts that the gateway listens *only* on the
configured address, which also proves the retired `:8766` port is not bound.

**Additional regression hardening.** Candidate PID discovery now requires one
unique `python -m gateway` process; missing or ambiguous attribution returns
no sockets rather than falling back to the host-wide listener table. Gate
acceptance requires exactly the candidate-owned `127.0.0.1:18775` listener, so
an unrelated listener, another port (including retired `:8766`), wildcard, or
additional socket fails. Evidence: `test_release_gate.py` covers missing and
wrong PID, unrelated host listener, exact expected port, wildcard, and retired
port refusal.

**Evidence:** `tests/test_release_gate.py` — hexadecimal IPv4 and IPv6
decoding, wildcard recognition in both families, explicit loopback binds,
non-listening sockets ignored, and process-scoped filtering that excludes a
wildcard socket belonging to another process. The gate itself now reports
`listening=[('127.0.0.1', 18775)]` for the candidate.

### R4 — Published aliases and profile identifiers are insufficiently validated — **closed**
**Severity: high correctness/recoverability risk.** *(closed)*

Mutation handlers accept unvalidated alias names, profile IDs, model IDs, and
route references (`src/gateway/http/app.py:861-902`, `917-956`, and
`1011-1068`). A caller can save an empty or arbitrary alias/profile identifier
or publish an alias that points at an unknown provider/model. The alias then
appears in `/v1/models` but fails later during routing. Profile-token storage
can also fail after an invalid profile ID is already persisted because the
credential-store ID validation occurs only when a token operation reaches it
(`src/gateway/credentials.py:50-57`).

**Required fix:** define one identifier grammar and length limit, validate
before opening a config transaction, require primary/fallback routes to refer
to enabled registered models, reject duplicate/primary-equal fallbacks, and
return actionable `4xx` errors. Add tests for invalid IDs, invalid routes, and
a route that is rejected before publication.

**Resolution.** `gateway/identifiers.py` defines one grammar and one length
limit for aliases and profile ids: a letter or digit first, then letters,
digits, dots, underscores and hyphens, at most 64 characters. A model id has
its own slightly wider grammar, because provider-qualified ids carry colons
and slashes. Nothing may contain a path separator or a control character.

`gateway/routes.py` validates a route against the live configuration: the
provider must be registered, and the model must be known **and enabled**. A
fallback may not repeat the primary or another fallback.

Everything is checked **before** a configuration transaction opens, so a
refused alias is never written and never appears in `/v1/models`. Publishing
re-checks the route, because that is the last moment a route that cannot be
served can be refused. Every refusal is an actionable 4xx naming the field
and the rule.

**Evidence:** `tests/test_validation.py` - the grammar and its length limit;
empty, illegal, over-long and path-like identifiers refused; routes to an
unknown provider, an unknown model and a disabled model refused; a fallback
repeating the primary or another fallback refused; a refused alias neither
persisted nor published; a valid alias saved and publishable.

### R5 — Observability documentation overstates the current implementation — **closed**
**Severity: medium; honesty and operations risk.** *(closed)*

`RequestLedger` describes request outcomes, timestamps, latency, and errors
(`src/gateway/observability.py:54-123`), but `Router.invoke` records only the
successful response path (`src/gateway/core/routing.py:469-487`). It passes an
empty timestamp and `None` latency. Provider errors that raise before that
point are not recorded, so the Status totals cannot support the claimed error,
rate-limit, or exhaustion counts for real failures.

**Required fix:** decide whether the ledger is a success-only trace or a real
metadata-only outcome ledger. If the latter, record safe error outcomes on
all terminal paths with a generated timestamp and measured latency; preserve
the no-content/no-secret rule. Add failure-path tests and make the Status UI
label match the actual retention/durability.

**Resolution.** The ledger is now a real metadata-only outcome ledger. Every
terminal path is recorded - success, provider failure, failed fallback, and
the "no eligible route" case - with a generated timestamp and a measured
latency. The record still carries only the alias, the provider that served
or failed, the outcome, the duration and the counters; the no-content rule is
unchanged and covered by a test.

The Status view now states the durability plainly: the ledger is in memory
only, in a bounded ring of the most recent 10,000 requests, is not written to
disk, and is lost on restart.

**Evidence:** `tests/test_observability_records.py` - a success records a
non-empty timestamp and a measured latency; a provider failure records its
error class and updates the error, rate-limit and exhaustion counters; the
record contains no prompt, message, content or completion field.

### R6 — Owner-session test TTL is ignored — **superseded**
**Severity: medium testability/correctness defect.** *(superseded)*

`OwnerAuth` accepts `session_ttl` in its constructor
(`src/gateway/auth.py:74-84`), but `OwnerSession.expired()` always uses the
module constant (`auth.py:59-68`). The injected value does not affect expiry,
which makes short-TTL tests misleading.

**Required fix:** carry the configured expiry/TTL into the session or evaluate
expiry in `OwnerAuth`; add boundary tests for expiry and pruning.

**Superseded.** The owner session, the passphrase and the owner login flow
were removed with the trusted-LAN administration mode
(`docs/TRUSTED_LAN_ADMIN_MODE.md`). `OwnerAuth` and `OwnerSession` no longer
exist, so there is no TTL to ignore.

### R7 — CI artifact recording can fail for locally built images — **closed**
**Severity: medium CI robustness risk.** *(closed)*

`image_digest()` has a fallback to the image ID when `RepoDigests` is absent
(`scripts/release_gate.py:86-94`). The CI workflow independently evaluates
`{{index .RepoDigests 0}}` without that fallback
(`.github/workflows/ci.yml:41-45`). A local candidate normally has no registry
repo digest, so this reporting step may fail despite a valid image and gate.

**Required fix:** share the release gate's ID/digest resolution logic or make
the workflow use a safe shell fallback. Test the empty-`RepoDigests` case.

**Resolution.** The workflow now falls back to the image ID when
`RepoDigests` is empty, so it records the same value the release gate
resolves and writes into the release identity.

### R8 — ChatGPT/Codex model refresh violates the shared adapter contract — **closed**
**Severity: release blocker for the ChatGPT/Codex onboarding flow.** *(closed)*

`ChatGPTCodexAdapter.list_models()` returns strings
(`src/gateway/adapters/chatgpt_codex.py:390-409`), whereas the generic refresh
handler requires `ModelInfo` attributes such as `model_id` and `dialect`
(`src/gateway/http/app.py:703-733`). A normal ChatGPT refresh therefore hits
the generic 500 path instead of populating selectable models.

**Required fix:** make every adapter return `list[ModelInfo]`, with the known
Responses dialect for ChatGPT/Codex, and add HTTP-level refresh tests for both
real adapters.

**Resolution.** The ChatGPT/Codex adapter now returns `list[ModelInfo]`.
Only the wire dialect is asserted - the ChatGPT-authenticated upstream is the
Responses protocol, which is a protocol fact rather than a per-account guess.
Availability, limits, retention and context for a given account are reported
as **unknown**, with a note saying so, because OpenAI does not publish them.

**Evidence:** `tests/test_adapter_contract.py` - both adapters return
`list[ModelInfo]`; the ChatGPT dialect is the Responses protocol and the
account-dependent facts are unknown; and the **generic** refresh handler is
exercised over HTTP for both real adapters, asserting that the model list is
populated with the right dialect.

### R9 — Non-streaming responses are returned in the upstream dialect — **closed**
**Severity: release blocker for OpenAI-compatible Chat Completions clients.** *(closed)*

When the source and upstream dialect differ, request conversion runs, but the
non-streaming response body is returned verbatim
(`src/gateway/http/app.py:382-400`). ChatGPT/Codex responds in the Responses
dialect (`src/gateway/adapters/chatgpt_codex.py:428-467`), so a request to
`/v1/chat/completions` can receive a Responses object rather than a Chat
Completions object. The existing tests cover request conversion and stream
conversion, not this response shape.

**Required fix:** add bidirectional buffered-response conversion or refuse the
unsupported non-streaming combination until conversion exists; cover both
endpoint/dialect permutations with end-to-end assertions.

**Resolution.** `gateway/bridge/buffered.py` converts a buffered response in
both directions: a Responses object becomes a Chat Completions object, and a
Chat Completions object becomes a Responses object. The assistant text, the
tool calls with their identifiers, the finish reason and the usage counters
all survive. A Responses reasoning item has no Chat Completions counterpart
and is **dropped rather than invented as content**, and the absence of usage
counters stays absent rather than becoming a zero.

A body that cannot be parsed or converted is returned unchanged, so a caller
sees the upstream's own object rather than a silently wrong one.

**Evidence:** `tests/test_buffered_bridge.py` - the text, the tool calls and
their identifiers, the finish reason and the usage counters survive in both
directions; a reasoning item is dropped rather than invented; and, end to end
over HTTP, a Chat Completions client receives a `chat.completion` object from
a Responses upstream and a Responses client receives a `response` object.

### R10 — Device authorization is neither setup-only nor shown-once — **closed**
**Severity: high credential-boundary and flow-correctness risk.** *(closed:
shown-once and interval fixed; the surface rule superseded)*

The device start/poll actions do not require `_can_enroll_credentials()`
(`src/gateway/http/app.py:751-788`), while API-key enrollment does
(`app.py:790-814`). In authenticated mode, the LAN owner console can therefore
initiate a device flow even though credential enrollment is documented as
setup-only. Separately, pending poll responses serialize the user code again
(`app.py:774-788`), contradicting the shown-once contract; the UI does not use
the provider-supplied poll interval (`src/gateway/static/console.js:595-615`).

**Required fix:** gate device start/poll/exchange on the setup surface and
owner session, bind each pending flow to that session, return the code only
from the initial explicit start action, honor the interval, and test pending,
denied, expired, approved, and owner-console-refusal cases.

**Resolution.** The shown-once rule now holds: the code is returned only by
the start action and never by a poll. The console polls at the provider's
interval, and keeps polling until the state is terminal. The pending record
is in memory, keyed by an opaque handle, and is pruned on expiry.

The *surface* requirement is superseded rather than implemented: there is no
longer a separate setup surface or an owner session, because
`docs/TRUSTED_LAN_ADMIN_MODE.md` makes the single owner console the
administration surface and states that any peer able to reach the configured
address is an administrator. The device actions are reachable only through
the owner console routes, never from the API surface, which is covered by a
regression test.

**Evidence:** `tests/test_device_flow.py` — the code is absent from the poll
response; an unknown, missing or expired handle is refused; the flow is
unreachable from the API surface; pending, approved and denied states are
exercised.

### R16 — The upstream conversation session was never supplied — **closed**
**Severity: release blocker for every OpenCode Go route.** *(closed; found by
live testing against the provider)*

OpenCode Go **requires** an `x-opencode-session` header and refuses a
request without one:

```json
{"type": "error", "error": {"type": "MissingSessionID",
 "message": "Request is missing x-opencode-session and cannot be routed
 efficiently"}}
```

The adapter only set that header when the *client* supplied one, and most
OpenAI-compatible clients send none, so every request to an OpenCode Go route
failed at the provider's router.

**Resolution.** The derivation now lives in the **router**, not in one
handler, so every path that reaches a provider carries one: the API surface,
the console's alias test, and any future caller. The gateway derives the
identifier server-side: stable for the same client and alias so the provider
can cache the conversation, different for different clients so two callers
cannot collide, validated so a caller-supplied hint cannot smuggle bytes
upstream or forge another client's session, and namespaced by provider so a
fallback does not inherit the primary's conversation key. The derivation key
is per-process, so a restart ends the session.

The console's alias test was a second instance of this defect: it called the
router directly and omitted the identifier, so the provider refused the test
with `MissingSessionID` even though the API path worked. Deriving it in the
router closes that path too.

Separately, the safe upstream-code pattern only accepted lowercase, so the
provider's camel-cased `MissingSessionID` was discarded and a generic `error`
was reported instead. The pattern now accepts uppercase. The upstream body is
still never echoed - only the code.

**Evidence:** `tests/test_session_header.py` - the identifier is always
present and provider-namespaced; it is stable per client and alias; two
clients differ; a supplied hint is bounded, sanitised and cannot be used to
collide; a camel-cased upstream code is reported and the body is not echoed;
and the console's alias-test path carries an identifier and the upstream
model id.

### R17 — The alias was sent upstream instead of the model id — **closed**
**Severity: release blocker for every aliased route.** *(closed; found by live
testing against the provider)*

The client asks for an alias such as `coding`, but the provider only knows
its own model ids. The gateway forwarded the payload unchanged, so the
provider received `"model": "coding"` and answered:

```json
{"error": {"type": "server_error", "message": "Upstream request failed:
 Model is unavailable."}}
```

The gateway classified that as a malformed request, which is a confusing
message for a substitution the gateway owns.

**Resolution.** The router replaces the payload's `model` with the upstream
model id of the chosen route. The caller's own request is not mutated, and
the ledger still records the requested alias alongside the provider and
model that actually served the request.

**Evidence:** `tests/test_upstream_model.py` - the upstream receives the
provider's model id, the alias does not appear in the upstream payload, the
caller's mapping is unchanged, and the ledger records both the alias and the
serving model.

### R11 — Production `/version` identity is not mechanically published — **closed**
**Severity: high release-traceability risk.** *(closed)*

The release gate writes `release.json` only inside a disposable candidate
container and deletes it afterwards (`scripts/release_gate.py:203-219`,
`264-287`, `370-371`). Compose uses a distinct named volume
(`compose.yaml:36-38`) and there is no production cutover step that writes the
verified digest to that volume. A normal Compose start can consequently report
`image_revision: "unknown"` despite the README/runbook claiming exact running
identity.

**Required fix:** deploy an immutable image reference without rebuilding,
atomically write the inspected exact image ID/digest to the actual Compose
state volume during cutover, then compare that value with `/version` from the
running Compose service.

**Resolution.** `scripts/release.py` is the cutover step. It resolves the
immutable identity of an **already built** image - the registry digest if it
has one, otherwise the image ID - and never rebuilds or retags it. It starts
the deployment, writes that identity into the state volume atomically
(temp file plus `os.replace`, mode 0600, as the runtime user), and then
verifies from outside the container that:

* `/health` answers;
* `/version` returns the fixed four-key schema;
* `/version` reports exactly the identity that was published;
* the running container's image is exactly that identity;
* the process listens only on the configured address, with no wildcard and no
  retired `:8766`.

Every step is checked, and a mismatch stops the release rather than leaving a
deployment that cannot say what it is.

**Evidence:** `tests/test_compose_deployment.py` runs the release step against
the real deployment and asserts that `/version` reports the exact image that
is running.

### R12 — Profile and provider session isolation need explicit policy — **closed**
**Severity: medium authorization/privacy risk.** *(closed)*

An empty profile allow-list permits every published alias
(`src/gateway/auth.py:262-269`), so a newly issued profile token is broad by
default. Separately, clients can supply `X-Session-ID`/`X-OpenCode-Session`,
which is forwarded upstream without profile/provider namespacing or validation
(`src/gateway/http/app.py:367-380`, `src/gateway/adapters/opencode_go.py:133-
147`).

**Required fix:** deny a new profile until aliases are explicitly granted (or
use a separately confirmed all-alias flag), and generate/bind upstream session
identifiers server-side with profile/provider scope, expiry, size limits, and
cross-profile collision tests.

**Resolution.** *Profiles deny by default.* A profile with no grant may use no
alias. Access is granted alias by alias, or with a separate, explicit
`all_aliases` flag that the owner has to set. An empty allow-list is not a
grant. The Anonymous LAN profile is granted everything explicitly, because
LAN API invocation is anonymous by design.

*Upstream session identifiers* are generated server-side in
`gateway/session.py` and are never taken verbatim from the client. The
derivation is stable per client and alias, differs between clients, is
namespaced by provider in the router, is reduced to a safe bounded alphabet
when a client supplies a hint, and is scoped to the process, so a restart
ends the session. The derivation lives in the router, so every path that
reaches a provider carries one.

**Evidence:** `tests/test_validation.py` - a new profile denies; an explicit
grant is honoured; the all-alias grant is separate; the anonymous profile may
use every published alias. `tests/test_session_header.py` - the identifier is
always present, provider-namespaced, stable per client and alias, different
between clients, and a supplied hint is bounded, sanitised and cannot be used
to collide.

### R13 — Plain HTTP is an explicit but high-risk LAN trust assumption — **closed**
**Severity: decision required before using owner/caller credentials on an
untrusted LAN.** *(closed: the decision was made and recorded)*

The implementation intentionally serves HTTP and has no TLS (`README.md`,
"Known limitations"), yet LAN traffic includes owner cookies, bearer tokens,
and request content. This is acceptable only where the LAN itself is trusted;
it is not safe against an on-path LAN attacker. The current forwarded-header
rejection also means the documented TLS-terminator suggestion needs an
explicit proxy-trust design before adoption.

**Required action:** either make the trusted-LAN assumption a clear deployment
precondition, or design a tested TLS/loopback/proxy model. Do not advertise a
proxy topology the request-hardening layer rejects.

**Resolution.** The decision is the first option, and it is recorded rather
than implied. `docs/TRUSTED_LAN_ADMIN_MODE.md` states the trust boundary
outright: any device that can reach the configured address can administer the
gateway and invoke its models, and there is no password or profile that
restricts administrative access. The console shows the same notice,
permanently and non-dismissably, at every viewport. The README lists "no TLS"
and "trusted LAN administration has no authentication boundary" as known
limitations.

The proxy topology is **not** advertised: forwarded and proxy headers are
rejected outright, and the documentation says so.

**Superseded by the owner-session candidate** (SEC-1 documentation
reconciliation, card `t_bbea51dc`): for `/owner/*` there is now a password,
so "no password or profile restricts administrative access" no longer
describes the candidate. `docs/TRUSTED_LAN_ADMIN_MODE.md` and the new
`docs/OWNER_AUTH.md` carry the current two-boundary statement, the README's
known limitations name the still-keyless model API instead of
"administration has no authentication boundary", and the console no longer
carries that notice. The plain-HTTP half of R13 is untouched: the listener
is still HTTP-only, the session cookie is still not `Secure`, and "trusted
LAN" still means "no on-path observer". The deployed gateway keeps the
boundary this Resolution recorded until the candidate ships.

### R14 — Configuration rollback instructions currently only make a copy — **closed**
**Severity: medium operator-recovery defect.** *(closed)*

The runbook's “Rollback the configuration” command copies current
`config.json` to a `.before-upgrade` file and restarts; it never restores a
prior snapshot (`docs/RUNBOOK.md:183-193`).

**Required fix:** document separate pre-upgrade backup and restore commands,
validate restored JSON/permissions before startup, and state the limits around
provider-side tokens and restart-lost state.

**Resolution.** `docs/RUNBOOK.md` now gives separate pre-upgrade **backup**
and **restore** commands, a validation step that parses the restored file
before the gateway is started, and an explicit statement that the credential
store is a separate file that a configuration rollback never touches, that
tokens the provider has already issued are not revoked by a rollback, and
that the request ledger is lost on restart.

### R15 — Setup-surface helper text still claims host-only access — **superseded**
**Severity: low documentation/UI honesty defect.** *(superseded)*

When credential enrollment is unavailable, the browser says setup is reachable
only from the host or an SSH tunnel (`src/gateway/static/console.js:563-566`).
That conflicts with the configurable LAN setup listener. It should render the
actual setup base URL or say "the configured setup listener" without
inventing a host-only restriction.

**Superseded.** The separate setup surface was removed with the trusted-LAN
administration mode, so the text no longer exists. The console now states the
trusted-LAN boundary plainly instead, which is the honest form of the same
warning.

## Documentation corrections made in this review

* `README.md` no longer claims the gateway never stores operational
  credentials; it now distinguishes stored owner-only secrets from exposed
  data.
* The fresh-install text now accurately says provider cards exist while
  credentials and discovered/published models do not.
* OpenCode Go setup now accurately says **Save and test**: that explicit UI
  action stores then tests the key, while ordinary page loads never call a
  provider.
* `RUNBOOK.md` now reflects the actual default: in LAN no-auth mode all
  mutation remains in setup, not the LAN owner console.
* `ARCHITECTURE.md` and `DEVELOPMENT.md` establish the verified runtime map,
  security/data boundaries, and an exact-image/Compose verification standard.

### R18 — The SSE frames were not terminated — **closed**
**Severity: release blocker for every streaming client.** *(closed; found by
live testing against a real client)*

The upstream reader consumed the blank line that terminates an SSE frame and
did not include it in the frame it yielded, so every **passthroughed** stream
reached the client as single-newline terminated lines with no frame separator
at all. A lenient reader still sees the lines, so this looked fine to `curl`
and in the console. A client that parses SSE strictly reads the whole stream
as one event, finds no `finish_reason`, and reports *"provider returned an
empty stream with no finish_reason"*.

This affected every same-dialect passthrough - in practice every OpenCode Go
streaming response. Converted streams were unaffected, because the converter
re-frames what it emits, which is why ChatGPT streaming worked and OpenCode
Go streaming never did.

**Resolution.** Each yielded frame is a complete frame: its data lines plus
the blank line that ends it, whether the frame ended at a blank line or at
the end of the stream.

**Evidence:** `tests/test_sse_framing.py` parses the output the way a
strict client does, and asserts the terminator is present in the raw bytes.

### R19 — A reasoning effort excluded the route — **closed**
**Severity: high correctness/availability risk.** *(closed)*

The capability check treated an unsupported `reasoning_effort` as a hard
requirement and **excluded** the route. Most OpenCode Go models publish no
reasoning levels at all - 2 of 37 - so every one of them was excluded from
any request that asked for one, and a configured fallback was effectively
dead for exactly the kind of request a coding agent sends. The route was
skipped before the fallback policy was ever consulted, and the request failed
with "no eligible route".

**Resolution.** A capability and a preference are now different things.
Dialect, streaming and function tools remain hard requirements. A
`reasoning_effort` is a preference: the route stays eligible, the effort is
dropped *for that route*, and the substitution is recorded. Only the route
that actually served the request records it.

**Evidence:** `tests/test_degraded_route.py`.

### R20 — The ChatGPT / Codex upstream is streaming only — **closed**
**Severity: release blocker for the ChatGPT path.** *(closed; found by
probing the live endpoint)*

The ChatGPT / Codex backend refuses a Responses request that does not set
`stream: true`, and it expects the same request fields OpenAI's own Codex
client sends. The adapter honoured the *client's* stream flag, so a
non-streaming client sent `stream: false` and the backend answered 400.

Probed directly, one field set at a time:

```text
stream=false, bare            -> 400
stream=true,  bare            -> 400
stream=false, codex fields    -> 400
stream=true,  codex fields    -> 200
```

**Resolution.** The adapter always asks the upstream to stream, and sends the
fields the Codex client sends: `tool_choice`, `parallel_tool_calls`,
`store: false` and `include`. A client that asked for a single, non-streamed
completion is served by consuming the upstream stream and presenting the
result as one object.

**Evidence:** `tests/test_streaming_only_upstream.py`, and the four API
permutations verified live.

### R21 — The Codex model catalogue has its own shape — **closed**
**Severity: release blocker for the ChatGPT onboarding flow.** *(closed)*

The Codex catalogue is a list under `models`, and a model is identified by
its **`slug`**, not by an `id`. The parser looked for `id`, found none, and
silently reported an empty catalogue - "Catalog refreshed: 0 models" - with
no error and nothing to act on. The catalogue also carries the reasoning
levels a model supports, its context window, its visibility, and whether the
account can use it through the API at all.

**Resolution.** The parser reads the slug and carries the published
metadata. A model with `supported_in_api: false` is not offered. An
OpenAI-shaped `{"data": [{"id": ...}]}` catalogue is still accepted.

**Evidence:** `tests/test_adapter_contract.py`, with a fixture in the real
Codex catalogue shape.

### R22 — The model list request needs the client version — **closed**
**Severity: release blocker for the ChatGPT onboarding flow.** *(closed)*

The Codex client appends `?client_version=<whole semver>` to the model-list
URL, and the backend expects it. The adapter called `/models` bare and the
backend answered `400 invalid_request_error`.

**Resolution.** The gateway appends its own version - honestly identifying
itself rather than impersonating the Codex client - and the value is
validated to be a whole semantic version.

**Evidence:** `tests/test_adapter_contract.py`.

### R23 — The release identity could be unknown without anyone noticing — **closed**
**Severity: medium release-traceability risk.** *(closed)*

A deployment reported `unknown` for all three release fields and nothing
said so. The image had been built without the source revision and tree, so
the identity baked in as `unknown`, and the identity was never written to
the state volume either.

**Resolution.** `scripts/release.py --build` builds from the checkout and
stamps the revision and the tree, then publishes the image digest into the
state volume and verifies it. The gateway warns at startup when its release
identity is unknown, because a deployment that cannot identify itself
cannot be audited - and because it is easy not to notice.

**Evidence:** `scripts/release.py --build`, and the startup warning in
`gateway/cli.py`.

### R24 — A dataclass field was declared twice, and the second one was shadowed — **closed**
**Severity: low latent-correctness risk.** *(closed)*

`ModelInfo` declared `context_window` twice in its class body
(`src/gateway/core/capabilities.py:125` and `:133`). A dataclass cannot hold
two fields of one name: the second declaration silently overwrites the first,
and the shadowed declaration is invisible at runtime, because
`dataclasses.fields` reports a mapping and a mapping cannot have a duplicate
key. Both declarations were identical, so no behaviour was wrong - but a
reader who changed only one of them would have had no signal that the other
existed, and the field's position in the generated `__init__` came from the
*first* declaration while its type and default came from the *second*.

**Resolution.** The shadowing declaration is removed. The surviving
declaration keeps the first declaration's position, so the generated
`__init__` signature is byte-for-byte the same: `model_id, dialect,
context_window, max_output_tokens, reasoning_efforts, supports_streaming,
supports_function_tools, supports_builtin_tools, supports_image_input,
supports_previous_response_id, context_limit, retention_class,
monthly_limit_usd, limits_source, limits_observed_at, source, notes`.

The defect is invisible to the runtime, so the regression test reads the
source instead: it parses every module under `src/gateway` and fails if any
class body declares the same annotated field name twice.

**Evidence:** `tests/test_capabilities.py` - the package-wide scan passes on
the current source and names the exact `(file, line, class, field)` when a
duplicate is reintroduced.

### R25 — A thinking control was silently dropped on a converted request — **closed**
**Severity: high contract-correctness risk.** *(closed; superseded by **R35**,
which closed it for the Chat Completions source path, and completed by
**T4**, which closed the same defect on the Responses source path and made
one path unable to disagree with another)*

`convert_request` forwards a same-dialect payload unchanged, but a
**converted** request is rebuilt from the normalised form, and the
normalised form carries only OpenAI's flat `reasoning_effort` string
(`src/gateway/bridge/requests.py:265`). Two shapes therefore vanish on the
converted path:

* `reasoning: {"effort": "high"}` - the newer Chat Completions spelling.
  `_without_disabled_reasoning` reads this exact field for the *disabled*
  case (`requests.py:58`), so the two code paths disagree about one field.
* `thinking: {"type": "enabled", "budget_tokens": 32000}` - Anthropic's
  thinking control, which the gateway does not understand at all.

Verified against the current code:

```text
chat->chat  (same dialect) : thinking survives
chat->responses (converted) : thinking is absent from the payload
chat->responses (converted) : reasoning is absent from the payload
```

This breaks the bridge's own stated contract: *"Nothing is silently
dropped. If the target dialect cannot express the request, the request is
refused."* (`requests.py:7`). A client that asks for a thinking level gets
a request that succeeded, at a level the owner did not choose, with no
substitution recorded and no error raised.

**Required fix:** one canonical thinking-level model, parsed from every
inbound spelling and rendered per dialect; a level the target dialect
cannot express is refused, and a level the model does not publish is
dropped *with the substitution recorded*, under the R19 preference rule.

**Resolution.** One vocabulary, parsed from every inbound spelling and
rendered per dialect, in `src/gateway/thinking.py` and
`src/gateway/bridge/requests.py`:

* `reasoning_effort`, `reasoning.effort`, `reasoning.enabled` and the
  disabled level `none` normalise to one canonical level on **both**
  source dialects. The Responses source path used to read only the
  nested spelling, so a flat `reasoning_effort`, a `thinking` budget, a
  `thinkingConfig`, a conflicting pair or a wrong-typed value was
  resolved or dropped without a word;
* the level renders as `reasoning: {effort}` for Responses and
  `reasoning_effort` for Chat Completions, and "disabled" is rendered
  per dialect rather than dropped: Responses spells it `effort: "none"`
  (both published Responses models list it), Chat Completions omits the
  control (no served chat-dialect model publishes it, and OpenAI
  documents HTTP 400 from a model that does not support `none`);
* the two paths can no longer disagree, because the rendering is a
  function of the **target** dialect on the same-dialect path as well as
  the converted one. The concrete case this finding named - the nested
  spelling disagreeing with `_without_disabled_reasoning` - is pinned by
  a test on all four path/dialect combinations;
* a control that is not portable - an Anthropic `thinking` budget, a
  Google `thinkingConfig`, a `reasoning.summary` on a Chat Completions
  target, an unknown field inside `reasoning`, a wrong-typed value, or
  two spellings that contradict each other - is **refused** with an
  actionable message;
* a level the selected model does not publish is still dropped for that
  route with the substitution recorded, under the R19 preference rule,
  and a level outside the gateway's own vocabulary is forwarded rather
  than refused, so the router - not the vocabulary - decides.

**Regression evidence:** `tests/test_thinking_levels.py` (19 tests: the
vocabulary and its cited sources, every inbound spelling on both source
dialects, per-dialect rendering, the refusal table, the four-path
disabled-state agreement, and the drop-with-record asserted against the
ledger's `degradations` and `serving_provider`/`serving_model`), plus
`tests/test_thinking_controls.py` and `tests/test_bridge.py`, which
continue to pass unchanged.

### R26 — The published per-token prices are never read, and no request records its usage — **closed**
**Severity: medium observability gap.** *(closed; request-scoped catalogue snapshot verified)*

Three finished pieces are not connected:

* the bridge already normalises usage on both paths
  (`src/gateway/bridge/streams.py:174-190`, `bridge/buffered.py:103-127`);
* `RequestEvent` already has a `token_counters` field
  (`src/gateway/observability.py:70`) that is **never populated** - it
  appears in the declaration and its serialiser and nowhere else;
* the catalogue already publishes `price_input_per_m`,
  `price_output_per_m` and `price_cached_read_per_m` for 29 of its 37
  models (`data/go_catalog.json`), and **no code under `src/` reads any of
  them**. Only `monthly_limit_usd` is used, and only for display.

So the gateway knows the token counts and the prices and reports neither.
This is the one gap in the project's central honest limitation: the
provider's allowance is Unknown, and a local spend figure computed from
counts and published prices is metadata the observability rules already
permit.

**Required fix:** populate `token_counters` on the buffered and the
streamed path, compute the cost with the price's source and observation
date, and aggregate it on the Status page. A model with no published price
reports Unknown, never zero and never "unlimited".

**Resolution.** Usage and local estimates are now surfaced in Status. The Status handler loads the published price catalogue once and passes that immutable request snapshot to both recent-ledger annotations and aggregate usage calculation; per-row cost work performs no catalogue reads. This preserves file refresh visibility on the next Status request rather than introducing a stale global cache.

**Regression evidence:** `tests/test_end_to_end.py::ConsoleApiTests.test_state_loads_published_prices_once_for_many_priced_ledger_rows` seeds 40 priced rows, exercises the real `/owner/api/state` HTTP endpoint and asserts exactly one catalogue load, while checking recent-row pricing and aggregate presence.

### R27 — Entering a model manually silently discarded its published capability metadata — **closed**
**Severity: medium correctness/honesty risk.** *(closed)*

The `manual` model action replaces the whole `ModelEntry` rather than
merging into it (`src/gateway/http/app.py:835-850`). The replacement
carries over `enabled`, `dialect` and `retention_class`, and rebuilds
everything else from defaults. The provider-published fields are therefore
**discarded**: `context_window`, `context_limit`, `reasoning_efforts`,
`monthly_limit_usd`, `limits_source`, `limits_observed_at`, and the
`supports_*` flags.

Verified against the current code, on a discovered `gpt-6-luna`:

```text
context_window      : 272000  -> None
context_limit       : 258400  -> None
reasoning_efforts   : [none,low,medium,high,xhigh,max] -> []
```

So the alias's context limit silently becomes **Unknown** instead of
258,400, and a reasoning request is dropped as a degradation instead of
being honoured - and nothing is shown to the owner, because the action
returns `{"ok": true}`.

This matters more than an ordinary bug because it is a **documented,
supported workflow**: the README's troubleshooting table tells the owner to
"enter it manually and set the dialect" when a model shows an empty
dialect. The natural fix for a missing dialect therefore destroys the
context limit and the reasoning levels of the very model it was applied
to. It is the same failure class the bridge has already been ruled on for
(R25, R26): a value is silently dropped and the record does not say so.

**Required fix:** the manual action must merge, not replace - it changes
only the fields the owner actually supplied, and preserves published
metadata it has no opinion about. A manual entry that cannot supply a
published fact keeps the published one; it must not silently blank it. Add

**Resolution.** The `manual` action now merges into the existing entry. It
changes only the fields the owner actually supplied - the dialect, and the
enabled state and the note when those are supplied - and preserves every
published fact it has no opinion about: the context window and its effective
limit, the reasoning levels, the monthly limit and its source and observation
date, the retention class, and the capability flags. A model that does not
exist yet is created enabled, with the dialect the owner supplied.

The owner's symptom is gone: a manual dialect edit no longer takes the
alias's limit from 258,400 to Unknown, and no longer empties the reasoning
levels so that a reasoning request is dropped as a degradation instead of
honoured. The console already reported a limit only when it had one, and now
it has one to report.

**Evidence:** `tests/test_manual_entry_merge.py` - a manual dialect edit
preserves the context window, the effective limit, the reasoning levels, the
monthly limit and its source, the retention class, the capability flags and
the enabled state; an explicit enabled flag is honoured; a new manual model
is created enabled; and the alias's limit survives the edit.

**Planned as** `docs/TODO.md` item **T16**.

### R29 — Cooldown display must cite the cooldown-setting failure — **closed**

A cooldown is now stored together with the failure reason, request ID, and
set timestamp at the point `_maybe_cooldown` activates it
(`src/gateway/core/routing.py`). The state API exposes that event with the
remaining duration; it does not infer a cause by searching unrelated ledger
attempts. The Runtime console renders a readable countdown and the stored
reason/event reference, preserves the local-policy warning, and shows an
explicit empty state (`src/gateway/static/console.js`).

**Evidence:** `node tests/console_dom_test.js` exercises the rendered DOM text
with a synthetic cooldown event, confirms that the matching failure reason
and request reference render, and confirms an unrelated stale error is not
shown. The test also checks the empty state.

### R28 — Every small response paid a 41ms stall from Nagle and the delayed ACK — **closed**
**Severity: high latency defect on every request path.** *(closed)*

`_RequestHandler` does not set `disable_nagle_algorithm`, and
`socketserver.StreamRequestHandler` defaults it to **False**, so TCP_NODELAY
is not set on the accepted socket (`src/gateway/http/core.py:225`). The
handler also writes a buffered response in **three** separate `send()`
calls - the status line from `send_response`, the header block from
`end_headers`, and then the body (`core.py:292-303`) - with
`wbufsize = 0`, so each one is its own syscall and its own segment.

Nagle's algorithm therefore holds the header block and the body until the
status line is acknowledged, and the client's TCP delays that
acknowledgement. The result is a **41ms stall on every response whose body
fits in one segment**.

Measured against the running deployment at `10.10.11.109:8765`, on a real
network segment, reusing the connection as every modern client does:

```text
endpoint               keep-alive   close-per-request
/health                  52.4ms            6.9ms
/version                 52.0ms            7.1ms
/v1/models               53.3ms            8.9ms
/owner/api/state         13.8ms           15.2ms   (29,516 bytes)
```

The last row is the control: a body larger than the MSS is not held, so it
does not stall. Reproduced in an isolated namespace with a veth pair,
serving the real handler:

```text
                      TCP_NODELAY OFF        TCP_NODELAY ON
body    200B          41.08ms                 0.09ms
body    800B          41.01ms                 0.07ms
body   1400B          41.07ms                 0.08ms
body   2000B           0.24ms                 0.08ms
body   3000B           0.15ms                 0.04ms
   (streaming)         0.01ms gap             0.01ms gap
```

Two things follow. The stall hits **every response of about 1.4KB or
less** - which is every `/health`, every `/version`, every `/v1/models`,
and **every short non-streaming completion**. A longer completion is
faster than a shorter one, which is the signature of the bug. Streaming is
unaffected, because a streaming client keeps the connection in quick-ack
mode.

The first request on a fresh connection is fast (0.7ms); every later one
on the same connection pays the full stall. A client that reconnects per
request hides the defect entirely, which is why it can pass a casual
look.

**Resolution.** Two changes, together in
`src/gateway/http/core.py`:

1. `_RequestHandler` now sets `disable_nagle_algorithm = True`, so
   TCP_NODELAY is set on the accepted socket and the response no longer
   depends on the client's acknowledgement timing.
2. A buffered response is written in **one** segment: the status line, the
   header block and the body go out in a single `write()`, instead of the
   body being written after the header block. Each streamed frame is one
   write as well, rather than three.

The second change is the substantive one. Measured against the same
handler, over the same veth segment, with TCP_NODELAY deliberately turned
**off** so that only the write coalescing is in play, a small response
went from 41ms to 0.07ms - the stall was never really about the socket
option, it was about a second segment queued behind an unacknowledged
first one. TCP_NODELAY is kept because it removes the dependence on the
client's ACK behaviour entirely.

```text
                      before      after (TCP_NODELAY off)   after (on)
/small                 40.98ms            0.07ms               0.09ms
body 200B              41.08ms            0.09ms               0.07ms
body 1400B             41.07ms            0.09ms               0.07ms
body 2000B              0.24ms            0.06ms               0.06ms
streaming              0.01ms gap        0.00ms gap           0.01ms gap
```

**Evidence:** `tests/test_response_writes.py` - the handler is exercised
over a real TCP connection with a socket that records every write and
every socket option. It asserts that TCP_NODELAY is set on the accepted
socket, that a buffered response is written in **one** write carrying the
status line, the headers and the body, that a `HEAD` request sends the
headers without the body, and that each streamed frame is a single write.

**Planned as** `docs/TODO.md` item **T17**.

### R29 — The provider's `Retry-After` was discarded — **closed**
**Severity: medium policy-effectiveness defect.** *(closed)*

When a provider rate-limits a request, it usually says how long to wait.
The gateway threw that number away: `error_from_response` never received
the upstream response headers (`src/gateway/adapters/base.py`), and the
adapter did not keep them either. The cooldown therefore always used the
alias's flat `cooldown_seconds` - five minutes by default - whatever the
provider had asked for.

The consequence is the wrong behaviour in both directions. A provider
that says *"come back in an hour"* is called again after five minutes,
so the gateway spends the owner's allowance on requests the provider has
already refused. And a provider that says *"come back in ten seconds"* is
still benched for five.

**Resolution.** `parse_retry_after` reads both forms of the header - the
seconds form and the HTTP-date form - and returns nothing for a value
that is unparseable, negative or already in the past. A value longer than
`MAX_RETRY_AFTER_SECONDS` (an hour) is **clamped**, not taken literally,
because waiting too long is the safer error. The header is read at
`error_from_response`, the one place where an upstream response becomes
an `AdapterError`, and both adapters pass their response headers to it.

The window is the **longer** of the local policy and the provider's ask,
so the gateway never retries earlier than either asks for. The cooldown
records which of the two it used, and the skip reason says so:
*"provider is in a local cooldown for 3600s (the provider asked us to
wait 3600s)"*.

The cooldown remains what it always was - a **local retry policy, never
evidence that the provider reset**.

**Evidence:** `tests/test_retry_after.py` - both header forms; unusable
values; the clamp; the longer-window rule in all three directions; the
skip reason; the cooldown details; and a real 429 carrying a real header
driven through the OpenCode Go adapter.

**Planned as** `docs/TODO.md` item **T33**.

### R30 — A cooldown was lost on every restart, and one blip benched a provider for five minutes — **closed**
**Severity: high availability and allowance risk.** *(closed)*

`CooldownState` was an in-memory timer keyed by **provider**. Four
weaknesses showed up in the same situation - a provider that is out of
allowance:

* **It was lost on a restart.** A gateway that came back up during an
  outage immediately called the exhausted provider again, which is
  exactly the moment a cooldown is worth most.
* **It was flat.** Every qualifying failure produced the same window,
  however many had already happened, and a success reset nothing.
* **It treated a blip like an outage.** A single transient error - a 520
  from an edge - benched the provider for the same five minutes as a
  confirmed exhaustion of the allowance.
* **It was per provider.** One route's failure benched every model at
  that provider, including the models that were working.

**Resolution.** `gateway.circuits` replaces the timer with a durable
circuit breaker:

* the state is written to `circuits.json` in the state directory, with the
  same atomic, owner-only rules as the rest of the state, and is reloaded
  on start - **metadata only**, never content;
* it is keyed by **route** - provider *and* model;
* a confirmed **exhaustion** is account-scoped and opens for at least an
  hour across the provider; a **rate limit** opens the route for as long
  as the provider asked; a **transient** failure is a blip and needs a
  pattern of consecutive failures before it opens anything;
* the window **escalates** with consecutive failures, up to a day, and a
  success resets the count;
* a circuit that ends is **half-open**: exactly one request is let
  through, a success closes it, a failure reopens it for longer, an
  abandoned probe is reclaimed after five minutes, and a probe is never
  remembered across a restart;
* the store is bounded, and a circuit that has not been touched for a
  week is forgotten;
* the route is still **recorded as skipped, with its reason**, so a
  cooldown can never look like a silent fallback.

The circuit remains what a cooldown always was: a **local retry policy,
never evidence that the provider reset**. There are still no background
probes.

**Evidence:** `tests/test_circuits.py` - the state survives a restart; the
stored file is metadata only and mode `0600`; a probe is never
remembered; an old entry is forgotten; the store stays bounded; an
exhaustion opens for an hour and covers the provider; a rate limit opens
the route only; a transient failure needs a pattern and a success resets
it; the window escalates and is capped; exactly one probe is let through;
a successful probe closes the circuit; a failed probe reopens it for
longer; an abandoned probe does not block the route; and the router
records the skip with its reason.

**Planned as** `docs/TODO.md` item **T32**.

### R31 — The release gate resolved an identity the gateway cannot report — **closed**
**Severity: high release-traceability risk.** *(closed)*

`release_gate.image_digest()` returned whatever `{{index .RepoDigests 0}}`
produced. For an image that has a repository digest, that is a
**reference** - `repository@sha256:...` - not a digest.

The gate then wrote that value into `release.json`, and the gateway's
release identity deliberately accepts only a **bare** `sha256:<64 hex>`
digest (`src/gateway/buildinfo.py:44-62`), so that no arbitrary string can
be smuggled into `/version`. The value was therefore rejected, `/version`
reported `unknown`, and the gate's own identity check failed:

```text
[FAIL] /version reports the exact running image digest - reported=unknown
```

The release step was **not** affected, because `release.py` resolved the
identity differently: it required the value to start with `sha256:` and
fell back to the image ID, which is the bare form. That is why the running
deployment reported its digest correctly while the gate could not pass.

The two paths were also free to drift, which is exactly what R7 warned
about when it asked the workflow and the gate to share one resolution.

**Resolution.** `release_gate.image_digest()` now resolves **one**
canonical identity: a bare `sha256:<64 hex>`, taken from the repository
digest when there is one (dropping the repository part) and from the
image ID otherwise. `release.py` delegates to it, so the gate and the
deployment can never disagree about which artifact is running. The CI
workflow records the same value.

**Evidence:** `tests/test_release_gate.py` - a repository digest is
reduced to the digest, a bare digest is kept, a local image falls back to
its ID, and the resolved value is one the gateway's own validator accepts.

### R32 — A deprecated model stopped the fallback chain — **closed**
**Severity: high availability risk.** *(closed; found in the live ledger)*

OpenCode Go returned **410 Gone** for a model it had deprecated:

```json
{"type": "error", "error": {"type": "ModelDeprecated",
 "message": "Model minimax-m3.1-flash-free has been deprecated."}}
```

`410` was classified `unknown`, and `unknown` never falls back - by design -
so the chain stopped with a second, perfectly usable fallback sitting unused,
and the owner got a 502 for every request.

The class is neither transient nor a policy failure. It is an **availability
fact about the route**: the upstream no longer serves this model there, and
another route may still serve the request. It is not retried on the same
route, because a deprecated model will not come back.

**Resolution.** A `route_gone` class now covers `410` and the provider's own
deprecation codes. It satisfies the "upstream unavailable" trigger, so a
chain continues when the owner has enabled that trigger. It is never retried
on the same route. And because the circuit store is scoped per route, the
deprecation of one model does not bench its provider's other models.

**Evidence:** `tests/test_route_gone.py` - `410` and `ModelDeprecated`
classify as route gone; the class is not retryable; a gone first fallback
continues to the second, and the attempt is recorded; and the provider's
other routes stay usable.

### R33 — Removing a fallback in the console did not remove it — **closed**
**Severity: medium correctness risk.** *(closed; found by driving the console
in a browser)*

The Remove control on a fallback row removed the row from the DOM but left
the entry in the array the save payload is built from, so the fallback was
silently restored on the next save. The console showed the row gone; the
saved alias was unchanged.

**Resolution.** The Remove control splices the entry out of the list as well
as removing the row, so the two stay in step.

**Evidence:** `tests/browser_console_test.js` drives the console in a real
browser: an alias with two fallbacks is edited, a row is removed, the save
is applied, and the alias is read back with one fallback and its primary
untouched. The test fails against the previous behaviour.

### R34 — An OpenCode client could not discover the gateway's aliases — **closed**
**Severity: medium usability gap.** *(closed; requested by the owner)*

`GET /v1/models` returned the published alias names and nothing else. An
OpenAI-compatible client that lists a catalogue before offering a model
selector - OpenCode being the obvious one - saw only bare strings, with no
context limit, no tool support and no reasoning levels, and a custom provider
had to list every alias by hand in its configuration. Every alias created in
the console afterwards was invisible to that client until its configuration
was edited again.

**Resolution.** Two pieces.

1. `GET /v1/alias-catalog` publishes, for each published alias, the
   **alias-level context limit** (already computed as the smallest limit
   among the routes that could serve it, and `Unknown` when any route's
   limit is unpublished), the primary route's published reasoning levels,
   and whether every route supports function tools. It is metadata only -
   no provider inventory, no ledger, no credential, no unpublished route -
   and it makes no provider call. It obeys the same anonymous/profile rules
   as `/v1/models`, and the same Host/Origin/forwarded-header refusals.
2. `integrations/opencode/` is an OpenCode V2 provider plugin. It reads
   that catalogue, registers one provider, and refreshes every 60 seconds,
   so an alias published in the console appears in `/models` and an alias
   that is unpublished disappears. Its input is validated before it
   replaces anything, and a failed refresh keeps the last complete
   inventory rather than erasing the owner's selection.

**Context limits stay honest.** OpenCode requires a *number*. For an alias
whose limit the gateway reports as Unknown, the plugin applies the
**owner-chosen 200,000-token OpenCode working budget** and labels the entry
`(200k client budget; upstream unknown)`. That number is a client-side
budget, not a provider guarantee: the gateway still reports Unknown, and it
is still the provider that decides what fits.

**Evidence:** `tests/test_opencode_catalog.py` - published aliases only, an
unknown limit reported as unknown, the finite case reported exactly,
no provider call on a page read, no provider inventory in the response, and
host/forwarded-header refusals; it also proves that documented MiMo and
MiniMax windows remove the client budget and that MiniMax's published effort
levels reach the selector catalogue. `tests/test_opencode_catalog.mjs` - the
plugin's own validator: a non-private or non-`/v1` base URL is refused, a
duplicate or malformed alias is refused, a limit too small to be usable is
refused, and the 200,000 budget is applied only when the limit is unknown.
The integration was then exercised against a running gateway: the aliases
appeared in `opencode api get /api/model` with their real limits, and a real
request completed through the gateway with the fallback chain and the
attribution recorded.

### R35 — A thinking control was silently dropped on a converted request — **closed**
**Severity: high contract-correctness risk.** *(closed; supersedes the
description of R25)*

A converted request is rebuilt from the normalised form, and the normalised
form carried only OpenAI's flat `reasoning_effort` string, so two spellings
vanished on the converted path:

* `reasoning: {"effort": "high"}` - the newer Chat Completions spelling.
  `_without_disabled_reasoning` read that exact field for the *disabled*
  case, so the two code paths disagreed about one field.
* `thinking: {"type": "enabled", "budget_tokens": 32000}` - Anthropic's
  thinking control, which the gateway did not model at all.

That broke the bridge's own contract: *"Nothing is silently dropped."*

**Resolution.** One normalisation, applied on the converted paths:

* both Chat Completions spellings are parsed into one canonical level -
  the flat string;
* the level is rendered per dialect - `reasoning_effort` for Chat
  Completions, `reasoning: {effort}` for Responses;
* a control the target dialect cannot express - an Anthropic thinking
  budget, an unknown reasoning field, or two controls that contradict each
  other - is **refused** with an actionable message, never discarded;
* a level the selected model does not publish is still dropped *for that
  route* with the substitution recorded, under the R19 preference rule,
  and only the route that actually served the request records it.

On a **same-dialect** request the gateway does not interpret or rewrite a
control it does not model: it forwards the payload and lets the provider -
which owns its own dialect - decide. Forwarding is not a drop.

**Evidence:** `tests/test_thinking_controls.py` - a nested Chat Completions
effort survives onto a Responses request as `reasoning: {effort}`, and onto
a same-dialect Chat Completions request as `reasoning_effort`; a thinking
budget, an unknown reasoning field and conflicting controls are each
refused on the converted path; a same-dialect payload the gateway does not
model is forwarded unchanged; and an unsupported level on the serving route
is dropped **and** recorded in the ledger.

**Not in scope here:** rendering a thinking control for the Anthropic
Messages and Google v1beta dialects. Those dialects are not served yet
(`docs/TODO.md` T5 and T6); until they are, a request that needs such a
conversion is refused rather than converted.

**Planned as** `docs/TODO.md` item **T4**.

### R36 — A configured Go route kept a retired MiniMax model ID — **open**
**Severity: medium availability and metadata correctness.** *(open; owner
approved the replacement, pending deployment and the explicit console edit)*

The Go documentation and live catalogue now name `minimax-m3.1-flash`; the
older `minimax-m3.1-flash-free` is no longer listed. Existing `coding` and
`flash` aliases still name the retired ID. The router safely continues a
`route_gone` failure to an authorised fallback (R32), but that is recovery,
not a current route: the stale entry keeps the alias context Unknown and
prevents the documented MiniMax thinking levels from reaching the selector.

**Resolution in code.** The dated Go snapshot now carries the current ID,
its official endpoint, its 1,000,000-token model window and its documented
`low`/`medium`/`high`/`xhigh`/`max` effort levels. It deliberately does
**not** rewrite an owner's configured alias during a catalogue refresh: a
route change is a privacy and fallback-policy decision, so it remains an
explicit trusted-console action.

**Required owner action after deploying this change:** refresh the OpenCode
Go catalogue, replace `minimax-m3.1-flash-free` with
`minimax-m3.1-flash` in `coding`'s fallback and `flash`'s primary, retain the
existing fallback-authorisation choices, save, then test each alias. This
finding closes only after the deployed catalogue and `/v1/alias-catalog`
show the documented limits and the routes are exercised.

**Regression evidence:** `tests/test_adapters.py` asserts the current ID,
documented context and effort metadata, and that the retired ID is absent;
`tests/test_opencode_catalog.py` proves those windows make aliases finite
and publishes the MiniMax effort levels.

### R37 — The Go snapshot asserted *no* model accepts images, on no evidence — **closed**

**Severity: medium metadata correctness, with a client-visible symptom.**
*(closed)*

`scripts/build_go_catalog_snapshot.py` wrote `"supports_image_input": False`
for **all 37 models** as a bare constant, while its own docstring claimed the
snapshot was "published information, transcribed from the provider's own
documentation... *not* a guess." The claim was therefore unfounded in
**both** directions: it denied image input to plainly multimodal models
(`minimax-m3.1-flash`, `claude-haiku-5-5`, `gemini-4-argon-contributor`) and
would have affirmed it for any model added later without a word of evidence.

The value was then dropped a second time: `opencode_go.py` built its
`ModelInfo` without passing `supports_image_input`, so even a corrected
snapshot would not have reached the router. `/v1/alias-catalog` published no
image field at all, and `integrations/opencode/index.ts` hardcoded
`input: ["text"]` behind the comment *"Image routing is not yet enforced by
the gateway."* Three independent drops meant a user whose models do accept
images could never attach one through OpenCode.

This is the same defect shape as the context-window problem, which the
project already solved with a tri-state plus a labelled fallback: **an
unsupported capability must be Unknown, not asserted false.**

**Resolution.** Image support is now recorded per **exact upstream model
ID**, only from official model-provider documentation, with the source URL
kept beside the flag (`IMAGE_INPUT_SOURCES`, rendered as
`image_input_source`). A model absent from that map stays false and
sourceless, and a regression test asserts the two always agree. MiniMax
documents that `MiniMax-M3.1-Flash` accepts OpenAI `image_url` parts
(`platform.minimax.io/docs/api-reference/text-openai-api`, read 2026-10-23),
and the published Go endpoint table establishes that the exact
`minimax-m3.1-flash` ID is served over that compatibility endpoint. That is
the only model currently claimed, because it is the only one evidenced.

`opencode_go.py` now propagates the capability from the snapshot;
`/v1/alias-catalog` publishes `supports_image_input` under an **any-route**
rule; the OpenCode plugin maps it to `input: ["text", "image"]`; and the
router **excludes** an image-incapable route from a request carrying an
image, with the reason recorded, failing closed at the gateway when no
capable route remains.

The any-route rule is deliberately looser than the all-routes rule the tools
flag uses, and it is safe only *because* the router now enforces the
capability per request. The hazard the all-routes rule guarded against — a
fallback serving an image to a model that cannot see it — is removed by that
enforcement, not by the publication rule. Without the router check, the
any-route rule would have reintroduced exactly the failure R37 describes.

**Regression evidence:** `tests/test_image_input.py` — every claimed
capability has a source and every unclaimed one has none; the capability
survives snapshot → adapter → catalog; an alias is image-capable when any
route is, and stays text-only when none is; an image request excludes a
text-only primary, uses a capable fallback, and fails closed with no capable
route; a text-only request is unaffected; and detection agrees with the
bridge across a dialect conversion.

### R38 — A per-phase reasoning effort was stored and shown, but never reached the provider — **open**

**Severity: medium contract-correctness.** *(open; the field is persisted and
round-trips, so the defect is silent rather than lossy)*

T37 lets an owner give a phase route its own reasoning effort. The field is
part of the config model (`PhaseRoute.effort`, `src/gateway/config.py:271`),
validated at save time (`src/gateway/routes.py:149-153`), serialised by
`Alias.to_dict()`, and exposed by the console editor. But nothing reads it.
The router selects a phase route by `route` alone
(`src/gateway/core/routing.py:545-551`) and the only effort it ever
considers is the one **in the caller's request**
(`_requested_reasoning_effort`, `src/gateway/core/routing.py:79-88`). A
phase route's `effort` is therefore inert: it survives every save and reload
and never reaches an upstream provider.

That is the same shape as a finding class this register already tracks: a
control the gateway accepts, stores, and displays, but does not honour. The
difference from R35 is that nothing is *dropped* - the request is served
correctly - so the symptom is a configured effort that silently has no
effect, which is exactly the "never silent" rule the phase-routing design
was written against.

**Why it was not fixed here.** The T37 backend contract was settled on
`f7cf5a4` and the console editor was scoped to expose the config as it
stands. Making the effort effective is a **routing** change - the effort must
be applied when the phase route is chosen, must still obey the R19
preference rule (a level the model does not publish is dropped *for that
route* with the substitution recorded, not refused), and must not override an
explicit caller effort. That is a change to the phase-resolution algorithm,
which is outside the console card's boundary and needs its own review.

**Evidence.** A focused probe on this tree: an alias whose `plan` phase route
is `opencode_go/mimo-v2.6-flash` with `effort="high"`, on a model that
publishes `none/low/medium/high`, was called with
`metadata.personal_gateway.phase = "plan"`. The phase route served the
request (`mimo-v2.6-flash`), and the upstream payload was
`{"model": "mimo-v2.6-flash", "messages": [...]}` - **no `reasoning_effort`
and no nested `reasoning`**. The configured effort did not appear.

**Required fix.** Apply the phase route's effort to the request when that
route serves it, under the R19 preference rule, and record the substitution
when the model does not publish the level. Until then the console says so
plainly rather than presenting the field as if it were in effect.

### R39 — “Remove phase policy” was ignored for enabled policies — **closed**

**Severity: medium UI correctness.** The save condition checked
`phaseEnabled.checked` first, so JavaScript short-circuiting prevented the
explicit remove checkbox from being read for an enabled policy. The editor
reported success while preserving the policy.

**Fix.** The remove checkbox is now authoritative: phase configuration is
serialized only when removal is not checked, regardless of the enabled
checkbox. With removal unchecked, existing disabled policies remain editable
and can be re-enabled. Jev inference is serialized within the phase policy;
there is no independent competing save toggle.

**Regression evidence.** `tests/browser_console_test.js` now edits an enabled
policy through the real console, checks “Remove phase policy,” saves, and
asserts the policy is null in `/owner/api/state`. `tests/test_browser_console.py`
seeds the enabled policy and enables that mutation flow. Verified with
`NODE_PATH=/tmp/pwtest/node_modules GATEWAY_CHROMIUM=/usr/bin/chromium
PYTHONPATH=src python3 -m unittest tests.test_browser_console -v` (1 test
passed). The console source-string assertion was also updated to reject the
old short-circuit condition.

### R42 — A half-open recovery probe was claimed during candidate enumeration, so one ineligible request pinned its primary to a fallback — **closed**

**Severity: high availability correctness.** *(closed; the claim moved to the
invocation boundary and every claim now carries an owner)*

`Router.candidates()` asked the circuit store `check()` for every route while
it was only *assembling* the candidate list (`src/gateway/core/routing.py`).
`check()` was not a question — it atomically **claimed** the half-open
recovery probe as a side effect. Any candidate that was then dropped never
invoked anything, so nothing ever released that claim, and the route stayed
excluded until `PROBE_TIMEOUT_SECONDS` (300s) had passed.

The provider had already recovered by then: the window that put the circuit
half-open had expired, and the probe was the mechanism meant to confirm the
recovery. Instead the probe was spent on a request that could never have used
the route, and every compatible request in the next five minutes was served
by a fallback while the primary sat idle.

Three enumeration paths could do this, and all three were reachable in
normal operation:

* **capability exclusion** — an image request against a primary that does not
  publish image input (`supports_image_input`). The primary was excluded one
  line *after* its probe had been claimed, so one image request pinned the
  text-serving primary to its fallback;
* **phase routing** — a caller phase hint that selects a different route. The
  unselected primary still had its probe claimed, so the route phase routing
  deliberately did not choose was the one left wedged;
* **conversion refusal** — `convert_request()` raising `ConversionError`
  before `adapter.invoke()`. The candidate was refused with its claim held.

A second defect compounded it: `release_probe()` cleared the flag for
*any* caller. A request that gave up could therefore release a probe that
**another** request was actively serving, silently admitting a second
concurrent invocation to a half-open route — the exact condition the probe
exists to prevent.

**Fix.** `CircuitStore.preview()` is a pure query: it answers eligibility and
never mutates, so enumeration cannot spend a probe. The claim is
`CircuitStore.claim()`, called in `Router.invoke()` **immediately before
`adapter.invoke()`**, after every local check that could decline the route
has passed. Each claim returns an owner token stored on the entry
(`CircuitEntry.probe_owner`, in memory only, like the flag itself), and
`release_probe(provider, model, owner)` refuses to clear a claim whose token
does not match. A candidate that is skipped now records its skip and
continues without touching the store, preserving the fallback attribution
and the skip reason for the primary.

Both the `AdapterError` path and the new `except Exception` path release the
claim with the owner token. The latter matters because an unexpected failure
is not evidence that a circuit should open, and it is not a probe that is
still running: without the release, a crash inside `invoke()` wedged the
route for the full probe timeout.
Those two release sites covered only `invoke()` itself; R43 replaced them
with a single guard that covers every post-claim exit.

**Regression evidence.** `tests/test_probe_claim.py` (10 tests) covers each
enumeration path above and asserts both the absence of a stuck claim
(`_probe_held`) and the user-visible property that an immediate compatible
request reaches the primary rather than waiting out the window. Concurrency
is covered deterministically with a blocking adapter that reports when it is
inside `invoke()`: a second request arriving mid-probe is served by the
fallback, the primary sees exactly one invocation, and the ledger records
the live-probe skip reason. Ownership is covered at store level: a foreign
owner token cannot release a live claim. Verified by reverting only
`candidates()` to the claiming `check()` — 6 of the 10 tests fail, and pass
again with the fix in place.

`PYTHONPATH=src python3 -m pytest -q` → 491 passed, 2 skipped, 93 subtests
passed. `ruff check` on the four changed files reports only the pre-existing
`F401` findings already present on the base revision. `git diff --check`
clean.

### R43 — An exception raised after a successful probe claim escaped every release path, so the route refused probes until the timeout — **closed**

**Severity: high availability correctness.** *(closed; one guard now
releases the claim on every post-claim exit)*

`Router.invoke()` claimed the half-open probe and then ran a `try` whose
handlers were **siblings**, not parents: `except AdapterError` and
`except Exception` both covered only `candidate.adapter.invoke()`. An
exception raised *inside* the `AdapterError` handler — while
`_maybe_open_circuit()` was processing the failure, before that handler's
own `release_probe()` call — is not caught by a sibling `except`, so it
escaped with the claim still held. Anything raised after a successful
invocation, in the post-claim recording (`record_success()`, the ledger
write), was worse: that code sat outside the `try` altogether.

The security probe reproduced it as an HTTP 500 with the primary's
in-flight claim still held. The route then refused every compatible
request for the rest of `PROBE_TIMEOUT_SECONDS` (300s) while nothing was
probing it — the same wedge R42 set out to remove, re-entered through a
different path.

**Fix.** The whole post-claim block now runs under one `try ... finally`
(`src/gateway/core/routing.py`), and the `finally` releases **this claim's
owner token**: success, a failure that falls back, and every exception all
pass through it. The two earlier release sites — inside the
`AdapterError` handler and inside the invoke-only `except Exception` — are
gone, leaving one release point and one invariant. Releasing an
already-released claim is a no-op, and `release_probe()` still refuses an
owner token that does not match, so a foreign request's live probe is
never cleared by this path and normal success/failure behaviour is
unchanged.

**Regression evidence.** `tests/test_probe_claim.py` (12 tests) adds
`test_an_exception_while_the_failure_is_processed_releases_the_claim` — a
synthetic exception injected into `_maybe_open_circuit()` after a
half-open claim — and
`test_an_exception_after_a_successful_probe_releases_the_claim`, where
`record_success()` raises after a successful probe. Both assert the claim
is released immediately (`_probe_held`) and that the next compatible
request claims the probe and closes the circuit. Verified by restoring
only `src/gateway/core/routing.py` to the candidate revision: both tests
fail with `AssertionError: True is not false : ... left the probe
claimed`, and pass again with the fix in place.

```text
PYTHONPATH=src python3 -m unittest discover -s tests -v
Ran 494 tests ... OK (skipped=2)

python3 -m compileall -q src tests                        OK
node tests/console_dom_test.js                            all checks passed
node tests/test_opencode_catalog.mjs                      OpenCode catalogue checks passed
PYTHONPATH=src python3 -m gateway --help                  OK
git diff --check                                          clean
```

### R44 — The read budget never reached the socket on the streaming path, so every SSE frame was judged by the connect budget — **closed**

**Severity: high availability correctness.** *(closed; the socket is
armed directly once the head is in, with a socket-level regression)*

`UpstreamClient._open()` armed the connect budget on the socket before
`getresponse()` — the head of a stream is a stall, by design — and then
re-armed the read budget **through the connection**:
`self._arm(connection, self.timeout)`. For a response without a
`Content-Length` — every SSE stream — `will_close` is true and
`http.client.HTTPConnection.getresponse()` passes the socket to the
response by closing the connection's reference to it, so
`connection.sock` is `None` from then on while the socket itself lives
on inside the response's file object. The follow-up `_arm` is guarded by
`if connection.sock is not None:` and became a silent no-op: the socket
kept the connect budget for the whole stream.

Impact: with stock budgets (read 120s, connect 10s) any inter-frame gap
over 10s aborted a stream that was still progressing — a long reasoning
pause is exactly this case — on **every** adapter, since all four
construct `UpstreamClient(user_agent=USER_AGENT)` with the stock
budgets. Measured at socket level by QA (`t_099ed4e6`) on candidate
`72d47b3`: with `timeout=2.0, connect_timeout=0.6`, the timeout in force
for every frame read was 0.6, never 2.0, and a 12s gap aborted the
stream at elapsed 10.01s. The module docstring's own contract — every
frame of a stream judged by the read budget — was violated by the code.

**Fix.** `_open()` holds the socket reference captured just before
`getresponse()` and arms the read budget on *that* socket once the head
is in (`src/gateway/adapters/http_client.py`). The connect budget still
judges the streamed head (unchanged), and the buffered path is untouched:
a content-length response keeps `will_close` false, so the connection
still holds its socket there.

**Regression evidence.**
`tests/test_upstream_timeouts.py::test_a_frame_gap_beyond_the_connect_budget_is_judged_by_the_read_budget`:
two frames with a single 1.5s inter-frame gap against
`connect_timeout=0.6, timeout=3.0` — the gap is above the connect budget
and within the read budget. It asserts (a) a spy on
`http.client.HTTPResponse.readline` observes the read budget (3.0) as
the socket timeout in force for every frame read, (b) both frames
arrive, and (c) the stream is waited out past the gap. Verified by
running the new test against the unmodified candidate `_open()`: it
fails with `AdapterError: the provider stream stalled before the response
finished` at the 0.6s connect budget — the exact defect QA measured — and
passes with the fix in place. The prior test only used per-frame gaps
*below* the connect budget (0.35s vs 0.4s), so it could not see which
budget governed the frames; this one crosses the connect budget with a
single gap.

```text
PYTHONPATH=src python3 -m unittest discover -s tests -v
Ran 563 tests in 57.5s ... OK (skipped=3)

python3 -m compileall -q src tests                        OK
node tests/console_dom_test.js                            all checks passed
node tests/test_opencode_catalog.mjs                      OpenCode catalogue checks passed
PYTHONPATH=src python3 -m gateway --help                  OK
git diff --check                                          clean
```

### R45 — Alias tags and bulk group publication — **closed**

*(carried from the T30 orphan branch, where this finding was numbered R32;
renumbered on integration because main's register already uses R32–R44)*

**Resolution.** `Alias` has an optional persisted tag. The owner console exposes group publish/disable, implemented as an atomic enabled-flag update over aliases matching a validated tag. The operation does not modify route configuration, fallback authorization or any per-alias policy, and refuses a group publish if any member has no primary.

**Evidence:** `tests/test_alias_groups.py` exercises published-state changes, preservation of each alias's route and policy, invalid/empty tags, incomplete group refusal, and legacy/tag round-trips. `AliasGroupHttpTests` in `tests/test_end_to_end.py` drives the HTTP action and asserts only members are affected and that refusal is atomic.

### R40 — A console alias save silently drops a file-configured request policy — **closed**

**Found:** 2026-10-24, during T21 (a request policy per alias).

T21 landed an opt-in `policy` block on `Alias` (`src/gateway/config.py`),
applied by the router on every provider-reaching path. The block
persists in `config.json` and is visible in the console state (the state
endpoint renders `alias.to_dict()`), but the console's own alias **save**
path rebuilds the alias from the submitted fields only:
`_alias_from_payload` (`src/gateway/http/app.py:1277`) constructs a
fresh `Alias` with no `policy`, and `handle_alias_action` (`save`,
`src/gateway/http/app.py:1008-1021`) replaces `config.aliases[name]`
outright. The only carried-over field is `enabled`.

**Consequence.** An owner who sets a policy in `config.json` and later
edits *any* field of that alias in the console loses the whole policy
silently - the exact failure mode T21's own tier rule ("a policy that
changes a request is recorded, not silent") exists to prevent, applied
to the configuration itself.

**Fix direction.** Either accept a `policy` block in the save payload
with the same parse-time validation (`AliasPolicy.from_dict`) plus a
console editor, or preserve the existing alias's policy on save.
Whichever is chosen, a save that drops a policy must report it.
`tests/test_alias_policy.py` should gain the regression.

**Closed:** 2026-10-24. `handle_alias_action` (`save`,
`src/gateway/http/app.py`) now carries the existing alias's `policy`
across a save whose payload omits the block - which is every save the
console form makes - and also accepts an explicit `policy` in the save
payload under the same parse-time validation as the configuration file
(`AliasPolicy.from_dict`). A block that is not an object or null, or
that rules nothing after validation, is refused with 400
(`invalid_alias`, field `policy`) rather than dropped: on a mutation
endpoint an invalid policy must never disappear quietly. Every save
response reports what happened to the policy (`_policy_save_outcome`:
`preserved`, `set`, `changed`, `cleared` or `unchanged`, with
`before`/`after` when they differ), so a change is never silent.
Regression: `tests/test_alias_policy.py`, `ConsoleSaveTests` - six
tests, including a save through `POST /owner/api/alias` that asserts
the policy survives and still governs the next upstream request. A
console *editor* for the block remains a separate, unlanded piece.

## Recommended order

Findings R0-R27 and R28-R36 are closed or superseded. **R36 and R38 are
open**; R36 is the explicit
post-deployment route replacement above, and R38 is the inert per-phase
reasoning effort, which needs a routing change. R37, R39, R40, R41, R42,
R43, R44 and R45 are closed. R75 is closed with regression tests on the
integrated tree; R75's third item is also an **operator** action, because
no code change can redeploy a stale image - production is still running
`f7cf5a4`, which predates the strip fix, and must be released from a
current tree by hand. The register
is kept so the next agent can see what was already paid for in time.

1. R0 is superseded by the approved trusted-LAN mode, which removes passphrase bootstrap entirely; see candidate update below.
2. Fix and test **R1**, **R8**, and **R9** before presenting ChatGPT/Codex or
   cross-dialect OpenAI compatibility as usable.
3. Fix **R10** before treating device authorization as a setup-only,
   shown-once credential flow.
4. Fix/prove **R2** and **R11**, then run a real Compose deployment test before
   any live release.
5. Repair **R3** and **R7** so release evidence is trustworthy.
6. Tighten **R4** and **R12**, then make observability truthful and complete in
   **R5**.
7. Resolve the trusted-LAN transport decision in **R13**, and fix **R6** and **R14** with targeted tests and runbook changes. R15 is superseded by removal of the setup surface.
8. Complete **R36**'s explicit post-deployment route replacement.
   R25 and R27 are closed by R35 and R27's own resolutions.

## Provider health view (T31)

The owner Status view now combines each provider's observed connection state,
last test outcome/time/error, catalogue observation time, configured model
count, active cooldown countdown, and recent success/failure rates from the
bounded metadata ledger. Rates count only requests actually served by that
provider; absent samples remain Unknown. This is a local metadata summary, not
a provider probe or claim of external health.

Evidence: `src/gateway/provider_health.py`; `tests/test_provider_health.py`
(asserts populated and missing-observation outcomes); and
`tests/test_end_to_end.py::ConsoleApiTests.test_state_reports_usage_costs_and_durability`
(asserts the integrated state payload).
## Trusted LAN admin-mode candidate update

The approved single-listener change resolves the former R0 first-passphrase
claiming condition by deleting passphrase creation and owner sessions rather
than adding a bootstrap race fix. Regression evidence: `test_login_and_passphrase_route_are_removed`, direct owner-state access and
`test_owner_console_mutates_same_origin_without_session_or_csrf` in
`tests/test_end_to_end.py`, plus CLI one-application coverage in
`tests/test_cli.py`. The formerly listed R15 setup-surface host-only message is
also removed with the setup UI; the console no longer presents that claim.

This historical note does not resolve unrelated R1/R8/R9/R2/R3/R7/R11 findings.
R4-R6, R10, R12-R14 remain open unless independently evidenced elsewhere.
Later changes and their current evidence are recorded below.

### R24 — Configuration backup import/export and optional snapshots — **closed**

The owner previously needed manual volume commands to copy `config.json`; an
import path did not exist. A backup must never carry credentials or
caller-token hashes, and import must not persist permissively parsed or
unroutable data.

**Resolution.** `gateway.backup.export_config` emits a sanitized config-only
export and excludes credential/token and secret-adjacent metadata. Import
builds a candidate, validates identifiers through `gateway.identifiers` and
routes through `gateway.routes`, then uses a disk-first atomic replacement.
Optional timestamped snapshots write only configuration into the state
volume and are disabled by default. The console exposes download, validated
import, and the interval. The runbook retains an updated manual-volume
fallback and documents the console path.

**Evidence:** `tests/test_backup.py` verifies secret exclusion, successful
round-trip/idempotent re-import, byte-identical config after bad identifier
and unroutable-route refusals, default-off interval visibility, and snapshot
worker enabled/disabled behavior. Full suite passed on the implementation
candidate (339 tests, 2 skipped); compileall and console DOM checks passed.
Independent candidate QA is tracked by the dependent QA card.
The retired setup-port reuse finding is fixed in `gateway.cli`: `--port` and
`GATEWAY_PORT` both reject `8766` during argument parsing with an actionable
error, before application construction or listener startup. Regression tests
`test_reserved_setup_port_is_rejected_from_cli_before_startup`,
`test_reserved_setup_port_is_rejected_from_environment_before_startup`, and
`test_allowed_explicit_private_port_remains_configurable` cover refusal and
the valid configurable-port path.


## Bind-policy follow-up (SEC-2–SEC-4) — closed in current worktree

The bind predicate now uses explicit allowlists: RFC1918 IPv4, IPv6 ULA
(`fc00::/7`), and loopback. It rejects RFC 5737 TEST-NET, other public
addresses, multicast, link-local, and unspecified addresses instead of
relying on Python's version-dependent `ipaddress.is_private`. The local-address
probe selects `AF_INET` or `AF_INET6` from the parsed address, so an assigned
ULA can proceed through startup validation. Bind policy documentation now
states these same supported classes.

Evidence: `tests/test_cli.py` — `test_public_addresses_are_refused` covers all
three TEST-NET examples and forbidden address classes;
`test_test_net_addresses_are_refused_before_startup` verifies `main()` refuses
all three before application construction; `test_owned_ipv6_ula_uses_startup_path`
verifies ULA is accepted by startup validation when locally owned. The HTTP
server now selects AF_INET6 for colon-containing addresses, and security
policy formats IPv6 URL/Host/Origin authorities with brackets. Regression
evidence: `test_ipv6_loopback_server_serves_canonical_mutation` traverses a real
IPv6 listener and successful same-origin mutation; `test_ipv6_authorities_are_bracketed_for_host_and_origin`
checks allowed Host and Origin forms. `test_ipv6_ula_listener_binds_when_host_owns_address`
is skipped when the test machine does not own the ULA. Focused suites pass;
full suite: 340 passed, 3 skipped.

### R40 — The integrated candidate carried stale claims and three leak paths — **closed**

The first integrated release candidate (`db7d349`, tree `3662365f`) drew two
CHANGES_REQUIRED verdicts. Each finding is closed here on the re-dispatch
candidate, with its regression:

1. **Stale Anthropic limitation** (QA `t_81ce92d9`): the Known Limitations
   claimed Anthropic support remained planned while the native Anthropic
   provider shipped in the same tree. The bullet now separates the
   implemented outbound Anthropic Messages provider from the unimplemented
   inbound `/v1/messages` surface (T5).
2. **Overstated exposure claim** (security `t_f4994f65` #1): the README
   asserted the gateway is "never publicly exposed". It binds private/loopback
   addresses and creates no firewall/NAT rule; exposure beyond the LAN is the
   operator's responsibility. Reworded in `README.md`.
3. **Imported capability coercion** (security #2): a config import accepted
   the string `"false"` for `supports_image_input` and `ModelEntry.from_dict`
   coerced it to True. `gateway.backup.import_config` now rejects any
   non-boolean capability/state flag, and `ModelEntry.from_dict` restores the
   declared default instead of coercing. Evidence:
   `tests/test_backup.py::test_import_rejects_non_boolean_capability_flags`,
   `tests/test_config.py::ModelEntryFlagTests`.
4. **Upstream identifiers in public refusals** (security #3): the
   `no_eligible_route` 503 serialized every considered route's provider id,
   model id, label and dialect to the API caller. The public refusal is now
   generic; per-route exclusion reasons stay on the router/owner side
   (catalog capabilities and route validation). Evidence:
   `tests/test_image_input.py::test_image_request_with_no_capable_route_fails_closed_at_the_gateway`,
   `tests/test_degraded_route.py`.
5. **Anthropic error-code/credential echo** (security #4): upstream error
   codes and key-shaped values must never reach the console, state or
   `last_error`. The shared `error_from_response` keeps only allowlisted
   classification codes and fixed class messages with credential shapes
   redacted; an Anthropic-specific regression drives a fake transport whose
   error body echoes the key in both `error.code` and the message.
   Evidence: `tests/test_anthropic_adapter.py::test_upstream_error_codes_and_credentials_never_echo`.

### R41 — A confirmed exhaustion held a provider for hours, stranding a reopened subscription window — **closed**

**Severity: medium.** *(closed)*

On 2026-10-24 the `chatgpt_codex` upstream reported `exhausted` for the
third consecutive time after its subscription window had already
reopened. `CircuitStore.record_failure` gave a confirmed exhaustion a
1-hour floor and then grew it exponentially with consecutive failures
(`base * 2^(failures-1)`, capped only at a day), so the third failure
escalated the persisted provider-wide circuit to a **four-hour** hold.
Every ChatGPT-primary alias fell back to OpenCode until the locally
calculated expiry, leaving reopened subscription capacity unused. The
window was opaque in the console (a raw seconds count), and the only
recovery was a hand edit of `circuits.json` on the host.

**Required fix.** Bound the confirmed-exhaustion window with a
configurable cap and no exponential growth; keep the provider/route
distinction; keep the single half-open recovery probe; give the owner a
deliberate, Origin-protected per-provider clear action; and surface the
full recovery telemetry (failure timestamp, scope, failures, hold, next
probe, probe-in-flight) in state and console, with circuit fallback
distinguishable from a phase-route selection.

**Resolution.** `gateway.circuits` now floors a confirmed exhaustion at
`EXHAUSTED_BASE_SECONDS` (15 min) and caps it at `EXHAUSTED_MAX_SECONDS`
(30 min default, `GATEWAY_EXHAUSTED_COOLDOWN_MAX_SECONDS` /
`--exhausted-cooldown-max-seconds`), with no exponential growth for this
class; rate-limit and transient windows are unchanged. The half-open
single probe is preserved: one claim, concurrent requests skipped with a
truthful reason, in-memory claim reset on restart, abandoned claim
reclaimed after five minutes. `POST /owner/api/provider` gained
`clear_cooldowns` (canonical-Origin mutation) returning the cleared
metadata-only entries for exactly the named provider, with an optional
single explicit `probe_alias` request; state and console expose the
telemetry plus `cooldown_policy`, and the ledger/Status view label
`phase route`, `held by local circuit`, and cross-provider `fallback`
distinctly. Evidence: `tests/test_circuits.py` (bounded window across
repeated failures, cap beats a larger alias cooldown, configurable cap,
telemetry fields, per-provider clear isolation),
`tests/test_cooldown_recovery.py` (successful probe restores the primary
with `fallback_used=false`, failed probe re-arms within the cap keeping
the class and count, concurrent request receives the probe-in-flight
skip, Origin refusal clears nothing, state policy/telemetry),
`tests/test_cli.py` (flag/env knob, invalid values refused at parse,
cap reaches the circuit store). Operator runbook §8/§9 document the
recovery procedure and the retired legacy systemd unit.
### R45 — A malformed top-level Responses `input_image` item bypassed capability detection and reached a text-only adapter — **closed**

**Severity: P2 / medium (security).** The security gate on the image
remediation candidate `0696c8c2` found that a Responses `input` item of the
exact shape `{"type": "input_image", "image_url": "data:image/png;base64,AA"}`
placed at the **top level** of the `input` array - i.e. as an item, not as a
content part inside a message - bypassed every check. The item carries no
`content` key, so `payload_has_image_input` saw no image and reported the
request as text-only; same-dialect pass-through then forwarded the item
verbatim to a text-only adapter, which silently ignored it. The caller
received `200` and lost the image without an error. The Responses contract
carries an image as an `input_image` **content part inside a message item**;
a root-level image-like item is malformed for every route.

**Fix.** The root of a Responses `input` array is now validated by one shared
check (`_validated_responses_root_items` in `src/gateway/bridge/requests.py`)
that every reader of the root consults: capability detection
(`payload_has_image_input`), dialect conversion (`from_responses`) and
same-dialect pass-through (`convert_request`). A root-level image-like item
(by `type`, or by carrying an `image_url`/`input_image` key), a non-object
item, and an item type the conversion cannot express are all refused with a
`ConversionError` -> HTTP 422 `conversion_refused`, **before route selection
and with zero adapter calls**. Non-image typed root items (function calls,
outputs, and unknown types such as `reasoning` history) still pass through
unchanged in their own dialect - the provider owns its dialect - and are
refused, never silently dropped, when converting to the other one.

**Regression evidence:**
`tests/test_image_input.py::TopLevelResponsesImageItemTests` (the gate's
exact probe over real HTTP: 422 + zero adapter calls, on both a text-only
and an image-capable route; malformed shape variants; the valid nested image
still reaches an image-capable route and fails closed 503 with only
text-only routes; a plain text request is unaffected) and
`tests/test_image_input.py::ImageDetectionTests` (root-item refusals at the
detection layer, non-object items, and the pass-through/conversion split for
non-image typed root items).


### R46 — The image verdicts never reached the stored rows, and four MiMo slugs had never been transcribed — **closed**

**Severity: medium capability correctness, with a client-visible symptom.**
*(closed)*

Observed live on 2026-10-24: on the deployed gateway (`59d6f9f`, `/health`
200), `GET /v1/alias-catalog` reported `supports_image_input: false` for
**every** published alias — `coding`, `flash`, `hermando`, `pm` — so an
image attached to any Hermes profile message found no route and was
refused. R37 had already made image evidence per-exact-id and
source-backed; two separate drops kept that from mattering.

1. **A catalogue verdict that never reaches a stored row reads as false.**
   The rows in the deployed `config.json` were written before
   `supports_image_input` existed and were never rewritten: the union of
   stored model keys held no image field at all, so `ModelEntry.from_dict`
   filled the declared default `False` for every row. That includes
   `minimax-m3.1-flash`, which the source-backed catalogue already marked
   capable — the evidence existed in the file and nowhere else.
2. **Four MiMo slugs had never been transcribed.** All four rows read
   `image: False` with `image_input_source: None`, which is *never read*,
   not *read and false*. `mimo-v2.6-flash` is the fallback on all four
   aliases, so its unwritten row is what made every alias text-only.

**Fix.** Each adapter now exposes its evidence as data —
`image_input_evidence()` returning
`{exact_slug: ImageEvidence(supports_image_input, source, observed_at)}`
for the slugs it has actually read (`OpenCodeGoAdapter` from the dated
snapshot, `ChatGPTCodexAdapter` from `IMAGE_INPUT_SOURCES`) — and
`image_evidence()` / `apply_image_evidence()` in `core/capabilities.py`
carry it into the stored rows. `build_application()` re-applies it inside
the boot transaction, so a corrected catalogue reaches `config.json` on
the next start with no hand-edited state. The rules are deliberately
narrow: **promotion only** (a restart never demotes), **exact slug only**
(never a family, never a name match), **no row added, enabled or
disabled**, **no provider call**, and **fail closed** — a missing hook, a
hook that raises, or a verdict without a source contributes nothing and
never stops boot.

The OpenCode evidence now transcribes the four MiMo slugs from OpenCode's
own per-model data pages (`https://opencode.ai/data/xiaomi/<slug>`, read
2026-10-24): `mimo-v2.6-flash`, `mimo-v2.6-pro` and `mimo-v2.5` accept
images, `mimo-v2.5-pro` does **not** — its page lists text only, and that
sourced *absence* is stored as `false` beside its source rather than left
blank. Audio and Video on the same pages are deliberately not transcribed:
the capability model is a single image boolean, and no field may be read
as an audio or video guarantee.

**Regression evidence:** `tests/test_image_input.py` —
`SnapshotImageEvidenceTests` (the verdict, source and read date of all five
evidenced slugs; unsourced rows staying false and sourceless; the sibling
`mimo-v2.5` / `mimo-v2.5-pro` disagreement kept apart; the builder's table
and the JSON it writes agreeing slug for slug; no audio/video field),
`ImageEvidenceReconciliationTests` (promoting exactly the evidenced rows,
idempotence, never demote, never add/enable/disable, fail-closed hook, a
verdict without a source dropped) and `BootReconciliationTests` (a real
`build_application` boot over a production-shaped `config.json` that
carries no image key: rows and file both updated, `/v1/alias-catalog` read
over HTTP reporting the capable alias `true` and the unevidenced one
`false`, a second boot changing nothing, and
`candidates(..., needs_image_input=True)` keeping only image-capable
routes).


### R47 — The rollup's request index grew one entry per request for the whole 30-day retention — **closed**

**Severity: medium (growth / availability).** *(closed)*

Security gate `t_eb1846b6` (finding S-1) on the analytics candidate
`bb5f066`: `RollupStore._index` mapped every `request_id` to its
contribution so a later amendment could replace it instead of counting it
twice, and the only pruning was by age — one entry per appended row, held
for the full 30-day retention. Measured: 300k rows → **+187 MB RSS**
(~0.6 KB/entry), so 30k requests/day ≈ 540 MB and 100k/day ≈ 1.8 GB on an
appliance whose uptime is effectively unbounded. Every other ledger
structure was already count-bounded (ring 10_000, writer queue 20_000,
ledger file 5 MiB × 3); this was the only runtime structure that was not.

**Fix.** The index is count-capped at `INDEX_MAX_ENTRIES = 50_000`
(`src/gateway/ledger_persistence.py`), documented at the constant: large
enough that a normal boot never evicts (the whole durable ledger, 15 MiB ≈
40k rows, fits under it) and several times wider than the 10_000-record
ledger ring that gates amendments — `RequestLedger.amend` only writes an
amendment row while the event is still in the ring, so the product can
never amend an entry the cap has already evicted. Eviction is
oldest-first by *last touch* (an amendment re-inserts), and it evicts the
**entry, never the counted request**: the contribution stays in its bucket,
`coverage["requests"]` now sums the buckets, and the evicted id is
remembered in a second bounded set of the same size so a late amendment is
**dropped and reported** (`coverage["index_evicted"]`,
`coverage["index_amendments_dropped"]`) instead of silently double-counted.
The cap itself is stated in coverage (`index_entries` /`index_max_entries`),
and `_prune_expired` now also ages out whole days, so count-evicted
contributions cannot keep finished days alive forever.

**Regression evidence:** `tests/test_rollup.py::RollupIndexBoundTests` —
70k-row synthetic flood (index pinned at the cap, eviction counters exact,
bucket totals lossless), a late amendment for an evicted id (dropped,
reported, original values intact), and the cap-vs-ring invariant
(`INDEX_MAX_ENTRIES ≥ 2 × DEFAULT_LEDGER_CAPACITY`).


### R48 — The sidecar persisted raw per-request fallback reasons with no cardinality cap — **closed**

**Severity: medium (growth / writer stall).** *(closed)*

Security gate `t_eb1846b6` (finding S-2): `_tally` stored
`fallback_reason` verbatim as a dict key. A recorded reason embeds a
per-second cooldown counter, a climbing failure count and a
provider-supplied `retry_after` (`MAX_BACKOFF_SECONDS = 86400`), so raw
strings are effectively unique per request during cooldown-driven
fallback. Measured: 6,000 fallback events → 6,000 keys → 2.5 MB sidecar
(~415 B/key); one 86400 s episode ≈ 86k keys ≈ 11 MB in a single row. The
source ledger is capped (5 MiB × 3) but the sidecar had no size guard, and
`maybe_save` rewrites the whole file from the ledger writer thread every 50
rows / 15 s — so a reason flood stalled appends, backed up the 20k write
queue, and drove the documented "full write queue drops persistence update"
path that loses authoritative ledger rows. `gateway.http.app` already
folded reasons to a closed vocabulary *at read time*; storage did not, and
the two rule sets could drift.

**Fix.** `FALLBACK_REASON_RULES` and `fallback_reason_class` moved to a new
shared module, `src/gateway/fallback_reasons.py`, imported by **both** the
reader (`gateway.http.app`) and the store (`gateway.ledger_persistence`),
which now folds the reason **at ingest** in `_row_delta` — one ordered rule
set, one closed vocabulary (`cooldown` / `route_ineligible` /
`upstream_failure` / `other` / `unknown`), idempotent so the reader-side
fold is a no-op on rows the store produced. Counts are preserved exactly:
folding changes which key a count lands under, never how many counts there
are, and coverage/`incomplete` semantics are untouched. No separate byte
cap was added: with the key set bounded by the vocabulary and the row set
bounded by 30 days × configured dimensions, sidecar size is bounded by
construction (measured: 6,000 distinct reasons → 1 key, 1,034-byte sidecar,
was 1,312,028 bytes).

**Regression evidence:** `tests/test_rollup.py::RollupReasonVocabularyTests`
(6,000 distinct reasons → key count within the vocabulary with every event
counted once; sidecar < 64 KiB under the same flood) plus the untouched
rollup, cached-completeness and R26 suites.

### R49 — Skip paths served a cross-provider fallback without the owner's authorization — **closed**

**Severity: medium (policy / data-flow control bypass).** *(closed)*

Security gate `t_01f5ce65` (finding S1): `_fallback_permitted` — the only
place `fallback_authorized`, `fallback_confirmed_providers` and
`fallback_triggers` were enforced — ran only on the AdapterError path. Two
other paths set `fallback_used = True` and served a later, cross-provider
route without consulting it: a primary excluded during enumeration
(cooldown, capability, configuration) and the invocation-boundary claim
refusal. With the default alias settings (fallbacks named, authorization
off, no provider confirmed) every request during a primary cooldown was
served by an unconfirmed provider, contradicting `config.py`'s own
contract and this document's fallback policy. Reproduced byte-identically
on base `main` `a2d1286d` (probe p6: 3/7 on both trees), i.e. pre-existing
— not a wave-1.6r regression.

**Fix (owner decision, 2026-10-25).** Every cross-provider switch away
from a skipped primary passes the same gate as a failed attempt —
authorization, confirmation of the next provider, and an enabled trigger —
on both skip paths, and the skip itself contributes the trigger:
`route_ineligible` (new) for capability/configuration exclusions,
`explicit_cooldown` for operator-placed cooldowns, the cooldown's own
recorded failure trigger for automatic ones (so existing owner
configurations keep working), and `probe_contention` (new) for a route
held by a live half-open recovery probe. The vocabulary is persisted
(`FALLBACK_TRIGGERS`, backup validation), exposed through the owner API
and console (both render from `state.fallback_triggers`), and documented
in ARCHITECTURE and the RUNBOOK troubleshooting table. A declined switch
is refused with the existing `fallback not used: <reason>` wording and
recorded in the ledger as served by nobody — status 503, `error_class`
`policy`, skips intact, `fallback_used=false` — without invoking either
route. A phase-selected route remains the primary for its phase and is
not gated.

**Regression evidence:** security probe `p6_authorization.py` 7/7 on this
tree (3/7 on both base and candidate before the fix);
`tests/test_skipped_primary.py::SkipPathAuthorizationTests` pins each gate
dimension refused and served on both skip paths, automatic-cooldown
trigger inheritance, a cooldown opened by a trigger this alias disabled,
the claim-branch gate (via a store rigged so only the invocation-boundary
claim refuses, the one interleaving no test can reach otherwise), the
recorded refusal outcome, and the owner API/save round-trip for the new
triggers; the reconciled merge-tree suite ran 709 tests, 3 skipped.

### R50 — Tool-bearing requests were refused on skipped, never-invoked primaries — **closed**

**Severity: P1 (availability / policy misapplication).** The skip path reused the side-effect guard intended for attempted routes. While `chatgpt_codex` was held in an exhausted-cooldown loop, a live A/B probe on alias `pm` showed tool-bearing requests refused with 503 `fallback_not_permitted` while a plain request fell back to healthy `opencode_go`; ledger rows had `attempts: []`. The operator counted 635 policy 503 rows with empty attempts plus 62 `502 exhausted` rows (697 recorded failures, not distinct user intents). An independent retained-ledger probe saw 97 policy rows, all with empty attempts. The owner later established that missing trigger coverage contributed to the incident and restored intended alias configuration; there is no production evidence that the historical 635 requests carried tools, so those rows are not attributed to the code defect documented here. A separate in-process reproduction confirmed the additional defect: with authorization, provider confirmation, and the automatic cooldown trigger enabled, tools-only traffic was refused while identical no-tools traffic fell back.

**Fix.** Skip decisions use `_owner_switch_controls` (authorization and provider confirmation) plus the applicable skip trigger, without the attempted-route side-effects gate. That gate remains on `_switch_preflight` for attempted routes: it refuses when the request carries tools and an attempt was recorded (`needs_tools and bool(attribution.attempts)`), since an upstream may already have executed a tool. Skip call sites remain three-argument; no primary attempt or call occurs on the cooldown/claim-refusal path. Refused skip decisions persist the bounded reason in `fallback_reason` and keep `fallback_used=false`. Owner-console trigger-coverage warnings and the README/architecture/runbook distinctions are retained.

**Regression evidence.** `tests/test_skipped_primary.py::SkipPathAuthorizationTests` covers tool-bearing fallback after cooldown, enumeration and claim-refusal paths, refusal when authorization or the skip trigger is missing, zero primary attempts/calls, and refusal-reason persistence. `tests/test_end_to_end.py::test_tools_never_trigger_a_fallback` and attempted-route tests retain the cross-provider side-effect refusal. Full suite: 709 tests, 3 skipped.

### R51 — New skip trigger vocabulary was unenabled in production aliases — **closed by owner configuration**

PM's source-of-truth state probe found `route_ineligible` and `probe_contention` enabled on zero aliases, and one alias also lacked `explicit_cooldown` despite the Oct 25 owner ruling. These were configuration gaps, not justification to revert R49: the gate closes the R49 security bypass and is correct when the owner policy covers the selected skip trigger. PM applied the approved six-trigger list to all four aliases via the owner API and verified persistence and behavioral probes. The new config coverage warnings prevent a future vocabulary addition from silently creating a fleet-wide refusal condition. Historical 635 policy refusals are evidence for a production window as counted by PM; 97 were independently counted from the retained raw ledger at this worker's probe, and all those rows had empty attempts — no provider calls. Hundreds of successful fallback serves existed in the retained window; exact user-visible loss is the recorded 635 refusals plus 62 exhausted 502s (697 failed requests total), with no deduplication of retries/user intent implied.

### R52 — Skip-policy refusal reason absent from durable ledger — **closed**

A refused owner gate had a useful HTTP message but `fallback_reason=null` in the request row, so the durable ledger could not distinguish authorization, confirmation, trigger, or side-effect refusal without inspecting transient access logs. Store the same bounded refusal wording in `fallback_reason` on policy 503s; keep `fallback_used=false`. This is metadata only and contains neither request content nor secrets.

### R53 — Owner-state phase routes did not round-trip through alias save — **closed**

`GET /owner/api/state` encodes each phase route as `{phase, route:{provider,model}, effort}`, while `validate_phase_routing` originally consumed only the flat `{provider,model,effort}` shape used by saves. Accept both representations. The regression test GETs current state, resubmits its phase policy with an alias save, and verifies it persists.

### R54 — LAN owner mutation authentication relies on reachability and Origin — **owner decision required**

The owner API is intentionally exposed to trusted-LAN devices and validates the browser Origin, but it has no independent owner token. During the incident the owner confirmed Mark intentionally made the config changes. Whether to add token-based mutation authorization is a separate product/security-boundary decision; this task does not modify that contract.

### R55 — The capability decision path applied proposal field names and non-boolean values without the capability vocabulary allowlist — **closed**

**Severity: Low (defense-in-depth / spec conformance).** `handle_capability_decision` ran `setattr(row, field, proposed_value)` for every field named in the persisted proposal sidecar. The only field check consulted that proposal's own field list — circular, since the sidecar is untrusted input whose loader validates only `schema == 1` and list-typed buckets, never field names or value types — while the catalogue-refresh path already enforced the vocabulary plus `isinstance(value, bool)`. Reproduced by the independent security gate with a tampered sidecar: a proposal naming `dialect` returned 200 and changed `dialect` in memory and on disk with provenance under `capability_approvals`, and a `proposed_value` of `"not-a-bool"` was persisted into `config.json` for a boolean capability field. That contradicted this endpoint's "No arbitrary metadata patch" contract in `docs/MODEL_CAPABILITY_DISCOVERY_SPEC.md`. No remote path creates proposals today (`record_fake_result` is unrouted and `/run` always returns 422) and writing the 0600 sidecar needs the privilege to edit `config.json` directly — hence Low; it becomes material when real probe transports build proposals from provider-influenced observations.

**Fix.** The decision path consults the shared `APPROVABLE_METADATA_FIELDS` vocabulary (`src/gateway/capability_discovery.py`: `{"supports_image_input", "supports_function_tools"}`) inside the config transaction, after the revision/fingerprint CAS and before any write: a decision key outside the vocabulary, a proposal field record that is missing, or a `proposed_value` that is not a `bool` raises `ValueError` → 400 with no `setattr`, no provenance row, no revision bump and no recorded decision. The refresh path reuses the same constant instead of restating the set literal, and `DiscoveryStore.decide` refuses out-of-vocabulary names as defense in depth. CSRF/Origin enforcement, preview-bound explicit selection, the atomic CAS, and audit/provenance are unchanged; the owner-accepted trusted-LAN boundary (reachability is administration, Origin/Host are not identity) is untouched.

**Regression evidence.** `tests/test_capability_discovery.py`: `test_decision_refuses_field_outside_capability_vocabulary` (tampered sidecar, out-of-vocabulary and mixed decisions, config byte-identical in memory and on disk, proposal still pending), `test_decision_refuses_non_boolean_proposed_value` (`"not-a-bool"` never reaches `config.json`), `test_decide_refuses_field_outside_capability_vocabulary` (store layer, legitimate vocabulary still decides), plus the unchanged happy-path approval and stale-revision CAS tests. Full suite: `unittest` 881 tests, 3 skipped; `pytest` 879 passed, 3 skipped, 665 subtests; compileall, `git diff --check`, `tests/console_dom_test.js` and `tests/test_opencode_catalog.mjs` clean.

### R57 — Capability approval could be recorded before config durability — **closed**

**Severity: High (approval/replay integrity under persistence failure).** The HTTP decision path persisted the discovery sidecar before the config transaction committed. If `config.json` persistence failed, the request returned an error while the sidecar recorded approval; an exact retry replayed success without applying the capability.

**Fix.** The decision handler now commits effective config first, with a bounded proposal/decision marker and provenance in that same config write. Only after that durable write does it commit the decision sidecar. If sidecar persistence fails or the process stops between writes, an exact retry checks the durable marker and repairs the sidecar; the request does not claim success until repair completes. `DiscoveryStore.decide()` stages mutations and rolls them back in memory on `OSError`, preventing a failed write from becoming an in-process replay. The config write remains atomic; failed config persistence leaves the sidecar pending and retry applies the decision.

**Regression evidence.** `tests/test_capability_discovery.py`: `test_config_persistence_failure_does_not_consume_decision_and_retry_applies` and `test_sidecar_failure_is_recoverable_by_exact_retry_from_config_marker` exercise real HTTP harness requests, injected persistence failures, retry, and sidecar reload/restart. The suite also covers partial decisions, exact replay, stale CAS conflicts and tampered-field refusals.

### R59 — The capability decision path refused a missing or bogus `classification` only after `decide()` had persisted the decision — **closed**

**Severity: Low (audit-trail integrity under privileged sidecar tamper).** The three S-1 refusals in `handle_capability_decision` (out-of-vocabulary decision key, unknown proposal field record, non-boolean `proposed_value`) all fire before `DiscoveryStore.decide()`, so a refused request records nothing. A fourth tamper shape slipped past them: a vocabulary-valid, bool-valued field record whose `classification` key is deleted (or set to a non-spec value) passes every pre-check, `decide()` writes the decision row and flips the proposal to `approved`, and only then does the apply loop raise on `field_record["classification"]` → 400 with the config transaction rolled back (no config write, no provenance row, no revision bump). The 0600 evidence sidecar therefore claimed an `approved` decision that never applied, and the burned proposal (`approved`) made any later legitimate decision on it a 409 "expired or resolved" — an audit-trail integrity gap, not a privilege escalation: writing the sidecar needs the same local privilege as editing `config.json`, the rationale R55 already accepted for Low. Reproduced on candidate `213d7a3c` with an isolated loopback harness (HTTP 400, config bytes identical, sidecar decisions: 1, proposal status: `approved`).

**Fix.** Every key the apply loop reads is now validated inside the config transaction, after the revision/fingerprint CAS and before `decide()`: the shared `EVIDENCE_CLASSIFICATIONS = {"documented", "observed"}` vocabulary (`src/gateway/capability_discovery.py`, the spec's own `classification` enum) is checked alongside the existing vocabulary/boolean checks, and the approved writes are precomputed so nothing after `decide()` can raise on sidecar input. A missing, non-string or non-spec `classification` raises `ValueError` → 400 with no config write, no provenance row, no revision bump, no recorded decision and a proposal that stays `pending` and decidable. CSRF/Origin, the CAS, the vocabulary/boolean checks and the trusted-LAN boundary are unchanged; R55's claim about its three named cases remains accurate as written.

**Regression evidence.** `tests/test_capability_discovery.py`: `test_decision_refuses_missing_classification_before_sidecar_write` (RED on the base candidate — the sidecar recorded the `approved` decision and burned the proposal — then green: 400, config byte-identical in memory and on disk, zero decision rows, proposal and field still `pending`) and `test_decision_refuses_out_of_vocabulary_classification` (also RED on base: `classification: "totally_verified"` was applied with HTTP 200 — now 400 with the same no-write assertions), plus the unchanged happy-path, OOV/mixed/non-bool refusal, stale-CAS and store-layer tests. Full suite on the R59 candidate: `unittest` 883 tests, 3 skipped; `pytest` 881 passed, 3 skipped, 665 subtests; `compileall`, `git diff --check`, `tests/console_dom_test.js` and `tests/test_opencode_catalog.mjs` clean.

**Id note.** This entry was numbered **R56** on the discovery branch; the 2026-10-30 union-merge dedupe (card `t_6ff860f6`) renumbered it to **R59** so that origin/main's `/owner/api/state` finding keeps **R56** (cited by commit `c3d86b0`, PR #39 and the wave-2B cards). Older cards and commit subjects that cite "R56" for the `classification` refusal mean this entry.

### R60 — Capability decisions could not complete safely across partial approvals or HTTP retries — **closed**

**Severity: High (approval workflow availability and correctness).** QA found that partial approval made the proposal terminal, HTTP revision CAS rejected exact retries before the store's idempotency check, and stale HTTP CAS failures left the sidecar pending.

**Fix.** Partial proposals stay decidable for still-pending fields. Following a successful partial config write, the sidecar advances its CAS base to the new revision and model fingerprint; a later explicit decision can resolve remaining fields, while stale tokens conflict. Exact decision ID + proposal + decision data replays the stored result before config mutation; ID reuse with different data conflicts. Stale CAS marks the proposal `conflicted` before HTTP 409. No automatic metadata updates or provider calls are introduced.

**Regression evidence.** `tests/test_capability_discovery.py` exercises sequential HTTP decisions with refreshed CAS, exact HTTP replay without another revision bump, stale HTTP conflict status and zero config mutation, and store-level partial progression. Candidate verification: full unittest suite 920 tests, 3 skipped; compileall, console/catalogue checks and `git diff --check` clean.

**Id note.** This entry was the discovery branch's second **R57**; the 2026-10-30 union-merge dedupe (card `t_6ff860f6`) renumbered it to **R60**. **R57** now means only "Capability approval could be recorded before config durability". Older cards citing "R57" for partial approvals / HTTP retries mean this entry.

### R59 — Capability probe accepted misattributed model evidence and had unbounded response/deadline paths — **closed**

**Severity: High.** The probe classified a nonce echo as support without requiring the response `model` to match the reviewed model, and treated NaN/infinite/negative published prices as `known`, allowing a nonsensical estimate to pass authorization. In addition, buffered probe reads had no response-byte ceiling or absolute end-to-end timeout; cancellation only set an event checked between requests, so an in-flight read could retain the run gate.

**Fix.** Pricing status now rejects non-finite and negative values; response classification refuses missing or mismatched model IDs as inconclusive. The reviewed probe protocol sets a 1 MiB response ceiling. Its transport reads bounded chunks, applies an absolute run deadline, and watches cancellation/deadline to close the active connection, so trickle progress cannot extend the budget and cancellation interrupts a blocked read. Probe cancellation/deadline errors remain inconclusive and the run reports the correct stop reason.

**Regression evidence.** `tests/test_capability_probe.py` covers NaN, ±infinity, negative pricing, mismatched/absent response model, and oversized response refusal. `tests/test_capability_probe_http.py::ProbeHttpTests::test_the_owner_can_cancel_a_run_in_flight` exercises cancellation against the isolated delayed loopback upstream. On this candidate: focused probe/HTTP/retry/upstream-pool suite 87 tests passed; full unittest suite 988 tests passed (3 skipped) before the final stop-watch cleanup adjustment; `compileall`, `git diff --check`, console DOM and catalogue checks passed. No paid provider calls or production operations.

### R58 — Proposal expiry prevented recovery of a config-committed approval audit — **closed**

**Severity: Medium (crash recovery and audit availability).** Config-first approval persistence intentionally makes config the durable transaction record. If the process stopped after the config commit but before the discovery sidecar commit, and the proposal TTL elapsed before retry, the exact retry was rejected by `DiscoveryStore.decide()` as expired. Effective config and provenance were already committed, but the sidecar could never be repaired through the owner endpoint.

**Fix.** The handler's config-marker recovery path now explicitly invokes `decide(..., recover_committed=True)` only after matching the durable marker's proposal, decision ID, and decision map. The store still performs its exact decision replay check first; recovery permits an expired proposal status only on that explicit path. Ordinary new decisions remain expiry-gated, while terminal resolved/conflicted states and conflicting decision-ID reuse remain refused.

**Regression evidence.** `tests/test_capability_discovery.py::CapabilityApprovalHttpTests::test_expired_proposal_recovers_only_exact_config_committed_decision` injects sidecar persistence failure, reloads the sidecar after advancing expiry, confirms exact retry recovers approval, and confirms a different decision ID receives 409 without changing the resolved proposal. It exercises the isolated HTTP harness only; no providers or production lifecycle.

### R60 — The probe transport leaked its active set, a stale stream watchdog could kill an unrelated request, and an unexpected transport error escaped the probe runner as an unclassified 500 — **closed**

**Severity: Medium (transport bookkeeping, cross-request interference, and evidence integrity).** QA re-gate `t_d746f999` found four defects in the R59 remediation on `901f1a48`. Re-measurement on `9a6414a` confirmed the timing and bound defects already closed there — a slow body with the production cancel event armed completes at 2.007 s against the advertised 15 s (was 0.107 s), a 3xx body is cut at ~1.06 MiB (was fully consumed at 64 MiB), a cancellation ends a blocked read 0.035 s after the cancel (was 14.7 s), and the run deadline holds at 1.501 s (was 5.878 s then a crash) — and the repo's own `tests/test_probe_transport_bounds.py` locks each of those. Three defects remained, all bookkeeping around that machinery:

1. `_open` added every checked-out connection to `_active` but only the buffered-success path removed it, so **every failed request left one entry forever** on the adapter's long-lived client (10 refused requests → 10 entries).
2. A streamed request's deadline/cancellation watchdog was **never stopped**: the watch thread leaked per stream, and when its absolute deadline later fired it `shutdown()`d the socket it remembered — which by then could belong to an **unrelated request** re-acquired from the same pooled connection (measured: an innocent second request died 0.351 s in with `RemoteDisconnected`).
3. `run_suite` caught only `AdapterError`, so any other exception from the transport — QA caught an `AttributeError` from the read/close race — escaped to the owner API as an **unclassified 500 with the confirmation consumed and no run recorded**.

**Fix.** Every release point now goes through `UpstreamClient._release()`, which drops the connection from `_active` before returning it to the pool, and `_open`'s failure paths discard before closing — no exit leaves an entry. Each request's watch tracks socket ownership: `_retire()` marks the request finished and stops its watch under the same lock `_interrupt()` re-checks before any `shutdown()`, and every exit — including the stream iterator's `finally`, which runs when the stream ends — retires *before* releasing, so a finished request can never touch a socket another request now holds. Watch threads are named `upstream-watch-*`. In `run_suite`, anything the transport raises that is not an `AdapterError` is failed closed: an inconclusive `transport` result carrying the run's honest stop reason (cancelled / deadline / prior_failure), so evidence is always recorded and nothing escapes as a 500. The fail-closed contract is unchanged: unknown price ⇒ no paid call; non-finite/negative ⇒ reject; timeouts/malformed/unexpected ⇒ inconclusive, never a false negative.

**Regression evidence.** RED-then-green on the base: `tests/test_upstream_http_pool.py::test_failed_requests_do_not_leak_the_active_set` (RED: 7 entries after 7 failures), `::test_a_finished_stream_watch_never_interrupts_a_later_request` (RED: finished stream still in the active set; on base the stale deadline then killed the next request — now the unrelated request completes at 1.501 s), `::test_watch_threads_do_not_outlive_their_requests`; `tests/test_capability_probe.py::RunnerBoundsTests::test_an_unexpected_transport_exception_fails_closed_with_evidence` and `::test_an_unexpected_exception_during_a_cancellation_still_reports_it` (RED: `AttributeError` escaped the runner). Independent QA probes re-run against the candidate: failed-request leak 0 of 10, stream entries 0 after each of 5 streams, stale-watch victim survives (200 at 1.501 s), every D1–D7 transport and A1–A10 HTTP scenario at base-or-better timings, evidence rows recorded on the cancellation path. No paid provider calls, no production lifecycle, no live config mutation — synthetic loopback upstreams only.
### R56 — `/owner/api/state` re-derived its ledger and price inputs per consumer — **closed**

Wave-2B (tree `3e2d6c86`, PR #38) made `handle_state` call
`ledger.recent(limit=10_000)` once per alias per consumer (budget per alias,
three statistics windows, provider health) and, far worse, every per-event
`calculate_cost` lookup inside `aggregate_statistics` and `budget_status`
re-read and re-parsed the 42KB published-price catalogue from disk: over
100,000 file reads per console poll. Measured 22.2s for a 10,000-row ring
with 4 aliases locally, 46.9s in production, before the wave-2B rollback of
2026-10-29; the console never rendered below its banner.

`handle_state` now materialises the ring snapshot and the published-price
catalogue exactly once per request and threads both into every consumer;
`aggregate_statistics` and `budget_status` accept an optional `_prices`
snapshot (default `None` preserves the per-lookup read for other callers).
Same numbers, same fail-closed history-incomplete semantics
(`ledger.evicted == 0`).

Regression gate: `tests/test_owner_state_latency.py` seeds 10,000 priced
ledger rows and 4 budgeted aliases, times `GET /owner/api/state` (asserts
< 2s; fails on `3e2d6c86`, where the request exceeds even the 10s client
timeout), and asserts every consumer's payload with hand-computed cost
totals. Fixed path measured 0.47s on the same fixture.

### R61 — Proposal expiry was enforced only after the config transaction committed — **closed**

**Severity: Medium (state change on a refused owner request; spec CAP-105 / CAP-201).** `handle_capability_decision` left expiry to `DiscoveryStore.decide()`, which runs **after** `with self.config.mutate()` has durably committed. An expired proposal with a fresh, correct `expected_revision`/`expected_fingerprint` and no config marker was therefore refused HTTP 409 **with config written**: revision 1→2, `supports_image_input` flipped, provenance row and decision marker persisted, zero sidecar decision rows — and because the refusal itself had armed the exact recovery marker, the identical retry then returned 200 `approved`. The store comment ("Expiry gates new decisions") was false at the HTTP layer. Found by the independent security re-gate (card `t_854b0944`) on candidate `22ec209` / tree `6c2eeeb6`, probe assertions C3b/C3d; the defect predates the R56 merge (byte-identical handler at `240b0afe`).

**Fix.** `handle_capability_decision` now evaluates a single shared `proposal_expired()` predicate (`src/gateway/capability_discovery.py`, also used by the store's own defence-in-depth check so the two layers cannot drift) immediately after the proposal fetch and the exact config-committed-marker recovery branch, and **before** `with self.config.mutate()`. An expired proposal without that exact marker raises → HTTP 409 with no config or sidecar write: no revision bump, no provenance, no marker, no decision row, proposal left exactly as found. `recover_committed=True` marker recovery (R58) is untouched, as are CSRF/Origin/Host enforcement, the CAS, provenance, config-before-sidecar ordering and the trusted-LAN reachability boundary. A missing or unparseable `expires_at` on the untrusted sidecar refuses as well — parsing raises before any write, so expiry is never read as "still valid".

**Regression evidence.** `tests/test_capability_discovery.py::test_decision_refuses_expired_proposal_with_no_state_change` — RED on `22ec209` (409 with revision bump, capability applied and marker written; exact retry then 200), green on this candidate (409, `config.json` byte-identical on disk and snapshot-identical in memory, capability unchanged, no provenance/marker, zero decision rows, proposal and field `pending`, exact retry still 409). The independent security probe `probe_gate.py` (92 assertions, card `t_854b0944`) measures 92/92 on this tree against its 86/92 baseline on `22ec209` (failures C3b/C3d gone), with `probe_idlen.py` covering R63. Full suite: `unittest` 931 tests, 3 skipped; `pytest` 929 passed, 3 skipped, 675 subtests; `compileall`, `git diff --check`, `tests/console_dom_test.js`, `tests/test_opencode_catalog.mjs` clean; ruff 0.17.2 line-agnostic diff vs `22ec209`: 185 vs 185 findings, 0 new / 0 removed. Release remains gated on the security re-gate and the QA gate both PASSing the exact fix SHA/tree.

### R62 — Decision values were validated only after the config transaction committed — **closed**

**Severity: Low (spec line 141: "on validation/persistence failure none are applied").** The pre-write validation inside the transaction covered decision *keys*, value type and `classification`, but never `decisions.values()` — that check lived only in `DiscoveryStore.decide()`, post-commit. Measured by the security re-gate on `22ec209` (probe F1b/F1d/F2b/F3b): a value typo (`"approvd"`) returned 400 **with** revision bump + a decision marker for a decision that never applied, after which the next legitimate decision was refused 409 (proposal burned); an empty map returned 400 with a revision bump and an empty approvals bucket; a mixed `{image: "approved", tools: "nope"}` map returned 400 **with `supports_image_input` applied**. No privilege escalation (the endpoint already requires owner reachability), but a refused request changed state and burned audit evidence.

**Fix.** Inside the same pre-write block as the existing vocabulary check: `if not decisions or not set(decisions.values()) <= {"approved", "rejected"}: raise ValueError("invalid decision values")`. It raises inside `with self.config.mutate()`, which commits only when no exception escapes, so the refusal is a guaranteed no-write: no `setattr`, no provenance, no revision bump, no marker, no decision row, proposal still `pending` and decidable. The store's own post-commit check remains as defence in depth; counts are bounded upstream by the key-vocabulary check (at most the two approvable fields), so no `decide()` input refusal is reachable pre-commit any more. Nothing else in the handler moved: replay, CAS, ordering, marker recovery and boundary checks are unchanged.

**Regression evidence.** Three new tests in `tests/test_capability_discovery.py` — `test_decision_refuses_non_vocabulary_decision_value_before_config_write` (typo → 400 no-write, then the next legitimate decision against the same snapshot → 200), `test_decision_refuses_empty_decisions_map_before_config_write`, `test_decision_refuses_mixed_valid_and_invalid_values_without_applying_either` (valid field not applied, both capability values unchanged) — all RED on `22ec209` and green here, asserting `config.json` byte-identical, no revision bump, no approvals/marker, zero decision rows and proposal `pending`. Probe `probe_gate.py` F1/F2/F3 now green (baseline failures F1b, F1d, F2b, F3b). Suite/gate numbers as listed under R61.

### R63 — Identifier bounds were validated only after the config transaction committed — **closed**

**Severity: Low (same root cause as R61/R62, found while fixing them).** `DiscoveryStore.decide()` also refuses an empty or >64-character `proposal_id`/`decision_id` — but only after the config commit, so an over-long id was refused HTTP 400 **with** config written. Measured directly on `22ec209` with an isolated loopback harness (`probe/probe_idlen.py`): a 65-character `decision_id` returned 400 while `config.json` went revision 1→2 with the capability applied and a decision marker recording an id the store had rejected.

**Fix.** Both id bounds are validated at the top of `handle_capability_decision`, before replay lookup, CAS and any mutation, raising `ValueError` → 400 with no write. This cannot hide a legitimate exact replay: `decide()` validates the bounds *before* recording a decision row, so no stored row can ever carry an out-of-bound id.

**Regression evidence.** `tests/test_capability_discovery.py::test_decision_refuses_over_long_ids_before_config_write` (subtests for both ids) — RED on `22ec209` (400 with revision bump + marker), green here (400, `config.json` byte-identical, no marker, zero decision rows, proposal `pending`); plus `probe/probe_idlen.py` before/after (revision 1→2 → 1→1, marker written → `None`). Suite/gate numbers as listed under R61.
### R55 — `/version` `image_revision` reported a stale release record across three cutovers — **closed**

**Severity: high release-traceability risk.** *(closed)*

`/version` reported `image_revision: sha256:a1592fb7…` while the running container was a different image on each of three consecutive cutovers (this deploy `e5117d2dc19a…`, previous `ea0b48a5…`). Provenance: the field is populated exclusively from `release.json` on the persistent state volume, written post-hoc by `scripts/release.py`. A read-only production probe (2026-10-29) showed the file unchanged since 2026-10-28 03:57 with no container binding — at least three cutovers never re-ran the release write, and the gateway had no way to detect the lag. There is no build-time digest (`GATEWAY_IMAGE_REVISION` was never set by the Dockerfile), and an in-container process cannot learn its own image digest from the runtime, so an unbound record could always lie.

**Required fix:** either report the true running image identity, or remove/rename the field so it cannot be mistaken for image provenance. Never silently repurpose it.

**Resolution.** The record is now bound to the container it was written for. `scripts/write_release_identity.py` (executed inside the container by `scripts/release.py` and by the release gate) writes `{"image_revision": <digest>, "container_id": <64-hex>}`, resolving the container id read-only from the process mount table: `/proc/self/mountinfo` carries `/var/lib/docker/containers/<id>/{resolv.conf,hostname,hosts}` for every Docker container, while `/proc/self/cgroup` cannot serve this purpose because Docker's default cgroup namespace is private (`0::/` on production). `gateway.buildinfo.release_identity()` reports the recorded digest only when the recorded id equals the running container's id. A record from an earlier container, a record with no binding, a non-digest value, an unresolvable container environment, or no record at all all report `unknown`, which the CLI already surfaces as a startup warning. The build-time fallback for this field was removed: a digest baked at build time cannot be the digest of the image being built. The four-key `/version` schema is unchanged, and `source_revision`/`source_tree` are unaffected.

**Evidence:** `tests/test_release_binding.py` (22 synthetic tests) pins the contract: a bound record is reported; the production defect shapes (stale container id, legacy record without binding) report `unknown`; there is no build-time fallback; the writer refuses unbound or non-digest writes and writes mode 0600. `tests/test_compose_deployment.py` exercises the writer through the real release step against an isolated deployment. Production follow-up: the first cutover on this candidate must re-run `scripts/release.py` (it rewrites the bound record); until then `/version` reports `image_revision: unknown` by design.
### R64 — The console capability panel could never run a probe (owner gate fields never sent) — **closed**

**Severity: High (feature shipped but unreachable by its only authorised user).** The console's **Discover capabilities** panel could preview, but never run: the backend's owner gate requires `owner.authorize_billing_route`, `owner.spend_policy == "estimate_accepted"` and `owner.max_spend_estimate_usd > 0` (`src/gateway/capability_probe.py`, `_owner_gate`), while both console calls sent `body: { provider_id, model_id, probes }` and `{ ..., preview, confirmed: true }` — zero occurrences of `authorize_billing_route` in `src/gateway/static/console.js`. The run route also requires `base_revision` and a single-use `confirmation` issued by the preview, neither of which the panel produced. `preview.available` was therefore always `false` from the console and the "Run selected probes" button never rendered: the probe feature was correctly gated for safety and unreachable in practice.

**Fix.** The capability panel now carries an explicit, owner-facing authorisation step (not a silently-sent field): the owner selects probes, enters a spend ceiling (client-validated numeric `> 0`), and clicks **Authorise billing route**, which sends `authorize_billing_route: true` / `spend_policy: "estimate_accepted"` with the ceiling on both preview and run; the run call additionally carries `base_revision` and the preview's single-use `confirmation`. The panel states that the probe is a paid inference request and that the price is an estimate, not a hard cap (`hard_ceiling_available` is false — no implication otherwise). **Cancel/withdraw sends nothing** and returns to the prompt. The authorisation is deliberately **not persisted**: it lives only in the open console page, so every preview/run re-authorises explicitly and closing the page ends the grant — this is why no expiry/revocation machinery is needed, though a future persisted grant must add both.

**Regression evidence.** `tests/console_dom_test.js` — new behavioural check "capability owner gate gathers billing authorisation before any provider run": run button hidden until authorise; authorising sends the exact three owner fields on preview and run plus `base_revision`/`confirmation`; invalid ceiling (0, negative, non-numeric) never reaches the network; cancel produces zero POSTs. RED on `53f1151` (no authorisation step exists), green here. Real-browser acceptance (headless Chrome against a seeded gateway with a loopback upstream, zero external provider traffic): authorise → preview `available: true` with live estimate → run executes → `observed_supported` evidence renders and a **pending, unselected** proposal is listed with both field checkboxes unchecked; gateway-side upstream request counter confirms exactly one bounded run (text + shared image/function probe), and the cancel path left the counter at zero.
### SEC-1 — The owner spend-ceiling gate accepted `NaN` / `±inf` ceilings (fail-open) — **closed**

**Severity: Low (defence in depth — the shipped console validates `isFinite` before it ever sends a ceiling).** `_owner_gate` (`src/gateway/capability_probe.py`) refused a bad ceiling with `... or ceiling <= 0`, but `float('nan') <= 0` and `float('inf') <= 0` are both `False`, so a raw `NaN`/`Infinity` JSON literal (accepted by the standard decoder) passed the gate; `build_preview`'s `ceiling < total` comparison is `False` for `NaN` as well, so the preview returned `available=True`, a single-use confirmation was issued and the run executed. Re-run here against `554807e`: the security review's 34-check probe reproduced it (28 PASS / 6 FAIL, including preview `200 ready` and a run that reached the upstream with a `NaN` ceiling). The estimate was never at risk — it is tariff-derived and `hard_ceiling_available` stays `false`; the ceiling only ever compares against it. A ceiling written as an integer too large to represent as a float additionally raised an uncaught `OverflowError` on the same path (HTTP 500).

**Fix.** `_owner_gate` resolves the ceiling through `_ceiling_usd()`, which yields `None` for anything that is not a finite real number — `NaN`, `±inf`, `bool`, `str`, `None`, absent, unrepresentable integer — or is `<= 0`, and refuses those with `spend_ceiling_required`. `build_preview` uses the same helper and treats an unusable ceiling as *exceeded* (`spend_ceiling_exceeded`), so the comparison itself fails closed even if the gate is ever reordered. No trust was added: `hard_ceiling_available` remains `false` and the estimate remains the only authority.

**Regression evidence.** `tests/test_capability_probe.py::test_invalid_owner_ceilings_fail_closed` (NaN, ±inf, 0, negative, both bools, string, absent, `10**400` → `spend_ceiling_required`, no `confirmation`, estimate byte-identical to the valid-ceiling preview) and `::test_build_preview_itself_treats_an_unusable_ceiling_as_exceeded` (gate neutralised → `spend_ceiling_exceeded`); `tests/test_capability_probe_http.py::test_a_raw_nan_ceiling_is_refused_on_preview_and_run` posts a raw `NaN` JSON literal byte-for-byte to `/owner/api/capabilities/preview` and `/owner/api/capabilities/run` → 422, no confirmation issued, zero upstream requests. All three fail on the pre-fix tree (5 failures, 2 errors — the `10**400` cases as `OverflowError`), green here. Targeted suite (`test_capability_probe`, `test_capability_probe_http`, `test_state_cache`) 84 tests OK; full suite 1081 tests OK (3 skipped); `compileall`, `git diff --check`, console DOM and catalogue checks pass; ruff findings identical to the base tree (159/159, zero new); security probe re-run 34/34 PASS.

### R65 — Capability probe: 1 of 50 models probeable (undocumented), and the refusal misreported a known price as Unknown — **closed**

**Severity: Medium (the owner was told the wrong reason they were blocked, and the feature read as broken rather than gated).** Three related defects on `53f1151`, raised from the owner console. (1) `PROBE_PROTOCOLS` is a deliberate one-entry opt-in allowlist (`opencode_go` / `mimo-v2.6-flash`), but no surface said so — every other staged model's refusal read as a malfunction. (2) The refusal reason was factually wrong: `capability_discovery.preview_for`'s single text claimed "no approved provider probe protocol **and trustworthy price**" and the console rendered "**Price: Unknown**" for `minimax-m2.7`, which carries a sourced, observed catalogue tariff (`0.3`/`1.2`/`0.06` USD per 1M, `https://opencode.ai/data/minimax/minimax-m2-7`, observed `2026-10-25`) — conflating "no protocol entry" with "no price" hides real information. (3) `preview_for` was a second, dead source of this text: imported by `app.py` but never called there (the routes use `capability_probe.build_preview`), which is exactly how the two copies drifted into one of them lying.

**Fix.** Two distinct refusal reasons are now reported end to end: `no_probe_protocol` (no reviewed protocol entry for the pair — reason names only the registry, never the price) and `no_trusted_price` (catalogue tariff missing or stale; a stale tariff now reports `status: "stale"` with its provenance instead of collapsing to `unknown`). The tariff lookup moved ahead of the protocol gate (local catalogue read, zero requests, gate *precedence* unchanged: target → protocol → plan → tariff → owner → ceiling), so a refused preview renders the real published rates, source and observation date in a `price` block; `amount` stays `null` because no run estimate exists without protocol bounds — the console's new `probePriceText()` prints the tariff honestly and says "no published price" only when the catalogue has none. Registry coverage is surfaced on `GET /owner/api/capabilities` (`probe_registry`: registered pairs, count, staged-model count, whether *this* pair is registered) and rendered in the panel before any preview click, so a refusal reads as policy. `preview_for` was deleted: `capability_probe._base_preview` is now the single definition of the preview shape, and the dead `app.py` import is gone.

**Regression evidence.** `tests/test_capability_probe_http.py::test_priced_but_unregistered_model_reports_the_registry_blocker_and_real_price` (stages the real `minimax-m2.7`, asserts `no_probe_protocol`, real tariff rendered, no "trustworthy price", no confirmation, zero upstream requests) and `::test_registry_coverage_is_reported_on_the_evidence_read`; unit level `tests/test_capability_probe.py::test_unregistered_target_reports_the_registry_blocker_and_still_renders_its_price` (frozen clock), `::test_unregistered_target_without_any_price_says_no_published_price`, `::test_registry_coverage_reports_the_allowlist_size`, stale-tariff status test; `tests/console_dom_test.js` — "probePriceText renders a published tariff honestly…" (behavioural: the helper is called on the real minimax payload and must not return "no published price") and "the capability panel surfaces probe-registry coverage before any preview". RED on `554807e` (HTTP: `'probe_unavailable' != 'no_probe_protocol'` + `KeyError: 'probe_registry'`; unit: `registry_coverage` does not exist; DOM: exit 1), green on this candidate. Suite: `unittest` 1081 tests OK (3 skipped), `pytest` 1079 passed / 3 skipped / 708 subtests, `compileall`, `console_dom_test.js`, `test_opencode_catalog.mjs`, `git diff --check` clean; ruff line-agnostic diff vs `554807e`: 158 → 157 findings, 0 new / 1 removed (the dead `preview_for` import, F401). Paid-call surface unchanged: fail-closed gates, `estimate_accepted` only, ceiling > 0 and finite.

### R65 — Two open discovery panels put two paid-run confirmations on screen, and the top one names the wrong target — **closed**

**Severity: High (money-safety honesty hazard).** The **Discover capabilities** handler toggled only its own `tr.capability-detail-row` and never closed another panel, so opening row A's panel and then row B's (no reload) left **both** panels open: two `.capability-authorisation` paragraphs, and the first in document order — the one a reader sees — still named A while the panel the owner was operating was B's. Reproduced on production `554807e` (container `5c226f262695`) with `chatgpt_codex / codex-auto-review` opened first and `opencode_go / mimo-v2.6-flash` second: the evidence GET correctly targeted B, but the visible confirmation read "sent to the configured OpenCode Go account for chatgpt_codex / codex-auto-review". Naming a different provider/model than the run that would actually be billed is exactly the honesty failure the probe feature's estimate/ceiling/target consistency is built to prevent. Each panel's own paragraph was rendered correctly; the defect was panel exclusivity, not a stale capture.

**Fix.** `renderModels()` now tracks the single open panel (`openCapabilityPanel = { row, button }`, per render pass): opening a panel removes any other open detail row, resets its button's `aria-expanded` to `false`, and records itself; closing clears the tracker. The invariant is at most one discovery panel on screen, so the paid-run confirmation always describes the row whose panel is open. Gate behaviour is untouched: authorisation stays page-local (0 requests), the ceiling stays numeric `> 0`, and `estimate_accepted` remains the only accepted policy.

**Regression evidence.** `tests/console_dom_test.js` — new behavioural check "only one capability panel may be open, so a paid-run confirmation always names the row that was opened": drives the real row click handlers through an extended stub DOM (parentNode/nextElementSibling/classList/insertBefore/remove) over a two-provider fixture — row A open names A; row B open leaves exactly one panel and one authorisation block naming B with no A text, and toggles `aria-expanded` false/true; toggling closes; reopening A names A again. RED on `554807e` (fails at "opening a second panel closes the first"), green here. Real-browser acceptance against a seeded gateway (loopback upstream, zero external provider traffic): row A then row B without reload → one detail row, confirmation names `opencode_go / mimo-v2.6-flash` both times opened, `codex-auto-review` absent; full authorise → preview (`available`, 0.00003710000000000001 USD sourced from `https://opencode.ai/data/xiaomi/mimo-v2-6-flash`) → run (owner gate + single-use confirmation) → `text_generation: observed_supported` evidence with a pending proposal listed, upstream request counter exactly 1; post-refresh the fresh panel is authorisation-gated again.

### R66 — A provider's non-finite `usage.cost` produced an invalid-JSON run body, and a `NaN` cost was counted as usable accounting — **closed**

**Severity: Low-Medium (F-1) / Low (F-2).** Found by the security review of the integrated candidate `ea925fe` (card t_7571c0c2); **both defects pre-existed on `origin/main` since `d54ddd6`** and were reproduced identically on production — regressions of nothing, escapes of the provider-value boundary. (F-1) `_reported_cost` (`src/gateway/capability_probe.py`) accepted `+inf` because `cost >= 0` is `True` for it; the run accumulated it into `outcome.reported_cost_usd`, `handle_capability_run` (`src/gateway/http/app.py`) emitted it verbatim as `reported_cost_usd`, and `json_response` (`src/gateway/http/core.py`, `allow_nan=True` default) serialised it as the non-RFC-8259 token `Infinity`. Reproduced live: a loopback upstream returning `usage.cost = 1e400` produced an HTTP 200 body containing `"reported_cost_usd":Infinity` — the console's `response.json()` throws *after a paid run executed and persisted*, so the owner sees a generic failure and a re-run means duplicate spend; API clients get invalid JSON on a 200. The evidence store, capability read and log lines were verified clean (response-body-only escape). (F-2) `cost_accounting_ok` tested only `cost < 0`, which is `False` for `NaN`, so a `NaN` provider cost returned `True`: the run recorded and persisted `observed_supported` evidence despite unusable cost accounting, where the scope's rule requires `inconclusive` (reproduced live and as a unit repro).

**Fix.** A single value-boundary helper, `_usable_cost()`, now sits between the provider's `usage.cost` and everything downstream: it returns the cost as a float only when it is a non-negative, `math.isfinite` real number, and `None` for `NaN`, `±inf`, bools, non-numerics and integers too large to represent as a float (which previously counted as "accounted" and crashed `_reported_cost` with `OverflowError`). Both `cost_accounting_ok` and `_reported_cost` route through it, so an unusable cost now takes exactly the missing-cost path: one request, `inconclusive` result, `blocked_reason: cost_accounting`, `reported_cost_usd: null`. Every previously accepted/rejected case is byte-compatible (`-0.0` still accepted; negatives, bools, strings, absent still rejected). `json_response` was deliberately **not** changed: passing `allow_nan=False` turns handler-side non-finite values into 500s (QA-established), so the sanitisation lives at the value boundary, not the serialiser. **The closing claim this sentence carried — "after this fix no provider-supplied non-finite value can reach `json_response` on the run path" — was falsified** by the security review of `a827afe` (card `t_a3997124`): the *operands* were bounded, but `run_suite` accumulated them with no check on the running sum, so two individually accepted costs (`usage.cost = 1e308` twice) added to `+inf` and the 200 run body carried `"reported_cost_usd":Infinity` after the paid run had persisted. The residual escape and its fix are **R67**. Cosmetic while in the file: the `build_preview` docstring's stitched fragment (merge `57644d8`) is merged back into one sentence.

**Regression evidence.** `tests/test_capability_probe_http.py::test_an_overflowing_provider_cost_never_reaches_the_run_body` (loopback upstream with `usage.cost = 1e400` → 200, raw body asserted strict-JSON via `json.loads(..., parse_constant=...)` raising, `Infinity` absent, `reported_cost_usd` `null`, result `inconclusive`) and `::test_a_nan_provider_cost_yields_inconclusive_results` (bare `NaN` upstream cost → strict-JSON body, `inconclusive`, never `observed_supported`); unit level `tests/test_capability_probe.py::test_non_finite_cost_accounting_is_inconclusive_not_a_claim` (`NaN`, `±inf`, `1e400` → one request, `cost_accounting`, no reported cost) and `::test_cost_accounting_and_reported_cost_draw_the_finite_line` (accepted/rejected value table incl. `10**400` as `None`, not `OverflowError`). RED on `d81700b` (9 assertion failures incl. `['observed_supported'] != ['inconclusive']`), green here. Full gates, run by me on this tree: `unittest` 1089 tests OK (3 skipped; base `d81700b` 1085 → +4 = the new tests), `pytest` 1087 passed / 3 skipped / 747 subtests, `compileall`, `console_dom_test.js`, `test_opencode_catalog.mjs`, `git diff --check` all clean; ruff `src tests` 197 → 197 findings, 0 new. Security probes re-run from t_7571c0c2: `sec_http.py` **147/147 checks pass** — all five `RESIDUAL` checks (overflow-cost run body strict JSON, run serialisation strict, NaN-cost inconclusive, `cost_accounting_ok` rejects NaN, `_reported_cost` rejects inf); gate matrix `sec_matrix.py`: **37 scenarios, 0 mismatches** between this tree and `origin/main` `d81700b` (the two refusal-code renames are already in main, so no mapping was needed; the matrix gained 2 scenarios since the 35 counted on the ea925fe review). `sec_static.py` 16/17: the four cost residual checks pass; the remaining failure, `json_response of an inf cost is strict JSON`, calls `json_response` directly with a raw `inf` payload — that check can only pass by changing the serialiser, which this fix deliberately does not do (see Fix); the provider-cost path it guards is covered by `sec_http.py`'s strict-JSON checks and is green. If serialiser-level defence in depth is wanted, it is a separate card.

### R67 — The reported-cost *accumulator* re-opened the F-1 escape: two accepted costs summed to `Infinity` in the 200 body of a paid run — **closed**

**Severity: Medium (pre-existing on `origin/main` `d81700b`, which is what production runs; found by the security review of candidate `a827afe`, card `t_a3997124`, blocking finding SEC-R66-A).** R66 bounded each provider `usage.cost` with `_usable_cost()`, but `run_suite` (`src/gateway/capability_probe.py`) accumulated the accepted values with no check on the running total: `outcome.reported_cost_usd = (outcome.reported_cost_usd or 0.0) + reported`. Every operand of `1e308` twice passes the value boundary (finite, non-negative, not a bool), so both were counted as accounted money, the second request was sent, the sum became `+inf`, and `handle_capability_run` (`src/gateway/http/app.py`) emitted it as `reported_cost_usd` — `json_response` (still `allow_nan=True` by decision) wrote the non-RFC-8259 token `Infinity` into an **HTTP 200** whose run had already executed and persisted. Identical impact to R66 F-1: the console's `response.json()` throws after the money is spent and recorded, the owner sees a generic failure, a re-run means duplicate spend, and API clients get unparseable JSON on a success status. QA passed `a827afe` (`t_b6314f61`) because it exercised the value boundary, not the accumulator; the escape is response-body-only (`capability_discovery.json` holds no cost total at all — `reported_cost_usd` has exactly one producer, `run_suite`, and one consumer, the run body).

**Fix.** The *total* now has the same finite-float line the operands got. `_accumulate_cost(total, added)` adds and then re-checks: a non-finite sum returns `None` instead of a value. `run_suite` treats that as unusable cost accounting **at the run level** — the total is dropped (`reported_cost_usd` → `null`), every claim the run had already made is withdrawn (`_downgrade_claimed_results`: each claimed row becomes `inconclusive` with a gateway-authored reason; probes never sent keep their honest `not_tested`), the runner stops before the next request, and `blocked_reason` is `cost_accounting`. The body stays a strict-JSON **200** — never `Infinity`, never `0.0`, never a 5xx a client could read as a retryable upstream error. `json_response` is again deliberately untouched. Every representable sum keeps the exact pre-fix arithmetic (`sum` of the same values, bit-for-bit), so there is no drift for genuine costs. Other accumulators on this path were checked rather than assumed: `request_count` is an int incremented once per planned request (plan-bounded, `max_requests = 2`, and `_bounded_count` clamps 0–1000 on persist); the preview/estimate sums (`request_estimate`, `build_preview`'s `total_usd = sum(...)`) multiply protocol-bounded token/byte counts by rates that must be finite, and even a maximum finite rate (`1.7976931348623157e308`) yields finite terms at this protocol's bounds — measured `4.19e304` (text) and `2.76e305` (visual), two terms, sum finite; durations are not summed anywhere in the run (the body carries ISO timestamps only); per-response token counts are metadata, not accumulators, and are bounded by SEC-R66-C (card `t_b60a4d20`). No second accumulator needed a bound in this path.

**Regression evidence.** Six new tests (16 subtests), written from the finding, each RED on **both** trees the card names and green here. Unit (`tests/test_capability_probe.py`): `RunnerBoundsTests.test_accumulate_cost_bounds_the_running_total` (value table for the sum: `None`-seeded and representable sums kept exactly; `1e308+1e308`, `9e307+9e307`, `9e307+8.98e307`, `-1e308+-1e308`, `inf` operand, `NaN` operand → `None`), `::test_an_unrepresentable_cost_total_fails_the_run_closed` (`1e308` twice, `9e307` twice, and a 20-request plan of `1e307` that overflows *mid-run* — the earlier `observed_supported` claim is withdrawn, `reported_cost_usd` is `None`, `blocked_reason: cost_accounting`, the run stops at the overflowing add, and `json.dumps(..., allow_nan=False)` yields `{"reported_cost_usd": null}`), `::test_a_finite_cost_total_is_reported_exactly_as_before` (control). HTTP (`tests/test_capability_probe_http.py`): `::test_an_unrepresentable_cost_total_never_reaches_the_run_body` (loopback upstream, both responses `usage.cost = 1e308` → **200**, raw bytes proven parseable by **`node -e JSON.parse`** on the exact response file — the console's parser, not `json.loads` — with `reported_cost_usd` `null`, `blocked_reason: cost_accounting`, `request_count: 2`, all statuses `inconclusive`; `b"Infinity" absent from the raw body`), `::test_persisted_evidence_after_an_overflowing_run_stays_clean` (stored `capability_discovery.json` strict-parses, `_non_finite_paths` walks it to zero non-finite floats, run `request_count: 2` with all rows `inconclusive`, proposal `pending` with `fields: []`), `::test_a_genuine_cost_total_is_reported_exactly` (control: two `0.000002` responses → `0.000004` under node's parser, `blocked_reason` null, three `observed_supported`).

RED proof, run by me on separate worktrees of the same two trees the card names, candidate tests + that tree's `src`: **on `a827afe`** 5 FAIL + 1 ERROR (`_accumulate_cost` ImportError) of the 6 — with the raw 200 body verbatim `..."blocked_reason":null,"cancelled":false,"reported_cost_usd":Infinity,...` and the unit repro `AssertionError: inf is not None`; the 20-value case sent all 20 requests with no stop. **On `d81700b` (production)** the identical 5 FAIL + 1 ERROR. Both control tests pass on both trees — the no-drift proof is the base tree passing its own arithmetic unchanged.

Full gates, run by me on this tree: `unittest` **1095 tests, 1 failure, 3 skipped**; `pytest` **1 failed, 1092 passed, 3 skipped, 763 subtests passed**. The single failure is `tests/test_end_to_end.py::ApiSurfaceTests::test_over_budget_refuses_before_provider_call_with_actionable_details` (`200 != 429`), **pre-existing on base `d81700b`** — reproduced there by me with the identical symptom on both runners (`d81700b` pytest: 1 failed / 1082 passed / 3 skipped / 723 subtests) — it is the month-window wall-clock fixture already fixed on unmerged card `t_c7595dd5`, untouched by this diff. `node tests/console_dom_test.js` all checks passed; `node tests/test_opencode_catalog.mjs` passed; `compileall`, `python3 -m gateway --help`, `git diff --check` clean. `ruff check src tests --no-cache`: **197 → 197 findings, 0 new** (line-agnostic diff of (file, rule, message) against a clean `d81700b` archive). Security's own probes re-run from `t_a3997124` against this tree: `sec_probe_http.py` **40/40 PASS** (their H4 residual check — the finding — now passes), `sec_probe_unit.py` **140 PASS / 1 FAIL / 141**: the one FAIL is their `D1 accumulation of accepted costs stays finite`, which asserts `reported_cost_usd is not None and math.isfinite(...)` — a *saturating-total* expectation written before this card existed. This card mandates the opposite (fail-closed `null`, detail printed by their own check: `reported_cost_usd=None`), so D1 must be re-pointed at the card's spec at re-review; D2/D3 and every other check pass.

### R66-A — SEC-R66-A: two accepted provider costs summed to `Infinity` in the 200 body of a paid run — **closed**

**Severity: Medium (pre-existing on `origin/main` `d81700b`, which production runs; raised as blocking finding SEC-R66-A by the security review of candidate `a827afe`, card `t_a3997124`).** R66 bounded each provider `usage.cost` with `_usable_cost()`, but `run_suite` accumulated the accepted values with no check on the running total: `usage.cost = 1e308` twice summed to `+inf`, and the **200** body of a paid run that had already executed and persisted carried `"reported_cost_usd":Infinity` — a non-RFC-8259 token the console's `response.json()` throws on (owner sees a generic failure, a re-run means duplicate spend) and API clients cannot parse. QA passed `a827afe` (`t_b6314f61`) because it exercised the value boundary, not the accumulator.

**Fix.** `_accumulate_cost(total, added)` (`src/gateway/capability_probe.py`) adds and then re-checks the *running* sum: a non-finite result returns `None`. `run_suite` fails closed at the **run** level on that — `reported_cost_usd` → `null`, every claim the run already made withdrawn to `inconclusive` via `_downgrade_claimed_results` (probes never sent keep their honest `not_tested`), stop before the next request, `blocked_reason: cost_accounting`, strict-JSON **200** — never `Infinity`, never `0.0`, never a 5xx a client could read as a retryable upstream error. `json_response` deliberately untouched (`allow_nan=True` by decision; `allow_nan=False` turns handler-side non-finite values into 500s). Representable sums keep the exact pre-fix arithmetic, so genuine costs do not drift. This is the same fix written up in full as **R67 directly above** — one commit (`7d25397`, integrated at `b260260`); this entry exists so the finding ID QA's merge-resolution checklist requires (`t_6142e3b2`: "REVIEW.md must carry R66 and R66-A and R66-C") is itself traceable in this register.

**Regression evidence.** The six tests (16 subtests) named under R67: unit value table for the sum including `1e308+1e308`, `-1e308+-1e308`, mid-run overflow of a 20-value plan, and finite controls; HTTP raw-body proof under `node JSON.parse`; persisted-evidence walk after an overflowing run. Independent re-reviews of the integrated candidate `d12d980`, their numbers reported in their own cards: QA `t_6142e3b2` ran an accumulator probe **32/32 PASS** on the candidate, **RED on both `d81700b` and `a827afe`**; security `t_8d1dc7fe` confirmed live that an overflowing total returns **200**, strict JSON, `reported_cost_usd: null`, `blocked_reason: cost_accounting`, no capability claim, persisted evidence clean. The residual unit-probe assertion of theirs that still fails — `D1`, expecting an overflowed total to remain finite/non-null — is a stale pre-fix expectation in their own harness (see R66-C for the harness locations) and must be re-pointed at this spec at re-review.

### R66-C — SEC-R66-C: provider token counters escaped every bound (401-digit counts 500ed owner state and read as `Infinity` in JS), and a refused count left no record — **closed**

**Severity: Medium (defect pre-existing on `origin/main` `d81700b`; card `t_b60a4d20`, integrated as `ca2bbf1`; the information-loss residue below was found by security card `t_8d1dc7fe` on candidate `d12d980`).** A provider response is valid JSON carrying a 401-digit `prompt_tokens`; every counter check was `isinstance(int) and >= 0`, so such a number flowed through `response_token_counters` / `normalise_usage` into ledger rows — `calculate_cost` raised `OverflowError` and the console's `GET /owner/api/state` 500ed for as long as the row stayed in the window — and into response bodies where the console's `JSON.parse` reads it as `Infinity` (a display lie on a success status).

**Fix.** One bound at every seam: `usable_token_count` (`src/gateway/bridge/streams.py`, `MAX_TOKEN_COUNTER = 10**9`; refuses bools, negatives, non-integers, over-bound ints) in response/stream counter parsing, the ledger write seam (`RequestLedger.amend`), rollup, statistics, persistence read, and the probe run body (`_usage_tokens`). A *reported* refusal is recorded, never erased: the ledger keeps the boolean `invalid_usage` marker beside the counters that survived, and the cost path answers *invalid* rather than *no usage* and never prices a refused counter as zero. The integrated candidate `d12d980` still had one lossy spot — security `t_8d1dc7fe`, finding "unintended information loss for huge reported token counters": the capability-probe run path dropped a refused count to exactly the `null`s absent usage produces, so neither the run body nor the persisted evidence could tell "the provider gave a number this gateway will not carry" from "the provider reported nothing". **Remediated on this card (the reconciliation security asked for, option "persist a bounded refusal marker"):** `_usage_tokens` now emits `invalid_usage: true` beside the nulls whenever `reported_counters_unusable()` reports a number the bound refuses — a bool, so it can never serialise into a number the console reads as `Infinity` — and `capability_discovery._usage_metadata` persists that marker (only when it is exactly `True`; a tampered non-bool value is dropped) while absent or non-numeric usage still produces the plain nulls and no `reported_usage` key, byte-identical to before. Cost accounting was unaffected throughout: a refused counter never downgrades a run, because `cost_accounting_ok` reads `usage.cost`, not token counts.

**Regression evidence.** `tests/test_token_counter_bounds.py` (bounds at each seam, refusal recorded not dropped, owner state stays 200 over pre-bound `10**400` rows, run-body bound) plus this card's behavior-driven additions, each written from the security finding: `ProbeRunBodyBoundTests.test_a_refused_run_body_counter_is_recorded_not_erased` (401-digit, `inf`, float, bool and `total_tokens`-only refusals → marker, no number above the bound), `::test_absent_or_non_numeric_usage_reports_nulls_without_a_refusal` (absence/non-numeric is not a refusal — no marker, control green on the pre-fix tree), `DiscoveryBackendTests.test_reported_usage_refusal_is_persisted_and_absent_usage_is_not` (marker in the stored file; absent usage keeps no `reported_usage` key; tampered `"true"` string dropped), and `ProbeHttpTests.test_a_refused_huge_token_counter_is_recorded_not_carried` (401-digit `prompt_tokens` upstream → **200**, bytes survive `node JSON.parse`, the digit run absent from body and store, `invalid_usage: true` beside `input_tokens: null` in both, `Infinity` absent, claims stand because cost accounting was fine). RED on `d12d980` (the rejected candidate): 2 failures + 6 subtest errors across the three marker tests, control green; green on this tree. Security's three probe assertions that contradict this spec — `H9` (expects the 400-digit count to reach the body), `D1` (expects an overflowed total to stay finite/non-null), `E1` (expects an unbounded integer counter to be carried) — live in reviewer-owned harnesses outside this repo (`sec_probe_http.py`, `sec_probe_unit.py` under a security card's workspace, not repository files) and must be re-pointed at this register at re-review, exactly as `D1` was after R67; the in-repo tests above are the authoritative expectations. Prior independent numbers, reported in their own cards: QA `t_6142e3b2` R66-C state-contract probe **10/10** (huge-only and finite mixed-state), security `t_8d1dc7fe` live probe confirmed non-finite counters do not escape the run body.

### R68 — The release gate asserted the superseded no-session boundary (2 of its 18 checks), and the console banner claimed any reachable device could administer — **closed**

**Severity: Medium (release process — every release failed the gate on false positives — plus one line of overstated UI copy).** `scripts/release_gate.py` still encoded the boundary `53918c1` deliberately replaced. Check 1 GET `/owner/api/state` with `Origin: http://evil.example.com` and required **200** ("the console exposes state directly (safe GET ignores Origin)", `release_gate.py:479-487`); check 2 POST `/owner/api/model` same-origin with **no session** and required a status outside {401, 403} ("same-origin owner mutation is dispatched without session or CSRF", `release_gate.py:496-506`). Against the owner-session lineage both answers are **401** — the documented contract (the owner-session boundary entry below: every other `/owner/*` route answers `401 authentication_required` before its handler runs) and the answer QA's independent probe treats as PASS. `git diff 2f32436..e0cb9b1 -- scripts/` is empty: the script was never updated for the session boundary, so the gate — observed on devbox against the deployed image `sha256:64b94accabaf9b1bfb5c026d264d3c8bec14e4680bfe0e91644ea8250f65b348`, log `/tmp/t815_gate.log`: 16/18, these two failing — could never pass on any tree in this lineage. Same root cause, one line of copy: `src/gateway/static/console.html:11` still said "any device that can reach this address can **administer** and use the gateway", served verbatim by the deployed image, although administering requires an owner sign-in (QA's D1 sweep scanned `.md` files only, so the copy was never registered).

**Fix.** The gate now asserts the boundary in both directions (**18 → 25 checks**), assertions only — no `src/gateway` behavior changed:

* **Without a session** (`record_pre_auth_checks`): `GET /owner/api/state` → **401**, bare and with a hostile `Origin` (the old safe-GET angle, now refused at the session gate); `POST /owner/api/model` same-origin → **401**; cross-origin mutation → **403** (the origin layer answers first). The two superseded expectations are gone.
* **With a session** (`OWNER_SESSION_PROBE_SOURCE`, executed inside the candidate container as `docker exec <c> python -c`, the source fed from **this checkout** so the artifact under test cannot substitute its own answers): first-run setup → 200 — the one-time setup code is read from the gateway's own store (the `docs/RUNBOOK.md` §1.4 operator path) and the password is generated at runtime, so no credential is hard-coded and only integer status codes cross back; state → 200 including with a hostile `Origin`; same-origin mutation dispatched (not 0/401/403); cross-origin mutation → 403. A probe that prints nothing, raises, or returns a non-object fails every signed-in check (fail closed, with a secret-free note on the first check).
* `console.html:11`: the banner now reads "…any device that can reach this address can use the gateway, and administering it requires an owner sign-in." — the three substrings `tests/test_http_limits.py` pins are preserved verbatim; no JavaScript was touched (file-ownership constraint from `t_256ba47e`).

**Regression evidence.** `tests/test_release_gate.py` — **48 tests (21 new)**: the documented refusals pass and **the superseded answers now fail** (`test_the_superseded_no_session_answers_fail`), only 401 counts as the session refusal, a gateway that answers nothing fails every check, the probe's parsing/fail-closed paths (non-object report rejected, non-integer values dropped, a raising runner reported not raised), and the exact probe source executed the way the container runs it (`python -c` + env) against a live gateway with a pending setup code, plus live pre-auth 401/403 assertions. Gates run by me on this tree: `PYTHONPATH=src python3 -m unittest discover -s tests` → **1291 tests, OK (3 skipped)**; `python3 -m pytest tests/ -q` → **1289 passed, 3 skipped, 979 subtests passed**; `node tests/console_dom_test.js`, `node tests/test_opencode_catalog.mjs`, `python3 -m compileall -q src tests`, `PYTHONPATH=src python3 -m gateway --help`, `git diff --check` all clean; `ruff check src tests` (ruff 0.17.2) → **232 findings on this tree and the same 232 on `e0cb9b1`, identical** (0 new, 0 gone; line-agnostic (file, rule, message) multiset diff). **End-to-end on devbox** (isolated clone, disposable container on `127.0.0.1:18775`, production stack untouched): `python3 scripts/release_gate.py personal-llm-gateway:gate-t_da15acaf` against an image built from this branch's `b542380` (tree `5b6dfeec965afbdfb5c12da161bfa9a0f21330f7`, image `sha256:a84785a77d7ac8f62e4d3af5a4023f6cfabcf3a5c18dbcfb5c03da2eacc5002b`) → **RELEASE GATE PASSED, 25/25 checks**, all nine boundary checks among them (401/401/401/403 without a session; setup 200, state 200, hostile-Origin safe GET 200, mutation dispatched 404, cross-origin 403 with one). That run's `scripts/`, `src/` and `tests/` are byte-identical to this commit — this commit touches only `docs/REVIEW.md`, which is not copied into the image. Because this card moves the SHA, QA and security delta gates must both be re-run on the exact new SHA/tree before it can serve as the release gate for the next deploy (card `t_da15acaf`).

### R69 — The third credential (machine keys) was undocumented, and `docs/OWNER_AUTH.md` carried two claims its own code had falsified — **closed**

**Severity: Medium (documentation assurance — `AGENTS.md:152` requires README, runbook, architecture/contract docs and this register to describe every security-boundary change, and F2 of the security gate `t_c1ef6394` measured the gap on RC `e6897c1f`).** The gate's sweep — `grep -rniE "machine key|machine-key|gwmk|MCP|automation credential"` over `README.md` + `docs/*.md` — returned **zero hits** while eight documents describe the credential model, and `tests/test_machine_keys.py:13` cited `gateway-mcp-spec.md`, a file that was not in this repository. Two claims in `docs/OWNER_AUTH.md` were also false against the code they document: line 3 said "Two boundaries, deliberately separate." (the `src/gateway/owner_auth.py` module docstring says three) and its CSRF bullet claimed the canonical `Origin` check runs "before authentication — on every browser mutation" (`check_origin` returns early for any request presenting a bearer `Authorization`, so that sentence stopped being true for machine requests).

**Fix (this change, decided by PM `t_e397267a`).** `docs/OWNER_AUTH.md`: three boundaries named (owner session, client `/v1` API key, machine key); the CSRF bullet rewritten to the as-built truth (`check_forwarded_headers`/`check_host` before everything, bearer included; exact `Origin` before authentication on cookie browser mutations; the bearer early-return cannot open a cookie-CSRF path because a cross-site page cannot set `Authorization` — not CORS-safelisted, no CORS served); and a new machine-key section: mint shown once, hash-only `0600` store + separate stat-keyed pepper, scopes, `MACHINE_KEY_READ_PATHS`/`MACHINE_KEY_WRITE_PATHS`/`MACHINE_KEY_BLOCKED_PROVIDER_ACTIONS` as built, `cookie_rejected`, a measured refusals table, and the F1 acceptance (R70). `README.md` ("Three credentials, deliberately separate" + machine-key bullet + reveal-once non-exposure line), `docs/ARCHITECTURE.md` (listener purpose cell, `MachineKeyStore` in the state map, new `### Machine-key automation` request-path map, step-5 CSRF clause), `docs/RUNBOOK.md` (§1.3 third bullet, §2 `### Machine keys (MCP / automation)` with mint/list/rotate/revoke and the pepper incident response, §6 troubleshooting rows), `docs/TRUSTED_LAN_ADMIN_MODE.md` (status sentence + third "what changed" bullet) and `docs/CLIENT_INTEGRATIONS.md` (a machine-keys-are-not-for-clients paragraph, plus two pre-existing false credential claims in that same document corrected in passing: "does not require or inspect a client API key" and "a reachable device can … administer the gateway"). `docs/TODO.md` and `docs/CHATGPT_CODEX_CONTRACT.md` were grepped and needed **no edit** — TODO's boundary bullets scope owner login + optional client key as in-scope decisions with the model API "off by default", and CONTRACT's credential references are all provider-side. The spec is vendored at `docs/GATEWAY_MCP_SPEC.md` with a provenance header (body byte-identical to the attachment, sha256 `2afa3c88425e8164…`), and `tests/test_machine_keys.py:13` now cites the vendored path — comment only, zero behaviour change.

**Evidence.** Gate grep re-run on this tree: `README.md` 4, `docs/ARCHITECTURE.md` 12, `docs/CLIENT_INTEGRATIONS.md` 1, `docs/OWNER_AUTH.md` 19, `docs/RUNBOOK.md` 14, `docs/TRUSTED_LAN_ADMIN_MODE.md` 3 hits, plus `docs/GATEWAY_MCP_SPEC.md` and this register — **zero** in `docs/TODO.md` and `docs/CHATGPT_CODEX_CONTRACT.md`; the stale-claim sweep (`Two access boundaries|Two boundaries|keeps the two boundaries|only two boundaries` over `README.md` + `docs/*.md`) → **NONE**. Every machine-key claim in those documents was written from code read on this tip (`app.py:370-401` allowlists, `app.py:_authorize_machine_key`, `owner_auth.py` store/pepper, `security.py:check_origin`, `app.py:_require_client_key`), and the response table in `docs/OWNER_AUTH.md` was measured rather than paraphrased (`MachineKeyHarness`, run by me this card). Because this card moves the SHA, QA (`t_932dede7`) and security (`t_d1a936fe`) re-gate the exact new tip.

**Correction (QA re-gate `t_932dede7` findings F2-A/F2-B/F2-C — fixed docs-only by `t_6e44bdd1`, which amends this entry rather than minting an R number).** The evidence sentence above is withdrawn as written: three claims that this very change introduced to `docs/` were false against the code on `7def7ed`, so not every machine-key claim in those documents was written from code read on that tip. (F2-A, Medium) `docs/OWNER_AUTH.md` said the mint is `gwmk_` + 64 hex; `owner_auth.py:909/947` mint `MACHINE_KEY_PREFIX + secrets.token_urlsafe(32)` — measured by me through the real create and rotate endpoints: suffix length **43**, base64url alphabet (an `_` occurred in the sample), not hex; reworded to "`gwmk_` + 43 base64url characters derived from 32 random bytes". (F2-B, Medium) `docs/RUNBOOK.md` said the list tab shows "created"; `MachineKeyStore.list()` returns exactly `display_id, name, scope, last_used_at` (docstring: "nothing else, ever"), the console table headers are `Key id / Name / Scope / Last used / Actions`, and `tests/console_dom_test.js` pins last-used only — same claim family: `docs/OWNER_AUTH.md`'s "timestamps" plural, since no later read returns `created_at` (it exists only in the create/rotate response); measured: store rows and `GET /owner/api/machine-keys` rows both = `['display_id', 'last_used_at', 'name', 'scope']`; `created` removed from the RUNBOOK claim and the plural corrected. (F2-C, Low) the CSRF bullet said `check_origin` enforces the exact `Origin` on a "browser-surface request"; `security.py:107` returns early for non-mutating methods — measured: signed-in `GET /owner/api/state` + hostile `Origin` → **200**, `POST /owner/api/model` + hostile `Origin` → **403 origin_mismatch** (canonical-Origin control reached the handler); the bullet now says browser-surface **mutation** and names the read early-return. Scope of this correction: `docs/OWNER_AUTH.md` (2 claims) + `docs/RUNBOOK.md` (1), probe `probe_f2.py` attached to `t_6e44bdd1`.

### R70 — F1: a write-scoped machine key's `clear_cooldowns` reach (including `scope: all`) was unconfirmable from the product — closed by decision + documentation

**Severity: Low (decision record + documentation).** F1 of `t_c1ef6394`: `gateway-owner-mcp` @ `1acc55b` ships `gateway_clear_cooldowns` with an explicit `scope` of `route|provider|all`, but no product document said whether the gateway's machine-key surface would honour `all` — a mass-clear of cooldown state across every provider — so the reach was neither asserted nor refuted anywhere an operator or reviewer could read it. PM decision `t_e397267a` records option (a): **deliberate acceptance** — a write-scoped machine key MAY run `clear_cooldowns` at scope `all`, `provider` or `route` and receives `200`. Rationale on record: the MCP server was built and shipped behind a dry-run and a single-use 120-second confirmation nonce (build card `t_0b1664c6`, QA `t_cf3205a9` PASS, security `t_ce0945dd` PASS), so refusing `all` would break a credential that has already been re-gated; the old 401 expectation was a pre-integration artifact (`t_e74eab5a`); and reach is bounded by the surface, not by scope — the write scope already reaches `POST /owner/api/settings`.

**Closed by documentation (this change).** `docs/OWNER_AUTH.md` now carries the acceptance with its rationale, the MCP-server provenance and a measured refusals table — every response below was produced by me on this tree with `tests/test_machine_keys.py:MachineKeyHarness`, not copied from the gate report: write key `clear_cooldowns` `scope=route` → `200 scope=route`, `scope=provider` → `200 scope=provider`, `scope=all` → `200 scope=all`; read key → `403 scope_required`; anonymous → `401 authentication_required` (canonical Origin) / `403 origin_required` (no Origin) / `403 origin_mismatch` (hostile Origin); session + hostile Origin → `403 origin_mismatch`; key + cookie → `403 cookie_rejected`; `scope` = `ALL` / `" all"` / `"all "` / `everything` → `400 invalid_scope`; **non-string `scope` → `400 invalid_request`** (the decision record's proposed gate wording groups the non-string case under `invalid_scope`; the handler answers `invalid_request` for a wrong-typed scope and `invalid_scope` only for a wrong-valued string — code truth recorded here so the round-2 re-gate does not raise a false mismatch); missing `scope` → `404 unknown_provider`; `enroll_key`/`device_start` and the machine-key/client-key endpoints → `403 machine_key_forbidden`; read key `GET /owner/api/state` → `200`. The vendored `docs/GATEWAY_MCP_SPEC.md` carries the provenance note that `route|provider|all` is sanctioned by `t_0b1664c6` beyond the spec's provider-only schema; `docs/RUNBOOK.md` §2 and `docs/ARCHITECTURE.md` point at the same acceptance.

### R71 — The capability preview rendered its result above the button in a collapsed div, so a working preview read as an inert control — **closed**

**Severity: Medium (the feature worked end to end and its only authorised user concluded it did nothing).** Owner report with a screenshot of the capability-discovery panel on `ac515eb`: *"the preview selected probes i dont feel works or does anything."* PM reproduced the feature working at all three levels: backend `POST /owner/api/capabilities/preview` answers 200 `available:true` with a price for `text_generation`, `image_input` and `function_tools` (and 400 `invalid_request` for a bad probe id — a gate working); deployed `/owner/app.js` byte-identical to `src/gateway/static/console.js`; `tests/console_dom_test.js` green. The defect was placement and state, not behaviour. In `renderCapabilityPanel` the `previewHost` div was appended **before** `previewButton`, so the result rendered *above* the button inside an initially-empty `div` of zero height, and the button never changed label or state. `Loading preview…` was replaced within ~1 ms on a LAN, so the whole cycle happened in a region the owner was not looking at and too fast to catch — exactly what "the button does nothing" describes. The button was also `disabled` with no stated reason (authorisation-gated, selection-gated), while the authorisation block beside it replaces its own content in place, which is why that half *felt* alive.

**Fix (console only — no endpoint, gate, payload, price or confirmation changed).** The result region now renders **after** the button (`previewButton` → reason line → `previewHost`), with `scrollIntoView({ block: "nearest" })` used only when the result would otherwise land below the fold. One function, `syncPreviewControl()`, owns the button's label, its `disabled` state and the reason line beneath it (`aria-describedby`-linked), so the three cannot drift: read-only session → "Read-only session…", nothing ticked → "Select at least one probe above…", no in-page authorisation → "Authorise the billing route above…"; the reason clears when the button unlocks and re-evaluates on every probe tick (`onchange`). In flight the button reads **Previewing…** and is disabled; the label returns on settle. Every outcome is now labelled where the owner is looking: `Preview available` (then **Run selected probes**), `Preview unavailable` (+ "Unknown is not false"), `Preview refused` (now `notice bad`, "No provider request was sent."), and a loading line that states no provider request is sent. `console.css` boxes `.capability-preview:not(:empty)` with a top border so a populated region can never lay out at zero height, and the empty region takes no space. Preview semantics are untouched: still zero provider requests, still behind the authorisation gate and the single-use confirmation, and the no-selection / no-authorisation guards (and their toasts) remain in the handler.

**Regression evidence.** `tests/console_dom_test.js` — two new checks (99 → 101): *"the preview output renders below its button, with feedback before the response arrives"* asserts the reason line at each gate state, the `aria-describedby` link, then observes the DOM **from inside `fetch()`** — i.e. after the click and before any response exists — for a `Loading preview…` region positioned below the button and a `Previewing…` disabled button, and after settle a later-sibling, non-empty result carrying "Preview available" and "Run selected probes", the restored label, and the CSS `:not(:empty)` rule; *"every preview outcome renders a distinct result below the button"* drives available (`available:true`), unavailable (`available:false`) and refused (400 without an `available` field) and requires each below the button, distinctly labelled, with the run button absent for unavailable and the refusal rendered `notice bad`. Both **RED on `ac515eb`** (the first fails on the missing reason line, the second on *"every outcome renders below the button"*), green here, and all 99 pre-existing checks still pass. Gates run by me on this tree: `node tests/console_dom_test.js` → all checks passed (101); `node tests/test_opencode_catalog.mjs` passed; `PYTHONPATH=src python3 -m unittest discover -s tests` → **1342 tests, OK (3 skipped)**; `python3 -m compileall -q src tests` and `git diff --check` clean. Independent QA of the rendered click (reproduce the click and capture the rendered DOM text the owner sees) is requested on the child card, per the task's acceptance criterion 7.

### R72 — The Ollama adapter never mapped the documented `tools` capability, so every tools-bearing request to an alias with no fallback died `503 no_eligible_route` — **closed**

**Severity: High (an enabled, tool-capable local model was unusable for every agent request while the docs promised the opposite).** `README.md` ("tools and image input start off (moving only on your explicit approval, or on Ollama's own documented `POST /api/show` output)") and the same clause in `docs/ARCHITECTURE.md` make `/api/show` an evidence class for tools; `_enrich_from_show` read only `vision` and `model_info`, and its comment asserted "Ollama publishes no capability value for tool use" — falsified live on 2026-10-31: `POST /api/show` for `qwen3.8:27b` on the owner's server returns `capabilities: ["completion", "vision", "tools", "thinking"]`, the documented schema (`docs.ollama.com/api-reference/show-model-details`, `capabilities` = "List of supported features", read 2026-10-31), and the same model answers the OpenAI surface with a valid `tool_call`. With the row stuck at `supports_function_tools: false`, the router excluded the route for any request carrying tools and, the alias having no fallbacks, answered HTTP 503 `no_eligible_route` for alias `local`.

**Fix.** `_enrich_from_show` now maps `"tools"` exactly as it maps `"vision"`: same evidence class, same explicit-refresh-only scope, owner-approval path untouched, fail-closed when the value is absent.

**Regression evidence.** `tests/local_fixture.py` `DEFAULT_SHOW` now mirrors a real tool-capable Ollama payload (adds `"tools"`); `test_show_evidence_sets_image_input_and_context_window` asserts tools true; new `test_show_without_tools_leaves_function_tools_false` keeps the fail-closed half honest (a capabilities list without `tools` never gains it). Gates run by me on this tree (node v22.20.0 installed user-space on devbox for the node-based JSON probes): `PYTHONPATH=src python3 -m unittest discover -s tests` → **1434 tests, OK (5 skipped)**; `python3 -m compileall -q src tests` clean.

## Owner-session boundary — the security review's SEC-1 (documentation assurance, card `t_e4577180`) — closed

*(Not to be confused with the earlier `### SEC-1` NaN-ceiling entry above; the security review of each candidate restarts its own SEC numbering.)*

**Severity: Medium (assurance).** The security review of the owner-login candidate `154971e3a03386153a03aa238471b91ce849d0f7` (tree `ea11e7151de36fc2d44b5033ae3294869077e1ee`, branch `llm-gateway/t_ab975ac3`) passed the implementation — its 117 independent adversarial checks against isolated fixtures went 117/117 — and returned **CHANGES_REQUIRED** on one documentation item: `docs/RUNBOOK.md`, `docs/ARCHITECTURE.md` and `docs/REVIEW.md` still asserted the superseded no-login boundary ("any device that can reach this address can administer the gateway", "owner state is readable without a login", "no owner session and no CSRF bootstrap", the "login/bootstrap is not part of this product mode" troubleshooting row), R0 above was left without a Resolution although this candidate implements its required fix, and `src/gateway/owner_auth.py:561` pointed at a runbook recovery path that did not exist. `AGENTS.md` requires the README, runbook, architecture/contract docs and this register to change whenever a security boundary does. This entry records the boundary those documents now describe. **Documentation only — no code was touched**, per the constraint that the reviewed implementation must not be "improved" while the docs are reconciled.

**The boundary, as now documented.**

* **Owner console and configuration API (`/owner/*`).** The pre-auth surface is exactly the console shell and its assets, `GET /owner/api/auth/session`, `POST /owner/api/auth/setup` and `POST /owner/api/auth/login`. Every other `/owner/*` route requires the `gw_owner_session` cookie (`HttpOnly`, `SameSite=Strict`, `Path=/`, 12-hour TTL, validated server-side) and answers `401 authentication_required` before its handler runs — always, except while the owner has set `owner_console_auth=false`, which serves them without a session (see the `owner_console_auth` register entry below). First run has no account and no default credential: a one-time setup code — printed to server stdout and kept in the `0600` `owner_setup_code` file, never an HTTP response — is consumed exactly once by setup. Sign-in, sign-out, password change and `--reset-owner-password` recovery (stdin only, revokes every session, picked up by a running gateway without a restart) are documented in `docs/RUNBOOK.md` §1.4, and the troubleshooting table now carries the real first-boot symptom, the `401`, and the throttle row.
* **Model API (`/v1/*`).** Stays **keyless** until the owner sets a client key from Settings → Access; `/version`, `/health` and `/metrics` are unauthenticated either way. Until a key is set, any reachable peer is a caller — including a cross-origin browser POST, because the `Origin` check covers `/owner/` only.
* **Transport caveat.** The listener is plain HTTP: the owner password, the session cookie and the client key cross the LAN in clear text and the cookie is not `Secure` unless the deployment says TLS. "Trusted LAN" therefore also means "no on-path observer".
* **Deployed vs candidate.** The running gateway still serves the no-login boundary, where a reachable peer administers `/owner/*`; the owner session ships only with this candidate, and the release gate needs QA PASS and security PASS on the same exact SHA/tree before deployment.

**Non-blocking observations recorded here, deliberately not fixed in this change:**

* The one-time setup code never expires (`src/gateway/owner_auth.py:362-366` stores `created_at`; nothing reads it).
* The client key is owner-supplied with no entropy floor (`src/gateway/owner_auth.py:152-166`, `validate_client_key` checks only length and printable ASCII) and is stored as an unsalted SHA-256 via `hash_caller_token` (`src/gateway/credentials.py:157-163`); a weak 32-character key is offline-dictionary-attackable if `owner_auth.json` leaks. The console generates a strong key (`crypto.getRandomValues`) and offers a *Generate* button.
* `request.context["owner_session"]` holds the raw session token (`src/gateway/http/app.py:733`); nothing serialises `request.context` today.
* The login throttle is in-memory and cleared by a restart (`owner_auth.py:176-184`) — now stated in `docs/RUNBOOK.md` §1.4 and in the troubleshooting table.
* `describe_store()` (`owner_auth.py:686-692`) is dead code reaching into `store._data`.

**Evidence for this change** (run by me on the tree of this commit; `git diff` touches only `docs/ARCHITECTURE.md`, `docs/REVIEW.md`, `docs/RUNBOOK.md`, so no Python or JavaScript line changed): `PYTHONPATH=src python3 -m unittest discover -s tests` → **1163 tests, OK (3 skipped)**; `PYTHONPATH=src python3 -m pytest tests/ -q` → **1161 passed, 3 skipped, 811 subtests passed**; `python3 -m compileall -q src tests`, `PYTHONPATH=src python3 -m gateway --help`, `node tests/console_dom_test.js`, `node tests/test_opencode_catalog.mjs` and `git diff --check` all clean. Because a fix-forward changes the SHA, the QA gate (`t_e6ac9132`) and the security gate (`t_e4577180`) must both be re-run on the new exact SHA/tree before any deployment.

## Local-egress boundary — the security review's D1 (documentation assurance, card `t_6f7b740f`) — closed

*(Not to be confused with the `### SEC-1` NaN-ceiling entry above or the owner-session boundary entry above it; each security review restarts its own numbering, so this is D1 of the `fd682bfa` review.)*

**Severity: Medium (assurance).** The security review of the local-provider candidate `fd682bfa64acfbbf382ed71d436992f07cbf8c36` (tree `a5a9729508f7eeb69ad1936c4189b687b7319bb6`, branch `llm-gateway/t_ccd7f6a5`, base `53918c18`, origin/main `2f32436` ancestors) passed the implementation — its independent SSRF/DNS-rebinding/injection/credential probes went **170/170** (save-time 84, connect-time 42, app surfaces 44) — and returned **CHANGES_REQUIRED** on one documentation item: this candidate added a **new outbound egress class** (an owner-configured destination the gateway dials at runtime) plus three new providers and new owner UI, and only `docs/PROVIDER_CONTRACTS.md` had been updated. `README.md` still said "Two providers are in scope" with none of the three local rows, `docs/ARCHITECTURE.md`'s *Provider seam* had no `LocalOpenAIAdapter` bullet and no statement of the egress boundary, `docs/RUNBOOK.md` carried no row for any of the new fixed refusal messages and no local-endpoint procedure, and this register had no entry for the boundary. `AGENTS.md` requires the README, runbook, architecture/contract docs and this register to change whenever a security boundary does. This entry records the boundary those documents now describe. **Documentation only — no code was touched**, per the constraint that the reviewed implementation must not be altered while the docs are reconciled.

**The boundary, as now documented.**

* **A new outbound destination class.** The three local providers — fixed ids `ollama`, `llama_cpp`, `lm_studio`, one shared `LocalOpenAIAdapter` (`src/gateway/adapters/local_openai.py`) — are the only place the owner types a URL the gateway will connect to. Allowed ranges (`src/gateway/adapters/local_policy.py`, `PRIVATE_NETWORKS`): **`127.0.0.0/8`, `10/8`, `172.16/12`, `192.168/16`, `fc00::/7`**. Explicitly outside them: public addresses, link-local `169.254/16` and `fe80::/10` (the cloud-metadata class, including `169.254.169.254`), multicast, the unspecified addresses and the broadcast address; IPv4-mapped IPv6 literals are unwrapped and judged as IPv4. A hostname is accepted only if it resolves to *nothing but* allowlisted addresses — a mixed public/private answer is refused, not averaged.
* **Two mandatory checks, neither droppable.** *Save time* (`normalize_base_url`, `local_policy.py:130`): plain `http`/`https` origin, no userinfo/query/fragment, short plain path, host inside the ranges or resolving only into them. *Connect time* (`make_peer_check`, `local_policy.py:209`): the socket's actual peer is read after the TCP handshake and **before any request byte is written** — installed as the connection object's socket factory (`http_client.py:101`), which puts it **before the TLS handshake** too, so an `https://` refusal costs a TCP connection and no SNI, no ClientHello, no credential — with a second check after `connect()` as the backstop (`http_client.py:449`). The save-time answer goes stale by design; the peer check is what makes a rebound hostname safe. **Redirects are never followed** (`http_client.py:768`), to a private or a public address.
* **Owner-only configuration.** The endpoint is written only by the `set_base_url` provider action (`http/app.py:2056`), which requires an owner session (401 without one) unless the owner has set `owner_console_auth=false`, and is refused for any non-local provider. Refusals are fixed reason strings that never echo the refused value. The endpoint is re-read from the config store on every request, so a saved URL takes effect without a restart; the gateway makes **no connection at boot** and an endpointless local provider stays `not_configured` and is never dialed.
* **Keyless outbound by default.** All three servers are keyless out of the box, so the gateway sends no `Authorization` header; an owner may enrol an optional local token when the server itself demands one (`llama-server --api-key`, LM Studio *Require Auth*). This is a **documented product decision** — `docs/TODO.md` T15 ("with no credential") and the console's `LOCAL_AUTH_TEXT` — and the README now states it instead of staying silent.
* **Capabilities and cost fail closed until evidenced.** `/v1/models` publishes no capability evidence on any of the three, so tools and image input start `False` and move only on owner approval or, for Ollama, its documented `POST /api/show`; cost is never a number because there is no tariff.

**Non-blocking observations recorded here, deliberately not fixed in this change** (all three are from the same review and are out of scope for this documentation card):

* **N1 (Low, defense-in-depth) — `base_url` is not normalised on import/load.** `src/gateway/backup.py:57` (`import_config`) and `src/gateway/config.py:290` (`ProviderState.from_dict`) accept any string, so an owner-session backup import or a hand-edited `config.json` can store `http://169.254.169.254/` raw, bypassing the save-time policy. **The transport is the real boundary and holds independently:** the connect-time peer check refuses such stored URLs with **zero bytes** at the peer (probe2 T7/T8/T9 — public, metadata, and userinfo-at-public cases). Remediation when taken: run `normalize_base_url()` over any `base_url` present during import/config load.
* **N2 (Low, disclosure wording) — the local-provider card names the wrong confirmation party — reworded, taken with this change.** Original finding: `src/gateway/static/console.js` (`LOCAL_PRIVACY_TEXT`) said the switch to a cloud provider was refused unless "this provider is explicitly confirmed", which on the local card reads as the local provider, while the enforced gate (`src/gateway/core/routing.py:508-521`) requires the **fallback target** to be confirmed. The substance — authorization **and** explicit confirmation, refusal without both, fail-closed — held and was probed (`probe3` A12), and the register deferred the wording to the next console-touching change (this delta touched `console.js` heavily). Taken now: `LOCAL_PRIVACY_TEXT` (line 1848) reads "…and that switch is refused unless fallback is authorized and the provider being fallen back to is explicitly confirmed for the alias." — the fallback target is named as the confirmation party, and both strings `tests/console_dom_test.js` pins (`stay on this LAN`, `explicitly confirmed for the alias`) are preserved verbatim. The DOM test now also pins the reword itself: it asserts the old party name (`this provider is explicitly confirmed`) is **absent** from the rendered card — measured red on this tree before the console edit (`FAIL a local provider card asks for an endpoint and states the privacy rule - the confirmation party must be the fallback target, not this provider`, 1 check failed) and green after (`all checks passed`). Console copy only; no routing behaviour changed.
* **N3 (Informational, fail-closed) — `[::1]` and `localhost` are refused at save.** `local_policy.py:45` `PRIVATE_NETWORKS` has no `::1/128` (`fc00::/7` is unique-local only), so `http://[::1]:11434` is refused and `http://localhost:11434` fails when its answer carries the `::1` half, while the message says "must be a private or loopback address". Refusal is the safe direction, both seams apply the same rule, and the console placeholder steers to `http://127.0.0.1:11434`. Routed to QA (`t_a1146e4c`) as a behaviour/UX expectation; `docs/RUNBOOK.md` and `README.md` now say to use `127.0.0.1`.

**Evidence for this change.** `git diff fd682bfa… -- src/ tests/` is **empty** and `git diff --name-only` lists exactly `README.md`, `docs/ARCHITECTURE.md`, `docs/REVIEW.md`, `docs/RUNBOOK.md` — no Python, JavaScript, test or config line changed, so the reviewed implementation is byte-identical to `fd682bfa`. Gates run by me on this tree: `PYTHONPATH=src python3 -m unittest discover -s tests` → **1270 tests, OK (skipped=3)**; `python3 -m compileall -q src tests` clean; `PYTHONPATH=src python3 -m gateway --help` exit 0; `node tests/console_dom_test.js` all checks passed; `node tests/test_opencode_catalog.mjs` passed; `git diff --check` clean. **Cited, not run by me** (other lanes' numbers, on the reviewed tree `a5a97295` / `fd682bfa`): security card `t_6f7b740f`'s independent probes **84 + 42 + 44 = 170/170** (save-time, connect-time, app surfaces) and @testerbot's gates **1270 tests OK (3 pre-existing env skips)**, compileall 0, console DOM + OpenCode catalogue pass (card `t_a1146e4c`). Because a fix-forward moves the SHA, QA (`t_a1146e4c` lane) and security must both re-gate the **new exact SHA/tree** before any deployment; this entry records the boundary as it stands on the reviewed tree, and the docs above now describe it.

## Console sign-in opt-out (`owner_console_auth`) — the security review's F-1 on `002774d` (card `t_8fca60b0`) — closed by documentation

*(Not to be confused with the owner-session boundary entry above or the `### SEC-1` NaN-ceiling entry further up; each security review restarts its own numbering — this is F-1 of the `002774d` review.)*

**Severity: Medium (assurance), documentation only.** The security review of the console-sign-in opt-out candidate `002774d637995f5c8b6ff80203c873d529cd0d2a` (tree `7492067141b59ae9f33fcc5e73d55fb35eaf4e51`, branch `llm-gateway/t_f799a497`, base `2a53c8248b262c5372449584126d2fb058c36527`) passed the implementation — its independent battery went **165/165 with zero failures** across all four gate questions (fail-open input and parse/write path; machine-key/client-key independence across four fence configurations; leak needles and state-cache identity; discoverability) — and returned **CHANGES_REQUIRED** on one documentation item: `README.md` (surfaces table and the model-API caveat), `docs/RUNBOOK.md`, `docs/ARCHITECTURE.md` (listener table), this register and `docs/CLIENT_INTEGRATIONS.md` still stated the `/owner/*` session requirement unconditionally, with zero mentions of `owner_console_auth`, although `AGENTS.md` requires the README, runbook, architecture/contract docs and this register to change whenever a security boundary does. This commit qualifies those six lines — plus the other present-tense session claims the stale-claim sweep turned up in `docs/DEVELOPMENT.md`, the client-key bullet of `docs/OWNER_AUTH.md`, the `max_in_flight` subsection of `docs/RUNBOOK.md`, `docs/ARCHITECTURE.md` steps 3/7 and above in this register — and adds this entry. **Documentation only — no code was touched**: `git diff --name-only 002774d..HEAD` lists exactly `README.md`, `docs/ARCHITECTURE.md`, `docs/CLIENT_INTEGRATIONS.md`, `docs/DEVELOPMENT.md`, `docs/OWNER_AUTH.md`, `docs/REVIEW.md`, `docs/RUNBOOK.md`.

**The boundary, as now documented.**

* **The key and its parse.** `owner_console_auth` (boolean, default `true`) is read in `GatewayConfig.from_dict` by identity test (`is not False`): only the literal JSON boolean `false` disables the `/owner` browser-session gate. Absent, `null`, `0`, `"no"`, `""`, a list or any other type ⇒ auth **REQUIRED** — fail-closed — and the parse cannot raise, so a typo, a missing key or a drifted default can never open the console. A machine key cannot flip it: the settings action answers `403 machine_key_forbidden`. The key governs the browser session only; `ConfigStore` reads the file once at startup (a hand edit needs a restart), while the in-session `POST /owner/api/settings` action applies on the next request.
* **What changes when it is off.** Every `/owner/*` route is served without a session. The console shell/assets and the three pre-auth endpoints (`auth/session`, `auth/setup`, `auth/login`) stay public in both states, and those three still refuse bad credentials with `401 invalid_credentials`. `GET /owner/api/auth/session` keeps reporting session truth (`authenticated` stays `false` without a cookie) and adds `console_auth: "disabled"`; `GET /owner/api/state` carries `console_auth: "disabled"` and replaces the canned `access_mode_description` with an explicit warning; owner mutations are authorised without a cookie (`can_mutate: true`), and a request with no cookie claims no identity (`owner.username` is `null` — no invented identity, while a real cookie still reports the real username). The console shows a persistent `OWNER CONSOLE SIGN-IN IS OFF` banner on every tab and a `console sign-in is OFF` header pill instead of the login view. Identity-bound password change still requires a real session; host-side `--reset-owner-password` is the recovery path.
* **What does not change — the fence, identical in both modes.** Machine key: an unknown key still `401` on a write, a read-scoped key still `403 scope_required`, a write-scoped key still writes, `/owner/api/machine-keys` still refuses machine keys, cookie + machine key still `403 cookie_rejected`, and a machine key attempting the flag flip still `403 machine_key_forbidden` (the flag does not move). Model client key: `/v1` behaves identically in both modes — keyless `200`, right key `200`, wrong key `401 invalid_api_key`. The route table (30 owner routes, 21 non-public — byte-identical across modes), the `Host`/`Origin`/forwarded-header checks, and every leak invariant (no password hash, setup code, machine-key material or session token in any response in either state) are unchanged. The default (gate-ON) `/owner/api/state` payload stays byte-identical to `tests/fixtures/mcp_contract_baseline.json` apart from the two UTC date keys, so `mcp_contract_version` is **not** bumped (a future capture made with the gate off would correctly add `console_auth` and demand one; the guard only ever captures the default gate-ON harness).

**Evidence for this change** (run by me on this tree; `git diff` touches only Markdown, so no Python, JavaScript or config line changed): `PYTHONPATH=src python3 -m unittest discover -s tests` → **Ran 1400 tests, FAILED (failures=1, skipped=3)** — the single failure is the pre-existing MCP-contract date bomb `test_gated_family_change_requires_a_version_bump` (snapshot pinned to `analytics.usage.2026-10-31`, today is `2026-11-07` UTC; proven environmental on clean base `2a53c82` by the QA gate), failure set identical to the gated implementation run; `node tests/console_dom_test.js`, `node tests/console_auth_off_dom_test.js`, `node tests/test_opencode_catalog.mjs` all exit 0; `python3 -m compileall -q src tests` clean; `PYTHONPATH=src python3 -m gateway --help` exit 0; `ruff check .` → **265 errors, the same 265 as base `2a53c82` (delta 0)**; `git diff --check` clean; `grep -rln owner_console_auth README.md docs/*.md AGENTS.md` hits all eight required files, and the stale-claim sweep for unconditional session claims (the three patterns the finding used: sign-in-required, behind-the-owner-session, session-cookie-required) returns only the three now-qualified lines — `README.md:55`, `docs/ARCHITECTURE.md:13`, `docs/REVIEW.md` — each carrying its `owner_console_auth` qualification in place. **Cited, not run by me** (other lanes' numbers, on implementation SHA `002774d` / tree `7492067`): QA card `t_76be1b0a` **PASS** — 386 independent probe checks, 0 failures (26-state fail-closed matrix, 30-route both-mode coverage, full fence across modes, suite failure set identical to base, DOM, ruff delta 0); security card `t_8fca60b0` — **165/165** own probes, verdict CHANGES_REQUIRED solely on this documentation item, which this commit closes. Because a docs commit still moves the tip, QA (`t_76be1b0a` lane) and security (`t_8fca60b0` lane) must both re-gate the **new exact SHA/tree** before any deployment.

### R73 — F1 (High): an RST-abort of a streaming request permanently leaked the provider's in-flight slot (the never-started stream generator skipped `held_stream`'s `finally`) — closed by fix-forward `t_b144c14e`

**Severity: High (availability).** F1 of the security review of `llm-gateway/t_59fd2408` = `ac515eb480dadb73a843d265be81c41e067178cd` (gate `t_3fd0925e`, verdict and probes in its `VERDICT.md`), **pre-existing at `origin/main` `e7f6788`** — `_CappedAdapter` was byte-identical there, so the finding did not block that SHA; the register entry could not ride the gate candidate because adding it would have moved the pinned SHA, so it is filed with this fix-forward instead. `_CappedAdapter.invoke` released the cap slot only inside the stream generator's `finally`. A client that reset the connection after the request body was read but before the response headers left the process made `_send_stream` raise at `end_headers()` **before the iterator's first `next()`**; `_send` swallows `BrokenPipeError`/`ConnectionResetError`; and closing a generator that was never started runs **no code at all**, so `held_stream`'s `finally` never executed, `_active` was never decremented, and the slot was gone until the process restarted. Gate evidence (cap=1, isolated loopback): FIN-abort of `stream:true` → follow-up **200**; RST-abort (`SO_LINGER=0`) → follow-up **429 with the dummy upstream never contacted, still 429 at +20 s**; clearing the override → 200 with exactly one slot lost; RST of a non-streaming request → no leak (the buffered path releases inside `invoke`). Exploit: N cheap unauthenticated RSTs at `/v1/chat/completions` (`stream:true`; `/v1` is keyless until a client key is set) hard-429 a provider — 4 for a local, 32 for a cloud — until restart.

**Fix (this change — the "release on handler exit if the iterator was never started" option the verdict itself offered).** The slot is now a lease that any exit path can give back, exactly once, without the stream ever being entered:

* `src/gateway/provider_concurrency.py` — `_StreamLease`: lock-guarded idempotent `release()` plus `mark_started()` (the stream generator's first line) and `release_if_unstarted()`. `invoke` now returns `_LeasedStream`, a thin iterator facade over the same `held_stream` generator, whose purpose is to keep the lease reachable from the response object (`release_if_unstarted` for the handler, an unconditional `close()` for the alias probe). The generator's `finally` keeps its original role: once a stream has been entered, its own teardown releases the slot exactly when the stream ends, so the release timing that passed the gate is unchanged. `release_if_unstarted` is deliberately a no-op for a started stream — while an abandoned stream is still being read the slot is genuinely in use, and freeing it at handler exit would hand the next request a slot that is not free.
* `src/gateway/http/core.py` — `STREAM_LEASE_CONTEXT_KEY` and a `finally` in `_handle` around `application()` + `_send()`: every handler exit path (client RST before the headers, `HEAD`/`head_only`, an application exception after registration) pops and invokes the registered release.
* `src/gateway/http/app.py` — `_handle_inference` registers the lease in `request.context` immediately after `router.invoke` returns, before anything else can raise; `_run_alias_probe` closes the stream it never reads (a streaming-only upstream's stream was dropped unstarted — the same never-started case on the owner's alias-test action).
* Untouched per the card: the 429 body / `detail.attempts` shape, circuit-skip logic, the console region.

**Regression evidence.** `tests/test_provider_cap.py` — 9 tests: RST-abort of `stream:true` at cap=1 over a real socket against the fixture's real threaded server, with the first invoke parked inside the adapter so the reset is provably processed before any response byte, after which a follow-up must reach the provider (adapter calls 1 → 2); a graceful close after the response started (FIN) must keep working; the alias probe must not strand its stream; and three lease-contract units (a never-entered stream releases on the handler path, the handler path does **not** free a started stream, `close()` releases). **RED on base `ac515eb`, run by me: 7 failures + 1 error of 9 — only the FIN guard passed** (the RST and probe tests stayed 429; the lease units failed on the missing handle and the missing release). **Green on this tree: 9/9.** Full gates, run by me with this commit's final `src/` and `tests/` (the register entries were appended afterwards, and `compileall`, both node suites, `gateway --help` and `git diff --check` were re-run on the complete tree): `PYTHONPATH=src python3 -m unittest discover -s tests` → **1351 tests, OK (3 skipped)**; `python3 -m pytest tests/ -q` → **1349 passed, 3 skipped, 998 subtests passed**; `python3 -m compileall -q src tests`, `node tests/console_dom_test.js`, `node tests/test_opencode_catalog.mjs`, `PYTHONPATH=src python3 -m gateway --help`, `git diff --check` all clean; `ruff check src tests --output-format concise` (ruff 0.17.2) → **233 findings on this tree and 233 on base `ac515eb`**, line-agnostic (file, rule, message) diff **0 new, 0 gone**. Runner note: the suites ran with `PYTHONPATH=src:<site-packages>` because this environment's `prometheus_client` — a test-only parse helper the suite imports; the project itself still has zero runtime dependencies — lives outside the default path. Disclosed in passing (test-only, rides this commit): `tests/test_http_limits.py`'s worker-pool recovery probe became a bounded poll, because its single-shot form flaked on **base `ac515eb` itself** (2 failures in 8 isolated runs there, 0/20 after) — the client can read the response before the server thread returns its worker slot, which would otherwise make every downstream full-suite gate a coin flip. Because this card moves the SHA, **QA and security must both re-run on the exact new SHA/tree** before this entry's closure stands as a release gate.

### R74 — F2 (Low): `max_in_flight` had no upper bound — `10**400` was accepted by the owner API and persisted, switching the cap off — closed by fix-forward `t_b144c14e`

**Severity: Low (hardening).** F2 of the same gate (`t_3fd0925e`): `check_limit` (`src/gateway/provider_concurrency.py`) and `_positive_int` (`src/gateway/config.py`) accepted any positive `int`, and `POST /owner/api/provider` `action=set_max_in_flight` (owner session or write machine key) stored it in `config.json`. The cap refuses a request only while `active >= limit`, so a value larger than any achievable concurrency is not a limit — it is the cap silently switched off. Gate battery: `NaN`/`Infinity`/`-5`/`0`/`true`/`"32"`/`3.5`/`[1]`/`{}` all 400 with no write, but a 401-digit integer was accepted (200) and persisted, `max_in_flight_effective` = 10**400.

**Fix (this change).** A documented hard ceiling, `MAX_IN_FLIGHT_CEILING = 4096` (`src/gateway/config.py`): `check_limit` refuses anything above it through the same `invalid_max_in_flight` 400 refusal, with a message that names the bound (`max_in_flight must be a positive integer no greater than 4096`; every previously-refused value keeps byte-identical wording), and the config parser drops an over-ceiling stored value to `None` exactly like any other invalid stored value, so a hand-edited `config.json` falls back to the provider's default instead of loading an unbounded cap. `docs/RUNBOOK.md` §Per-provider concurrency documents the ceiling and both refusal paths; `docs/RUNBOOK.md` also now states the R73 slot-lifecycle truth (a slot is never leaked, recovery never needs a restart).

**Regression evidence.** `tests/test_provider_cap.py::MaxInFlightCeilingTests` — 3 tests: `10**400` → 400 `invalid_max_in_flight` naming 4096, `max_in_flight` still `null`, revision unchanged (no write); `4096` accepted (effective 4096) and `4097` refused; `ProviderState.from_dict` drops `10**400` → default 32 and keeps 4096. **RED on base `ac515eb`, run by me: all three fail** (`200 != 400` with the 401-digit value echoed in the body; the parser kept it). Gates as listed under R73. QA and security re-gate the new SHA as with R73.
### R75 — A malformed 502 named nothing: no field, no alias, no route — **closed**

**Severity: high diagnostic, with an availability consequence.** On
2026-10-24, 16:35:12Z-17:08:06Z, the production gateway returned **93
`502 malformed`** responses to `POST /v1/chat/completions` in an exact
three-per-minute cadence (the Hermes kanban decomposer retries at 0/1 s/2 s),
followed by one `502 exhausted` at 17:08:35Z that opened a 3600 s
provider-wide cooldown. None of the 93 could be diagnosed from inside the
gateway, and three separate defects compounded:

1. **The rejection body was discarded.** `error_from_response` kept only the
   fixed class message, so nothing said which request field the Codex
   backend had refused - the whole diagnosis for a shape rejection.
2. **A failed request was unattributable.** `_handle_inference` recorded the
   alias/attribution/request id on the request context only on the success
   path, so the 502 access-log line carried no `alias`, no `provider`, no
   `model`; and `_record()` filled `serving_provider`/`serving_model` from an
   `Attribution` that the failure path never populated, so the durable
   ledger row showed null, null for exactly the requests an operator most
   needs.
3. **The deployed strip list was stale.** The running image was `f7cf5a4`,
   which predates `1d9fe2f`; the fields the client sends (`temperature`,
   `max_tokens`) were still forwarded, and the backend refused them. The
   rollback that installed that image left `/version` reporting the
   rolled-back source revision against the *previous* release's image
   digest, because `release.json` on the state volume is rewritten only by a
   proper release - so the identity check could not see the rollback.

**Fix.**

* `AdapterError.rejected_fields` carries the names a shape rejection named.
  Only identifier-shaped tokens leave a body: a pydantic `loc` path, or the
  documented `Unsupported parameter: <field>` message. Prose, quoted values,
  prompt echoes and over-long tokens are dropped by
  `gateway.adapters.base.sanitize_rejected_fields`, which is the single gate
  between upstream text and gateway state; the durable ledger re-validates
  on read and *derives* `reason` from the class message plus the validated
  names, never copying the row.
* The failure path sets `serving_provider`/`serving_model` to the route it
  failed on (the success path overwrites them with the serving route), and
  `RoutingError.log_attribution` carries the alias and route into the
  access-log line. The public error *body* stays generic; `log_attribution`
  is deliberately absent from `to_dict()`.
* The Codex strip list was re-swept live (see
  `docs/PROVIDER_CONTRACTS.md`): nine further fields are refused with the
  same 400 and the same control, and are now declared. `store` and
  `previous_response_id` are refused by the backend but deliberately not
  declared: the adapter already forces `store: false`, and dropping a
  caller's continuation would change which conversation a request continued.

**Regression evidence.** `tests/test_failure_attribution.py` (15 tests: the
field-name gate, the durable row, the 502 access-log line, the owner-policy
fallback, and the incident payload itself served with `temperature` dropped
and `max_tokens` crossing as `max_output_tokens`),
`tests/test_ledger_persistence.py::test_load_keeps_only_field_names_in_an_attempt`,
`::test_round_trip_preserves_rejected_field_names`, and
`tests/test_unsupported_parameters.py` (declared set, deliberate exclusions,
no false substitutions). Full suite: **541 passed, 3 skipped**.

Live evidence for the strip list: `scripts/probe_codex_param_sweep_round2.py`
(both enabled models, text and image, run inside the production container)
and `scripts/probe_codex_param_sweep_round2b.py` (confirms
`max_output_tokens` - the spelling a converted request carries - is
accepted, while both Chat spellings are refused).


### R76 — A critical alias's whole route chain could sit on one provider, so one cooldown emptied it and nothing reported the shape — **closed**

**Severity: high (availability).** *(closed)*

The 2026-10-23 fleet-wide stall: `coding` had exactly one fallback
(`opencode_go/mimo-v2.6-flash`), and the primary's circuits had opened
provider-wide (`chatgpt_codex/*`, held for hours — `exhausted` is a
provider-scoped class by design, R41). When the same burst also rate-limited
the one fallback, the alias's eligible set became empty and the gateway
returned 503 `no eligible route` to 21 + 21 + 111 requests across workers
simultaneously, every worker then burning its retry ladder before falling
back client-side. Two routes would not have been enough either: a second
route on the *same* provider shares the same provider-wide circuit, so the
chain would still have emptied. Nothing in configuration validation
reported that any enabled alias had placed every one of its routes on a
single provider — the condition is servable and looks redundant, and was
therefore invisible until an incident made it visible.

**Fix.** Two parts, split by ownership.

1. **Source:** `gateway.routes.single_provider_warning` — a loud,
   non-fatal config-validation guard for an enabled alias with two or more
   routes whose routes all name one provider. It is reported, never
   refused (an owner with a single live provider has no legal
   two-provider chain to choose, and refusing would leave their alias
   unsaved): on the alias save and publish responses as `warning`, on every
   owner-state read as `route_warning` beside the alias, and rendered as a
   `notice warn` on the alias card, with an immediate toast at save and
   publish time. The public `/v1` surfaces are unchanged — the warning
   names providers, exactly like the rest of owner state.
2. **Owner configuration** (approved route inventory, Mark, 2026-10-25):
   each critical alias — `coding`, `flash`, `hermando`, `pm` — carries a
   second fallback so its chain spans the two live chat providers, and
   every second-fallback provider appears in that alias's
   `fallback_confirmed_providers` (`hermando` gains `chatgpt_codex`).
   The ceiling is stated, not oversold: such a chain survives **one**
   provider failing, not both.

**Regression evidence:** `tests/test_second_fallback.py` — circuits armed
the way production arms them (route scope, and provider scope for the
incident's `provider/*` shape) → a chat-completions request returns 200
served by the second fallback with `fallback_used` and **both** skipped
routes named and reasoned in the ledger, an image request included; a
provider-wide cooldown on the fallback's own provider still returns 503
(proving the guard's rationale rather than asserting it); and all four
approved chains authorize their second-fallback provider.
`tests/test_single_provider_warning.py` — a single-provider chain warns on
save, publish and owner state, a two-provider chain is silent, and the
public catalogue and models list never carry the warning.

### R77 — An upstream that dies mid-answer is recorded as success and the client is silently truncated — **closed**

**Severity: high.** *(closed)*

Found by QA card `t_1b61e593` (2026-10-24) and independently reproduced
with synthetic adapters and loopback sockets only — no network, no
credentials — on the integrated T11 tree `8a9daa9` (tree `6b7990bf`) and
on canonical main `a2d1286`. The streaming path (`app.py` `frames()`) is
byte-identical on both trees; the only T11 delta in `http/core.py` is the
client-hangup branch, which does not intercept `AdapterError`, so the gap
is on main today. Four failure shapes, probed end to end:

| shape | client sees | ledger `error_class` | `provider_error_count` |
|---|---|---|---|
| upstream refuses before answering (`AdapterError('auth')`, buffered) | 502 `{"error":{...,"type":"auth"}}` | `auth` (record status 502) | 1 |
| upstream dies mid-stream after one frame (`AdapterError('transient')`) | truncated `200` chunked stream: no `0\r\n\r\n` terminator, no `[DONE]`, no error event — indistinguishable from a network cut | **`null`: the record still reads success, status 200** | **0** |
| client disconnects mid-stream (T11) | — (the client left) | `client_abort` | 0 |
| buffered client, streaming-only upstream dies mid-answer | **502** `{"error":{...,"type":"transient"}}` (outer catch, `app.py:201`) | **`null`: the record still reads success, status 200** | **0** |

**Root cause.** The ledger record is written at route time; when the
failure happens *after* routing succeeded — while the stream is being
consumed — nothing amends it. On the streaming path the `AdapterError`
also escapes `_send_stream` unhandled and `socketserver.handle_error`
prints the traceback to stderr. `gateway.core.abort.classify_late_error`
implements exactly this decision (the provider's own class when the
request was not aborted, `client_abort` when the client had already
left) but has **no production call site**. On the buffered path the
client is told the truth (502) while the two observability surfaces
disagree: the access log records `transient` parsed from the 502 body,
the ledger says success.

**Consequences.** A provider that repeatedly dies mid-answer is counted
by nothing: `totals["errors"]` does not rise, the circuit/cooldown
machinery never sees the class (`TRIGGER_FOR_ERROR`), the route is never
benched, and every truncated answer reaches the client with no in-band
signal that it is incomplete.

**Required fix (QA decision, card `t_1b61e593`).**

1. **Class.** Amend the record with `classify_late_error(route_context,
   exc)` — the raised `AdapterError`'s own class (`transient` here;
   `client_abort` if the client had already disconnected, per T11 rule 2
   in `gateway.core.abort`). No new error class: a mid-stream failure is
   a provider failure, it must be counted by
   `client_abort.provider_error_count(ledger)` (the class is in
   `ERROR_CLASSES`, so circuits/cooldowns consume it), and it is distinct
   from `client_abort` by construction. A dedicated `upstream_abort`
   class was considered and rejected: it would need circuit/cooldown/
   fallback semantics defined from scratch while adding no distinction
   anything consumes.
2. **Client signal.** After the last delivered frame emit exactly one SSE
   error event `data: {"error": {"message": <class message>, "type":
   <class>}}`, then end the chunked stream cleanly (`0\r\n\r\n`) **without**
   `data: [DONE]` — `[DONE]` stays reserved for a complete answer, so no
   consumer can mistake a truncation for completion. The HTTP status
   stays 200; the headers were already sent, and the error frame is the
   in-band signal.
3. **Traceback.** The traceback is wanted (t_7198bfa3: the log is not
   blinded) but as a *handled* error: catch `AdapterError`, log it
   through the gateway logger at ERROR **with** the traceback, and catch
   only `AdapterError` — any other exception must keep propagating to
   `socketserver.handle_error` unchanged
   (`MidFlightLoggingTests.test_an_application_error_still_prints_a_traceback`
   must stay green).
4. **Buffered path.** The outer `AdapterError` catch in `app.py` must
   amend the same record (returned status, class from
   `classify_late_error`) so the ledger and the access log agree.

**Guardrail that must keep passing:**
`tests/test_t11_client_disconnect_acceptance.py::UpstreamAbortDistinctnessTests::test_upstream_midstream_failure_is_never_classified_as_client_abort`
— the mid-stream record is never relabeled `client_abort`, and
`error_class is None` must not be enshrined as desired behaviour.

**Resolution.** Both paths now amend the route-time record through
`gateway.core.abort.classify_late_error` (its first production call
site): the class the adapter raised is the class that is recorded —
`transient` here, `auth` when the adapter said so, `client_abort` only
when the caller had already left (T11 rule 2) — and no new class was
introduced, so the class stays a member of `ERROR_CLASSES`, is counted
by `client_abort.provider_error_count(ledger)`, and is consumed by
circuits and cooldowns. On the streaming path (`app.py`, `frames()`)
the failure is now *handled*: the record is amended (class; status
stays 200, the status actually sent), the gateway operational logger
records it at ERROR **with** the traceback, the client receives exactly
one in-band `data: {"error": {"message": <class message>, "type":
<class>}}` frame after the last delivered frame, and the chunked stream
then ends cleanly with `0\r\n\r\n` and **without** `data: [DONE]`.
On the buffered path the outer `AdapterError` catch in `handle()`
amends the same record with the status it returns (502), so ledger and
access log agree. Only `AdapterError` is caught; any other exception
still propagates to `socketserver.handle_error` untouched. Evidence:
`tests/test_r42_upstream_midstream_ledger.py` (clean chunked EOF with no
`[DONE]`; exactly one error frame after the delivered answer; class,
`provider_error_count` and `totals["errors"]` all recorded; `auth` kept;
buffered 502 and ledger record agree; a refusal before the answer still
records at route time; a non-`AdapterError` mid-stream failure still
reaches stderr with its traceback), plus the T11 matrix green on the
same tip — `tests/test_t11_client_disconnect_acceptance.py` in full,
including
`UpstreamAbortDistinctnessTests::test_upstream_midstream_failure_is_never_classified_as_client_abort`
and
`MidFlightLoggingTests::test_an_application_error_still_prints_a_traceback`.
Full suite on this tip: 583 tests, `OK (skipped=4)` — the four skips are
environmental (node/chromium, docker, IPv6 ULA) or the t_3acad3f0
conditional.

*(Authored as R42 on its origin card; R41 and then R42-R76 were taken by
the live register while it was in flight, so it is renumbered to the
PM-reserved R77 on integration.)*