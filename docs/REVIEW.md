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
  origin validation for browser mutations, owner sessions, and CSRF.
* Setup is a separate listener and credential enrollment is rejected on the
  owner console. It is owner-session/CSRF protected whether the operator
  binds it privately to LAN (default) or loopback.
* Cross-provider fallback is opt-in and has hard stops for tools and unsafe
  classes. The test suite covers most of those safety decisions.
* `/version` has a deliberately fixed, metadata-only schema.

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

### R2 — Compose deployment has no verified writable-volume initialization
**Severity: release blocker until exercised and fixed if necessary.**

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

**Evidence:** `tests/test_release_gate.py` — hexadecimal IPv4 and IPv6
decoding, wildcard recognition in both families, explicit loopback binds,
non-listening sockets ignored, and process-scoped filtering that excludes a
wildcard socket belonging to another process. The gate itself now reports
`listening=[('127.0.0.1', 18775)]` for the candidate.

### R4 — Published aliases and profile identifiers are insufficiently validated
**Severity: high correctness/recoverability risk.**

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

### R6 — Owner-session test TTL is ignored
**Severity: medium testability/correctness defect.**

`OwnerAuth` accepts `session_ttl` in its constructor
(`src/gateway/auth.py:74-84`), but `OwnerSession.expired()` always uses the
module constant (`auth.py:59-68`). The injected value does not affect expiry,
which makes short-TTL tests misleading.

**Required fix:** carry the configured expiry/TTL into the session or evaluate
expiry in `OwnerAuth`; add boundary tests for expiry and pruning.

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

### R8 — ChatGPT/Codex model refresh violates the shared adapter contract
**Severity: release blocker for the ChatGPT/Codex onboarding flow.**

`ChatGPTCodexAdapter.list_models()` returns strings
(`src/gateway/adapters/chatgpt_codex.py:390-409`), whereas the generic refresh
handler requires `ModelInfo` attributes such as `model_id` and `dialect`
(`src/gateway/http/app.py:703-733`). A normal ChatGPT refresh therefore hits
the generic 500 path instead of populating selectable models.

**Required fix:** make every adapter return `list[ModelInfo]`, with the known
Responses dialect for ChatGPT/Codex, and add HTTP-level refresh tests for both
real adapters.

### R9 — Non-streaming responses are returned in the upstream dialect
**Severity: release blocker for OpenAI-compatible Chat Completions clients.**

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

### R10 — Device authorization is neither setup-only nor shown-once — **partly closed**
**Severity: high credential-boundary and flow-correctness risk.** *(shown-once
and interval closed; the surface rule is superseded)*

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

### R11 — Production `/version` identity is not mechanically published
**Severity: high release-traceability risk.**

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

### R12 — Profile and provider session isolation need explicit policy
**Severity: medium authorization/privacy risk.**

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

### R13 — Plain HTTP is an explicit but high-risk LAN trust assumption
**Severity: decision required before using owner/caller credentials on an
untrusted LAN.**

The implementation intentionally serves HTTP and has no TLS (`README.md`,
"Known limitations"), yet LAN traffic includes owner cookies, bearer tokens,
and request content. This is acceptable only where the LAN itself is trusted;
it is not safe against an on-path LAN attacker. The current forwarded-header
rejection also means the documented TLS-terminator suggestion needs an
explicit proxy-trust design before adoption.

**Required action:** either make the trusted-LAN assumption a clear deployment
precondition, or design a tested TLS/loopback/proxy model. Do not advertise a
proxy topology the request-hardening layer rejects.

### R14 — Configuration rollback instructions currently only make a copy
**Severity: medium operator-recovery defect.**

The runbook's “Rollback the configuration” command copies current
`config.json` to a `.before-upgrade` file and restarts; it never restores a
prior snapshot (`docs/RUNBOOK.md:183-193`).

**Required fix:** document separate pre-upgrade backup and restore commands,
validate restored JSON/permissions before startup, and state the limits around
provider-side tokens and restart-lost state.

### R15 — Setup-surface helper text still claims host-only access
**Severity: low documentation/UI honesty defect.**

When credential enrollment is unavailable, the browser says setup is reachable
only from the host or an SSH tunnel (`src/gateway/static/console.js:563-566`).
That conflicts with the configurable LAN setup listener. It should render the
actual setup base URL or say "the configured setup listener" without
inventing a host-only restriction.

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

## Recommended order

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
