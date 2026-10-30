# OpenAI-compatible client integrations

This guide connects Hermes Agent, Oh My Pi (`omp`), and the OpenCode **V2 CLI** to this gateway. It documents the OpenAI-compatible inference endpoints, not the gateway's owner API. The guide covers a basic Chat Completions text setup; its evidence section identifies which local client tests actually ran. Auxiliary and non-chat endpoint paths are not implied to work.

The gateway does not require or inspect a client API key. Where a client requires one, use any clearly marked placeholder; it is not a security control.

## At a glance

| Client | Gateway endpoint | Model value in client | Client extension/plugin required? | Evidence |
|---|---|---|---|---|
| Hermes Agent | `http://<gateway-private-ip>:8765/v1` | `<published-alias>` | No; use Hermes' built-in `custom` provider | Verified 2026-10-30: `hermes chat --provider custom … --oneshot` completed a text turn through the gateway (Hermes Agent v0.22.5) |
| Oh My Pi (`omp`, package `@oh-my-pi/pi-coding-agent`) | `http://<gateway-private-ip>:8765/v1` | `personal-gateway/<published-alias>` | No; define a static custom model in `models.yml` | Verified 2026-10-30: print-mode text turn succeeded with `omp` 18.14.0 and Bun 1.4.4; auxiliary behavior not covered |
| OpenCode CLI V2 (`@opencode/cli`) | `http://<gateway-private-ip>:8765/v1` | `personal-gateway/<published-alias>` | No for a static provider/model; optional repository plugin syncs the catalogue | Verified 2026-10-30: `opencode run --standalone …` completed a text turn with `@opencode/cli` 2.2.1 |

Here OMP means **Oh My Pi**, as confirmed by the task owner. OpenCode V2 refers to the official `@opencode/cli` distribution, distinct from the V1 `opencode-ai` package. npm metadata checked on 2026-10-29 reports Oh My Pi package 18.13.3 and OpenCode CLI 2.2.1; the 2026-10-30 verification run installed `@oh-my-pi/pi-coding-agent` 18.14.0 (Bun 1.4.4) and `@opencode/cli` 2.2.1 into isolated task scratch. The client runtime tests below prove one Chat Completions text turn each; they are not equivalent to full end-to-end gateway compatibility tests.

The exact aliases are owner-defined: enable a model and publish its alias in the gateway console first. `coding` below is an example alias, not a built-in gateway model. For each client, its own `provider/model` selector is local namespacing; the HTTP request's `model` must end up as the gateway alias (for example `coding`). The gateway replaces that alias with the selected upstream model ID and does not send the alias upstream.

## Before you connect

The gateway exposes one OpenAI-compatible base URL at `http://<gateway-private-ip>:8765/v1`. Preserve the `/v1` suffix. Chat Completions clients call `/v1/chat/completions`; a Responses client calls `/v1/responses`. The gateway also exposes `GET /v1/models` and the gateway-specific, read-only `GET /v1/alias-catalog`.

**Trusted-LAN security boundary:** there is no API-key authentication or login boundary for callers. A reachable device can call every published model and administer the gateway, including replacing provider credentials. Client-side `api_key` values below are placeholders some SDKs require; the gateway does not verify them and they do not secure access. Do not expose or port-forward this HTTP listener, use an untrusted network, or send sensitive prompts over an untrusted LAN. The service is HTTP-only, not TLS.

The examples below are for a trusted LAN. Replace `10.10.11.109` with the actual private address and `coding` with an alias actually published on your gateway.

## Hermes Agent

**No plugin is needed** to use the gateway for ordinary chat. The official Hermes provider guide documents a built-in `custom` endpoint configured through `hermes model` or `model:` settings. Its GitHub Copilot provider, by contrast, is a separately installed labs plugin; that is an unrelated provider and not required here.

### Configure interactively (recommended)

Run `hermes model`, select **Custom endpoint**, and enter the private base URL, a published alias such as `coding`, and a placeholder key if the wizard requires a value. Hermes saves provider settings to `config.yaml`. The key is not gateway authentication.

### One-off CLI request

```sh
hermes chat \
  --provider custom \
  --base-url http://10.10.11.109:8765/v1 \
  --api-key placeholder \
  --model coding \
  --query "Summarize this repository" \
  --oneshot
```

The installed CLI is Hermes Agent v0.22.5. Its `hermes chat --help` explicitly describes `--base-url` as an OpenAI-compatible endpoint for `--provider custom`, and `--api-key` as launch-only. **Verified 2026-10-30:** the command above (with the fixture's loopback base URL substituted) completed a text turn against the gateway and printed the fixture's scripted reply `SYNTHETIC_OK`, exit 0. No provider-backed request was made.

One observed caveat: on a host with a populated Hermes profile `config.yaml` (this run's host already had entries under `providers:`), the identical `--provider custom …` command failed before any request with `invalid_params` ("Hermes couldn't start the model connection"); adding `--ignore-user-config` or `--safe-mode` made the same command succeed. The exact config key responsible was not isolated. If you see `invalid_params` on an otherwise correct custom-endpoint command, isolate the host's Hermes configuration first.

> **Important:** this gateway does not advertise `/api/show`, a GET `/v1/models/{id}` model-detail endpoint, or the `POST /v1/responses/compact` endpoint. An OMP run may attempt such auxiliary metadata/compaction requests; those are outside the gateway API described here. The synthetic OMP turn issued an auxiliary `GET /api/show` which received 404 and also made a regular chat completion successfully. Configure OMP against a static model entry; whether compaction or other auxiliary paths work is unverified and should be treated as unsupported unless separately proven.

For durable configuration, use the wizard or the current Hermes provider guide rather than hand-editing secrets. The official guide's manual example uses the `model` section with `provider: custom`, `base_url: http://.../v1`, `default: <alias>`, and an `api_key` or `key_env`. Hermes may make auxiliary calls (such as summarization/vision) through the configured model; configure and verify those separately if used. A text-only, single-chat success does not establish an auxiliary feature's compatibility.

## Oh My Pi (`omp`)

Here **OMP means Oh My Pi**, the coding agent published as `@oh-my-pi/pi-coding-agent` and invoked as `omp`. Its official model docs define custom providers in the active agent directory's `models.yml` (normally `~/.omp/agent/models.yml`); a custom model is selected with the provider-qualified `provider/model-id` form.

For a gateway, provide a model entry directly. Do not enable model-list discovery against the gateway: the generic OpenAI model-discovery path may request auxiliary model-detail endpoints which this gateway does not implement. A static `models.yml` entry avoids relying on unverified discovery behavior.

Example `~/.omp/agent/models.yml`:

```yaml
providers:
  personal-gateway:
    baseUrl: http://10.10.11.109:8765/v1
    auth: none
    api: openai-completions
    models:
      - id: coding
        name: Gateway coding
        api: openai-completions
        reasoning: false
        input: [text]
        contextWindow: 4096  # conservative local client budget; replace only with a known value
        maxTokens: 1024      # local output cap; replace only with a known value
```

The context/output values above are examples for client budgeting, **not gateway or upstream model claims**. Set them to conservative owner-verified values if known. The gateway does not publish those fields on `/v1/models`; do not infer them from a model name.

Run (from the project you intend OMP to work in):

```sh
omp --model personal-gateway/coding -p "Summarize this repository"
```

**Verified 2026-10-30:** the command above (base URL substituted with the fixture's runtime-chosen loopback port) completed a text turn with `omp` 18.14.0 and Bun 1.4.4 in print mode with a task-local `PI_CODING_AGENT_DIR`, printing `SYNTHETIC_OK`, exit 0. This was not run through the production gateway or a real provider. The test disabled tools/extensions and did not verify auxiliary calls, compaction, or image input. Official package metadata states Bun `>=1.4.4`.

When `PI_CODING_AGENT_DIR` is set, OMP resolves `models.yml` **inside that directory** (the default agent directory is `~/.omp/agent`, so the env var must point at the directory that contains the file — `models.yml`, not `agent/models.yml`). With the file misplaced, OMP exits 1 with `Model "personal-gateway/coding" not found` and names the exact path it expected.

## OpenCode CLI V2 (`@opencode/cli`)

This section targets the V2 CLI distributed as `@opencode/cli`, not the distinct legacy V1 package `opencode-ai`. OpenCode V2's official docs define `providers`, the `@opencode/ai/providers/openai-compatible` package, `settings.baseURL`, and custom models. A client-side provider plugin is not needed when you configure this static provider and model yourself.

Add this example to a project-level `opencode.jsonc` or your user `~/.config/opencode/opencode.json` (merge with existing configuration):

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "model": "personal-gateway/coding",
  "compaction": { "auto": false },
  "providers": {
    "personal-gateway": {
      "name": "Personal Gateway",
      "package": "@opencode/ai/providers/openai-compatible",
      "settings": {
        "baseURL": "http://10.10.11.109:8765/v1",
        "apiKey": "placeholder"
      },

      "models": {
        "coding": {
          "name": "Gateway coding",
          "modelID": "coding",
          "capabilities": {
            "tools": false,
            "input": ["text"],
            "output": ["text"]
          },
          "limit": { "context": 4096, "output": 1024 }
        }
      }
    }
  }
}
```

The OpenCode `capabilities` and `limit` values are client model metadata; replace them with verified values for your published alias. This example sets `tools: false` because this task did not test a tool round trip. Set it to `true` only after checking `GET /v1/alias-catalog` reports `supports_function_tools: true`; that field is true only when every configured route supports function tools. OpenCode's V2 docs say omitted custom-model capabilities can receive defaults, so explicitly declare only capabilities you have checked. The gateway does not implement `/v1/responses/compact`; verify whether your exact OpenCode build uses that endpoint before relying on compaction.

Run a one-shot request with an isolated standalone OpenCode service:

```sh
opencode run --standalone --model personal-gateway/coding \
  "Summarize this repository"
```

**Verified 2026-10-30:** the official V2 binary was installed into isolated task scratch as `@opencode/cli@2.2.1`; `opencode --version` returned `opencode v2.2.1`, and `opencode run --standalone --model personal-gateway/coding "Summarize this repository"` (with the config above, base URL substituted with the fixture's runtime-chosen loopback port) completed a text turn, printing `SYNTHETIC_OK`, exit 0. The client-side `Compaction summary did not match the required template` error seen in an earlier attempt did not recur with this configuration. npm metadata lists `@opencode/ai@2.2.1`; it was not independently installed. No provider-backed request was made.

### Optional OpenCode catalog plugin

This repository also contains `integrations/opencode/index.ts`. Its role differs from the static provider above: it reads `GET /v1/alias-catalog`, validates alias metadata, and refreshes OpenCode's model catalogue periodically. Use it if you want published aliases and their supported metadata to appear dynamically in the OpenCode model selector. It is not required for a manually declared provider/model. The plugin does not make provider requests to test a model.

The metadata endpoint reports alias IDs, context status, primary reasoning efforts, whether all configured routes support function tools, and whether any route has evidenced image support. It does not return credentials or provider inventory. Image-capable alias metadata means the gateway routes image requests only to a route with source-backed image support; image support is **not** established by the CLI synthetic text test.

## What is and is not verified

| Capability | Evidence-backed status |
|---|---|
| Oh My Pi Chat Completions text | Verified 2026-10-30: one print-mode text turn through the gateway completed with `@oh-my-pi/pi-coding-agent` 18.14.0 and Bun 1.4.4, static model config and placeholder credentials; tool/auxiliary behavior not included. |
| OpenCode V2 CLI request/response | Verified 2026-10-30: one `opencode run --standalone` text turn through the gateway completed with `@opencode/cli` 2.2.1 using the documented static provider/model config. The earlier client-side compaction-template error did not recur. |
| Hermes Agent request/response | Verified 2026-10-30: one `hermes chat --provider custom … --oneshot` text turn through the gateway completed with Hermes Agent v0.22.5; see the `invalid_params` caveat in the Hermes section. |
| No provider request | All three client checks ran against a local fixture only. No real provider, paid call, or production gateway was used. |
| Gateway base path and alias behavior | Source-verified in `src/gateway/http/app.py` and `src/gateway/core/routing.py`, and observed on the wire 2026-10-30: every fixture provider call arrived with the upstream model id (`gpt-6-luna`), never the requested alias `coding`. |
| Tools | Gateway eligibility depends on route capability. Client-side tool flags are not proof; no client tool round-trip was run. |
| Images | Gateway accepts supported image forms only when the route has evidenced image capability; no client image test was run. |
| Responses API | Gateway implements `/v1/responses`; these examples configure Chat Completions. Client selection of Responses transport is not established. |
| Auxiliary calls, compaction, retries, reasoning options, fallback behavior | Not established for these client/version combinations. |

The 2026-10-30 fixture was **the gateway itself, not an impersonating mock**: this repository's own HTTP surface (branch base `origin/main` at `0cd241c`) run from a checkout on a loopback port chosen at runtime (free-port probe; nothing hardcoded), backed by a scripted in-process provider speaking the OpenAI Responses dialect. Both gateway conversion paths were exercised directly (`curl` checks: buffered `chat.completion` JSON, and a streaming request that returned converted `chat.completion.chunk` SSE frames ending in `data: [DONE]`), and the fixture's provider-call log recorded every upstream call with the upstream model id `gpt-6-luna` — never the alias `coding`. Each of the three clients then completed one Chat Completions text turn through that gateway. This proves the documented client configurations complete one text turn against this gateway's `/v1` surface; it does not establish tools, images, compaction, auxiliary endpoints, retries, or fallback behavior.

## Verify and troubleshoot

From a trusted LAN machine, substitute the gateway address:

```sh
# Published aliases (empty means no alias is currently published)
curl -fsS http://10.10.11.109:8765/v1/models

# Confirm the local OpenAI-compatible chat endpoint and alias
curl -fsS http://10.10.11.109:8765/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer placeholder' \
  -d '{"model":"coding","messages":[{"role":"user","content":"Reply only OK"}],"stream":false}'
```

A placeholder bearer header is shown only for SDK compatibility and local transport testing; the gateway ignores it for access control. Avoid putting a real provider credential into client config, shell history, URLs, or reports.

| Symptom | Check |
|---|---|
| Empty `/v1/models` | In the owner console, enable a discovered model, create an alias, and publish it. The gateway starts with no published aliases. |
| 4xx `unknown_model` / no route | The HTTP `model` value must be the published gateway alias, not the provider's upstream model ID or a client provider prefix. |
| Client reports model unavailable | Check OpenCode provider/model IDs or OMP `models.yml` provider/model names, and run the CLI from the intended project/config location. |
| Connection refused or timeout | Confirm private address/port, reachability from client, and that the gateway is healthy; keep `/v1` in the configured base URL. |
| 401/403 from a provider | This is the upstream route's credential/entitlement or provider response, not gateway caller authentication; verify the provider in the owner console. |
| Tool request fails or is excluded | Ensure every configured route that might serve this alias supports function tools; do not assume support from a client-side flag. |
| Image request fails or a route is skipped | Verify source-backed image capability on a route and correct client input metadata. A name containing “vision” is not evidence. |
| Hermes: `invalid_params` before any request | Host Hermes configuration interfered with `--provider custom` in one observed host profile; retry with `--ignore-user-config` or `--safe-mode` to isolate. |
| OMP: `Model "…" not found` at startup | Check the `models.yml` path: with `PI_CODING_AGENT_DIR` set, OMP expects `models.yml` directly inside that directory and names the expected path in the error. |

## Sources and observation dates

Official docs and package registry metadata checked **2026-10-29**; the end-to-end client runs described above were performed **2026-10-30** against this repository's own gateway HTTP surface on a loopback fixture (Oh My Pi `omp` 18.14.0 with Bun 1.4.4, OpenCode CLI `@opencode/cli` 2.2.1, Hermes Agent v0.22.5 — all installed or present in isolated task scratch; no global install, no secrets, no production gateway, no real provider call).

- [Hermes Agent — LLM and Model Providers](https://hermes-agent.nousresearch.com/docs/integrations/providers) — custom endpoint, `hermes model`, `custom` provider; also documents which unrelated providers require plugins.
- [Oh My Pi — Providers](https://github.com/can1357/oh-my-pi/blob/main/docs/providers.md) and [Model and Provider Configuration](https://github.com/can1357/oh-my-pi/blob/main/docs/models.md) — identity, custom `models.yml`, provider/model schema and model selection. npm: [`@oh-my-pi/pi-coding-agent`](https://www.npmjs.com/package/@oh-my-pi/pi-coding-agent).
- [OpenCode V2 — Providers](https://opencode.ai/v2/docs/providers/), [Models](https://opencode.ai/v2/docs/models/), [Config](https://opencode.ai/v2/docs/config/), and [CLI](https://opencode.ai/v2/docs/cli/) — custom OpenAI-compatible provider, configuration, selector semantics, and `opencode run`. npm: [`@opencode/cli`](https://www.npmjs.com/package/@opencode/cli), distinct from `opencode-ai` V1.
- Gateway behavior: [`README.md`](../README.md), [`docs/PROVIDER_CONTRACTS.md`](PROVIDER_CONTRACTS.md), current source `src/gateway/http/app.py` and `src/gateway/core/routing.py`.
