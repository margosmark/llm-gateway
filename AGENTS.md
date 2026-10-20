# Personal LLM Gateway — agent instructions

This is the repository-level instruction file for every coding agent. The
full developer policy is in
[`.opencode/agents/gateway_dev.md`](.opencode/agents/gateway_dev.md); read it
before changing this project. If instructions conflict, choose the safer
boundary and flag the conflict.

## Read first

1. `README.md` — product/operator contract and known limitations.
2. `docs/ARCHITECTURE.md` — actual component and data-flow map.
3. `docs/DEVELOPMENT.md` — verification and release discipline.
4. `docs/REVIEW.md` — open findings; do not treat a finding as fixed without
   a code change and regression evidence.
5. `docs/TRUSTED_LAN_ADMIN_MODE.md` — approved single-console product
   boundary and acceptance criteria.
6. `docs/CHATGPT_CODEX_CONTRACT.md` before modifying that adapter or flow.

## Non-negotiable constraints

- This is private LAN infrastructure. Bind only explicit private or loopback
  addresses. Never add wildcard/public binds, router/firewall rules, or a
  public reverse proxy.
- The approved product is one explicit trusted-LAN owner/API listener. Do not
  restore an owner passphrase, login/session bootstrap, separate setup
  listener, `GATEWAY_SETUP_BIND`, SSH-tunnel requirement, or `:8766` port.
- Any LAN peer reaching that explicit address is an administrator and caller
  by product decision. State this plainly in the console and docs; do not
  imply password or profile protection of administration.
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
- A converted request must receive a matching converted response on both
  buffered and streaming paths, or the request must be refused.
- Device authorization needs trusted-console enforcement, short-lived
  in-memory pending state, a shown-once user code, and interval/lifecycle
  tests.
- Validate identifiers, routes, enum values, and ordering before persistence.
  Profiles deny access by default; any all-alias access requires explicit,
  reviewable opt-in.
- Do not forward caller-controlled upstream session identifiers without
  server-side validation, profile/provider namespacing, and expiry.
- Fallback stays explicit and fail-closed. Never fall back after streams,
  possible tool side effects, ambiguous delivery, or an unapproved error class.
- Treat observability as metadata only: no prompt, completion, token, or secret
  content. Do not claim data is recorded unless the implementation proves it.

## Verification and release

- Add a regression test for every defect, including refusal-path tests for
  security and state-boundary changes.
- Run before handoff:

  ```sh
  PYTHONPATH=src python3 -m unittest discover -s tests -v
  python3 -m compileall -q src tests
  ```

- Never require real credentials or provider access in tests.
- A release requires an exact candidate image, no post-gate rebuild, and a
  Compose-equivalent first-boot/restart proof with the actual state volume,
  non-root UID/GID `10001`, intended listener binds, and `/version` identity.
  A disposable candidate gate alone is insufficient.
- Update the README, runbook, architecture/contract docs, and review register
  whenever behavior, security boundaries, durability, UI, or release steps
  change.
