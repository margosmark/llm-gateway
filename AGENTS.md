# Personal LLM Gateway — agent instructions

This is the repository-level instruction file for every coding agent. The
full developer policy is in
[`.opencode/agents/gateway_dev.md`](.opencode/agents/gateway_dev.md); read it
before changing this project. If instructions conflict, choose the safer
boundary and flag the conflict.

**If you are starting with no context, read this file, then
`docs/PROVIDER_CONTRACTS.md`.** That second document holds the upstream
details that are not derivable from the code, and every one of them was
learned the hard way.

## Read first

1. `README.md` — product/operator contract and known limitations.
2. `docs/PROVIDER_CONTRACTS.md` — **the upstream request and response
   contracts, and the traps.** Read this before touching an adapter.
3. `docs/ARCHITECTURE.md` — actual component and data-flow map.
4. `docs/DEVELOPMENT.md` — verification and release discipline.
5. `docs/REVIEW.md` — the finding register. Do not treat a finding as fixed
   without a code change and regression evidence.
6. `docs/TRUSTED_LAN_ADMIN_MODE.md` — the approved product boundary and its
   acceptance criteria.
7. `docs/CHATGPT_CODEX_CONTRACT.md` — the ChatGPT/Codex contract in detail.
8. `docs/RUNBOOK.md` — operator procedures.
9. `docs/TODO.md` — the agreed plan of open work, with what "done" means
   for each item.

## What this product is

A **single trusted-LAN appliance**. One explicit private LAN listener serves
the owner console, the configuration API, the OpenAI-compatible API, and the
release identity. There is no passphrase, no login, no session cookie and no
second listener.

```text
http://10.10.11.109:8765/owner/     owner console
http://10.10.11.109:8765/v1/        OpenAI-compatible API
http://10.10.11.109:8765/version    release identity
```

**Any device that can reach the configured address is an administrator and a
caller, by product decision.** State this plainly in the console and in
operator docs. Do not imply that a password or a profile restricts
administrative access, and do not reintroduce one.

## Non-negotiable constraints

- This is private LAN infrastructure. Bind only explicit private or loopback
  addresses. Never add wildcard/public binds, router/firewall rules, or a
  public reverse proxy.
- Do not restore an owner passphrase, login/session bootstrap, a separate
  setup listener, `GATEWAY_SETUP_BIND`, an SSH-tunnel requirement, or the
  `:8766` port. `8766` is reserved and rejected at argument parsing.
- Preserve forwarded-header rejection and canonical `Host`/`Origin` checks.
  Cross-site browser mutations must still be refused; do not invent a
  proxy-trust model.
- Never access, print, commit, fixture, or log real credentials, tokens,
  device codes, prompts, completions, or provider account identifiers.
- Keep credentials and caller-token hashes out of `GatewayConfig`, status,
  ordinary state responses, URLs, release metadata, and the request ledger.
- No implicit provider calls: connection tests, catalogue refreshes, alias
  tests, and device authorization are explicit owner actions.
- Provider entitlement, limits, reset times, and undocumented model
  capabilities are **Unknown** unless backed by official evidence.

## Engineering rules

- Use adapters for provider-specific behavior. Every `list_models()` method
  returns `list[ModelInfo]`; test it through the generic refresh handler.
- A converted request must receive a matching converted response on **both**
  the buffered and the streaming path, or the request must be refused.
- **A capability and a preference are different things.** Dialect, streaming
  and function tools are hard: an unsupporting route is excluded. A
  `reasoning_effort` is a preference: the route stays eligible, the effort is
  dropped for that route, and the substitution is recorded. Never silent.
- **An SSE frame is its data lines plus the blank line that ends it.** Test
  stream framing with a strict parser, not with `curl`.
- **The alias never reaches the provider.** The router replaces the payload's
  `model` with the upstream model id of the chosen route.
- Device authorization needs trusted-console enforcement, short-lived
  in-memory pending state, a shown-once user code, an opaque poll handle, and
  interval/lifecycle tests. The provider's own device auth id never leaves
  the process.
- Validate identifiers, routes, enum values, and ordering before persistence.
  Profiles deny access by default; any all-alias access requires explicit,
  reviewable opt-in.
- Do not forward caller-controlled upstream session identifiers without
  server-side validation, profile/provider namespacing, and expiry. The
  derivation lives in the router, so every path that reaches a provider
  carries one.
- Fallback stays explicit and fail-closed. Never fall back after streams,
  possible tool side effects, ambiguous delivery, or an unapproved error
  class. A **skipped** primary is recorded with its reason, so a cooldown
  cannot look like a silent failover.
- Treat observability as metadata only: no prompt, completion, token, or
  secret content. Do not claim data is recorded unless the implementation
  proves it.
- A reported limit is the **smallest applicable constraint**, and Unknown if
  any eligible route's limit is not published. Report it; do not enforce it —
  the gateway has no tokenizer.

## The commands

```sh
# tests
PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 -m compileall -q src tests

# the console's own JavaScript regressions
node tests/console_dom_test.js

# build and release (one command; stamps the identity and verifies it)
python3 scripts/release.py --build --image personal-llm-gateway:candidate

# the release gate against an exact candidate
python3 scripts/release_gate.py personal-llm-gateway:candidate
```

`scripts/release.py --build` builds from the checkout with the source
revision and tree, deploys it, writes the image digest into the state
volume, and verifies `/version` reports it. **If `/version` says `unknown`,
the deployment was not released properly** — the gateway also warns about
this at startup.

## Verification and release

- Add a regression test for every defect, including refusal-path tests for
  security and state-boundary changes.
- Never require real credentials or provider access in tests.
- A release requires an exact candidate image, no post-gate rebuild, and a
  Compose-equivalent first-boot/restart proof with the actual state volume,
  non-root UID/GID `10001`, intended listener binds, and `/version` identity.
  A disposable candidate gate alone is insufficient.
- Probe an unclear upstream contract from **inside the container**, using the
  stored credential, printing only status codes and field names. See
  `docs/PROVIDER_CONTRACTS.md`.
- Update the README, runbook, architecture/contract docs, and review register
  whenever behavior, security boundaries, durability, UI, or release steps
  change.

## Where things are

| Path | What |
|---|---|
| `src/gateway/cli.py` | entry point; bind rules; the reserved-port check |
| `src/gateway/http/app.py` | the application: routes, console API, the API surface |
| `src/gateway/http/security.py` | forwarded-header rejection, `Host`/`Origin`, CSRF helpers |
| `src/gateway/http/core.py` | the HTTP layer, chunked streaming |
| `src/gateway/core/routing.py` | the routing core: eligibility, fallback, attribution |
| `src/gateway/core/capabilities.py` | the capability model and the limit rules |
| `src/gateway/bridge/` | Responses <-> Chat Completions, buffered and streamed |
| `src/gateway/adapters/` | the provider seam |
| `src/gateway/session.py` | the derived upstream conversation identifier |
| `src/gateway/identifiers.py` | the identifier grammar |
| `src/gateway/routes.py` | route validation |
| `src/gateway/logging.py` | the metadata-only access log |
| `src/gateway/static/` | the owner console |
| `data/go_catalog.json` | the dated OpenCode Go catalogue snapshot (generated) |
| `scripts/` | the snapshot builder, the release gate, the release step, the walkthrough |
| `tests/` | the suite; `test_end_to_end.py` is the main harness |

## The traps

Five upstream assumptions that each cost hours, all now fixed and all
covered by a regression test. They are listed in full in
`docs/PROVIDER_CONTRACTS.md`:

1. The SSE frame terminator belongs to the frame.
2. A capability and a preference are different things.
3. The alias must never reach the provider.
4. A converted request needs a converted response, on both paths.
5. A route can be skipped without being tried — and must be recorded.

## The review register

`docs/REVIEW.md` holds every finding, with its evidence. R0–R24, R28 and
R29 are closed or superseded; **R25, R26 and R27 are open** and are
planned in `docs/TODO.md`. **Add a finding when a defect is found**, and
close it only with a cited fix and a regression test — the register is how
the next agent avoids repeating the same work.
