# Development and verification guide

## Local commands

The supported development path is Python 3.11+ with no project runtime
dependencies:

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 -m compileall -q src tests
PYTHONPATH=src python3 -m gateway --help
node tests/console_dom_test.js        # the console's JavaScript regressions
node tests/test_opencode_catalog.mjs  # the OpenCode plugin's catalogue validator
```

The unit suite must be deterministic: do not require network access, real
provider accounts, credentials, device codes, or a live LAN listener. Use the
adapter/client seams and synthetic values that cannot resemble production
secrets.

Regenerate the dated OpenCode Go catalogue only from the sources it
transcribes — the published endpoint/price/limit table, the live catalogue,
and each model's own data page — review the resulting diff, and record each
row's observation date and source URL:

```sh
python3 scripts/build_go_catalog_snapshot.py
```

Do not guess a model's dialect, capability, eligibility, quota, reset time, or
retention rule. Unknown is a valid and often required result.

## Change checklist

Before changing code, identify the boundary being touched:

| Change | Required checks |
|---|---|
| HTTP/browser surface | canonical Host, Origin and forwarded-header tests; direct same-origin mutation and cross-origin refusal; bounded worker and local rate-limit refusal tests |
| Credential/auth path | no secret in config/state/log/error; private-file permissions; one-time caller-token behavior |
| Adapter | explicit connection/catalog action; safe error classification; no redirect/credential leak; fixture-based tests |
| Router/bridge | capability compatibility; fallback hard stops; stream and tool-bearing behavior |
| Config mutation/import | input validation; revision behavior; atomic persistence; invalid state cannot be published; sanitised export excludes credentials, token hashes and secret-adjacent metadata |
| Container/release | exact image identity, non-root startup with the real state volume, listener exposure, metadata-only `/version` |
| Documentation | command, URL, endpoint, UI label, and limitation checked against the code in this repository |

For every defect, add a regression test that proves the old behavior is gone.
For every new provider or route capability, add a refusal test as well as a
happy-path test. A failure that makes a configuration impossible to recover
from is not acceptable merely because an API handler returns an error.

## Secret-handling rules

Never place real credentials, tokens, device codes, prompts, completions, or
provider account identifiers in source, tests, fixtures, issue text, commits,
images, or generated documents. Do not read local credential stores while
developing or reviewing the code.

A credential may cross only this narrow path:

1. the write-only credential form on `/owner/`;
2. request body handled by the relevant explicit adapter action;
3. private credential store;
4. provider adapter request headers.

It must not cross into `GatewayConfig`, a normal state/status response, a URL,
the request ledger, a release identity, an exception message, or an upstream
error echo. Provider calls must not follow redirects.

## Container and release verification

**One command builds, deploys, publishes the identity and verifies it:**

```sh
python3 scripts/release.py --build --image personal-llm-gateway:candidate
```

It reads the revision and the tree from the checkout, stamps them into the
image, starts the Compose deployment, writes the image digest into the state
volume, and then verifies from outside the container that `/version` reports
exactly that identity, that the running container's image is that image, and
that the process listens only on the configured address.

**Do not deploy with a bare `docker compose up -d --build`.** Compose passes
`SOURCE_REVISION` through from the environment, so without it the image is
built with `unknown` and `/version` cannot say what is running. The gateway
warns about this at startup, and the release step is the fix.

To build and gate without deploying:

```sh
SOURCE_REVISION="$(git rev-parse HEAD)"
SOURCE_TREE="$(git rev-parse HEAD^{tree})"
docker build \
  --build-arg SOURCE_REVISION="$SOURCE_REVISION" \
  --build-arg SOURCE_TREE="$SOURCE_TREE" \
  -t personal-llm-gateway:candidate .
python3 scripts/release_gate.py personal-llm-gateway:candidate
```

The image tag is convenience only. Record the immutable local image ID or
registry digest that the gate inspected. Do not rebuild, retag, or substitute
the artifact between verification and deployment.

The release gate is necessary but not enough: it runs a disposable container.
Before a deployment can be approved, separately prove the Compose deployment
with its **actual named state volume** can start as UID/GID `10001`, write
`config.json` and credentials, expose only the intended explicit private
listener, and serve `/owner/` directly with no login. Then verify externally:

```sh
curl -sS http://<private-ip>:<port>/health
curl -sS http://<private-ip>:<port>/version
curl -sS http://<private-ip>:<port>/v1/models
```

Do not bind `0.0.0.0`, `::`, or a public address. Do not add router/firewall
rules, a public reverse proxy, or a provider login as part of a test.

`tests/test_compose_deployment.py` exercises the real Compose deployment on
its own project and volume: first boot, a configuration write that survives
a restart, the non-root runtime, the exact listener, the absence of the
retired port, and the release identity. It is skipped when Docker is absent.

## Probing an upstream

The unit suite cannot tell you the upstream's request or response *shape*,
and that shape is where every defect in this project has been. When a
contract is unclear, probe the real endpoint:

* **from inside the container**, so the egress path matches production and
  the stored credential is used;
* printing **only status codes and field names** - never a token, a device
  code, a prompt or a completion;
* with **no credential of your own in the command line**.

`docs/PROVIDER_CONTRACTS.md` gives the procedure and the contracts that were
established this way. A probe from the host can give a different answer from
the same request - the same request answered 200 from one path and 520 from
another.

## Review standard

A review report must distinguish:

* **verified:** directly exercised or supported by a cited source/test;
* **static finding:** demonstrated from source but not runtime-exercised;
* **assumption:** an operator/provider condition that needs manual proof.

Do not convert an assumption into a product claim in the README. Update
`REVIEW.md` when a confirmed fix closes a finding, and remove/replace the
corresponding regression test only if a stronger test now covers it.


## Capability discovery (staged)

Run `PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_capability_discovery.py' -v` for isolated synthetic store/preview tests. No provider transport is registered: the HTTP run action intentionally refuses with 422 while provider bounds and reliable per-model pricing remain unapproved. Do not enable a real adapter by copying the fake test seam.
