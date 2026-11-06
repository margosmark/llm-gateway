> **Vendored document — provenance header added, body verbatim.**
> Source: kanban attachment of task `t_2fce4e14` (`gateway-mcp-spec.md`);
> vendored 2026-11-05 by the F2 documentation card `t_c164770c`.
> **Implementation status:** machine keys are implemented in this
> repository (`gateway.owner_auth.MachineKeyStore`,
> `gateway.http.app`); the MCP server ships as
> `margosmark/gateway-owner-mcp` @ `1acc55b` (QA `t_cf3205a9` +
> security `t_ce0945dd` PASS). Scope note: this spec's schema is
> provider-only for `clear_cooldowns`; the shipped gateway and server
> accept `route|provider|all`, sanctioned by build card `t_0b1664c6`
> beyond the spec — see `docs/REVIEW.md` R70 and `docs/OWNER_AUTH.md`.
> The text from the horizontal rule through the end of `## Source references`
> is the spec verbatim: attachment sha256
> `2afa3c88425e8164cf3747e0552a2b646ca0e18c0d5fa6c9a9b1aafdd18c3449`.
> An **addendum** — not part of the vendored spec, added 2026-11-06 by card
> `t_d43744ae` — follows it at the end of this file.

---

# llm-gateway owner MCP — design specification

**Evidence baseline:** `origin/main` at `2f324360ebb41dd59232ee6e34ee1c7849d59555` (fetched and inspected 2026-11-04). This is a design only; no source/config changes and no mutating live calls were made. Endpoint citations below refer to that immutable baseline.

## Recommendation

Ship a small, separately versioned `gateway-owner-mcp` package/repository, initially configured as a **stdio** MCP server per local Hermes profile. It makes loopback HTTP calls to the gateway's owner API; it does not embed gateway internals or bind a second network listener. Keep the gateway-side auth/token-store changes with the owner-auth implementation owners (the gateway's hot HTTP/auth files are shared); the MCP package can proceed independently once the API contract and machine auth land. Do not choose streamable HTTP for v1: it adds another network listener, origin/host/CORS and token-exposure surface without a demonstrated multi-host need. If later needed, make it a separately secured deployment, not an implicit mode.

The existing owner API is **not ready for safe MCP automation as-is**: `origin/main` is anonymous trusted-LAN admin (any reachable peer can mutate; `src/gateway/auth.py:85-89`, `src/gateway/http/app.py:563-572`). Owner-auth candidate `53918c1` adds human password/session-cookie authentication, but cookies are the wrong unattended machine credential. Add a first-class automation-token layer before enabling MCP writes.

## Auth and network boundary (required before write tools)

1. Exactly three credentials, with distinct meaning and storage: (a) owner login password for the human account and its session cookie; (b) optional model client API key scoped only to `/v1` inference, set/cleared by the owner; (c) machine key for MCP/automation. Never derive the machine key from a password, reuse the client API key, or accept one credential in place of another.
2. The owner console UI generates machine keys (name + `read` or `write` scope), displays plaintext exactly once at creation, and never offers retrieval. Console also supports list (display-id, name, scope, last-used only), rotate, and revoke. Rotation invalidates the prior key immediately. Persist only a cryptographic hash (e.g. HMAC-SHA-256 with a separate local pepper) plus display-id, name, scope, created/last-used/revoked metadata; store mode 0600 in a 0700 data directory, never plaintext. Independent key IDs allow per-client audit/revocation.
3. Use `Authorization: Bearer <machine-key>` header only—never URL/query/body, config, MCP tool arguments/results, logs, state, status, crash output, errors, or transcripts. Redact authorization headers in access/error logging. `read` keys authorize read tools only. Define `write` as read+write so an automation client can inspect current state before a guarded change; enforce write scope on every write tool. Do not invent a third scope. Authenticated owner-only console operations mint/list/rotate/revoke; MCP cannot mint/reveal/revoke keys.
4. Do not accept client `/v1` API keys as owner credentials. They authorize model inference only; they cannot authorize owner endpoints. Human cookie auth remains for console use and is not used by MCP. `53918c1`'s session storage/revocation semantics remain independent; implement machine bearer auth as a separate authentication principal and policy, not a synthetic cookie or password login.
4. Bind MCP stdio process to local profile. Its upstream target must be a configured loopback URL and reject non-loopback destinations unless an explicit separately-reviewed deployment mode exists. Gateway owner listener remains bound only to intended LAN interface; bearer token must additionally be IP/network scoped (loopback and configured private LAN CIDRs) and checked server-side. A stolen bearer token must not be valid from public networks; do not treat MCP stdio alone as that boundary. Do not trust forwarded headers; reject them as current `security.py:65-74` does.
5. Non-browser client requests have no Origin and do not need browser CSRF tokens. For bearer-authenticated MCP API requests, require Authorization, reject cookies, do not apply browser-only Origin requirement; still enforce canonical Host and forwarded-header checks. Browser cookie mutations retain exact Origin checking. Never loosen existing browser checks for the machine path; use explicit auth-mode routing.
6. Read-only mode: server `--read-only`/config flag denies every write tool locally before network call, and gateway token scope independently denies writes. Default should be read-only unless operator explicitly opts into write tools. Dry-run is available where meaningful, but not a substitute for read-only.
7. Audit every owner mutation with timestamp, token id/client identity (not token), action, target ids, result/error code, request/correlation id, and dry-run flag. No provider secrets, request bodies, prompt/response content, bearer token, session cookie, or full exception text. Retention and rotation documented. Existing access log records action/provider identity at `app.py:1646-1650`; extend safely rather than recording payloads.
8. MCP tool schemas must contain no credential, arbitrary prompt, provider secret, or opaque request-body field. Sanitize tool outputs with closed allowlists: provider state/metadata only, never credential handles, auth material, raw upstream error bodies, prompt/response, or raw ledger records. Provider `test` and alias test currently can cause upstream requests (`app.py:1657-1665`, `2041-2052`); they must return bounded redacted status only. Never expose detailed state if it might include secrets without auditing `_render_state` serialization.

## Endpoint inventory: existing vs required

Owner routes are installed under each mount (`/owner` here) at `src/gateway/http/app.py:425-454`. Existing endpoint payload contracts should be pinned against the source revision and exercised in contract tests. Use the fixed `/owner` mount (not caller-provided paths).

| MCP operation | Existing endpoint and evidence | Status / notes |
|---|---|---|
| gateway version | `GET /version`, `app.py:409`, handler `576-583` | Exists; public metadata. |
| health | `GET /health`, `app.py:410`, handler starts `585` | Exists; health/metrics surface, not a detailed owner health substitute. |
| gateway state; aliases list/detail; providers list/status; cooldowns; provider health; aggregate usage | `GET /owner/api/state`, `app.py:430-431`; handler `1010-1025`, render builds provider model rows `1044-1065`, health/cooldown/usage fields at `1174-1182` | Exists as one comprehensive snapshot. Aliases serialized in same state; no standalone detail route. MCP should filter locally and avoid returning entire state by default. Ensure config serialization does not expose secret values. |
| usage leaderboard | `GET /owner/api/leaderboard`, `app.py:438`; handler `988-1008`; aggregate-only guarantee `997-998` | Exists; supports `days` query via `analytics_days` (`1000`). Add bounded paging/limit if payload grows; current matrix can be large. |
| capability catalog / discovery evidence | `GET /owner/api/capabilities`, `app.py:444`; handler `1237-1250` | Exists; filters `provider_id`, `model_id`. |
| alias save / publish | `POST /owner/api/alias`, `app.py:451`; handler `1945-2007` (save), `2009-2029` (publish) | Exists; no dry-run. Add a safe validate/preview operation (or `dry_run` implemented before transaction) and expected-revision CAS to prevent stale writes. |
| provider test / refresh | `POST /owner/api/provider`, `app.py:449`; handler `1636-1713` | Exists; both contact upstream and mutate provider state/catalog. MCP tool annotations non-read-only; require write scope/confirmation and bounded timeout. `test` is not side-effect-free. |
| enable/disable model/provider | `POST /owner/api/model` and `/provider`, `app.py:449-450` | Existing action handlers; exact actions to be pinned from handler and tests before implementation. Provider disable/disconnect is more destructive than model toggle. No endpoint change required, but typed MCP mapping must allowlist actions. |
| clear cooldowns | `POST /owner/api/provider` action `clear_cooldowns`, `app.py:1781-1800` | Exists. Call with provider only; MCP must not permit implicit `probe_alias` because that triggers upstream probe. Require confirmation. |
| capability proposal approve/reject | `POST /owner/api/capabilities/decision`, `app.py:447`; handler `1458-1515+` | Exists; uses proposal/decision ids and expected revision/fingerprint, supports replay. Map explicit approve/reject, preserve CAS fields, require confirmation. |
| machine key create/list/rotate/revoke | none in owner route table `app.py:425-454` | **New owner-auth feature required** in the authenticated owner console UI. Create name + `read`/`write`, show plaintext exactly once; list display-id/name/scope/last-used only; rotate/revoke invalidate immediately. MCP cannot manage its own key. |
| machine bearer authentication and scope enforcement | absent in anonymous `app.py:563-572`; owner-auth candidate documents cookie/session model (`53918c1:docs/OWNER_AUTH.md:11-49`) | **New gateway auth middleware required**. Must permit bearer auth for owner API using scopes while preserving cookie/browser behavior. |
| dry-run/validation for mutations | alias save/publish currently mutate (`app.py:1958-2007`, `2009-2029`) | **New behavior required**. Return `would_change`, normalized diff/target and current revision; never write or contact providers in dry-run. For unsupported operations explicitly return `dry_run_unsupported`; do not silently execute. |
| independent owner state/detail endpoints | none; current state aggregate at `app.py:1010-1025` | Not required for v1; if snapshot paging becomes necessary add `GET /owner/api/mcp/v1/...` typed endpoints rather than expose internal state wholesale. |

### API contract pinning

Define a versioned OpenAPI/JSON schema contract for owner automation API (e.g. `owner-api-contract` version `1`). At server startup MCP checks `GET /version` source revision and a new `GET /owner/api/automation/capabilities` (contract version, supported operations/scopes, pagination limits). Refuse writes on unknown contract/version; reads may proceed only when compatible schema hash is allowlisted. Pin MCP package to a tested minimum gateway release/contract version, not an arbitrary moving `origin/main`. Existing `/version` reports release identity (`app.py:576-583`) but does not currently negotiate owner API contract: new endpoint/field required.

## Tool inventory

Names are namespaced `gateway_`. Structured JSON outputs are bounded and validated. Read operations carry MCP annotations `readOnlyHint=true`, `destructiveHint=false`, `idempotentHint=true`; writes use `readOnlyHint=false`, set destructive per operation, and `openWorldHint=false` except operations that contact upstream providers.

| Tool | Input (only identifiers/filters) | Existing mapping | Annotation / behavior |
|---|---|---|---|
| `gateway_get_version` | none | `GET /version` | read-only. |
| `gateway_get_health` | none | `GET /health` | read-only; health summary only. |
| `gateway_get_state` | `section`, `cursor`, `limit` | `GET /owner/api/state` | read-only; whitelist sections; default limit 100, max 500; cursor stable within revision. Redact secrets. |
| `gateway_list_aliases` | `cursor`, `limit`, optional `enabled` | state snapshot | read-only; return metadata/routes only. |
| `gateway_get_alias` | alias id | state snapshot | read-only; exact match; not found deterministic. |
| `gateway_list_providers` | `cursor`, `limit` | state snapshot | read-only; no credentials. |
| `gateway_get_provider_status` | provider id | state snapshot/provider health | read-only; status and redacted last error category only. |
| `gateway_list_cooldowns` | `cursor`, `limit`, optional provider | state snapshot `cooldowns`/details | read-only; avoid request identifiers if sensitive. |
| `gateway_get_usage` | date/window filters, `cursor`, `limit` | leaderboard | read-only; aggregate only. |
| `gateway_get_capabilities` | provider id, model id, cursor, limit | capabilities | read-only. |
| `gateway_save_alias` | typed alias configuration, `dry_run`, `expected_revision` | alias action save | destructive=false? configuration-changing but reversible; mark `destructiveHint=true` because changes live routing config. Requires explicit operator confirmation except dry-run; dry-run default. |
| `gateway_publish_alias` | alias id, enabled, `dry_run`, expected revision | alias action publish | destructive=true; confirmation required; dry-run default. |
| `gateway_test_provider` | provider id, `dry_run` (must be false to run) | provider action test | upstream contact and status mutation; destructive=true/openWorld=true; explicit confirmation. In dry-run, return plan only and do not connect. |
| `gateway_refresh_provider` | provider id, dry_run, expected revision | provider action refresh | updates catalog and may affect enabled model inventory; destructive=true/openWorld=true; confirm; dry-run no provider call. |
| `gateway_set_model_enabled` | provider id, model id, enabled, dry_run, expected revision | model action | config mutation; destructive=true; confirm. |
| `gateway_set_provider_enabled` | provider id, enabled, dry_run, expected revision | provider action allowlisted action | config mutation; destructive=true; confirm. Do not map disconnect/enroll/device flows. |
| `gateway_clear_cooldowns` | provider id, dry_run, confirmation phrase/token | provider action clear_cooldowns | destructive=true; confirmation required; prohibit `probe_alias`. |
| `gateway_decide_capability_proposal` | proposal id, decision map (`approved`/`rejected`), decision id, expected revision/fingerprint, dry_run | capabilities decision | destructive=true; confirmation required; preserve exact idempotency/CAS behavior. Dry-run computes proposal changes without decision commit. |

Confirmation semantics: Tool descriptions and schema explain impact. Server returns a `confirmation_required` preview with normalized target and short-lived one-time confirmation nonce bound to client identity, operation, canonical payload hash, and expected revision. The caller must explicitly re-invoke with nonce; no natural-language confirmation inferred from a prior prompt. Nonce expires in 2 minutes and is single use. Dry-run never consumes confirmation or creates side effects. Alias save may be treated as configuration-changing but retain confirmation because it affects routing; publish/model/provider enable and cooldown clear always require it. Provider tests/refresh are outbound and require explicit confirmation too.

## Deterministic errors and resource limits

Return MCP `isError=true` plus structured `{code, retryable, message, remediation, request_id}`. Stable codes: `AUTH_REQUIRED`, `SCOPE_REQUIRED`, `READ_ONLY_MODE`, `CONFIRMATION_REQUIRED`, `CONFIRMATION_EXPIRED`, `CONTRACT_VERSION_UNSUPPORTED`, `INVALID_ARGUMENT`, `NOT_FOUND`, `REVISION_CONFLICT`, `RATE_LIMITED`, `UPSTREAM_TIMEOUT`, `UPSTREAM_UNAVAILABLE`, `DRY_RUN_UNSUPPORTED`, `RESPONSE_TOO_LARGE`, `INTERNAL`. Never include raw provider response/error text or secret-bearing request fragments. HTTP 401/403/404/409/429/5xx map consistently; retryable only for rate limit/transient timeout/upstream errors. Include remediation, e.g. “re-read state and retry with current expected_revision.”

Timeouts: connect 2s, normal read 10s, write 15s; provider test/refresh ceiling 30s and report timeout without leaking upstream body. MCP request cancellation propagates; no unbounded retries. State/leaderboard/capabilities paginate with bounded max page size (500), total omitted when expensive, opaque cursor bound to snapshot revision + sort key; reject malformed/expired cursor deterministically. Cap serialized response at 1 MiB; return `RESPONSE_TOO_LARGE` with pagination guidance. Never cache credential-bearing data. Version/health max 3s.

## Acceptance matrix

| ID | Test | Expected |
|---|---|---|
| A1 | Read tools with valid owner:read bearer | Return allowlisted data; no provider secrets, cookies, prompts, responses, or auth headers in output. |
| A2 | Read without token / invalid / revoked token | `AUTH_REQUIRED`; no owner state exposed. |
| A3 | Write with read-only scope | `SCOPE_REQUIRED`; state unchanged. |
| A4 | Write in local `--read-only` | `READ_ONLY_MODE`, and verify no upstream request or state change. |
| A5 | Attempt public/non-private source IP using valid token | Gateway denies; token is unusable outside configured loopback/private LAN boundary. |
| A6 | Cookie-only request from MCP / Bearer request with hostile Origin | Cookie-only rejected for machine auth; bearer path does not require browser Origin but still enforces Host/forwarded checks; hostile forwarded headers rejected. |
| A7 | Owner console machine-key create/list/rotate/revoke | Create accepts name + `read`/`write`, displays key once; list exposes display-id/name/scope/last-used only; storage is hash-only; rotate/revoke invalidate immediately. |
| A7a | Revoked-key request immediately after revoke | `AUTH_REQUIRED`; no grace window, cached authorization, or state leakage. |
| A7b | Read-scoped key invokes each write tool | Every write tool returns `SCOPE_REQUIRED`; gateway state and upstream call counts remain unchanged. |
| A7c | Key-value leakage scan | Machine key absent from every state/status/log/error surface; grep redaction tests over captured fixtures; no key material in MCP tool arguments or results. | 
| A8 | Alias save/publish dry-run | No config revision change, no upstream call; normalized diff returned. |
| A9 | Mutating tool without explicit confirmation nonce | `CONFIRMATION_REQUIRED`; no side effect. |
| A10 | Wrong/expired/replayed/cross-client confirmation | Denied; one-time nonce bound to canonical payload/client and never enables another operation. |
| A11 | Stale expected revision/fingerprint | `REVISION_CONFLICT`; no partial write; actionable re-read instruction. |
| A12 | Provider test/refresh | Requires write scope + confirmation, bounded upstream call; outputs only redacted status/counts; timeout handled. |
| A13 | Clear cooldown request includes probe alias or arbitrary action | Schema rejects; no probe runs. Clear only named provider circuits. |
| A14 | Capability approve/reject exact replay | Existing decision idempotency preserved; mismatched reuse/stale CAS fails without extra mutation. |
| A15 | Malformed ids, unknown aliases/providers/actions | Stable `INVALID_ARGUMENT`/`NOT_FOUND`; no traceback or raw body. |
| A16 | Large state/leaderboard/capability dataset | Cursor pagination stable per revision; bounded pages/bytes; malformed cursor rejected. |
| A17 | Prompt/secret leakage canary | Seed canary in simulated upstream errors/credential data; assert absent from MCP result, server log, audit log, state and failure trace. No prompt input schema accepted. |
| A18 | Contract/version mismatch | Unknown contract blocks writes; compatible pinned version reads; mismatch reports supported version. |
| A19 | Audit verification | Each attempted mutation (success, denied, dry-run) has identity/action/target/result; no payloads/secrets/prompts. |
| A20 | Transport | stdio only; no TCP listener opened; configured upstream URL restricted to loopback by default. |
| A21 | API-client key confusion | `/v1` model key cannot authorize owner routes; owner automation token cannot be used as model API key. |
| A22 | MCP annotations | Reads marked read-only/idempotent; config and outbound actions marked non-read-only; destructive actions flagged and surfaced for confirmation. |
| A23 | Compatibility regression with owner-auth candidate 53918c1 | Human cookie/session console continues; machine bearer scope enforced independently; cookie CSRF/Origin policy unchanged. |
| A24 | Pinning | MCP startup verifies server release/contract; unknown breaking version fails closed for writes. |

## Source references

All paths/lines are from pinned `origin/main` SHA above. Route table and existing endpoints: `src/gateway/http/app.py:407-454`. Trusted LAN anonymous baseline: `src/gateway/auth.py:85-89`, `src/gateway/http/app.py:563-572`. Security Host/Origin/forwarded policy: `src/gateway/http/security.py:65-107`. State/leaderboard/capability handlers: `src/gateway/http/app.py:988-1008`, `1010-1025`, `1044-1065`, `1174-1182`, `1237-1250`. Provider mutations: `src/gateway/http/app.py:1636-1713`, cooldown clear `1781-1800`; alias save/publish `1945-2029`; capability decisions `1458-1515+`. Provider tests and alias tests can contact upstream: `src/gateway/http/app.py:1657-1665`, `2041-2052`. Owner-auth candidate `53918c1` (not the `origin/main` baseline) documents separate owner cookie and client key: `docs/OWNER_AUTH.md:11-49`, `91-100`; candidate explicitly states not deployed at `docs/OWNER_AUTH.md:7-9`.

---

## Addendum — the MCP contract version (not part of the vendored spec)

Added 2026-11-06 by card `t_d43744ae`. This section is the gateway-side
half of acceptance items **A18** (contract/version mismatch) and **A24**
(pinning) above: the pin is an integer contract version the gateway
publishes, not a git SHA.

### What is published

`mcp_contract_version` (integer, starting at `1`) is part of the release
identity, so it appears on every surface that carries that identity:

| Surface | Path |
|---|---|
| public release identity | `GET /version` |
| owner state (what `gateway_get_version` reads through `/version`, and `state.release`) | `GET /owner/api/state` |

It is a single module-level literal, `MCP_CONTRACT_VERSION` in
`src/gateway/buildinfo.py`. No other module defines it, it is never read
from `release.json`, from the environment, or from any other file, and
nothing in this repository increments it. An automatic bump would be an
automatic widening of the write gate and is forbidden.

### The four gated payload families

The label increments **only** when one of these four read payloads changes:

| # | Family | Endpoint |
|---|---|---|
| 1 | `version` | `GET /version` |
| 2 | `state` | `GET /owner/api/state` |
| 3 | `leaderboard` | `GET /owner/api/leaderboard` |
| 4 | `capabilities` | `GET /owner/api/capabilities` |

"Changes" means a key added, removed, or re-typed at **any** nesting depth
of those payloads — not merely a required-key set change. A nested addition
such as `state.providers[].max_in_flight` is exactly the class of change a
presence-only check cannot see, and it counts.

### How the rule is enforced

* `tests/test_mcp_contract_guard.py` captures the four families as
  `key-path -> type` maps and compares them against the newest **committed**
  snapshot in `tests/fixtures/mcp_contract_baseline.json` that still differs
  from the current schema. A differing schema with an unchanged constant
  fails: that is a gated change that shipped without a bump. The snapshot is
  read from git history rather than from the working tree, so regenerating
  the file cannot hide a change from the guard.
* The guard is proven by mutation on every candidate that touches it: change
  one field of a family → RED; increment the constant → GREEN; restore the
  tree → clean.
* Refreshing the snapshot is `PYTHONPATH=src python3
  scripts/capture_mcp_contract.py`; that script writes the snapshot only and
  never touches the constant.
* The package side (`margosmark/gateway-owner-mcp`) refuses writes when the
  gateway reports no contract version, a lower one, or a higher one, with
  `CONTRACT_VERSION_UNSUPPORTED`. Absent and unknown both fail closed.
