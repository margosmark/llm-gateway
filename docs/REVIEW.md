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
  and origin validation for browser mutations. There is no owner session and
  no CSRF bootstrap, by product decision.
* There is one listener. The owner console, the configuration API and the
  OpenAI-compatible API share it, and any peer that can reach it is an
  administrator by product decision.
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

### R0 — A LAN client can claim the first owner passphrase (superseded)
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

## Recommended order

Every finding above is closed or superseded. The register is kept so the next
agent can see what was already paid for in time.

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

## Trusted LAN admin-mode candidate update

The approved single-listener change resolves the former R0 first-passphrase
claiming condition by deleting passphrase creation and owner sessions rather
than adding a bootstrap race fix. Regression evidence: `test_login_and_passphrase_route_are_removed`, direct owner-state access and
`test_owner_console_mutates_same_origin_without_session_or_csrf` in
`tests/test_end_to_end.py`, plus CLI one-application coverage in
`tests/test_cli.py`. The formerly listed R15 setup-surface host-only message is
also removed with the setup UI; the console no longer presents that claim.

This candidate does not resolve unrelated R1/R8/R9/R2/R3/R7/R11 findings.
R4-R6, R10, R12-R14 remain open unless independently evidenced elsewhere.
The focused suite currently exercises the direct console, same-origin mutation,
cross-origin refusal, hostile Host/forwarded-header rejection, anonymous fresh
API behavior, and write-only credential state. Deployment/image/volume claims
remain unverified here.

The retired setup-port reuse finding is fixed in `gateway.cli`: `--port` and
`GATEWAY_PORT` both reject `8766` during argument parsing with an actionable
error, before application construction or listener startup. Regression tests
`test_reserved_setup_port_is_rejected_from_cli_before_startup`,
`test_reserved_setup_port_is_rejected_from_environment_before_startup`, and
`test_allowed_explicit_private_port_remains_configurable` cover refusal and
the valid configurable-port path.
