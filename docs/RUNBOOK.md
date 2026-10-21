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

The gateway's state lives in one volume. Rollback is either:

**Rollback the image.** Redeploy the previous image, without rebuilding
it, and publish that identity the same way.

```sh
docker compose down
GATEWAY_IMAGE=personal-llm-gateway:<previous> \
  python3 scripts/release.py --image personal-llm-gateway:<previous>
```

**Rollback the configuration.** The configuration is a single JSON file on
the volume. **Back it up before every upgrade**, and restore it explicitly.

```sh
# before an upgrade
docker run --rm -v personal-llm-gateway-state:/state \
  python:3.14-slim cp /state/config.json /state/config.json.before-upgrade
```

```sh
# to restore
docker compose down
docker run --rm -v personal-llm-gateway-state:/state \
  python:3.14-slim cp /state/config.json.before-upgrade /state/config.json
python3 scripts/release.py --image personal-llm-gateway:candidate
```

Validate the restored file before starting:

```sh
docker run --rm -v personal-llm-gateway-state:/state \
  python:3.14-slim python -c "import json;json.load(open('/state/config.json'));print('valid')"
```

The credential store is a separate file and is never touched by a
configuration rollback. Tokens the provider has already issued are not
revoked by a rollback; the gateway will refresh or reject them on its own.

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
| `/version` reports `unknown` | The deployment was not released properly | Deploy with `scripts/release.py --build`; it publishes the identity. The gateway also warns about this at startup |
| A client reports *an empty stream with no finish reason* | The stream was not framed correctly | Update to a build that emits terminated SSE frames; check the raw bytes for `\n\n` between frames |
| A fallback did not run | The alias has not authorised it, or the trigger is off | The alias needs **both** the trigger and the confirmation of the fallback provider. The record says which was missing |
| A fallback did not run and the record says *skipped* | The primary was in a local cooldown | The cooldown is local policy, not evidence the provider reset. It expires on its own |

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
