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

### R25 — A thinking control was silently dropped on a converted request — **superseded**
**Severity: high contract-correctness risk.** *(superseded by **R35**, which
closes it with the normalisation and its regression tests)*

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

**Planned as** `docs/TODO.md` item **T4**.

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

## Recommended order

Findings R0-R27 and R28-R36 are closed or superseded. **R36 and R38 are
open**; R36 is the explicit
post-deployment route replacement above, and R38 is the inert per-phase
reasoning effort, which needs a routing change. R37, R39, R40, R41, R42,
R43, R44 and R45 are closed. The register
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
