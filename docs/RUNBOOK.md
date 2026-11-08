# Operator runbook

Everything an operator needs to run, verify, cut over, and roll back the
gateway. Nothing here is required for *using* the gateway — that is all done
in the browser.

---

## Operational metrics

`GET /metrics` on the configured trusted-LAN listener returns Prometheus text
exposition with counts and latency derived from the bounded in-memory request
ledger and the remaining duration of active local provider cooldowns. It is
metadata only; the endpoint uses the same canonical Host/Origin and forwarded-
header checks as other gateway routes. No token totals are currently emitted.

## 1. First run

### 1.1 Build the candidate

**The short way** - build, deploy, publish the identity and verify it, in
one command:

```sh
cd /home/marslan/Projects/llmgtwy
python3 scripts/release.py --build --image personal-llm-gateway:candidate
```

It reads the revision and the tree from the checkout, stamps them into the
image, starts the deployment, writes the image digest into the state volume,
and verifies `/version` reports exactly that identity.

**Do not deploy with a bare `docker compose up -d --build`.** Compose passes
`SOURCE_REVISION` through from the environment, so without it the image is
built with `unknown` and `/version` cannot say what is running. The gateway
warns about this at startup.

To build and gate without deploying:

```sh
export GATEWAY_BIND=10.10.11.109
export SOURCE_REVISION="$(git rev-parse HEAD)"
export SOURCE_TREE="$(git rev-parse HEAD^{tree})"

docker build \
  --build-arg SOURCE_REVISION="$SOURCE_REVISION" \
  --build-arg SOURCE_TREE="$SOURCE_TREE" \
  -t personal-llm-gateway:candidate .
```

Record the digest. **The digest, not the tag, identifies the candidate.**

```sh
docker image inspect personal-llm-gateway:candidate \
  --format '{{index .RepoDigests 0}}'
```

A locally built image has no registry digest, so the release gate falls back
to the **image ID** (`{{.Id}}`), which is the content hash of the image
config. That value is what gets written to `release.json` and what
`/version` then reports. If the image is pushed to a registry, the registry
digest is used instead. Either way the value is immutable and identifies
exactly one artifact.

### 1.2 Run the release gate

```sh
python3 scripts/release_gate.py personal-llm-gateway:candidate
```

Every check must pass. The gate runs against the exact candidate digest and
never rebuilds or substitutes the artifact.

### 1.3 Start it

```sh
docker compose up -d
docker compose logs -f gateway
```

The HTTP server uses a bounded worker pool (default 32 concurrent requests) and
a per-client rolling limit of 120 requests per 60 seconds. Configure them with
`GATEWAY_MAX_WORKERS`, `GATEWAY_RATE_LIMIT_REQUESTS`, and
`GATEWAY_RATE_LIMIT_WINDOW_SECONDS` (or the corresponding `--max-workers`,
`--rate-limit-requests`, and `--rate-limit-window-seconds` options). Over-cap
requests receive HTTP 429 with error type `rate_limited`. This is local capacity
protection, not an access-control boundary or protection against an on-path LAN
attacker; it does not authenticate or authorize clients. Client identity is the
peer IP observed by the socket, not a request header.

The gateway binds one listener at `GATEWAY_BIND:GATEWAY_PORT` (default port
`8765`), serving `/owner/`, `/v1/`, `/health`, and `/version`. It refuses
wildcard binds. There is no setup listener and no second port: first-run
setup, sign-in and password recovery all happen on this one listener or on
the gateway host itself (§1.4). Port `8766` stays reserved and refused.

Three credentials, deliberately separate:

* **Owner console and configuration API** (`/owner/*`) are protected by one
  owner account — always, except while the owner has set
  `owner_console_auth=false`, which serves them without a session
  (`docs/OWNER_AUTH.md` §Turning console sign-in off) — username and
  password, verified server-side with PBKDF2,
  delivered as an `HttpOnly`, `SameSite=Strict` session cookie with a
  12-hour TTL. A fresh gateway has no account at all — you create the first
  one in first-run setup (§1.4) using a one-time setup code read from the
  gateway host. There is no default credential; `admin/admin` does not
  exist.
* **Model API** (`/v1/*`) is **keyless by default**: any device that can
  reach this address is a caller until the owner sets an optional client
  API key from Settings → Access. `/version`, `/health` and `/metrics` are
  unauthenticated either way.
* **Machine keys** are the third credential: console-minted automation
  credentials for the owner API (Settings → Machine keys), scoped `read`
  or `write`, stored only as a hash under a separate pepper. A machine
  key is not a login — it never issues a session cookie — and not the
  client API key; `/v1` refuses it. Procedures in §2, design in
  `docs/OWNER_AUTH.md`.

Do not expose the address beyond a LAN you trust. The listener is plain
HTTP, so the owner password, the session cookie and the client key cross
the LAN in clear text and the cookie is not marked `Secure` (set
`GATEWAY_COOKIE_SECURE=1` only when you terminate TLS in front of the
gateway). "Trusted LAN" therefore also means "no on-path observer".

### 1.4 First-boot setup, sign-in, and password recovery

**First boot — create the owner account.** A fresh gateway has no owner
account, so `http://10.10.11.109:8765/owner/` shows *Set up the owner
account* instead of the console shell. The one-time setup code is
server-side only; read it from the gateway host, never from HTTP:

```sh
docker compose logs -f gateway | grep "one-time setup code"   # server stdout
# or, from the state volume (mode 0600 inside a 0700 directory):
docker run --rm -v personal-llm-gateway-state:/state \
  python:3.14-slim cat /state/owner_setup_code
```

* On the very first start the gateway prints the code itself:
  `first-run owner setup required: one-time setup code ...: <code>`. On
  every start after that, until setup is completed, it prints only the path
  (`... the one-time setup code is at /var/lib/gateway/owner_setup_code`).
* No endpoint, error body, log line or access-log record ever returns the
  code. Anyone who can read it can create the owner account, so the gateway
  host and its container logs are secrets.

Enter the code, a username, and a password (8–1024 characters, not equal to
the username), then **Create owner account**. The code is consumed exactly
once and the file is deleted; setup attempts are throttled like sign-in.
The console signs you in on success. There is no default account to fall
back on. The code stays in `owner_setup_code` until it is consumed, so a
lost copy is only a re-read; if that file is deleted while setup is still
pending, no HTTP surface re-mints it — restore the file, or clear the
`setup` field in `owner_auth.json` and restart, and the next start mints a
fresh code.

**Signing in.** Open `http://10.10.11.109:8765/owner/` from a trusted LAN
device and enter the username and password. The session lives 12 hours,
validated server-side; expiry, **Sign out**, a password change, and
`--reset-owner-password` all revoke it server-side, so a browser that drops
the cookie changes nothing.

**Changing the password.** Settings → *Access* → *Owner account*: current
password, new password, repeat, then **Change password**. It rewrites the
stored hash and revokes every other session; the console you changed it
from stays signed in.

**Forgotten password — local recovery.** Requires filesystem access to the
data directory, on the gateway host:

```sh
python -m gateway --reset-owner-password            # prompts on stdin, twice
# inside the container:
docker compose exec gateway python -m gateway --reset-owner-password
```

The new password is read from **stdin, never argv** (argv is visible in the
process list and in shell history). A password passed on the command line
anyway is refused with exit 2 and is **not echoed back**: argument errors
print `<redacted>` in place of any argv value, so the secret stays out of
the terminal and of any captured stderr. Every owner session is revoked, and the
running gateway picks the change up **without a restart** — the store
re-reads the file when its fingerprint changes, so a stolen cookie stops
working at the moment of recovery rather than at the next reboot. If no
account exists yet it refuses and points you back to the one-time setup
code.

**Throttling.** Failed sign-ins and setup attempts are throttled in memory,
per account and per client address: 5 failures inside a 300-second window
return `429 login_throttled` with `Retry-After`, and the correct password is
refused too while the window is open. The throttle is in process memory, so
a gateway restart clears it.

**Model API.** `/v1/*` needs no key until you set one (§1.3). To require
one, use Settings → *Access* → *Model client API key* → *Generate*, then
hand the key to your clients; rotation keeps the previous key valid for a
10-minute grace window, and *Disable and restore keyless access* brings the
keyless default back. The gateway never edits a client's own configuration.

---

## 2. Everyday use

Everything else happens in the browser.

For related aliases, assign the same optional tag in Models & Routes, then use Publish group or Disable group. This only toggles publication for each alias; routes, fallback chains, authorization and per-alias policy remain independent. Publishing refuses the whole group when any member lacks a primary route.

| Task | Where |
|---|---|
| Connect a provider | Owner console → Providers |
| Test a provider | Owner console → Providers → *Test connection* |
| Refresh a catalogue | Owner console → Providers → *Refresh model catalog* |
| Configure a local model endpoint | Owner console → Providers → *Ollama (local)* / *llama.cpp server (local)* / *LM Studio (local)* → **Server base URL** → **Save endpoint** (see below) |
| Enable a model | Owner console → Models & Routes |
| Create, edit, or publish an alias | Owner console → Models & Routes (use **Edit** on an existing alias; its name remains fixed while its route policy is changed) |
| Manage profile metadata | Owner console → Profiles (does not restrict LAN invocation) |
| Read status and limits | Owner console → Status |
| Read the model leaderboard (rankings, date x model matrix, fallback rate) | Owner console → Leaderboard |
| Make the aliases appear in OpenCode's `/models` | Install `integrations/opencode/` once (see §3.1) |
| Preview a Chat Completions request without sending it | `POST /owner/api/preview` (JSON body, published alias in `model`) |
| Manage machine keys for MCP/automation | Owner console → Settings → Machine keys (mint, list, rotate, revoke) |

### Machine keys (MCP / automation)

Machine keys are the gateway's third credential (§1.3): console-minted
bearer tokens for automation against the owner API — the
`gateway-owner-mcp` server and anything else that cannot sign in
interactively. They are neither an owner login nor the model client
key. Design, the exact allowlists and the F1 `clear_cooldowns`
acceptance: `docs/OWNER_AUTH.md`; MCP contract: `docs/GATEWAY_MCP_SPEC.md`.

- **Mint.** Owner console → Settings → Machine keys → name + scope →
  **Create**. The value is shown **exactly once** — copy it before you
  leave the row; the console never shows it again, and it never
  appears in a log line, an export or `/version`. At most 100 active
  keys.
- **List.** The same tab shows display id, name, scope and last use
  for every key — never the value.
- **Rotate.** Same display id, new value shown once; the old value
  stops working **immediately** — there is no rotation grace window
  (unlike the model client key's 10-minute one).
- **Revoke.** The record is removed and the key is dead on the very
  next request: the store re-reads its file when the file changes, so
  no restart and no cache delay.
- **Scope.** `read` = status/state/leaderboard/capabilities/ledger
  reads; `write` = those plus configuration mutations. Credential
  enrolment, device flows, the machine-key endpoints and the
  client-key endpoint are refused even to `write`; `/v1` refuses every
  machine key; a key presented with a session cookie is refused with
  `403 cookie_rejected`.
- **Hand it to the MCP server** as its key (env/config per that
  server's README). Never paste a key value into an issue, a log, a
  chat or a shell that lands in shell history.

**Pepper incident response (`machine_key_pepper`).** If every machine
key suddenly answers `401 authentication_required` while owner sign-in
still works, or minting and rotating answer `500
machine_key_store_unavailable`, the pepper file is deleted, replaced
or corrupt:

1. The gateway fails **closed on purpose**: a live store re-stats the
   pepper on every use, so a bad or missing pepper stops every key on
   the very next request. Nothing keeps answering from a stale cache.
2. **Restore** `machine_key_pepper` (`0600` in the data directory)
   from backup, byte-for-byte — digests are keyed on it. Keys work
   again on the next request; no restart (the same fail-closed rule,
   forward: restored pepper → restored keys; `tests/test_machine_keys.py`
   `PepperFailClosedTests`).
3. If the pepper is unrecoverable: **revoke every dead key** (revoke
   does not need the pepper), then mint new ones — with no keys left,
   the next mint creates a fresh pepper. Every old value stays dead.
4. Never hand-write a stand-in pepper file while old keys exist: their
   digests were keyed on the original bytes, so they stay dead
   (correct, but confusing). If a key value may have leaked, rotate it
   — rotation kills the old value immediately, no grace.

### Configure a local model endpoint

Ollama, `llama-server` and LM Studio all expose the same OpenAI-compatible
`/v1` surface, so all three are configured the same way and need no key.

1. Sign in at `/owner/` and open **Providers** → *Ollama (local)*,
   *llama.cpp server (local)* or *LM Studio (local)*.
2. Enter the **Server base URL** — a plain origin, e.g.
   `http://127.0.0.1:11434` (Ollama) or `http://192.168.1.20:8080`. A
   trailing `/v1` is stripped for you; nothing else belongs in the field.
   Per server, the origin is the **root** — the gateway appends
   `/v1/chat/completions`, `/v1/models` (and Ollama's `/api/show`):
   * **Ollama** — `http://HOST:11434`. Ollama ignores any `Authorization`
     value, and the gateway sends no credential unless one is enrolled.
   * **llama.cpp (`llama-server`)** — `http://HOST:PORT` for whatever
     `--port` you passed: current upstream builds default to `9931`, older
     builds and the official Docker images to `8080`, so take the port from
     the server's startup log. `--host` defaults to `127.0.0.1`, so start it
     LAN-facing (`--host 0.0.0.0` or the LAN address) or the gateway cannot
     reach another machine. Optional `--api-key` pairs with
     **Optional local token**.
   * **LM Studio** — `http://HOST:PORT` from its local-network port display.
3. Choose **Save endpoint**. The URL is normalised and checked against the
   private-destination policy on save, and refused with a fixed reason if it
   fails — the value you typed is never echoed back (§6).
4. **Refresh model catalog** to discover models, then enable the ones you
   want in Models & Routes. Saving an endpoint itself makes **no**
   connection, and neither does a page load: the gateway opens no
   connection at boot and only ever dials the saved endpoint.

**Keyless by default.** All three servers are keyless out of the box, so
the gateway sends no `Authorization` header and you need to enter nothing
here. Enrol a token in **Optional local token** only if you enabled
authentication on the server itself (`llama-server --api-key`, LM Studio
*Require Auth*) — otherwise **Test connection** fails with an
authentication error.

**The destination is bounded, and checked twice.** The host must be inside
`127.0.0.0/8`, `10/8`, `172.16/12`, `192.168/16` or `fc00::/7`; public,
link-local and metadata addresses are refused at save time **and again
against the socket's actual peer after the TCP handshake, before any
request byte is written** (before the TLS handshake on `https://`). So a
hostname whose DNS answer changes between saving and requesting is refused
with nothing sent, and redirects are never followed. Use `127.0.0.1` for a
server on the gateway host: `[::1]` is outside the allowlist (there is no
`::1/128`), and `localhost` is refused whenever its answer carries that
IPv6 half — deliberately fail-closed.
Capabilities and cost stay **Unknown** until the server or an owner approval
evidences them, and the gateway never downloads a model.

### Per-provider concurrency (`max_in_flight`)

Every provider has its own in-flight cap: a request that would take a
provider past its cap is refused **immediately** with HTTP 429, error type
`rate_limited`, and the body
`local concurrency limit reached for this provider; retry later`. There is
no queue and no wait — the caller retries or the request fails fast. A
streamed response holds its slot until the stream finishes or closes; a
response the client abandoned before its stream ever started (the
connection vanished before the response headers, or a `HEAD` request)
gives the slot back when that request finishes. A slot is never leaked,
so a saturated provider always recovers on its own — no restart.
This is a local protection only: it is never a limit the provider
published, and it is per provider, per gateway process.

| Family | Default | Why |
|---|---|---|
| Cloud providers (`anthropic`, `chatgpt_codex`, `opencode_console`, `opencode_go`) | **32** | The owner's number; a cloud account can absorb concurrent streams. |
| Local servers (`ollama`, `llama_cpp`, `lm_studio`) | **4** | They share this machine's memory and GPU. 32 concurrent streams against a 27B model is an out-of-memory risk, not a speed-up. |

To change one: sign in at `/owner/`, open **Providers** → the provider, and
use **Concurrency** → **Maximum in flight** → **Save limit**. **Use
default** (or an empty field) clears the override and returns the provider
to its default. The card shows both numbers: the saved override and the
one in force right now.

* A value must be a positive whole number. `0`, a negative, a decimal, a
  string or a boolean is refused with HTTP 400
  (`invalid_max_in_flight`) and writes nothing. So is any whole number
  above **4096** — the hard ceiling for this field (`max_in_flight`
  cannot exceed `MAX_IN_FLIGHT_CEILING`): no value can be large enough
  to switch the cap off, which a persisted `10**400` used to do silently.
  A `config.json` that already carries a larger value is ignored on load
  and the provider runs at its default.
* A cap refusal never opens a cooldown. The route is not benched for the
  gateway's own capacity, so once a slot frees the next request is served
  normally — the `rate_limited` circuit still only ever opens for a
  provider's **own** rate limit.
* The override is stored in `config.json` as the provider's
  `max_in_flight` (non-secret, `null` when unset) and is read on **every**
  acquire, so it takes effect on the next request — no restart, no rebuild.
* The endpoint is `POST /owner/api/provider` with
  `{"action": "set_max_in_flight", "provider_id": "...", "max_in_flight": N}`.
  It is owner-session-gated exactly like every other provider setting
  (401 without a session while console sign-in is on — the default; with
  `owner_console_auth=false` it is served without one, see
  `docs/OWNER_AUTH.md`); it adds no unauthenticated surface.
* `GET /owner/api/state` reports `max_in_flight` (the override, `null` when
  unset) and `max_in_flight_effective` (the number in force) per provider.

### OpenCode Console Jev / System One

To use Jev, open **Providers → OpenCode Console Jev**, enter the API key in its
write-only field, then explicitly test and refresh the catalogue. Enable the
Jev model you intend to call. The native API is `POST /v1/systemone`; it needs
a model, a state, and named `noul`, `choice`, or `score` questions. The
matching `GET /v1/systemone/models` returns only saved, enabled model
metadata and never makes a provider call. System One state/questions are sent
to OpenCode Console for that direct request; they are not retained by the gateway.

The Status ledger is metadata-only and asynchronously persisted to
`request-ledger.db` in the state volume (SQLite, WAL, one writer thread -
the request hot path never waits on it). Retention is by AGE ONLY: rows
older than 30 days are pruned on load and on a daily timer, with no size
cap on history, so the full window survives at the measured write rate
(~5,200 requests/day, ~156k rows over 30 days, tens of MiB on disk). The
plaintext `request-ledger.jsonl` journal tail (5 MiB plus two rotations)
is still written for debugging; it is not the history bound - the first
boot imports it and its rotations without losing a row. Disk-write errors
or a full queue can still cause a persistence update to be dropped, and
the Status view reports the store's real size, row count and oldest row
(`store` block in the durability section). The in-memory ring
is the live view. Local usage aggregates cover the last 24 hours; costs use
published prices only and display their source and observation date. Request
statistics can be selected for the last hour, day or 30 days, grouped by alias,
serving provider and model. Counts and latency percentiles use available ledger
history (up to 10,000 recent records); unavailable usage, latency or prices are
reported as **Unknown**, never zero. This is not a billing system.

The same append path also maintains `request-ledger-rollup.json` beside the
store: one row per UTC day, alias, provider and model with requests, input,
output and cached tokens, fallbacks by reason, errors by class and statuses,
plus a per-day trend. Reasons are folded onto a closed vocabulary when the
row is written - `cooldown`, `route_ineligible`, `upstream_failure`, `other`,
`unknown` - so a per-second cooldown sentence never becomes a bucket or a
sidecar key. Its `coverage` block also reports the in-memory request index
bound (`index_entries` / `index_max_entries`, default 50,000) and anything
that bound evicted (`index_evicted` / `index_amendments_dropped`), so totals
are auditable on a long-running appliance. It is rebuilt from the ledger at
every start, so daily history survives restarts and ring eviction. To
regenerate it by hand after restoring or moving a state volume:

```sh
PYTHONPATH=src python3 -m gateway.ledger_persistence /var/lib/gateway/request-ledger.jsonl
```

The command prints the rollup's `coverage` and exits `1` when coverage is
incomplete (rows it could not represent) and `2` when there is no ledger;
`--dry-run` reports without writing. The sidecar holds aggregates only -
no request identifier, no prompt, no response, no cost estimate - is written
0600, and is not a billing record.

The Status page reads that rollup. The usage table keeps the live 24-hour
ring as its default window and adds a selector for every day in the
requested window (`?days=N` on `/owner/api/state`, default 7, clamped to 30),
including its fallbacks, cost estimate and price source. Cost cells show the
**known subtotal** for the group with the unknown coverage next to it
(`known subtotal; N of M requests unpriced (x in / y out tokens)`); a group
with no priced request renders **Unknown** with its coverage rather than a
bare `Unknown`, an unpriced provider is labelled "no published actual/list
price - never inferred", a cached count the upstream never reported is named
instead of guessed (`cached count missing on N request(s)`, never read as a
reported zero), and a request that reported no usage is Unknown, never
`$0.00`. The **Fallback
analytics** card reports the overall fallback rate, the reasons behind it,
the per-alias, per-provider and per-model split, and the daily trend. A day
with no recorded rows is drawn as a gap and a day before the first recorded
row is labelled outside the recorded history - neither is ever drawn as
zero - and a day still in progress is labelled as such. The poll reads the
in-memory rollup, never the ledger file, and the whole view is metadata
only: prompts, responses, tokens and secrets are never stored.

The model leaderboard is a **separate read-only endpoint**,
`GET /owner/api/leaderboard?days=N` (default 7, clamped to 30), so the
`/owner/api/state` poll keeps its 2s bar. It returns the ranked models
(primary metric: input + output tokens, with requests, share of the window
and the cost block) and the date x model matrix. If the console's leaderboard
view is slow or empty, check this endpoint directly first: `available: false`
with `unavailable_reason` says whether persistence is off
(`durable_rollup_disabled`) or the sidecar schema is from a future build
(`unrecognised_rollup_schema`). Cost cells follow the same rules as the usage
table — a null amount means **no published price**, never zero — and a day
with no rows shows a status (`gap`, `outside_coverage`, `no_rows`), never a
zero row. Like the rest of the console this endpoint is metadata only and is
reachable only from the trusted LAN.

The **Leaderboard** tab renders it: open the tab and the console fetches
`/owner/api/leaderboard?days=N` (window selector: 7, 14 or 30 days) without
touching the state poll. The tab shows the ranked models with share % and the
honest cost cell (an unpriced model reads **"no published price"** with its
reason and unpriced coverage, never `$0.00`), the stacked daily usage chart,
the date x model matrix, a leader-vs-challenger comparison, the fallback-rate
panel and the methodology panel (which cites each model's price source URL
and observation date). A day with no recorded data is hatched in the chart
and shown as a single "no data" span in the matrix; a measured zero renders
as `0`. If the tab shows *Leaderboard unavailable: durable_rollup_disabled*,
ledger persistence is off — enable it (§ rollup) and reload; the endpoint
itself can be checked directly with `curl http://127.0.0.1:8765/owner/api/leaderboard`.

Provider tests and catalogue refreshes are **explicit actions**. An ordinary
page load or configuration save never calls a provider. Anthropic uses the
native Messages API with a separately stored API key; its catalogue is loaded
only when **Refresh model catalog** is selected. Anthropic-dialect models are
bridged to Chat Completions and Responses; Anthropic-compatible clients can use
`POST /v1/messages` under the same Host/Origin and forwarded-header policy as
the other API routes. Credentials are stored separately and never returned by
state or status APIs.

The read-only preview endpoint returns the converted payload for the first
currently eligible route, plus its derived session identifier. It never invokes
a provider or adds a ledger event; any fallback after a provider error is
necessarily unevaluated. Treat the returned request body as sensitive: it is
returned only to the caller and is not logged or retained.

---

## 3. Verifying a deployment

```sh
curl -s http://10.10.11.109:8765/version | python3 -m json.tool
curl -s -o /dev/null -w '%{http_code}\n' http://10.10.11.109:8765/health
curl -s http://10.10.11.109:8765/v1/models
```

`/version` must report exactly the five keys. `image_revision` is the
digest of the running image only when the release step recorded it for the
running container; it reports `unknown` otherwise (a cutover that bypassed
`scripts/release.py`, or a record left behind by an earlier container).
`docker inspect --format '{{.Image}}'` on the container remains the
authoritative image check.

### 3.1 The OpenCode integration

The gateway exposes its published aliases to OpenCode at
`GET /v1/alias-catalog`. Install the plugin once on every machine that runs
OpenCode and should see the gateway:

```jsonc
// ~/.config/opencode/opencode.jsonc
{
  "plugins": [
    {
      "package": "/path/to/llm-gateway/integrations/opencode",
      "options": { "baseURL": "http://10.10.11.109:8765/v1" }
    }
  ]
}
```

Verify it, without spending a provider request:

```sh
curl -s http://10.10.11.109:8765/v1/alias-catalog
opencode api get /api/model | grep -c personal-gateway
```

The first command must list the published aliases with their context
decision; the second must be greater than zero. An alias that is
unpublished in the console disappears from the selector on the plugin's
next refresh (60 seconds), so unpublishing is a real revocation for this
client - it is not access control for the API itself, which stays keyless
until an owner sets a client key (§1.4).

The snapshot uses a documented model window when Go's own endpoint table
identifies the exact model and the model provider publishes its window:
MiMo-V2.6-Flash is 1,048,576 tokens and MiniMax M3.1 Flash is 1,000,000.
Do not substitute a Go price tier for a window. An alias without equivalent
evidence is shown with the owner-chosen **200,000-token OpenCode working
budget** and labelled `(200k client budget; upstream unknown)`. That is a
client-side fallback, not a provider guarantee; do not hand-write a guessed
number into a model entry.

The base URL must be the gateway's private address with the `/v1` prefix.
The plugin refuses a public host, an HTTPS host, another path, or a URL
carrying credentials or a query.

If the gateway is unreachable or predates `/v1/alias-catalog`, the plugin
logs a warning and lists no gateway aliases; it never invents a model, and a
failed refresh keeps the last complete inventory rather than deselecting the
model in use.

The console and setup path are not separate surfaces: `/owner/` is served on
this listener, while `/setup/` is absent — confirm there is no second
listener on port `8766` through your normal host-level listener inspection.
On the owner-session candidate, owner state is **not** readable without a
login: the pre-auth surface is exactly the console shell and its assets,
`GET /owner/api/auth/session`, `POST /owner/api/auth/setup` and
`POST /owner/api/auth/login`; every other `/owner/*` route answers `401
authentication_required` before its handler runs until a valid session
cookie is presented (§1.4) — always, except while the owner has set
`owner_console_auth=false`, which serves them without a session
(`docs/OWNER_AUTH.md`). Browser mutations still require the exact
canonical `Origin`; CSRF is carried by the `SameSite=Strict` cookie plus the
existing `Origin`/`Host` checks, not by a bootstrap token. The OpenCode
integration above calls `/v1`, which stays keyless until an owner sets a
client key in Settings → Access — once one is set, every `/v1` request must
send `Authorization: Bearer <key>`.

The deployed gateway still serves the previous no-login boundary, where any
reachable peer administers `/owner/*`; the session boundary above ships only
with this candidate (see `docs/OWNER_AUTH.md`).

---

## 4. Cutover

The live deployment at `10.10.11.109:8765` is **left running** until the
candidate has passed its gates. Cut over side-by-side-safe:

1. Verify the candidate in an isolated host/network namespace or disposable
   environment using an alternate single listener port.
2. Exercise the owner console, synthetic configuration path, no-route API
   refusal, and release identity. Provider requests are out of scope for this
   deployment check; tests use mocked transport only.
3. Stop the disposable candidate. For a production cutover, use the approved
   immutable candidate and exact state-volume release procedure.

---

## 5. Rollback

The gateway's state lives in one volume. Before an upgrade, use Owner console → Backup → Download sanitised export. This includes configuration only; provider credentials and caller-token hashes are not included. For a local in-volume snapshot, enable a scheduled snapshot in that same panel. The manual copy commands below remain an operator fallback.

**Rollback the image.** Redeploy the previous image, without rebuilding
it, and publish that identity the same way.

```sh
docker compose down
GATEWAY_IMAGE=personal-llm-gateway:<previous> \
  python3 scripts/release.py --image personal-llm-gateway:<previous>
```

**Rollback the configuration.** The configuration is a single JSON file on
the volume. **Back it up before every upgrade**, and restore it explicitly.

Restore a downloaded console export through Owner console → Backup → choose the JSON file → Validate and import. The gateway validates identifiers and all routes before replacing configuration; credentials remain separate. A failed import leaves the stored config unchanged.

The manual in-volume copy remains available when the console is unavailable:

```sh
# before an upgrade
docker run --rm -v personal-llm-gateway-state:/state \
  python:3.14-slim sh -c 'mkdir -p /state/snapshots && cp /state/config.json /state/snapshots/config.json.before-upgrade'
```

```sh
# to restore
docker compose down
docker run --rm -v personal-llm-gateway-state:/state \
  python:3.14-slim cp /state/snapshots/config.json.before-upgrade /state/config.json
python3 scripts/release.py --image personal-llm-gateway:candidate
```

The console's scheduled snapshots are disabled by default and are written to `/state/snapshots/` when enabled.

Validate the restored file before starting:

```sh
docker run --rm -v personal-llm-gateway-state:/state \
  python:3.14-slim python -c "import json;json.load(open('/state/config.json'));print('valid')"
```

The credential store is a separate file and is never touched by a
configuration rollback. Tokens the provider has already issued are not
revoked by a rollback; the gateway will refresh or reject them on its own.

The open circuits are a third file, `circuits.json`, and a configuration
rollback leaves it alone as well. That is deliberate: a gateway that comes
back after an outage stays away from the provider that was exhausted,
instead of calling it again on the first request. A confirmed-exhaustion
circuit is bounded (at most `GATEWAY_EXHAUSTED_COOLDOWN_MAX_SECONDS`,
default 30 minutes), so it expires on its own; the owner console's
**Clear cooldowns** action clears one provider's circuits deliberately
without editing this file by hand.

---

## 6. Troubleshooting

| Symptom | Cause | Action |
|---|---|---|
| `refusing to start: a wildcard bind...` | `GATEWAY_BIND` is `0.0.0.0` | Set it to an explicit local RFC1918 IPv4, IPv6 ULA, or loopback address |
| The owner console will not load | The configured private address is not assigned to the host, or the listener is unavailable | Use the configured private address and port; IPv6 URLs need brackets, e.g. `http://[fd12::1]:8765/owner/` |
| The console shows *Set up the owner account* instead of the shell | Fresh gateway: no owner account exists yet, so first-run setup is due | Read the one-time setup code from the gateway host — server stdout or the `0600` `owner_setup_code` file; it is never an HTTP response — and complete setup (§1.4). The code is single-use |
| An owner API call answers `401 authentication_required` | The session cookie is absent or expired (12-hour TTL), or the caller is an unauthenticated LAN peer | Sign in again at `/owner/` (§1.4). On the deployed no-login gateway this response does not occur — that build has no owner session |
| A machine key answers `401 authentication_required` while owner sign-in still works, or minting/rotating answers `500 machine_key_store_unavailable` | `machine_key_pepper` is missing, replaced or corrupt — the store fails closed on purpose | Pepper incident response, §2 *Machine keys*: restore the original file byte-for-byte, or revoke every dead key and re-mint |
| A machine key answers `403 scope_required` or `403 machine_key_forbidden` | `scope: read` on a mutation, or the path is outside the read/write allowlists (or the action is a blocked provider action) | Re-mint with `write` scope for configuration paths. Credential enrolment, device flows, the machine-key and client-key endpoints, and `/v1` are refused to every machine key |
| A machine key answers `403 cookie_rejected` | The request carries both a machine key and an owner session cookie | Send the key without the cookie — the two credentials never combine |
| Sign-in returns `429 login_throttled` | Five failed attempts inside a 300-second window, per account and per client address | Wait for the `Retry-After` window; the correct password is refused while it is open. The throttle is in memory, so a gateway restart clears it |
| A provider shows *Failed* | The credential was rejected or the provider is unreachable | **Test connection** and read the message; the stored credential is never displayed |
| Saving a local endpoint answers `400` with *the endpoint host must be a private or loopback address* | The literal host is outside `127.0.0.0/8`, `10/8`, `172.16/12`, `192.168/16`, `fc00::/7` — a public, link-local/metadata or otherwise-outside address. `[::1]` lands here too: the allowlist has no `::1/128`, so IPv6 loopback is refused on purpose (fail-closed) | Use `127.0.0.1` for a server on the gateway host, or the server's LAN address for one elsewhere. The console placeholder already suggests `http://127.0.0.1:11434` |
| Saving a local endpoint answers `400` with *the endpoint host did not resolve only to private or loopback addresses* | The hostname resolved to at least one address outside the allowlist — a public or metadata answer, or a mixed one. `localhost` lands here when its `::1` half is in the answer | Point at an IP literal (`127.0.0.1`, `192.168.x.x`) or a hostname that resolves only into the allowlisted ranges |
| A local endpoint save is refused with *the endpoint must be an http or https URL*, *must not carry a username, password, query or fragment*, *must include a host*, *the endpoint port must be between 1 and 65535*, *the endpoint path must be a short plain prefix such as / or /v1*, or *the endpoint host could not be resolved* | The URL shape, not the destination: a non-HTTP scheme, credentials or a query/fragment embedded in the URL, no host, an unparseable port, a path outside the short plain-prefix grammar (including percent-encoding or `.`/`..`), or a name DNS cannot answer | Paste a plain origin only — `http://127.0.0.1:11434`, `http://192.168.1.20:8080`, optionally with a short path such as `/v1`. A trailing `/v1` is stripped for you, and the refused value is never echoed back |
| A model shows an empty dialect | The provider serves it but does not document the endpoint | Enter it manually and set the dialect, or use an OpenAI-dialect model |
| A local request or **Test connection** fails with *the configured local endpoint did not resolve to a private address* | Connect-time peer check: the address the socket actually reached is outside the allowlist. The save-time answer went stale — DNS changed, or the name now rebinds to a public/metadata address | Fix the DNS answer or save an IP literal. This refusal happens after the TCP handshake and **before any request byte** (before the TLS handshake on `https://`), so nothing was sent; it is the boundary that makes a rebinding hostname safe |
| A local provider answers with *no local endpoint is configured for this provider* | No endpoint has been saved, so the provider stays *not configured* and is never contacted | Save one: Owner console → provider card → **Save endpoint** (§2). Saving an endpoint makes no connection by itself; **Refresh model catalog** is what discovers models |
| A route says the dialect cannot be translated | The model is served over Google v1beta or includes a control that cannot be represented | Anthropic Messages models are bridged to/from OpenAI dialects; unsupported controls are refused rather than guessed. Google v1beta remains unsupported |
| Limits show *Unknown* | The provider publishes no machine-readable allowance for that plan | That is the truthful answer. Check the provider's own console. ChatGPT account windows are the exception: `account_quota` on the ChatGPT provider row is read read-only once per 60s state poll, and `unavailable_reason` there (`credential_missing`, `http_<code>`, `timeout`) names the reason instead |
| A request failed with *no eligible route* | Every route was excluded | The error names each excluded route and why |
| The published catalogue is empty | No alias is published yet | Publish one in Models & Routes |
| OpenCode shows no gateway models | The plugin cannot read `/v1/alias-catalog` - the gateway is unreachable, or it predates the endpoint | `curl` the endpoint; redeploy with `scripts/release.py`; the plugin warns in the OpenCode log and recovers on its own |
| An inbound image reaches the gateway but is refused | The client sent pixels while the deployment still reports every alias text-only | Expected until this release is deployed: the refusal is the gateway's own `no_eligible_route` with `capability: image_input`, not a provider error. Deploy, then re-check `GET /v1/alias-catalog` |
| An OpenCode model says *200k client budget* | The alias has a route whose context limit is not published | That is the owner-chosen client budget, not a provider guarantee. The gateway reports Unknown on purpose |
| An OpenCode reasoning variant had no effect | The serving route does not publish that level | Expected: the level is dropped **for that route** and the substitution is recorded in the request ledger |
| OpenCode offers no image input for a model you know accepts images | No route of that alias has *source-backed* image evidence | The gateway records image support per exact model ID from the documentation page named in the row's `image_input_source`, and never infers it from a model name or dialect. Check `data/go_catalog.json` for `supports_image_input` and `image_input_source`, then re-run `scripts/build_go_catalog_snapshot.py` and redeploy — every boot re-applies that evidence to the stored rows, so a redeploy is what makes a new verdict visible |
| An image request failed with *no eligible route* | No route of the alias accepts image input | Expected: the gateway excludes an image-incapable route rather than letting the provider reject it opaquely. The error names the excluded routes and the reason |
| `/version` reports `unknown` | The deployment was not released properly | Deploy with `scripts/release.py --build`; it publishes the identity. The gateway also warns about this at startup |
| A client reports *an empty stream with no finish reason* | The stream was not framed correctly | Update to a build that emits terminated SSE frames; check the raw bytes for `\n\n` between frames |
| A quiet stream appears idle | The provider has not emitted a frame yet | The gateway sends an SSE `: ping` comment every 15 seconds without client-facing output; SSE clients ignore comments |
| A fallback did not run | The alias has not authorised it, or the trigger is off | The alias needs **Authorize fallback**, **both** the trigger and the confirmation of the fallback provider. The record says which was missing |
| A fallback did not run and the record says *skipped* | The primary was in a local cooldown, was not capable of serving the request (for example an image on a text-only route), or was held by a live recovery probe | Skip paths require authorization, confirmation, and the trigger the skip contributes - `route_ineligible` for capability/configuration, the cooldown's own trigger for an automatic cooldown, `explicit_cooldown` for a cooldown you placed, `probe_contention` for a held probe. A skipped primary was never invoked, so tool-bearing requests can switch safely if this owner gate passes; a failed attempted route may have executed a tool and remains non-fallbackable. The side-effect control is not part of this gate for a skip: a skipped primary never reached a provider, so a tool-bearing request may switch (a *failed* attempt with tools still never falls back). The owner console warns when trigger coverage is missing. The cooldown itself is local policy, not evidence the provider reset; the Status view shows the hold and the next probe, and **Clear cooldowns** on the provider card releases it when you have confirmed the reset window yourself |
| A request was refused with *fallback not used* | The owner's gate declined the switch away from a skipped or failed primary | Expected and safe: the message names what was missing (authorization, confirmation, or the disabled trigger). The ledger records the request as served by nobody with its skips, and no fallback is claimed. Enable what the alias needs on Owner console → Aliases, or accept the refusal |
| A request was served by a phase route, not the primary | The alias has an enabled phase-routing policy and a phase applied (from the caller's `metadata.personal_gateway.phase` hint or from Jev inference) | Expected. The ledger records `phase` and `phase_source` (`caller`/`jev`/`default`) as metadata only. A phase route is the primary for that phase, **not** a fallback |
| Jev did not pick a phase | Inference is off for the alias, the request carried tools, Jev was uncertain, or Jev failed | Expected and safe: the ordinary route is used and the ledger records why. Raise `minimum_confidence` only if you accept more guessing, never to force a route |

### Hermes profile vision routing (where an inbound image goes)

The gateway publishes image capability; the **client** decides whether an
attached image travels as pixels or is pre-described into text first. On
this LAN that client is Hermes, and the decision lives in each profile's
`config.yaml`, not in the gateway. It reads, in order: an explicit
`agent.image_input_mode`, then an explicit `auxiliary.vision` backend
(which forces the describing path), then a `supports_vision` override,
then catalog probes. With none of those the verdict is *unknown*, which
Hermes treats as "describe it" — and `auxiliary.vision.provider: auto`
then has no backend to describe with, so the image went nowhere. That was
the undefined state this release reconciles.

Every profile whose main model is a gateway alias now declares, for all
four published aliases:

```yaml
providers:
  personal-llm-gateway:
    models:
      coding:    {supports_vision: true}
      flash:     {supports_vision: true}
      hermando:  {supports_vision: true}
      pm:        {supports_vision: true}
```

The value is not a new claim: it repeats what `GET /v1/alias-catalog`
reports for those aliases, so a profile and the gateway cannot disagree.
Applied with `hermes config set providers.personal-llm-gateway.models.<alias>.supports_vision true`
per profile (never by hand). The exact choice per profile, verified by
calling `decide_image_input_mode()` against that profile's own config:

| Profile (channel) | Main model | Image path |
|---|---|---|
| default (base `mattermost`) | `personal-llm-gateway/hermando` | **native** pixels → gateway → image-capable route |
| pm | `personal-llm-gateway/pm` | **native** pixels → gateway |
| designer, qa, security, tester, ux | `personal-llm-gateway/coding` | **native** pixels → gateway; the primary is text-only, so the image-capable MiMo fallback serves it |
| coding (no channel; CLI and kanban sessions) | `personal-llm-gateway/coding` | **native** pixels → gateway, as above |
| soft-dev | `personal-llm-gateway/coding` | **described** by `opencode-go/mimo-v2.6-flash` — its explicit `auxiliary.vision` override, deliberately kept because it resolves without the gateway |
| researcher | `opencode-go/gpt-6-luna` | **native** pixels; the verdict comes from the models.dev catalog, not this gateway — *configured separately* |
| vacation-planner | `opencode-go/kimi-k3` | **native** pixels; same — *configured separately* |

* `researcher` and `vacation-planner` do not use this gateway as their main
  provider, so nothing in the gateway's evidence reaches them; re-check them
  when the model changes.
* Until this release is deployed the gateway reports every alias
  text-only, so the native path ends in the gateway's own deterministic
  refusal (`503 no_eligible_route` with `capability: image_input`) rather
  than a silent drop. Deploy before expecting an image to be answered.
* No gateway restart is needed: Hermes re-reads `config.yaml` on its file
  signature, so `hermes config set` applies to the next image.
* Re-check one profile:

```sh
HERMES_HOME=/home/hermes/.hermes/profiles/pm \
  /home/hermes/.hermes/hermes-agent/venv/bin/python - <<'PY'
import sys
sys.path.insert(0, "/home/hermes/.hermes/hermes-agent")
from hermes_cli.config import load_config
from agent.image_routing import decide_image_input_mode
cfg = load_config()
print(decide_image_input_mode(cfg["model"]["provider"], cfg["model"]["default"], cfg))
PY
```

---

## 7. What an operator must never do

* **Never** bind a wildcard address. There is no flag for it, on purpose.
* **Never** create a firewall or router rule for this gateway.
* **Never** expose it publicly, or put it behind a public reverse proxy.
* **Never** commit the credential store, a device code, a token, or a
  passphrase.
* **Never** rebuild an image during release verification. The gate runs
  against one exact digest.
* **Never** change a subscription, buy an allowance, or top up a balance as
  part of a deployment.
* **Never** sign in to a provider account on the owner's behalf. The owner
  completes the device-code flow in their own browser.

### Alias budgets

Set an optional monthly USD budget while editing an alias. It is a local soft cap: the gateway checks recorded spend before routing, so concurrent in-flight requests may exceed it. Spend uses only successful ledger events with token counters and published per-token prices. If any spend in the month is unpriced or the ledger history is incomplete, the console reports the alias uncapped with Unknown spend; it never treats unknown as free. A refusal returns HTTP 429 with the configured amount, known spend, and the UTC month-boundary reset timestamp.

---

## 8. Recovering a held provider

When a provider reports that the account's allowance is exhausted, the
gateway opens a **bounded** provider-wide cooldown: at least 15 minutes,
at most `GATEWAY_EXHAUSTED_COOLDOWN_MAX_SECONDS` seconds (default
`1800` = 30 minutes; set it with `--exhausted-cooldown-max-seconds` or
the environment variable, a whole number of seconds ≥ 1). The window does
not grow with repeated failures, so a subscription window that reopens on
the provider's own schedule is never stranded behind a multi-hour hold.

Transient route failures are also bounded: the exponential backoff has a
global cap of **3600 seconds (1 hour)** by default, configurable with
`--transient-cooldown-max-seconds` or `GATEWAY_TRANSIENT_COOLDOWN_MAX_SECONDS`
(a positive whole number of seconds). Its `base × 2^(failures−1)` sequence
is unchanged below the cap. This local transient cap does not shorten an
upstream `Retry-After` for a rate-limit response; the provider's explicit
wait remains authoritative.

Recovery is automatic and stampede-safe: when the window ends, the next
request claims the single **half-open probe**; every concurrent request
is skipped with a truthful reason (`a recovery probe is already in
flight ...`) and falls back if the alias allows it. A successful probe
clears the provider and route circuits and the configured primary serves
again with `fallback_used=false`; a failed probe re-arms the circuit
within the same bounded window and records the failure class and count.

To recover immediately after **you have confirmed** the subscription or
reset window is open:

1. Open the owner console → Providers.
2. Press **Clear cooldowns** on the provider card and confirm. The
   response lists the cleared circuits (metadata only — never a
   credential) for exactly that provider.
3. Press **Test connection**, or test an alias, to verify service.

The same action over the API (canonical `Origin` required, like every
owner mutation):

```sh
curl -s -X POST http://10.10.11.109:8765/owner/api/provider \
  -H 'Content-Type: application/json' \
  -H 'Origin: http://10.10.11.109:8765' \
  -d '{"action": "clear_cooldowns", "provider_id": "chatgpt_codex"}'
```

Optional: add `"probe_alias": "<alias>"` to run exactly one explicit
recovery probe through that alias as part of the same action; the
response reports the serving provider/model and whether a fallback was
used. Without it, no provider is called.

The Status view's cooldown table shows, per circuit: the original failure
timestamp, scope (whole provider or one route), failure count, current
hold, the next eligible probe time, and whether a probe is in flight.
The request ledger labels every non-primary outcome — a phase-route
selection is labelled `phase route`, a circuit skip is labelled
`held by local circuit`, and a cross-provider fallback carries its
reason — so the three are never confused.

---

## 9. Legacy user systemd unit (retired)

An audit on 2026-10-24 found a separate **user** systemd unit at
`/home/developer/.config/systemd/user/personal-llm-gateway.service` on
the dev box. It describes a loopback-only venv deployment
(`.venv/bin/llm-gateway --host 127.0.0.1 --port 8765`) that is **not**
the supported deployment: the production gateway is the Docker Compose
stack on the private LAN address. At audit time the unit was `inactive
(dead)` and `disabled` — it is not a live second surface.

**Decision:** the unit is retired. It is not started, enabled, or
documented as a supported surface; the Compose stack is the only
supported deployment. Removing the unit file is an operator action (it
was deliberately not modified as part of the cooldown-recovery feature
work). If the loopback venv deployment is ever wanted again, it must be
designed and documented deliberately — do not revive this unit by
accident with `systemctl --user enable --now`.


## 10. Running a bounded capability probe

A probe sends **real, paid inference requests** to one reviewed pair:
`opencode_go` / `mimo-v2.6-flash`. Nothing else can be probed. Budget and
time: at most two requests, worst case well under one US cent, and under a
minute of wall clock.

1. **Stage the pair.** The provider must be `connected` and the model staged
   with the OpenAI Chat dialect. A provider that has not been tested is
   refused with `provider_not_configured`.
2. **Preview** (`POST /owner/api/capabilities/preview`, or the console's
   capability panel). Send the exact probes and the owner fields:
   `authorize_billing_route: true`, `spend_policy: "estimate_accepted"`, and
   `max_spend_estimate_usd` covering the estimate. The preview replies with
   the full contract — request count, output-token cap, timeouts, byte
   ceilings, the tariff it priced against and its observation date — plus a
   single-use `confirmation`. Preview sends nothing upstream.
   * `no_probe_protocol` → no reviewed protocol entry exists for this exact
     pair; the probe registry is an opt-in allowlist (the console shows its
     coverage before you preview). The published tariff, if the catalogue
     has one, still renders — it is not the blocker.
   * `no_trusted_price` → the published tariff is missing or stale; nothing
     can run until the catalogue is refreshed.
   * `hard_cap_unavailable` → a hard dollar ceiling was requested; OpenCode
     Go publishes no enforceable cap, so the run is refused rather than
     estimated as guaranteed.
3. **Run** (`POST /owner/api/capabilities/run`) with that confirmation, the
   same probes and owner fields, and the preview's `base_revision`. The
   response carries per-capability evidence, the request count, and a
   `proposal_id` that is *pending*.
4. **Cancel if needed** (`POST /owner/api/capabilities/probe/cancel`) while
   it runs; the response reports `cancelled: true` and the run stops before
   the next request.
5. **Decide** the proposal (`POST /owner/api/capabilities/decision`) if the
   evidence should become metadata. Until you do, the run changes nothing.

Reading the evidence: `observed_supported` is observed; `inconclusive` means
the run could not tell; `not_tested` means the run stopped before that probe
(failure, missing cost accounting, cancellation, or the deadline). A probe
never proves a capability is *unsupported*.

What the run must not disturb: config revision, routing, cooldowns and the
ledger. If any of those change after a probe, that is a defect — capture the
run id and open a review finding.
