# Model capability discovery with owner-approved metadata

**Status:** implementation-ready proposal; research/spec only. No application files, provider accounts, credentials, or production systems were changed or called.  
**Code baseline inspected:** candidate `a3d4046a6c8d93c0baa487309161342eb438f8f2`, tree `35a0aab39676f1d2972abb6122febc11ef6b047e` (2026-10-28).  
**Scope:** opt-in per-model probes for text, image understanding, and function-tool call (without execution); explicit, field-by-field approval for metadata changes. Phase 2: streaming, structured output, reasoning controls.

## 1. Executive summary

Add a manual **Discover capabilities** action to the existing Models & Routes workflow. It previews the exact provider/model, synthetic test cases, maximum request/output envelope, and price-based spend estimate before any probe is sent. Nothing probes on page load, startup, catalogue refresh, or routine inference. Results are evidence records—not guarantees—and do not affect routing or stored metadata until the owner reviews a field-level diff and explicitly approves selected fields.

The gateway already stages discovered models, supports explicit provider catalog refresh, stores model metadata in `config.json`, and routes using per-model capability fields. The missing product is a test runner plus durable, reviewable evidence and an approval transaction. **Current console has no owner identity/authentication:** the repository explicitly calls every reachable trusted-LAN peer an administrator; canonical Host/Origin and forwarded-header checks mitigate browser cross-site requests but do not authenticate an owner. Do not describe the resulting action as individually authenticated owner approval until that boundary is decided and implemented (see §11).

## 2. Baseline facts and relevant behavior

Reviewed against the exact baseline SHA above; references are repository-relative.

- `src/gateway/config.py`: `ModelEntry` stores `source`, `observed_at`, dialect, image/function/streaming flags, reasoning effort levels, limits, and notes. `ProviderState` stores model rows and `catalog_observed_at`. `GatewayConfig.revision` is monotonic; `ConfigStore.mutate()` serializes under an in-process lock, increments revision, and atomically persists configuration.
- `src/gateway/http/app.py`: `POST /owner/api/provider` accepts explicit provider actions including `refresh`; refresh calls `adapter.list_models()`, updates catalog observation time, and reconstructs each model row. It preserves only `enabled` and a previous dialect when the new dialect is empty; other discovered fields come from the adapter on each refresh. `POST /owner/api/model` supports enable/disable/manual; manual changes dialect, enabled and notes, not image or other capability fields. `/owner/api/state` exposes staged model metadata and revision. Current owner routes use `_require_mutation`; in this trusted-LAN build it is a no-op.
- `src/gateway/static/console.js`: Providers includes explicit **Refresh model catalog**; Models & Routes lists staged models and has enable/disable and manual-add controls. State is refreshed after writes. No discovery/probe or metadata-approval UI exists.
- `src/gateway/core/routing.py`: streaming, function tools, built-in tools and image input are hard candidate exclusions when request shape requires them. A false flag may change route eligibility; an unknown/improperly defaulted false can also exclude. Reasoning effort is checked separately and may be dropped with a recorded degradation.
- `src/gateway/core/capabilities.py` and provider adapters: model facts are assembled from provider/catalogue observations and curated snapshot/documentation. Image documentation evidence is currently exact-slug and source-backed; startup `apply_image_evidence()` promotes only, creates no rows, and does not downgrade/remove. This special-case reconciliation is not a general approval or metadata-precedence system.
- `src/gateway/adapters/{anthropic,chatgpt_codex,opencode_console,opencode_go}.py` implement provider-specific catalogue and request protocols. OpenCode Go’s snapshot and Codex’s exact-slug documentation map supply some facts beyond live catalogue fields; Anthropic’s provider contract explicitly leaves unsupported facts unknown absent evidence.
- `src/gateway/http/security.py` rejects forwarded headers, requires the configured Host, and enforces canonical Origin for browser mutations. `docs/ARCHITECTURE.md` and README describe one trusted-LAN listener with no owner login/session; any reachable peer is an administrator. This is not an owner-authentication boundary.
- `tests/` already exercises HTTP handlers with fake adapters and isolated state. Extend that fixture approach; do not use real credentials, live upstreams, or production state for tests.

### Existing metadata precedence (as implemented, not proposed)

1. Adapter response and adapter-owned curated/catalogue sources assemble `ModelInfo`.
2. Explicit refresh writes `ModelInfo` fields into the persisted `ModelEntry`; latest refresh overwrites most fields, preserving `enabled`, and preserving dialect only if newly discovered dialect is empty.
3. Explicit manual model action merges only supplied manual values (enabled/dialect/notes), leaving other fields as-is; source is set to `manual`.
4. On startup, the exact-slug documented image evidence helper may promote stored false to true. It does not handle general fields and it never revokes a true value.

This is not a uniform precedence contract. Capability discovery must not piggyback on refresh’s overwrite behavior or silently alter this baseline.

## 3. Product contract and states

### Opt-in and target scope

- Entry point: a **Discover capabilities** button on an exact provider-qualified model row. Disabled until provider is configured and model exists in staged state. Never probe a bare model id; same string may mean different things at different providers.
- Default is **not tested**. No background schedule, startup probe, catalogue-refresh probe, or inference-time learning. Do not automatically retry.
- Scope each run to one exact `(provider_id, model_id)` and a fixed, versioned probe suite. An owner can choose individual MVP probes and can cancel before sending. Do not probe all models in bulk in MVP.
- Probe only capability behaviors implemented by an adapter’s explicit supported test protocol. Unsupported adapter/test combinations stay **not available**, not negative.

### Evidence/result vocabulary

Per capability, use one of: `documented_supported`, `documented_unsupported`, `observed_supported`, `observed_unsupported`, `inconclusive`, `not_tested`, `conflicting`. Keep documentation and observation as separate evidence rows; neither erases the other. `observed_*` is scoped to the exact provider/model, method, probe version and time. A timeout, transport failure, 429/rate limit, auth/policy failure, invalid response, malformed probe, ambiguous refusal, or an unusable answer is **inconclusive**, never evidence of unsupported capability. Only a valid, capability-specific, unambiguous provider refusal on a well-formed synthetic probe may qualify as observed unsupported. A valid observed success can qualify as observed supported.

Never represent an inconclusive or absent probe as `false`. Existing boolean router metadata is not an evidence state: retain `unknown`/absence in the evidence layer and require owner decision for promotion/demotion. A successful probe says only that the tested request worked at that time; it does not promise future service, availability, plan entitlement, behavior for other inputs, or provider stability.

### MVP probes

1. **Text generation:** one deterministic synthetic request asks the model to emit a generated per-run marker in an exact bounded format. Success requires a valid response matching the expected marker and a non-error completion. Do not store the marker or raw output. This is only evidence for basic text-response behavior, not a quality benchmark.
2. **Image understanding:** generate a tiny, non-user-derived test image locally in memory with a simple unambiguous visual question (e.g., count/color of a small set of shapes). Require an answer matching expected visual contents; mere HTTP acceptance or a response containing any text is not success. Explicit image-input unsupported errors may be negative; wrong/ambiguous answers, generic refusals, transport/rate/auth errors are inconclusive. Do not fetch arbitrary image URLs or accept owner-supplied image URLs in MVP. Send inline synthetic image bytes only through an adapter that safely supports this format; never write the image to logs/state/artifacts.
3. **Function-tool call, no execution:** send a fixed synthetic prompt and one harmless function schema with a unique synthetic argument. Use provider-native forced tool choice only where the adapter contract supports it; otherwise classify a non-call as inconclusive, not unsupported. A valid returned tool-call name and arguments count as observed support. **Never invoke, evaluate, follow, or execute the returned function**, and never use tools that can cause side effects. Do not count a mere echoed tool schema or HTTP 200 as success.

All test names, system/user text, image generation recipe, tool schema and expected values are gateway-owned static templates with a suite version. No conversation text, prompts, image, tool data, provider credential, or arbitrary user-provided content enters the probe.

## 4. Preview, request bounds, and spend

Before sending any probe, render a preview and require a separate explicit **Run selected probes** confirmation. Preview shows provider label and exact model slug; capabilities selected; fixed synthetic case descriptions; number of upstream requests (MVP maximum 3 per run); adapter/probe suite version; input/output ceilings; timeout; and pricing source/as-of date and worst-case estimate where a reliable per-model price is available.

- Set strict per-request input-size/image-byte, max-output-token, connect/read timeout, total-run deadline and concurrency=1 limits. No redirect, no retry, no failover, no fallback, no streaming in MVP.
- Enforce an owner-selected total estimated spend ceiling. Require a trustworthy price and token estimate to make a paid probe eligible. If price or tokenization is unknown, do not call the provider; explain that a hard spend ceiling cannot be established. A preview estimate is not a provider bill or guarantee: document provider-side minimums/hidden charges and show this limitation before confirmation.
- MVP target maximum: at most one request per selected capability and three total; do not increase retry count when a probe fails. Any optional diagnostic repeat is a new preview and confirmation.
- Do not rely on a prompt-only output cap. Enforce the adapter’s API output limit where available; reject a protocol with no enforceable output/size/time bounds unless a safe cap can be implemented.
- Probe failures must not alter provider connection state, model enablement, aliases, fallback policy, cooldowns/circuits, request analytics, or published catalogue. Store only bounded non-content metadata and evidence classification.

**Open product decision:** whether the spend cap is a conservative estimate only or a hard ceiling. The gateway cannot guarantee provider billing unless the provider enforces a usable account/project limit and returns sufficiently complete usage/price facts. Product copy must not claim a guaranteed cap on estimate alone.

## 5. Evidence and metadata proposal data model

Keep durable evidence/proposals separate from router-effective `ModelEntry` metadata. Suggested JSON-compatible records (SQLite is also acceptable if the project selects one persistence strategy; see §11):

```json
{
  "schema": 1,
  "runs": [{
    "run_id": "opaque-id",
    "provider_id": "exact-provider-key",
    "model_id": "exact-upstream-slug",
    "started_at": "UTC RFC3339",
    "finished_at": "UTC RFC3339",
    "trigger": "owner_console",
    "suite_version": "mvp-1",
    "results": [{
      "capability": "image_input",
      "status": "observed_supported|observed_unsupported|inconclusive|not_tested",
      "method": "synthetic_inline_image_understanding",
      "evidence_kind": "observed",
      "evidence_summary": "bounded sanitized outcome only",
      "request_count": 1,
      "reported_usage": {"input_tokens": null, "output_tokens": null},
      "spend_estimate": {"amount": null, "currency": "USD", "source": null, "as_of": null},
      "error_class": null
    }]
  }],
  "proposals": [{
    "proposal_id": "opaque-id",
    "provider_id": "exact-provider-key",
    "model_id": "exact-upstream-slug",
    "base_revision": 123,
    "base_model_fingerprint": "digest-of-canonical-row",
    "created_at": "UTC RFC3339",
    "expires_at": "UTC RFC3339",
    "status": "pending|partially_approved|approved|rejected|conflicted|expired",
    "fields": [{
      "field": "supports_image_input",
      "current_value": false,
      "proposed_value": true,
      "evidence_refs": ["run-or-document-evidence-id"],
      "evidence_source": "URL or exact observed probe method/time",
      "classification": "documented|observed",
      "routing_consequence": "May make this route eligible for image-bearing requests; alias routes/fallbacks may change.",
      "decision": "pending|approved|rejected"
    }]
  }],
  "approvals": [{
    "proposal_id": "opaque-id",
    "provider_id": "exact-provider-key",
    "model_id": "exact-upstream-slug",
    "field_decisions": [{"field": "supports_image_input", "decision": "approved|rejected"}],
    "approved_at": "UTC RFC3339",
    "actor": "authenticated owner principal or explicit trusted-LAN actor marker",
    "resulting_revision": 124
  }]
}
```

Store no raw request/response, synthetic marker, image bytes, function arguments returned by a model, credentials, auth headers, cookies, or unbounded upstream error bodies. Store at most normalized capability outcome, safe error class, evidence URL/method, exact provider/model, timestamp, version, limits/usage metadata, and bounded sanitized summary. Validate and bound every persisted/provider-derived string; escape in console via text nodes. Evidence URLs are metadata, never fetched by the gateway as part of discovery.

Evidence retention should be bounded (recommend 90 days for runs/proposals; approved provenance retained as long as the corresponding override exists). Add a local bounded/rotated store with atomic writes, private permissions, and corruption/version behavior; do not put probe content in request ledger. Final retention period and storage mechanism are decisions in §11.

## 6. Per-field diff, approval, and refresh conflict semantics

- After a run, create a **proposal only**. Each changed capability field is a separate row with: current effective value and provenance; proposed value and source/evidence; documented-vs-observed label, time and exact model; and potential routing consequences. Include unchanged/unknown/conflicting evidence separately; do not fabricate a proposal value from an inconclusive result.
- The owner can select any subset of fields to approve and explicitly reject others. Actions: **Approve selected changes**, **Reject selected changes**, **Cancel**. No default-selected fields. Approval does not imply approval of unselected rows. Rejection is durable and does not change effective metadata. Refreshing the page or restarting restores the same pending proposal and its decisions until expiry or resolution.
- Proposal generation and approval never auto-enable/disable a model, publish an alias, edit a route/fallback/phase policy, change cost/limits, or run a probe. On approval, update only selected capability fields and their provenance. Do not overwrite unrelated fields or bump model `enabled`, provider state, alias state or routes. No routing change occurs except the consequences of an owner-approved capability value on future route eligibility; show a before/after impact preview before the final approval click.
- Persist approved override separately from the latest discovery evidence. Recommended effective precedence **per field**: (1) explicit owner-approved override; (2) exact-slug dated/curated documented source; (3) valid live provider catalogue field; (4) prior value only where refresh contract explicitly preserves it; otherwise unknown. Observed probe results are evidence offered for approval, not an automatic precedence layer. An owner-approved value remains effective across catalog refresh, restart and evidence refresh until the owner explicitly changes/revokes that override. A newer contradictory observation creates a new proposal and conflict warning; it never silently wins.
- Refresh writes new catalogue evidence but must not erase pending proposals, prior approved provenance, or owner overrides. Update catalog timestamps/snapshots independently. A model disappearing from a catalog does not delete metadata, routes, approval history, or model row; report missing/deprecated status for owner resolution.
- Proposal approval uses compare-and-swap: carry the exact `base_revision` and a canonical fingerprint of that exact provider/model row. Under the config transaction lock, require both current revision and row fingerprint to match; if either changed since preview, reject with `409 conflict`, make no write, retain evidence, mark proposal conflicted, then regenerate a current diff. This prevents stale tabs and concurrent refresh/manual actions from clobbering owner data. Apply selected fields and provenance in one atomic commit; audit resulting revision.
- Idempotency: approval/rejection requests include proposal id plus a unique decision id; replay returns the prior result without applying the fields again. A second conflicting decision for the same field returns conflict. Approval across multiple fields is atomic for selected fields; on validation/persistence failure none are applied.
- Explicit removal/reversal of an approved override is a separate owner mutation with old/new values and impact preview; never infer revocation from refresh. Preserve provenance of superseded/rejected values.

### Capability impact copy

For every proposed routing capability change, calculate effects using the current alias/route graph before approval. Show exact aliases and route positions affected, e.g. “If approved, `provider → model` can become eligible for image requests. Existing published aliases using this route may select it for future image-bearing requests; no alias or fallback order changes.” If the result cannot be computed reliably, say “Routing impact unknown; no automatic policy change” and require explicit acknowledgement. For negative capability, show that the route may be excluded for affected request shapes and whether no eligible route may remain. Never promise which route will serve; runtime eligibility/failure can vary.

## 7. API and console design

Proposed endpoints under current `/owner` mount; all new mutation endpoints must pass through shared owner mutation/security gate and canonical Host/Origin/forwarded-header checks, enforce request size/content type, and use revision/fingerprint CAS. Exact paths can be folded into existing provider/model action handlers if that gives one consistent policy path.

| Method/path | Behavior |
|---|---|
| `GET /owner/api/capabilities?provider_id=…&model_id=…` | Read stored evidence, current effective field provenance, pending proposals and last-run summary. No provider call; no secret/content. |
| `POST /owner/api/capabilities/preview` | Validate exact provider/model, selected MVP capability ids, suite version and owner spend limit; return deterministic preview and maximum bounds. **No provider call and no persisted proposal.** |
| `POST /owner/api/capabilities/run` | Require explicit confirmation token bound to preview hash, exact model, chosen probes, expiry and base revision. Execute only those bounded synthetic probes; persist metadata-only run record and pending proposal. No inference routing or retries. |
| `POST /owner/api/capabilities/{proposal_id}/decision` | Require exact selected field decisions, expected revision/fingerprint, decision id and explicit confirmation. Revalidate diff and impacts under lock; atomically approve/reject selected fields. No arbitrary metadata patch. |

Success returns run/proposal ids, safe statuses, exact target, timestamps, request counts, safe error class and resulting revision. Conflict is HTTP 409 with current revision and a refresh/re-preview instruction; never silently rebase approval. Validation is 400; unknown provider/model 404; unsupported probe adapter 422; run in progress 409; price/bounds unavailable 422; rate limit 429. Error bodies contain no upstream response body, credential, prompt or raw exception string.

Console: model row has **Discover capabilities**; preview modal precedes **Run selected probes**; results table separates Documented / Observed / Unknown and shows time/method; a diff review shows each field as an independent checkbox (none selected), source/evidence, current/new values and routing impact; final decision confirmation names model, fields, and aliases affected. Pending state is durable across state refresh, reload and restart. All controls keyboard operable; status updates announced accessibly. Use existing safe DOM `textContent`/text-node path; never `innerHTML` with evidence/provider text.

## 8. Security, privacy, and threat model

| Threat | Required control |
|---|---|
| Cross-site attacker triggers paid probe or metadata mutation | Same canonical Origin, Host validation, forwarded-header rejection and shared owner mutation gate; no GET side effects; explicit POST confirmation. Because current server has no owner identity/session, resolve §11 before calling this owner approval. |
| Non-browser LAN peer or compromised trusted-LAN device | Current baseline permits any reachable peer to administer. Either accept/document that trust boundary explicitly or add owner authentication; Origin alone does not protect direct HTTP clients. Do not imply authenticated approval under current state. |
| Run-trigger abuse / spend exhaustion | Opt-in only, preview-bound confirmation, concurrency=1, provider/model allowlist, request/input/output/deadline/spend caps, no retries/fallback, per-peer/API rate limit, bounded run queue/storage. Avoid automatic probes. |
| Probe content sent to provider / PII leakage | Generate fixed synthetic text/image/tool payloads internally; no user conversations; no arbitrary image URL; no raw image/output persistence; disclose provider will receive synthetic probes before confirmation. |
| Provider/model response injection or malicious strings | Treat all provider text/source labels as untrusted; parse only expected response schema; length/field allowlists; render as text, not HTML; never execute tool call; do not follow returned URLs. |
| False capability claim / noisy result | Exact provider+slug+method evidence; separate documented and observed states; inconclusive class; no family/dialect inference; explicit owner approval and no auto-routing updates. |
| Stale proposal / concurrent refresh or manual edit | CAS on global revision plus exact row fingerprint; atomic per-field writes; conflict requires new preview/diff. |
| Secret leakage in logs, analytics, exports, errors | Metadata-only run records; redact headers and response bodies; no probes in request-content paths; tests search sentinel prompt/secret/image bytes across state, logs, console, analytics, backups and errors. |
| SSRF via image test or evidence link | Generate inline image locally; prohibit external URL sources; never fetch evidence links; adapter endpoint remains fixed/configured and redirect policy unchanged. |
| Approval CSRF or forged confirmation | Confirmation token is opaque, short-lived, single-use, server-bound to preview and selected fields; canonical origin/host; mutation gate; consume atomically. Do not treat client-provided `confirmed: true` alone as evidence. |
| Capability downgrade causes outage | Preview alias/route impact; require explicit field selection; no automatic negative values; conflicts preserve state; allow owner-only explicit reversal with provenance. |

## 9. File ownership and implementation boundaries

Spec-only ownership map for the implementation team; no code edits were made.

| Area | Likely paths | Boundary |
|---|---|---|
| Capability evidence schemas, statuses, probe suite definitions and field whitelist | `src/gateway/core/capabilities.py` or new `src/gateway/capability_discovery.py` | Domain types and validation only; no provider-specific calls or routing side effects. Decide before implementation whether to expand core module or isolate discovery. |
| Provider-specific safe synthetic probe transport/capability methods | `src/gateway/adapters/base.py`, each selected `src/gateway/adapters/<provider>.py` | Adapter allowlist only; fixed endpoints, bounded synthetic requests; no generic arbitrary URL/body facility. Do not probe an adapter until its semantics/limits are reviewed. |
| Probe store, proposal store, CAS/atomic persistence | new `src/gateway/capability_store.py` (or existing `ConfigStore` only if evidence data is included there deliberately) | Separate evidence lifecycle from active config; file mode 0600/atomic write or transactional DB; bounded retention. Never co-own provider credential files. |
| Router-effective approved override resolution | `src/gateway/config.py`, `src/gateway/core/capabilities.py`, `src/gateway/cli.py` startup reconciliation | Single documented per-field precedence function; inject approved override into effective config without mutating unrelated data; no observation changes routing. |
| Owner endpoints, authorization, validation, CAS | `src/gateway/http/app.py`, `src/gateway/http/security.py`, perhaps shared owner mutation helper | All endpoints use shared gate; bounded JSON; canonical origin; owner principal decision; proposal confirmation and revision CAS. No lifecycle/production operations in this feature. |
| UI preview, result, diff and impact view | `src/gateway/static/console.js`, `console.html`, `console.css` | Present evidence and distinctions; no provider calls from browser; safe text rendering; no selected changes by default. |
| Fake upstreams, isolated state, acceptance tests | `tests/` (new `test_capability_discovery.py`, extend adapter/security tests) | Synthetic credentials only; deterministic provider fake; no network; exercise concurrency, failure taxonomy, persistence, route impact and non-execution. |
| Developer/product explanation | `docs/PROVIDER_CONTRACTS.md`, `docs/ARCHITECTURE.md`, `docs/RUNBOOK.md` | Document each provider probe contract, cost limitations, precedence, owner security boundary, support lifecycle and manual recovery. |

Avoid edits to `src/gateway/core/routing.py` in MVP if possible: route eligibility already consumes effective capability fields. A change there would expand feature risk; only add integration tests showing effective metadata affects existing behavior after approval.

## 10. Acceptance criteria (testable IDs)

### MVP / product
- **CAP-001 Opt-in:** startup, page load, state polling, ordinary configuration save, catalogue refresh and runtime inference issue zero capability probes. No scheduler or bulk-run action exists.
- **CAP-002 Exact target:** every preview/run/evidence/proposal is keyed by exact provider id and model slug. Same slug under another provider remains untouched.
- **CAP-003 Preview gate:** every run requires a displayed preview and separate explicit confirmation bound server-side to its exact target, probe selection, bounds and expiry. Expired/altered/replayed confirmation sends zero upstream calls.
- **CAP-004 Synthetic-only:** fake-upstream assertions show fixed generated text, generated inline image, and fixed function schema only. Request content, credentials and arbitrary user/provider-supplied URLs are absent.
- **CAP-005 Spend/size/time bounds:** test enforced request count ≤ selected probe count ≤3, no retry/fallback/stream, input/image/output/deadline caps, estimate source disclosure, and refusal without trustworthy price where spend policy requires one. UI never labels an estimate a guaranteed bill ceiling.
- **CAP-006 Probe semantics:** text success requires exact bounded marker; image requires correct visual answer (HTTP 200 alone fails); function success requires valid returned call. Returned function is never executed. Valid explicit feature rejection may be negative; timeout, 429, auth/policy failure, malformed probe/response, wrong ambiguous image answer and generic refusal are inconclusive.
- **CAP-007 Provenance:** every evidence row retains exact provider/model, UTC time, method, probe suite/version, documented-vs-observed distinction, evidence URL or probe outcome, safe status and bounds/usage. No raw prompt, response, image, argument, credential or unbounded error body persists.
- **CAP-008 Non-guarantee:** UI/API/docs distinguish observed success from future availability/entitlement. Unknown, absent, stale and inconclusive evidence are not presented as supported or unsupported.

### Metadata approval / persistence
- **CAP-101 Field-level diff:** proposal enumerates every candidate field separately with current value/provenance, proposed value/source, evidence, classification and routing consequences. No default-selected fields.
- **CAP-102 Explicit choice:** cancel/reject changes no effective metadata; approving one selected field changes only that field plus provenance. Unselected/rejected fields and enabled/provider/alias/route/fallback/settings fields remain byte-for-byte equivalent in semantic value.
- **CAP-103 Refresh persistence:** pending/rejected/approved evidence and provenance survive console refresh, catalogue refresh and process restart. Catalogue refresh does not overwrite owner-approved fields or auto-demote them.
- **CAP-104 Conflict-safe:** two concurrent proposals against one row; stale revision and same-revision changed fingerprint each return 409, change no config, and demand re-preview. Atomic approval of selected fields increments revision once and does not clobber concurrent unrelated edits.
- **CAP-105 Idempotent decisions:** duplicate decision id does not reapply changes; second contradictory decision conflicts. Expired proposal cannot be approved.
- **CAP-106 Routing non-side-effect:** preview/run/proposal creation/rejection causes no routing change. Approved values change only named effective capability fields; existing router then enforces those fields. Tests assert aliases/routes/enablement/order/publication unchanged and check exact before/after eligibility impact.
- **CAP-107 Documented-vs-observed conflict:** conflicting evidence is shown as conflict and neither source silently overrides the other; only explicit selected-field approval changes effective metadata.

### Security and fixture isolation
- **CAP-201 Mutation boundary:** cross-origin, missing Origin, hostile Host, forwarded headers, invalid/expired/replayed confirmation and failed owner policy checks reject with no upstream call and no state change. Include direct non-browser request tests and verify the chosen owner-auth boundary.
- **CAP-202 No leakage:** synthetic prompt, unique marker, fake credential, image-byte sentinel and malicious fake-provider string are absent from config/evidence store/logs/analytics/errors/backup/export/UI source; safe classification and provenance remain.
- **CAP-203 Fake-only:** all integration tests use an isolated temporary state directory and deterministic fake adapter/upstream; test asserts no live network access and never reads production paths or credentials.
- **CAP-204 Error containment:** fake upstream tests timeout, 429, auth/policy denial, malformed JSON, unsupported feature, empty response and noisy error body; no raw upstream payload is surfaced/persisted and no failure is misclassified as negative.
- **CAP-205 Existing behavior:** full suite plus existing image/tool routing tests pass; no lifecycle/release/deployment/production operation is part of implementation or acceptance.

### Phase 2 (explicitly out of MVP)
- **CAP-301 Streaming:** characterize incremental stream support with bounded output, total deadline, cancellation, and terminal-event validation; no claim from buffered success.
- **CAP-302 Structured output:** independently test JSON/schema-constrained output, validate schema/result rather than HTTP acceptance; preserve documented and observed evidence separation.
- **CAP-303 Reasoning controls:** distinguish **reasoning capability/control support** from **reasoning effort vocabulary**. Model booleans/controls such as `reasoning.enabled`, `reasoning.summary`, or provider-specific thinking must be separate dimensions from an enumerated `reasoning_efforts` list. Probe one control at a time, show exact supported names/values; a supported effort does not imply a reasoning toggle and a toggle does not imply named effort levels.

## 11. Outstanding decisions before implementation

1. **Owner identity/authentication (blocking security semantics):** current gateway has no owner login or authenticated principal; any trusted-LAN peer is administrator. Is that accepted as the owner approval boundary for this feature, or must an authenticated owner session be added first? Canonical Origin/Host checks are CSRF/cross-site safeguards, not identity/authentication. Recommend do not claim owner-only approval until settled.
2. **Spend ceiling:** is a source-backed conservative estimate acceptable, or is provider-enforced hard spend ceiling required? Without provider-side enforcement/comprehensive pricing, a strict bill cap cannot be guaranteed.
3. **Evidence persistence:** separate atomic JSON store vs transactional DB vs config-embedded evidence. Recommendation: separate bounded store for run evidence/proposals; active overrides/provenance persisted in config with CAS to align atomic routing state. Confirm backup/export expectations and recovery behavior.
4. **Override scope:** should owner-approved metadata always override newer documented source evidence, with a warning/proposal, or can designated provider policy evidence be immutable/authoritative? Recommendation: preserve explicit owner decision until manually revoked, but show conflicts prominently.
5. **Negative observation policy:** is explicit provider error enough for `observed_unsupported`, or require repeat confirmation? Recommendation: one well-formed explicit capability-specific rejection can be evidence; never use a generic 400/422 or natural-language refusal as negative.
6. **Probe support matrix:** which providers expose enforceable max-output, image payload and forced-tool-call semantics? Build provider-by-provider protocol table before adapter implementation. Exclude any provider lacking safe bounds rather than approximate support.
7. **Retention:** confirm run/evidence retention (recommend 90 days, bounded; override provenance as long as override active) and whether owner can manually purge evidence.
8. **Terminology and UI warnings:** decide wording for “observed supported” and “not guaranteed”; decide whether cost estimates are displayed when usage/pricing is partial (recommend show Unknown, never $0).

## 12. Evidence and verification record

Inspected code and docs from candidate `a3d4046a6c8d93c0baa487309161342eb438f8f2` (tree `35a0aab39676f1d2972abb6122febc11ef6b047e`) on 2026-10-28. Read the model/config and owner API paths (`config.py`, `http/app.py`, `cli.py`), current console (`static/console.js`), capability/router/adapter implementation (`core/capabilities.py`, `core/routing.py`, `adapters/`), and architecture/provider contract docs and fake fixture tests. This is repository evidence, not a live production inspection; no provider call, credential read, production request, code edit, build or deployment was performed.
