# Owner authentication and the client API key

Two boundaries, deliberately separate. This document is the design and
operator reference for both; `docs/TRUSTED_LAN_ADMIN_MODE.md` keeps the
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
  sent cross-site, and the existing canonical `Origin`/`Host` checks
  still run - before authentication - on every browser mutation.

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
  (`POST /owner/api/auth/client-key`, owner session required). The owner
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

## What is never exposed

No HTTP response, log line, export or `/version` body ever contains the
password, the PBKDF2 hash, a session token, the setup code, or the
client key value. Status reports presence and transition only
(`client_key_status()`). The state snapshot cache keys on the auth
store's revision, so a key or password change is visible on the very
next read.

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
- `tests/console_dom_test.js` - the sign-in view, the access card, and
  the copy that keeps the two boundaries apart.
- `tests/live_owner_auth_smoke.py` - spawns a real gateway on a
  throwaway data directory and a local port, then drives setup, sign-in,
  throttling, key set/rotate/clear and secret non-exposure over actual
  HTTP. Run by hand: `PYTHONPATH=src python3 tests/live_owner_auth_smoke.py`
  (34 checks; not collected by `unittest discover`).
