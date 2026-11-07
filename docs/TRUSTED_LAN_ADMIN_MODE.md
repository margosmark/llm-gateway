# Trusted LAN administration mode

## Status

**Owner login is the default for console access.** The previously
documented rule - "any device that can reach the configured address is
an administrator, there is no password, login or session cookie" - is no
longer the boundary: `docs/OWNER_AUTH.md` describes the owner account,
the server-side session cookie, the optional model client API key, and
console-minted machine keys for automation.

That old behaviour is still reachable, but only as an **explicit,
default-off owner opt-out**: the config key `owner_console_auth=false`
turns the console session gate off. It is an **owner-credentials
control**, not a mode switch - the default is `true`, the parse is
fail-closed (only the literal JSON boolean `false` opens the console;
absent, `null`, `0` and `"no"` all leave authentication REQUIRED), and it
governs the **browser session only**: the machine key and the model
client key keep their own boundaries in both states, and a machine key
can never flip it. With it off, the console and `/owner/api/state` say so
persistently so an open listener cannot be forgotten.

Independent QA and security review must pass on the same SHA before any
deployment.

Everything else in this document still holds unchanged: the single
explicit private listener, no TLS, no proxy trust, no separate setup
listener, no reserved port `8766`, and profiles remaining configuration
objects rather than access control.

## Decision

The gateway is a **single trusted-LAN appliance**. Its owner configures
and uses it from the browser at one explicit private-LAN address:

```text
http://10.10.11.109:8765/owner/
```

Provider enrollment, model catalogue refresh, aliases, routes, profiles,
and status all live in that console on the normal gateway listener - one
listener, no second setup port, no `GATEWAY_SETUP_BIND`, no SSH tunnel
requirement.

What changed in this candidate:

- **Owner console access** requires the owner account: one
  username/password, PBKDF2-hashed, with server-side sessions delivered
  as an `HttpOnly`, `SameSite=Strict` cookie. A fresh gateway has no
  account; first run creates one from a random one-time setup code that
  exists only on the gateway host (server stdout and the owner-only
  `owner_setup_code` file). There is no default credential.
- **Console sign-in is opt-out, never opt-in** (`owner_console_auth`):
  the default `true` keeps every `/owner` route behind the session. The
  literal boolean `false` - in the gateway config, or via
  `POST /owner/api/settings` with
  `{"action": "owner_console_auth", "required": true|false}` while
  signed in - serves the owner routes without a session; absent, `null`,
  `0` and `"no"` all mean authentication REQUIRED. With it off the
  console shows a persistent `OWNER CONSOLE SIGN-IN IS OFF` banner,
  `GET /owner/api/state` reports `console_auth: "disabled"` (and an
  honest `access_mode_description`), and `GET /owner/api/auth/session`
  keeps reporting session truth plus the mode. It changes the browser
  session only: the machine key cannot flip it
  (`403 machine_key_forbidden`) and the model client key still gates
  `/v1`. See `docs/OWNER_AUTH.md` for the full behaviour and risk.
- **Model API access (`/v1`)** stays keyless by default. When the owner
  sets the optional client API key, every `/v1` request must send
  `Authorization: Bearer <key>`; rotation keeps the previous key valid
  for an explicit 10-minute grace window; clearing restores keyless
  access. The gateway never edits a client configuration itself.
- **Machine keys** are a third credential, separate from both of the
  above: console-minted from Settings → Machine keys for MCP and other
  automation, shown exactly once, stored only as a hash under a
  separate pepper, scoped `read` or `write`. They are neither an admin
  login (no session cookie is ever issued for one) nor the model client
  API key (`/v1` refuses a machine key), and they reach only an
  explicit allowlist of owner-API paths: even a write-scoped key can
  never enrol provider credentials or run a device flow.

## Trust boundary

Owner sign-in is a boundary against *other devices on the LAN*, not
against an on-path attacker: the listener is plain HTTP on a private
address, so session cookies are not marked `Secure` unless the
deployment is served over HTTPS (an `https` canonical origin, or
`GATEWAY_COOKIE_SECURE=1` in front of a TLS terminator).

A reachable LAN peer that is not signed in can no longer read state or
change configuration - **unless the owner has set
`owner_console_auth=false`**, which restores exactly the old rule for the
console: every reachable peer is an administrator. That is the owner's
explicit choice, and the console says so on every tab while it lasts.
Until a client API key is set, that same peer can
still *invoke* the model API - by decision, and stated plainly in the
console and in the README.

This mode is appropriate only on a LAN the owner trusts. It does not add
TLS, firewall rules, router rules, port forwarding, proxy trust, or
public exposure.

## Invariants that remain mandatory

- Bind one explicitly configured address owned by this host: RFC1918 IPv4,
  IPv6 unique-local (`fc00::/7`), or loopback. The listener supports IPv6;
  format IPv6 literals in bracketed URL/Host/Origin form. Reject wildcard,
  public (including RFC 5737 TEST-NET), multicast, link-local, and unspecified binds.
- Continue to reject hostile or non-canonical `Host` values, forwarded/proxy
  headers, and cross-site state-changing browser requests. These checks run
  **before** authentication, so a refused request never reaches a handler.
- Keep provider credentials, caller tokens, the owner password hash,
  session tokens and the client key write-only: never return them in
  configuration/status responses, URLs, logs, ledgers, exports, error
  bodies, or `/version`.
- Keep `/version` metadata-only and preserve its fixed schema.
- Keep explicit provider actions: saving a credential, testing it, device
  authorization, and model-catalogue refresh are distinct user actions. Page
  loads never call a provider.
- Keep fallback opt-in and fail-closed after streams, possible tool side
  effects, ambiguous delivery, or an ineligible failure class.
- Continue to reject forwarded headers until a separately designed and tested
  proxy-trust model is approved. `X-Forwarded-Proto` never decides the
  `Secure` cookie flag.
- Profiles stay configuration and attribution objects. They are not
  access control and must not be presented as if they were.

`Origin` is a browser same-origin safeguard, not an access-control
substitute: the session cookie is what gates the owner surface, and
`SameSite=Strict` is what stops a cross-site request from carrying it.

## Endpoint inventory

| Path | Boundary |
|---|---|
| `GET /owner/`, `/owner/app.js`, `/owner/style.css`, `/favicon.ico` | public (the console shell and its assets, so the sign-in view can render) |
| `GET /owner/api/auth/session` | public pre-auth probe: `authenticated`, `setup_required` only |
| `POST /owner/api/auth/setup` | public, one-time setup code, throttled |
| `POST /owner/api/auth/login` | public, throttled per account and per client address |
| every other `/owner/api/*` and `/owner/*` | owner session cookie, always - except while the owner has set `owner_console_auth=false`, which serves them without a session (see Status); the three auth endpoints above and the console assets stay public in both states |
| `GET /v1/models`, `GET /v1/alias-catalog`, `POST /v1/chat/completions`, `POST /v1/messages`, `POST /v1/responses`, `GET /v1/systemone/models`, `POST /v1/systemone` | client key **only when configured**; keyless otherwise |
| `GET /version`, `GET /health`, `GET /metrics` | unchanged: anonymous metadata on the trusted LAN |

Refusal happens before the handler, so a streaming endpoint answers with
the same JSON error and never opens a stream, emits a frame, or calls a
provider.

## Operator notes

- **First run:** read the one-time setup code from the gateway host
  (`owner_setup_code` in the state volume, mode `0600`, or the process
  stdout) and create the account in the console. The code is single-use.
- **Forgotten password:** `python -m gateway --reset-owner-password`
  with access to the data directory. It reads the new password from
  stdin (never argv), rewrites the hash, and revokes every session. The
  running gateway picks the change up without a restart.
- **Key transitions:** Settings → Access states the impact before every
  change and shows the remaining grace window after a rotation. No client
  configuration is modified by the gateway.

## Acceptance criteria

### Functional

1. A fresh disposable runtime bound to `10.10.11.109:8765` serves
   `/owner/` and asks for first-run setup; there is still no `:8766`
   listener, no `GATEWAY_SETUP_BIND`, and no default credential.
2. The console performs a synthetic, credential-free configuration round
   trip only *after* an owner session exists; without one, every owner
   API answers `401 authentication_required` and mutates nothing. That
   holds for every value of `owner_console_auth` except the literal
   boolean `false`, where serving without a session is the point - and
   there the machine key and client key boundaries above still refuse
   exactly as they do with the flag on.
3. Provider save/test/catalogue buttons remain explicit. Tests use mocked
   transport only and prove page load/config save does not invoke a provider.
4. Stored synthetic secret material is never returned by read endpoints,
   rendered HTML, errors, request ledger, `/version`, or logs - and the
   client-key value, the setup code and the password are never in any
   HTTP response at all.

### Boundary regression

1. The process listens only on the configured explicit LAN IPv4 address and
   port; it never listens on `0.0.0.0`, `::`, or `:8766`.
2. Canonical `Host` succeeds; hostile `Host`, `Origin`, `Forwarded`, and
   `X-Forwarded-*` cases are refused on read and mutation paths as applicable.
3. Cross-site browser mutation is refused; a same-origin mutation succeeds
   only with a valid session cookie and no CSRF token.
4. `GET /v1/models` is anonymous and safely returns an OpenAI-compatible
   empty list on a fresh zero-profile runtime with no client key set; a
   no-route chat request returns a JSON 4xx and does not call a provider.

### Release evidence

1. Unit and HTTP tests pass without real provider credentials.
2. A browser test verifies the sign-in/first-run view and the signed-in
   console at desktop and 390 px viewport widths.
3. On the dev box, a disposable Compose-equivalent first-boot/write/restart
   test proves non-root persistent-volume ownership, exact bind, no `:8766`,
   and truthful `/version` identity.
4. Independent QA and security review pass against one immutable source SHA and
   image identity before any new runtime is started.
