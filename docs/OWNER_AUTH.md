# Owner authentication, the client API key, and machine keys

Three boundaries, deliberately separate: the owner session, the client
`/v1` API key, and the machine key. This document is the design and
operator reference for all three - the same three `src/gateway/owner_auth.py`
counts in its module docstring; `docs/TRUSTED_LAN_ADMIN_MODE.md` keeps the
listener/trust-model invariants.

> **Status:** implemented in this candidate, **not deployed**. The
> running gateway still serves the previous no-login boundary until
> this SHA passes independent QA and security review and is deployed.

## Owner authentication (console + configuration API)

One username/password account. It protects `GET /owner/` and every
`/owner/api/*` endpoint except the three listed below.

- **Storage.** `owner_auth.json` in the data directory, mode `0600`
  inside a `0700` directory. The password is stored as
  `pbkdf2_sha256$<iterations>$<salt>$<digest>` at 600 000 iterations
  (module constant `DEFAULT_PBKDF2_ITERATIONS`, so a test can pin it).
  Sessions are stored as SHA-256 hashes of their tokens; the plaintext
  token exists only in the cookie.
- **Sessions.** One `HttpOnly`, `SameSite=Strict`, `Path=/` cookie named
  `gw_owner_session`, 12-hour TTL, validated server-side on every
  request. Expiry, logout and password change all revoke server-side;
  the browser dropping the cookie is cosmetic.
- **`Secure` flag.** Set only when the deployment says TLS: an `https`
  canonical origin at construction, or `GATEWAY_COOKIE_SECURE=1` for a
  deployment behind a TLS terminator. Never inferred from
  `X-Forwarded-Proto` or any other client-controlled header - on a
  plain-HTTP LAN listener a `Secure` cookie would simply be dropped.
- **First run.** A fresh gateway has **no account**. `ensure_setup_code()`
  mints a random one-time code, prints it on server startup and writes it
  to `owner_setup_code` (mode `0600`, JSON: `{"setup_code": "..."}`). `POST /owner/api/auth/setup`
  consumes it exactly once and deletes the file. There is no default
  credential: `admin/admin` does not exist.
- **Throttling.** Failed sign-ins are throttled in memory per account
  *and* per client address: 5 failures inside a 300-second window return
  `429 login_throttled` with `Retry-After`, and the correct password is
  refused too while the window is open. Setup attempts are throttled the
  same way; password changes have their own key.
- **Pre-auth surface.** Only `GET /owner/api/auth/session` (returns
  `authenticated` and `setup_required`), `POST /owner/api/auth/setup`,
  `POST /owner/api/auth/login`, and the console shell/assets. Everything
  else answers `401 authentication_required` before its handler runs, so
  a streaming endpoint cannot emit a frame and a mutation cannot
  half-apply.
- **CSRF.** No CSRF token: `SameSite=Strict` stops the cookie from being
  sent cross-site. `check_forwarded_headers` and `check_host` run for
  every request first - bearer included - and `check_origin` enforces
  the exact canonical `Origin` on a browser-surface **mutation** before
  its authentication runs (non-mutating methods return early: a read is
  not origin-checked), with one deliberate exception: it returns
  early for any request that presents a bearer `Authorization` header
  (a machine client has no browser Origin). That exception cannot open
  a cookie-CSRF path: a hostile page cannot set `Authorization` (it is
  not CORS-safelisted and this gateway serves no CORS headers), so a
  cookie-carrying browser mutation still faces the exact-Origin check
  before the session is consulted.

### Turning console sign-in off (`owner_console_auth`)

Console sign-in is an **owner-credentials control with an explicit,
default-ON opt-out**. The config key `owner_console_auth` (boolean) is
read alongside the rest of the gateway config:

| stored value | behaviour |
|---|---|
| absent | auth **REQUIRED** — fail-closed |
| `null`, `0`, `1`, `"no"`, `"false"`, `""`, a list or object | auth **REQUIRED** — fail-closed parse |
| `true` | auth required (the default: today's behaviour, byte-for-byte) |
| `false` | console auth off: every `/owner` route serves the owner without a session |

Only the literal JSON boolean `false` disables the gate. The parse in
`GatewayConfig.from_dict` is an identity test (`is not False`) and cannot
raise, so a typo, a missing key, a `null` or a drifted default can never
open the console by accident — the failure this flag would otherwise be
perfect at creating.

**What changes when it is off**

- `GET /owner/api/auth/session` keeps reporting **session truth** —
  `authenticated` stays `false` when no cookie was presented — and adds
  `console_auth: "disabled"`, which is what the console uses to skip the
  login view instead of being handed a session it never established.
- `GET /owner/api/state` carries `console_auth: "disabled"` and replaces
  the canned `access_mode_description` with an explicit warning. The
  default (auth-required) body is unchanged byte for byte: no extra key
  and no `mcp_contract_version` bump, because `gateway-owner-mcp` pins
  that version by exact equality.
- The console shows a persistent `OWNER CONSOLE SIGN-IN IS OFF` alert on
  every tab and a `console sign-in is OFF` header pill; a request with no
  cookie claims no identity (`owner.username` is `null`), while a real
  cookie still reports the real username.
- Owner mutations are authorised without a cookie (`can_mutate: true`).

**What does not change — the fence**

- **Machine key:** `/owner/api/machine-keys` stays human-session-only, an
  unknown or read-scoped key is still refused, a write-scoped key still
  writes, and a machine key can never flip this flag
  (`403 machine_key_forbidden`). The flag governs the browser session
  only.
- **Model client key:** `/v1` still requires the client API key when one
  is set, in both states — proven identically in
  `tests/test_owner_console_auth_flag.py`.
- Route table, `Host`/`Origin`/forwarded-header checks and every leak
  invariant: no password hash, setup code, machine-key material or
  session token appears in any response in either state.

**Turning it back on.** While signed in, `POST /owner/api/settings` with
`{"action": "owner_console_auth", "required": true}` takes effect on the
next request. The config key itself works too, but `ConfigStore` reads
the file once at startup, so a hand edit needs a restart to be seen.
With the gate off that endpoint is reachable without
a session *by design* — that is the mode — and the only value that
changes how much protection exists is the re-arm, which is fail-safe: it
always closes the console, never opens it. `required` must be a real
boolean; `"false"`, `0` and `null` are refused with `400 invalid_setting`
and change nothing.

**Risk — read this before turning it off.** With `owner_console_auth=false`
every device that can reach the listener is an administrator of the
console: it can change configuration, mint and revoke machine keys, and
read every owner surface. The console and `/owner/api/state` both say so
persistently so the state cannot be forgotten. Password change is
identity-bound and still needs a real session; if you are locked out
while the gate is off, use `python -m gateway --reset-owner-password`
on the gateway host (next section).

### Local recovery (forgotten password)

```sh
python -m gateway --reset-owner-password   # prompts on stdin, twice
```

Requires filesystem access to the data directory. The password is read
from **stdin, never argv** (argv is visible in the process list and in
shell history). It rewrites the stored hash and revokes every session.
Handing the flag a password anyway (`--reset-owner-password <password>`)
is refused with exit 2, and the refusal does not echo the value: argument
errors replace every argv-supplied value with `<redacted>`, so a secret
that lands on the command line by mistake stays out of the terminal and
out of anything capturing stderr. The recovery regime is entered on how
the flag *resolves* - the exact flag, `=`-attached spellings, every
unambiguous abbreviation (`--reset-owner-p`), and close misspellings of
it (`--rest-owner-password`) - and the misspelled flag keeps its own name
in the message, because that is the diagnosis. Outside that regime a
value is withheld whole, including tokens that start with `--` after the
`--` separator and positional tokens containing `=` (rewriting `secret=`
to `secret=<redacted>` would still print the secret); a token that could
be an option name of this parser - `--` plus lowercase letters, digits
and hyphens, the grammar every registered long option matches - stays
named, so a plain typo is still diagnosable.

**Documented boundary.** Because unknown flags are named verbatim (the
preserved diagnosis: `unrecognized arguments: --bogus-flag`), a password
that is byte-identical to a flag-shaped unknown token - `--` plus
lowercase letters, digits and hyphens, e.g. `--summer2026-pass` - on a
command line with no recovery signal (no recovery flag, abbreviation or
misspelling) cannot be told apart from a typo'd flag and is echoed in
those two shapes: a lone token, or a token beside another unknown flag.
This is the one residual of the argv-redaction rule, pinned by
`tests/test_cli_argv_secrets.py::DocumentedResidualBoundaryTests`. Do not
put a password on the command line: the recovery flag reads it from
stdin for exactly this reason. The running gateway picks
the change up without a restart: the store
re-reads the file when its fingerprint changes, so a stolen cookie dies
at the moment of recovery rather than at the next reboot.

## Model client API key (`/v1`)

**Off by default.** With no key configured, every existing client keeps
working exactly as before - this is the backward-compatibility
requirement, and it is tested.

Inventory covered once a key is set (the check runs before the handler,
so streaming endpoints answer with the same JSON error and never open a
stream): `GET /v1/models`, `GET /v1/alias-catalog`,
`POST /v1/chat/completions`, `POST /v1/messages`, `POST /v1/responses`,
`GET /v1/systemone/models`, `POST /v1/systemone`.

- `GET /version`, `GET /health`, `GET /metrics` are unchanged.
- **Set / rotate / clear** from Settings → Access
  (`POST /owner/api/auth/client-key`, owner session required — by default;
  with `owner_console_auth=false` it needs none). The owner
  supplies the key they will hand to clients; it must be 32-256
  characters.
  The response carries only a random display id and the transition, plus
  an `impact` sentence that states what clients will experience. The key
  value itself is never echoed, logged or stored in plaintext (SHA-256
  hash + display id).
- **Rotation grace:** the previous key stays valid for 600 seconds
  (`CLIENT_KEY_GRACE_SECONDS`), reported as
  `previous_key_valid_for_seconds` in the response and shown in the
  console, so a rollout cannot lock clients out mid-change.
- **Clearing** restores keyless access exactly as before a key was set.
- **No silent agent config changes:** the gateway never edits any client
  or agent configuration. The console says so before every transition.

Refusal body: `401` with `type: invalid_api_key`, a
`WWW-Authenticate: Bearer ...` header and `Cache-Control: no-store`.

## Machine key (the third boundary: console-minted automation credential)

An **automation credential for the owner API** - minted in the owner
console (Settings → Machine keys), presented as a bearer
`Authorization` header (`gwmk_` prefix), and nothing else: it is not a
login (it never issues or satisfies a session cookie) and not the
client API key (`/v1` refuses a valid machine key with
`401 invalid_api_key`, even on the keyless default).

- **Mint - shown once.** The value is generated server-side (`gwmk_`
  + 43 base64url characters derived from 32 random bytes) and returned
  exactly once, in the create (and the rotate) response; every later
  read returns only the random `display_id`, name, scope and last use
  — no `created_at`: that timestamp exists only in the create/rotate
  response. At most 100 active keys (`MACHINE_KEY_MAX_ACTIVE`).
- **Storage - hash only.** `machine_keys.json`, `0600` inside the
  `0700` data directory, holds a SHA-256 digest per key keyed on a
  **separate pepper** file `machine_key_pepper` (`0600`, independent
  of `owner_auth.json` - machine-key state never touches the password
  or sessions). The pepper is stat-keyed: a deleted, replaced or
  corrupt pepper stops every key on the very next request of a live
  store (fail-closed, not a latch - restore the file and the keys work
  again; incident procedure in `docs/RUNBOOK.md` §2).
- **Scopes.** `read` covers five owner-state reads; `write` covers
  those plus nine configuration mutations. Both allowlists are
  mount-relative frozensets in `src/gateway/http/app.py`:
  - `MACHINE_KEY_READ_PATHS`: `/api/status`, `/api/state`,
    `/api/leaderboard`, `/api/capabilities`, `/api/ledger/export`.
  - `MACHINE_KEY_WRITE_PATHS`: `/api/alias`, `/api/alias-group`,
    `/api/model`, `/api/provider`, `/api/settings`,
    `/api/capabilities/preview`, `/api/capabilities/run`,
    `/api/capabilities/decision`, `/api/capabilities/probe/cancel`.
  - Everything else under `/owner/*` - the machine-key endpoints
    themselves, `/api/auth/*` (including the client-key endpoint),
    backup export/import, the console shell - answers `403
    machine_key_forbidden`; a read-scoped key on a write path answers
    `403 scope_required`.
- **`MACHINE_KEY_BLOCKED_PROVIDER_ACTIONS`** - `enroll_key`,
  `device_start`, `device_poll`, `redeem_reset` - are refused (`403
  machine_key_forbidden`) even to a write-scoped key: credential
  enrolment and device flows stay human-session-only, and
  `redeem_reset` spends a real pooled one-shot credit (the same bank
  Hermes' `/usage reset` draws on), so spending is owner-click-only
  for the same reason credential minting is - an automation must not
  be able to drain banked resets.
- **`cookie_rejected`.** A machine key presented together with an
  owner session cookie is refused with `403 cookie_rejected` rather
  than resolved either way: the two credentials never combine.
- **Origin/Host.** `check_origin` returns early for any bearer request
  (CSRF bullet above), but `check_forwarded_headers` and `check_host`
  run before it on every request, machine included.

### `clear_cooldowns` at scope `all` - deliberate F1 acceptance (PM `t_e397267a`)

A **write-scoped** machine key MAY run the cooldown-clear provider
action (`POST /owner/api/provider` with `action: clear_cooldowns`) at
`scope` `route`, `provider` **or `all`** and receives `200`. Recorded
as a deliberate acceptance, not an oversight: the shipped
`gateway-owner-mcp` server (`margosmark/gateway-owner-mcp` @ `1acc55b`,
QA `t_cf3205a9` PASS + security `t_ce0945dd` PASS) exposes
`gateway_clear_cooldowns` with an explicit `scope` of
`route|provider|all` behind a dry-run and a single-use 120-second
confirmation nonce, so refusing `all` here would break an automation
credential the gateway has already shipped and re-gated (the old 401
expectation was a pre-integration artifact, `t_e74eab5a`).

Reach is bounded by the surface, not by scope - the write scope
already reaches `POST /owner/api/settings` - and every other half is
unchanged. Measured on this SHA with a real harness
(`tests/test_machine_keys.py:MachineKeyHarness`):

| Request | Answer |
|---|---|
| write key, `clear_cooldowns`, `scope=route` | `200`, `scope=route` |
| write key, `clear_cooldowns`, `scope=provider` | `200`, `scope=provider` |
| write key, `clear_cooldowns`, `scope=all` | `200`, `scope=all` |
| read key, same call | `403 scope_required` |
| anonymous, canonical `Origin` | `401 authentication_required` |
| anonymous, no `Origin` | `403 origin_required` |
| anonymous or session, hostile `Origin` | `403 origin_mismatch` |
| machine key + session cookie | `403 cookie_rejected` |
| `scope` = `ALL` / `" all"` / `"all "` / `everything` | `400 invalid_scope` |
| non-string `scope` (e.g. a number) | `400 invalid_request` |
| `clear_cooldowns` with no `scope` and no provider | `404 unknown_provider` |
| write key on `enroll_key` / `device_start` | `403 machine_key_forbidden` |
| write key on `redeem_reset` | `403 machine_key_forbidden` |
| write key on the machine-key or client-key endpoints | `403 machine_key_forbidden` |
| read key, `GET /owner/api/state` | `200` |
| valid machine key on `/v1` | `401 invalid_api_key` |

## What is never exposed

No HTTP response, log line, export or `/version` body ever contains the
password, the PBKDF2 hash, a session token, the setup code, or the
client key value. A machine key value is the one credential with a
single deliberate appearance: the create/rotate response that mints it,
exactly once — every later response carries only its `display_id`, and
the stored file holds only the peppered hash. Status reports presence
and transition only (`client_key_status()`). The state snapshot cache
keys on the auth store's revision, so a key or password change is
visible on the very next read.

## Tests

- `tests/test_owner_auth.py` - setup bootstrap, session cookie flags,
  throttle windows (fake clock), logout, password change, stdin recovery,
  session expiry, keyless default, key enforcement across the exact
  endpoint inventory, rotation grace expiry, clear-and-restore, secret
  non-exposure in responses *and* in the stored file, concurrent
  rotations, corrupt-store fail-closed.
- `tests/owner_session.py` - the shared fixture helper: completes setup
  through the real endpoints and hands the cookie to fixtures that call
  `app.handle` directly.
- `tests/test_end_to_end.py` - the HTTP boundary (unauthenticated reads
  and mutations refused, authenticated ones work).
- `tests/console_dom_test.js` - the sign-in view, the access card (the
  copy that keeps owner auth and the client key apart), the machine-key
  Settings UI, and the local-provider privacy copy (N2: the fallback
  target named as the confirmation party).
- `tests/test_machine_keys.py` - the machine-key boundary end to end:
  mint/rotate/revoke, reveal-once, scopes and allowlists,
  `cookie_rejected`, the `/v1` refusal, the F1 `clear_cooldowns`
  acceptance at all three scopes, and pepper fail-closed on a live
  store.
- `tests/live_owner_auth_smoke.py` - spawns a real gateway on a
  throwaway data directory and a local port, then drives setup, sign-in,
  throttling, key set/rotate/clear and secret non-exposure over actual
  HTTP. Run by hand: `PYTHONPATH=src python3 tests/live_owner_auth_smoke.py`
  (34 checks; not collected by `unittest discover`).
