# Development and verification guide

## Local commands

The supported development path is Python 3.11+ with no project runtime
dependencies:

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 -m compileall -q src tests
PYTHONPATH=src python3 -m gateway --help
```

The unit suite must be deterministic: do not require network access, real
provider accounts, credentials, device codes, or a live LAN listener. Use the
adapter/client seams and synthetic values that cannot resemble production
secrets.

Regenerate the dated OpenCode Go catalogue only from the provider's published
source, review the resulting diff, and record its observation date and source:

```sh
python3 scripts/build_go_catalog_snapshot.py
```

Do not guess a model's dialect, capability, eligibility, quota, reset time, or
retention rule. Unknown is a valid and often required result.

## Change checklist

Before changing code, identify the boundary being touched:

| Change | Required checks |
|---|---|
| HTTP/browser surface | Host, Origin, forwarded-header, configured setup bind, session, and CSRF tests |
| Credential/auth path | no secret in config/state/log/error; private-file permissions; one-time token/code behavior |
| Adapter | explicit connection/catalog action; safe error classification; no redirect/credential leak; fixture-based tests |
| Router/bridge | capability compatibility; fallback hard stops; stream and tool-bearing behavior |
| Config mutation | input validation; revision behavior; atomic persistence; invalid state cannot be published |
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

1. owner-controlled setup UI on its separately configured private listener;
2. request body handled by the relevant adapter action;
3. owner-only credential store;
4. provider adapter request headers.

It must not cross into `GatewayConfig`, a normal state/status response, a URL,
the request ledger, a release identity, an exception message, or an upstream
error echo. Provider calls must not follow redirects.

## Container and release verification

Build the candidate with the exact source identity intended for release:

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
`config.json` and credentials, expose the LAN listener only on the intended
private address, and expose setup only on the explicit `GATEWAY_SETUP_BIND`
address. Then verify externally:

```sh
curl -sS http://<private-ip>:<port>/health
curl -sS http://<private-ip>:<port>/version
curl -sS http://<private-ip>:<port>/v1/models
```

Do not bind `0.0.0.0`, `::`, or a public address. Do not add router/firewall
rules, a public reverse proxy, or a provider login as part of a test.

## Review standard

A review report must distinguish:

* **verified:** directly exercised or supported by a cited source/test;
* **static finding:** demonstrated from source but not runtime-exercised;
* **assumption:** an operator/provider condition that needs manual proof.

Do not convert an assumption into a product claim in the README. Update
`REVIEW.md` when a confirmed fix closes a finding, and remove/replace the
corresponding regression test only if a stronger test now covers it.
