# Codebase review

## Scope and evidence

This review covers the repository at commit `a4ab1ef` before the documentation
changes listed below. It is a static code review plus local, credential-free
verification; it is **not** a provider-account or deployed-container test.

Verified locally:

```text
PYTHONPATH=src python3 -m unittest discover -s tests -q
Ran 181 tests ... OK

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
* Setup is a separate loopback listener and credential enrollment is rejected
  on the LAN console.
* Cross-provider fallback is opt-in and has hard stops for tools and unsafe
  classes. The test suite covers most of those safety decisions.
* `/version` has a deliberately fixed, metadata-only schema.

These are code/test observations, not a claim that a provider account or a
container deployment has been exercised.

## Findings requiring implementation work

### R1 — ChatGPT/Codex device flow cannot complete in the current UI
**Severity: release blocker for the ChatGPT/Codex setup flow.**

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

### R3 — Release gate listener probe does not reliably inspect bind addresses
**Severity: high.**

The gate reads `/proc/net/tcp` as raw strings and indexes character positions
rather than parsing the local address/state fields
(`scripts/release_gate.py:325-345`). It then compares that output to textual
`0.0.0.0`/`::`, even though proc entries are hexadecimal. This cannot prove
that a wildcard listener is absent. It also does not inspect IPv6.

**Required fix:** parse `/proc/net/tcp` and `/proc/net/tcp6` correctly, or use
a small standard-library socket/proc parser with unit fixtures. Add negative
fixtures for wildcard IPv4 and IPv6 plus the expected explicit loopback/LAN
binds.

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

### R5 — Observability documentation overstates the current implementation
**Severity: medium; honesty and operations risk.**

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

### R6 — Owner-session test TTL is ignored
**Severity: medium testability/correctness defect.**

`OwnerAuth` accepts `session_ttl` in its constructor
(`src/gateway/auth.py:74-84`), but `OwnerSession.expired()` always uses the
module constant (`auth.py:59-68`). The injected value does not affect expiry,
which makes short-TTL tests misleading.

**Required fix:** carry the configured expiry/TTL into the session or evaluate
expiry in `OwnerAuth`; add boundary tests for expiry and pruning.

### R7 — CI artifact recording can fail for locally built images
**Severity: medium CI robustness risk.**

`image_digest()` has a fallback to the image ID when `RepoDigests` is absent
(`scripts/release_gate.py:86-94`). The CI workflow independently evaluates
`{{index .RepoDigests 0}}` without that fallback
(`.github/workflows/ci.yml:41-45`). A local candidate normally has no registry
repo digest, so this reporting step may fail despite a valid image and gate.

**Required fix:** share the release gate's ID/digest resolution logic or make
the workflow use a safe shell fallback. Test the empty-`RepoDigests` case.

## Documentation corrections made in this review

* `README.md` no longer claims the gateway never stores operational
  credentials; it now distinguishes stored owner-only secrets from exposed
  data.
* The fresh-install text now accurately says provider cards exist while
  credentials and discovered/published models do not.
* OpenCode Go setup now says Save, then explicit Test connection—the UI does
  not test as part of saving.
* `RUNBOOK.md` now reflects the actual default: in LAN no-auth mode all
  mutation remains in setup, not the LAN owner console.
* `ARCHITECTURE.md` and `DEVELOPMENT.md` establish the verified runtime map,
  security/data boundaries, and an exact-image/Compose verification standard.

## Recommended order

1. Fix and test **R1** before presenting ChatGPT/Codex setup as usable.
2. Fix/prove **R2**, then run a real Compose deployment test before any live
   release.
3. Repair **R3** and **R7** so release evidence is trustworthy.
4. Tighten **R4**, then make observability truthful and complete in **R5**.
5. Fix **R6** with its targeted tests.

No provider credentials, provider calls, subscriptions, or deployment state
were accessed during this review.
