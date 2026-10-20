# Operator runbook

Everything an operator needs to run, verify, cut over, and roll back the
gateway. Nothing here is required for *using* the gateway — that is all done
in the browser.

---

## 1. First run

### 1.1 Build the candidate

```sh
cd /home/marslan/Projects/llmgtwy
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

The gateway binds `GATEWAY_BIND` (an explicit private LAN address) and starts
setup on `GATEWAY_SETUP_BIND` (the same private LAN address by default) at
port `8766`. Set `GATEWAY_SETUP_BIND=127.0.0.1` for host-only setup. It
refuses wildcard binds for either listener.

**Bootstrap safety:** before an owner passphrase exists, do not use a
LAN-reachable setup listener. Start the first boot with
`GATEWAY_SETUP_BIND=127.0.0.1`, create the passphrase locally or through an
SSH tunnel, then restart with the desired private setup bind. The current code
does not otherwise distinguish the first LAN client from the intended owner;
see R0 in `REVIEW.md`.

### 1.4 Create the owner passphrase

On the host, with setup bound to loopback for first boot:

```
http://127.0.0.1:8766/setup/
```

Open it on the host itself, or tunnel it:

```sh
ssh -L 8766:127.0.0.1:8766 <host>
```

The passphrase is created **only** in the setup UI. It is stored as a salted
PBKDF2 hash in the owner-only credential store and is never displayed again.
There is no default passphrase and no recovery path other than removing the
credential store and starting over.

### 1.5 Optional LAN-reachable setup after bootstrap

After the owner passphrase is set, the setup UI may bind the private LAN
address. Open it directly:

```
http://10.10.11.109:8766/setup/
```

If you have restricted it to loopback, tunnel it:

```sh
ssh -L 8766:127.0.0.1:8766 <host>
# then open http://127.0.0.1:8766/setup/
```

The owner passphrase is required either way.

---

## 2. Everyday use

Everything else happens in the browser.

| Task | Where |
|---|---|
| Connect a provider | Setup UI → Providers |
| Test a provider | Setup UI → Providers → *Test connection* |
| Refresh a catalogue | Setup UI → Providers → *Refresh model catalog* |
| Enable a model | Setup UI in LAN no-auth mode; owner console only in authenticated mode |
| Create or publish an alias | Setup UI in LAN no-auth mode; owner console only in authenticated mode |
| Issue a caller token | Setup UI in LAN no-auth mode; owner console only in authenticated mode |
| Read status and limits | Owner console or Setup UI → Status |

Provider tests and catalogue refreshes are **explicit actions**. An ordinary
page load never calls a provider.

The OpenCode Go **Save and test** action stores the key and explicitly runs a
connection test. Ordinary page loads never call a provider.

---

## 3. Verifying a deployment

```sh
curl -s http://10.10.11.109:8765/version | python3 -m json.tool
curl -s -o /dev/null -w '%{http_code}\n' http://10.10.11.109:8765/health
curl -s http://10.10.11.109:8765/v1/models
```

`/version` must report exactly the four keys, and `image_revision` must be
the digest of the image that is running.

The setup listener must **not** be served on the owner port, and it must
require a session:

```sh
curl -s -o /dev/null -w '%{http_code}\n' http://10.10.11.109:8765/setup/api/state
# expected: 404 - the setup surface has its own port

curl -s -o /dev/null -w '%{http_code}\n' http://10.10.11.109:8766/setup/api/state
# expected: 401 - the setup surface requires an owner session
```

---

## 4. Cutover

The live deployment at `10.10.11.109:8765` is **left running** until the
candidate has passed its gates. Cut over side-by-side-safe:

1. Verify the candidate on an alternate port:

   ```sh
   GATEWAY_BIND=10.10.11.109 GATEWAY_PORT=8767 GATEWAY_SETUP_PORT=8768 \
     docker compose up -d
   ```

2. Exercise it: connect a provider, publish an alias, send a completion.
3. Stop it and restart on the production port:

   ```sh
   docker compose down
   GATEWAY_BIND=10.10.11.109 docker compose up -d
   ```

4. Confirm the release identity and the health endpoint.
5. Only then decide what to do with the previous service. **Do not stop it
   before the candidate has passed.**

---

## 5. Rollback

The gateway's state lives in one volume. Rollback is either:

**Rollback the image.** Redeploy the previous digest. The volume is
compatible, because the configuration format has not changed.

```sh
docker compose down
# edit compose.yaml to reference the previous digest, then:
docker compose up -d
```

**Rollback the configuration.** The configuration is a single JSON file on the
volume:

```sh
docker compose down
docker run --rm -v personal-llm-gateway-state:/state alpine \
  cp /state/config.json /state/config.json.before-upgrade
docker compose up -d
```

The credential store is separate and is never touched by a rollback.

---

## 6. Troubleshooting

| Symptom | Cause | Action |
|---|---|---|
| `refusing to start: a wildcard bind...` | `GATEWAY_BIND` is `0.0.0.0` | Set it to the explicit private LAN address |
| The setup UI will not load | `GATEWAY_SETUP_BIND` does not match the address you opened, or is not assigned to the host | Use the configured setup address; use `127.0.0.1` plus an SSH tunnel only for loopback-only setup |
| The console asks for a passphrase and there is none | The passphrase has not been created yet | Create it in the setup UI |
| A provider shows *Failed* | The credential was rejected or the provider is unreachable | **Test connection** and read the message; the stored credential is never displayed |
| A model shows an empty dialect | The provider serves it but does not document the endpoint | Enter it manually and set the dialect, or use an OpenAI-dialect model |
| A route says the dialect cannot be translated | The model is served over Anthropic Messages or Google v1beta | The gateway refuses rather than guessing; publish an OpenAI-dialect route |
| Limits show *Unknown* | The provider publishes no machine-readable allowance for that plan | That is the truthful answer. Check the provider's own console |
| A request failed with *no eligible route* | Every route was excluded | The error names each excluded route and why |
| The published catalogue is empty | No alias is published yet | Publish one in Models & Routes |

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
