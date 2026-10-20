# Architecture

This document describes the code that is in this repository today. It is an
orientation document, not a statement that every future provider flow is
supported or production-ready.

## Runtime topology

The gateway starts two independent standard-library HTTP servers from
`gateway.cli`:

| Listener | Default bind | Mount | Purpose |
|---|---|---|---|
| LAN | explicit private IP, port `8765` | `/owner/`, `/v1/`, `/health`, `/version` | owner console and OpenAI-compatible client API |
| setup | `127.0.0.1`, port `8766` | `/setup/`, `/health`, `/version`, `/v1/` | loopback-only setup surface |

The setup and LAN applications share one configuration store, credential
store, adapter registry, router, owner-session authority, request ledger, and
limit store. They differ only in their mounted browser surface and the
canonical host/origin policy. `cli.py` refuses wildcard and non-private binds.

The setup route is independently guarded by the actual peer address. A request
to `/setup/` from a non-loopback address is rejected; it is not protected by a
client-supplied forwarding header. Forwarded headers are rejected rather than
trusted.

## State boundaries

`gateway.config.ConfigStore` persists the non-secret configuration as
`config.json`, with atomic replace, mode `0600`, and a monotonic revision.
It contains provider state, model metadata, aliases/routes, profile metadata,
and access mode. It must never contain a credential or caller-token hash.

`gateway.credentials.CredentialStore` persists separate JSON files under a
private `credentials/` directory. Provider adapters receive a handle scoped to
one provider. The owner-passphrase hash and caller-token hashes also use that
store. A read of a credential is available only to the internal adapter/auth
code; console state renders presence, not values.

The following state is intentionally in-memory only and disappears on restart:

* owner browser sessions and CSRF values;
* in-flight ChatGPT/Codex device authorization state;
* provider cooldowns;
* request ledger events and totals;
* limit snapshots.

This means the Status page is operationally useful but is **not** a durable
audit or billing system.

## Request paths

### Browser configuration

1. A browser reaches `/setup/` or `/owner/` on its canonical listener.
2. `GatewayApp.handle` rejects forwarded headers, invalid `Host`, invalid
   `Origin` for browser mutations, and non-loopback setup requests.
3. Owner login establishes an `HttpOnly`, `SameSite=Strict` session cookie and
   a separate CSRF cookie. Mutating browser requests require a matching CSRF
   header and an owner session.
4. In default LAN no-auth mode the LAN owner console is deliberately
   read-only; configuration and credential enrollment happen through setup.
   Authenticated mode permits non-credential mutation in the LAN owner
   console after owner sign-in. Credential enrollment remains setup-only.
5. Provider tests and catalogue refreshes are explicit actions. No page load
   should call a provider.

### Client inference

1. A LAN client calls `GET /v1/models`, `POST /v1/chat/completions`, or
   `POST /v1/responses`.
2. `ProfileAuth` resolves either the shared Anonymous LAN profile (LAN
   no-auth) or a bearer-token profile (authenticated mode).
3. The request model must name a published alias and be allowed for that
   profile.
4. `Router` evaluates the primary route followed by configured fallbacks. A
   candidate must have a configured adapter, an enabled known model, a known
   supported dialect, matching capabilities, and no active local cooldown.
5. The bridge converts between Chat Completions and Responses only when the
   target capability permits it. Unsupported capability/dialect combinations
   fail closed.
6. The adapter makes the provider request. The returned provider/model is
   attributed to the route result. A stream is forwarded incrementally.

## Provider seam

The routing core knows aliases, error classes, model capabilities, and fallback
policy. Provider-specific behaviour lives in `gateway.adapters`.

* **ChatGPT / Codex** uses the device-code flow and the Responses dialect.
  See `CHATGPT_CODEX_CONTRACT.md` for the exact documented/assumed boundary.
* **OpenCode Go** discovers model IDs from its explicit catalogue endpoint.
  The checked-in `data/go_catalog.json` supplies the dated, published mapping
  from model ID to wire dialect. An undocumented dialect is refused rather
  than guessed.

Adding a provider requires an adapter, model-capability handling, safe error
classification, registry registration, UI/state treatment, tests, and an
explicit credential-isolation review. It is not just a URL plus an API key.

## Routing and fallback policy

Fallback does not happen by default. A fallback requires an alias-level
authorization, confirmation of the next provider, and an enabled trigger.
Tool-bearing requests never fall back. Authentication, policy, and malformed
request failures also fail closed unless the owner explicitly configures a
safe policy for that failure class. A plain HTTP 429 is rate limiting, not a
confirmed allowance exhaustion.

The router only decides a fallback before it returns an upstream response. It
does not retry a stream after the caller has begun receiving bytes.

## Release identity

`GET /version` returns exactly four keys: `service`, `source_revision`,
`source_tree`, and `image_revision`. The first two revisions are build inputs;
the image revision is read from `release.json` in the data directory when a
release step has written a digest-shaped value. The endpoint must never become
a generic environment or configuration inspector.

## Verification boundaries

The unit suite uses fakes and fixtures; it does not authenticate with a
provider or require credentials. A complete release verification still needs
an exact-image gate and a Compose/runtime proof, especially for the writable
state-volume ownership and listener boundaries. See `DEVELOPMENT.md` and the
open findings in `REVIEW.md`.
