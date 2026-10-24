# Operator runbook

Everything an operator needs to run, verify, cut over, and roll back the
gateway. Nothing here is required for *using* the gateway — that is all done
in the browser.

---

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
wildcard binds. There is no setup listener, second port, owner passphrase, or
login/bootstrap step.

Open `http://10.10.11.109:8765/owner/` from a trusted LAN device. Any device
that can reach this address can administer the gateway and invoke its models;
this is an intentional product boundary, not authentication. Do not expose the
address beyond a LAN you trust.

---

## 2. Everyday use

Everything else happens in the browser.

| Task | Where |
|---|---|
| Connect a provider | Owner console → Providers |
| Test a provider | Owner console → Providers → *Test connection* |
| Refresh a catalogue | Owner console → Providers → *Refresh model catalog* |
| Enable a model | Owner console → Models & Routes |
| Create, edit, or publish an alias | Owner console → Models & Routes (use **Edit** on an existing alias; its name remains fixed while its route policy is changed) |
| Manage profile metadata | Owner console → Profiles (does not restrict LAN invocation) |
| Read status and limits | Owner console → Status |
| Make the aliases appear in OpenCode's `/models` | Install `integrations/opencode/` once (see §3.1) |

### OpenCode Console Jev / System One

To use Jev, open **Providers → OpenCode Console Jev**, enter the API key in its
write-only field, then explicitly test and refresh the catalogue. Enable the
Jev model you intend to call. The native API is `POST /v1/systemone`; it needs
a model, a state, and named `noul`, `choice`, or `score` questions. The
matching `GET /v1/systemone/models` returns only saved, enabled model
metadata and never makes a provider call. System One state/questions are sent
to OpenCode Console for that direct request; they are not retained by the gateway.

The Status ledger is metadata-only and asynchronously persisted to
`request-ledger.jsonl` in the state volume. It retains up to 30 days with
bounded rotation (5 MiB active file plus two rotations); disk-write errors or
a full queue can cause a persistence update to be dropped. The in-memory ring
is the live view. Local usage aggregates cover the last 24 hours; costs use
published prices only and display their source and observation date. A model
without published prices reports **Unknown**. This is not a billing system.

Provider tests and catalogue refreshes are **explicit actions**. An ordinary
page load or configuration save never calls a provider. Credentials are stored
separately and never returned by state or status APIs.

---

## 3. Verifying a deployment

```sh
curl -s http://10.10.11.109:8765/version | python3 -m json.tool
curl -s -o /dev/null -w '%{http_code}\n' http://10.10.11.109:8765/health
curl -s http://10.10.11.109:8765/v1/models
```

`/version` must report exactly the four keys, and `image_revision` must be
the digest of the image that is running.

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
client - it is not access control for the API itself, which stays
anonymous.

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
this listener, while `/setup/` is absent. Owner state is readable without a
login. Browser mutations require the exact canonical `Origin`, but no session
or CSRF bootstrap. Confirm there is no second listener on port `8766` through
your normal host-level listener inspection.

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
instead of calling it again on the first request. Delete the file by hand
if you want the gateway to try every route immediately.

---

## 6. Troubleshooting

| Symptom | Cause | Action |
|---|---|---|
| `refusing to start: a wildcard bind...` | `GATEWAY_BIND` is `0.0.0.0` | Set it to the explicit private LAN address |
| The owner console will not load | The configured private address is not assigned to the host, or the listener is unavailable | Use the configured private address and port; login/bootstrap is not part of this product mode |
| A provider shows *Failed* | The credential was rejected or the provider is unreachable | **Test connection** and read the message; the stored credential is never displayed |
| A model shows an empty dialect | The provider serves it but does not document the endpoint | Enter it manually and set the dialect, or use an OpenAI-dialect model |
| A route says the dialect cannot be translated | The model is served over Anthropic Messages or Google v1beta | The gateway refuses rather than guessing; publish an OpenAI-dialect route |
| Limits show *Unknown* | The provider publishes no machine-readable allowance for that plan | That is the truthful answer. Check the provider's own console |
| A request failed with *no eligible route* | Every route was excluded | The error names each excluded route and why |
| The published catalogue is empty | No alias is published yet | Publish one in Models & Routes |
| OpenCode shows no gateway models | The plugin cannot read `/v1/alias-catalog` - the gateway is unreachable, or it predates the endpoint | `curl` the endpoint; redeploy with `scripts/release.py`; the plugin warns in the OpenCode log and recovers on its own |
| An OpenCode model says *200k client budget* | The alias has a route whose context limit is not published | That is the owner-chosen client budget, not a provider guarantee. The gateway reports Unknown on purpose |
| An OpenCode reasoning variant had no effect | The serving route does not publish that level | Expected: the level is dropped **for that route** and the substitution is recorded in the request ledger |
| OpenCode offers no image input for a model you know accepts images | No route of that alias has *source-backed* image evidence | The gateway records image support per exact model ID from official model-provider docs only, and never infers it from a model name or dialect. Check `data/go_catalog.json` for `supports_image_input` and `image_input_source`, then re-run `scripts/build_go_catalog_snapshot.py` and redeploy |
| An image request failed with *no eligible route* | No route of the alias accepts image input | Expected: the gateway excludes an image-incapable route rather than letting the provider reject it opaquely. The error names the excluded routes and the reason |
| `/version` reports `unknown` | The deployment was not released properly | Deploy with `scripts/release.py --build`; it publishes the identity. The gateway also warns about this at startup |
| A client reports *an empty stream with no finish reason* | The stream was not framed correctly | Update to a build that emits terminated SSE frames; check the raw bytes for `\n\n` between frames |
| A fallback did not run | The alias has not authorised it, or the trigger is off | The alias needs **both** the trigger and the confirmation of the fallback provider. The record says which was missing |
| A fallback did not run and the record says *skipped* | The primary was in a local cooldown | The cooldown is local policy, not evidence the provider reset. It expires on its own |
| A request was served by a phase route, not the primary | The alias has an enabled phase-routing policy and a phase applied (from the caller's `metadata.personal_gateway.phase` hint or from Jev inference) | Expected. The ledger records `phase` and `phase_source` (`caller`/`jev`/`default`) as metadata only. A phase route is the primary for that phase, **not** a fallback |
| Jev did not pick a phase | Inference is off for the alias, the request carried tools, Jev was uncertain, or Jev failed | Expected and safe: the ordinary route is used and the ledger records why. Raise `minimum_confidence` only if you accept more guessing, never to force a route |

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
