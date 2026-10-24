# Trusted LAN administration mode

## Decision

The gateway is a **single trusted-LAN appliance**. Its owner configures and
uses it from the browser at one explicit private-LAN address:

```text
http://10.10.11.109:8765/owner/
```

There is no owner password, login page, session cookie, SSH tunnel,
loopback-only bootstrap, separate setup listener, or second setup port.
Provider enrollment, model catalogue refresh, aliases, routes, profiles, and
status all live in that console on the normal gateway listener.

This supersedes the former owner-passphrase and `GATEWAY_SETUP_BIND` design.

## Trust boundary

This is deliberately **not multi-user administration**. Any device that can
reach the configured LAN address can administer the gateway and invoke its
models. In particular, a reachable LAN peer can change routes or replace a
provider credential. The product must say this plainly in the console and
operator docs; it must not imply that a password or profile is protecting
administration.

This mode is appropriate only on a LAN the owner trusts. It does not add TLS,
firewall rules, router rules, port forwarding, proxy trust, or public exposure.

## Invariants that remain mandatory

- Bind one explicitly configured address owned by this host: RFC1918 IPv4,
  IPv6 unique-local (`fc00::/7`), or loopback. The listener supports IPv6;
  format IPv6 literals in bracketed URL/Host/Origin form. Reject wildcard,
  public (including RFC 5737 TEST-NET), multicast, link-local, and unspecified binds.
- Continue to reject hostile or non-canonical `Host` values, forwarded/proxy
  headers, and cross-site state-changing browser requests.
- Keep provider credentials and caller tokens write-only: never return them in
  configuration/status responses, URLs, logs, ledgers, exports, error bodies,
  or `/version`.
- Keep `/version` metadata-only and preserve its fixed schema.
- Keep explicit provider actions: saving a credential, testing it, device
  authorization, and model-catalogue refresh are distinct user actions. Page
  loads never call a provider.
- Keep fallback opt-in and fail-closed after streams, possible tool side
  effects, ambiguous delivery, or an ineligible failure class.
- Continue to reject forwarded headers until a separately designed and tested
  proxy-trust model is approved.

`Origin` is a browser same-origin safeguard, not an access-control substitute:
direct requests from a trusted LAN peer are allowed by the product decision.

## Required product changes

### Listener and startup

1. Remove the independent setup listener and `GATEWAY_SETUP_BIND` from the
   runtime, Compose file, CLI help, and documentation.
2. Make the normal explicit LAN listener host the owner console and all owner
   configuration actions.
3. Remove initial-passphrase bootstrap and owner login/logout/session-cookie
   flows from trusted-LAN operation. The console must open directly.
4. Keep the normal API and owner-console paths on port `8765`; do not add a
   redirect, reverse proxy, or new public listener.
   The gateway port may be configured, but port `8766` is reserved and must be
   rejected from both CLI and `GATEWAY_PORT` configuration.

### Console

1. Replace login/setup interstitials with the configured console.
2. Show a persistent, non-dismissable notice:
   **"Trusted LAN admin mode — any device that can reach this address can
   configure and use this gateway."**
3. Provider cards may accept credentials only through write-only form inputs.
   The UI confirms that a credential is stored but never renders its value.
4. Keep all provider-network actions explicitly labelled and initiated by a
   button. Saving configuration alone is not a provider request.

### API and persistence

1. Owner configuration endpoints must no longer require an owner session,
   passphrase, or CSRF token in this mode.
2. Keep canonical-host and cross-origin mutation refusal. For a valid
   same-origin browser request, configuration mutations must work without a
   login/session bootstrap.
3. LAN API invocation is anonymous by design. Profiles remain configuration
   objects for aliases/routes and optional future attribution; they do not make
   the LAN admin surface private.
4. Delete obsolete owner-auth state safely. Fresh installs must not create
   passphrase/session records; existing state must remain readable or receive
   a documented, atomic compatibility migration that removes only obsolete
   owner-auth records and never exposes provider credentials.

## Out of scope

- Per-user administration or per-device authorization.
- TLS termination, reverse proxies, wildcard/public binds, firewall/router/DNS
  changes, or remote exposure.
- Provider account login by an agent, live provider calls in automated tests,
  or revealing stored credentials.
- Claiming that a trusted LAN protects traffic from an on-path attacker.

## Acceptance criteria

### Functional

1. A fresh disposable runtime bound to `10.10.11.109:8765` serves
   `/owner/` directly: no login, passphrase, setup wizard, or `:8766` listener.
2. The same console can perform a synthetic, credential-free configuration
   round trip (for example, create/edit/delete a profile and alias) without
   session or CSRF bootstrap values.
3. Provider save/test/catalogue buttons remain explicit. Tests use mocked
   transport only and prove page load/config save does not invoke a provider.
4. Stored synthetic secret material is never returned by read endpoints,
   rendered HTML, errors, request ledger, `/version`, or logs.

### Boundary regression

1. The process listens only on the configured explicit LAN IPv4 address and
   port; it never listens on `0.0.0.0`, `::`, or `:8766`.
2. Canonical `Host` succeeds; hostile `Host`, `Origin`, `Forwarded`, and
   `X-Forwarded-*` cases are refused on read and mutation paths as applicable.
3. Cross-site browser mutation is refused; valid same-origin mutation succeeds
   without cookies, a password, or a CSRF token.
4. API model listing is anonymous and safely returns an OpenAI-compatible empty
   list on a fresh zero-profile runtime; a no-route chat request returns a JSON
   4xx and does not call a provider.

### Release evidence

1. Unit and HTTP tests pass without real provider credentials.
2. A browser test verifies direct console use and the visible trusted-LAN notice
   at desktop and 390 px viewport widths.
3. On the dev box, a disposable Compose-equivalent first-boot/write/restart
   test proves non-root persistent-volume ownership, exact bind, no `:8766`,
   and truthful `/version` identity.
4. Independent QA and security review pass against one immutable source SHA and
   image identity before any new runtime is started.
