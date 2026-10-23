# Provider contracts and the traps

Everything here was established by reading official documentation or by
probing the real endpoints. Each entry says how it was established, so a
future agent can tell an observed fact from an inference.

**Read this before changing an adapter.** Almost every bug in this project's
history was a wrong assumption about an upstream's request or response
shape, and the symptom was always the same: a generic error with no clue.

---

## The five traps

These each cost hours. They are listed first because they are the most
likely to bite again.

### 1. The SSE frame terminator is part of the frame

An SSE frame is its data lines **plus the blank line that ends it**. An
upstream reader that consumes that blank line and does not include it in
the frame it yields produces a stream with **no frame separators at all**.

A lenient reader still sees the lines, so this looks fine to `curl` and in
the console. A client that parses SSE strictly reads the whole stream as
one event, finds no `finish_reason`, and reports:

> Provider returned an empty stream with no finish_reason
> (possible upstream error or malformed SSE response)

`gateway.adapters.http_client.UpstreamClient._iter_sse` now emits complete
frames; `tests/test_sse_framing.py` parses the output the way a strict
client does. **When touching stream framing, test it with a strict parser,
not with `curl`.**

### 2. A capability and a preference are different things

The capability check originally treated *every* mismatch as disqualifying.
That excluded a route whose model did not publish the requested
`reasoning_effort` — and **most OpenCode Go models publish no reasoning
levels at all** (2 of 37), so every OpenCode Go route was excluded from any
request that asked for one, and a configured fallback was effectively dead.

The rule now is:

| Requirement | Treatment |
|---|---|
| wire dialect, streaming, function tools | **Hard.** The route is excluded. Dropping tools would change the API contract. |
| `reasoning_effort` | **A preference.** The route stays eligible, the effort is dropped *for that route*, and the substitution is recorded. Never silent. |

A degradation is recorded only for the route that **actually served** the
request. A route that failed did not serve anything, so it does not get to
describe how it would have answered.

### 3. The alias must never reach the provider

The client asks for an alias (`coding`); the provider only knows its own
model ids. The router replaces the payload's `model` with the **upstream
model id of the chosen route**. Sending the alias upstream makes the
provider answer "Model is unavailable" — which the gateway then classifies
as a malformed request, because a 400 is a 400.

### 4. A converted request needs a converted response, on both paths

The request was converted between dialects but the **non-streaming**
response body was returned verbatim, so a Chat Completions client received a
Responses object. The streaming path converted; the buffered path did not.

Both paths convert now, and the streaming-only upstream is handled by
aggregating the stream (§3 below).

### 5. A route can be skipped without being tried

A primary that is **skipped** — in a local cooldown, disabled, or unknown —
produces a request served by a later route that looks like an unexplained
fallback. The record now names the skipped route and its reason, and a
cooldown cannot look like a silent failover.

---

## OpenCode Console Jev / System One

**Established from** OpenCode Console's official models documentation at
`https://opencode.ai/v2/docs/console/models/#jev` (read 2026-10-22) and the
live `https://opencode.ai/inference/v1/models` catalogue.

| Fact | Value |
|---|---|
| Catalogue endpoint | authenticated `GET https://opencode.ai/inference/v1/models` |
| Native decision endpoint | authenticated `POST https://opencode.ai/inference/v1/systemone` |
| Authentication | `Authorization: Bearer <API_KEY>` |
| Request | `model`, `state` (string/object/array), and named `questions` |
| Supported initial question types | `noul`, `choice`, `score` |
| Response | native `{model, answers, usage}` structure |

The gateway exposes Jev only through its native `GET /v1/systemone/models` and
`POST /v1/systemone` surfaces. It does not present Jev as an OpenAI chat or
Responses model. The first implementation accepts JSON only and refuses
assets, `box` questions, and multipart bodies. Catalogue refresh, connection test,
and credential enrollment remain explicit owner actions.

A direct System One request intentionally sends its submitted state and
questions to OpenCode Console. That content is never stored in gateway configuration,
logs, or the ledger. Jev is not a hidden completion-router in this release.

### Opt-in phase inference

An alias may enable a **bounded, closed-set** Jev call to infer a request's
phase (`plan`/`implement`/`validate`) for phase routing. This is off by
default and only runs when the alias enabled it *and* the request is safe to
classify (no tools, not a continuation, has text). The gateway sends a single
`choice` question it owns - never a caller-authored prompt - with a short,
bounded slice of the request text as `state`. A choice applies only when it
names a configured phase and meets the alias's `minimum_confidence`;
otherwise the ordinary route is used. A Jev failure never fails the
completion. The request content, the question text, and the answer
distribution are never recorded - only the phase and its source as metadata.
The `choice` response may carry a `probabilities` distribution; the gateway
reads only `choice` and `confidence` and never forwards or stores the rest.

---

## OpenCode Go

**Established from** `https://opencode.ai/docs/go/` and from probing
`https://opencode.ai/inference/go/v1/models`.

| Fact | Value |
|---|---|
| Catalogue endpoint | `GET https://opencode.ai/inference/go/v1/models` — answers **without** authentication |
| Live catalogue | 37 model ids (observed 2026-10-19) |
| Published table | 29 of them, with limits, prices and endpoints |
| Undocumented ids | 8 — `glm-5.1`, `grok-4.6`, `omen-alpha`, `kimi-k2.6`, `minimax-m2.5`, `minimax-m2.7`, `qwen3.6-plus`, `qwen3.7-max` |
| Base for inference | `https://opencode.ai/inference/go` |

**Four wire dialects, per model.** The dialect is a property of the *model*,
not of the provider:

| Dialect | Path |
|---|---|
| OpenAI Responses | `/openai/v1/responses` |
| OpenAI Chat Completions | `/openai/v1/chat/completions` |
| Anthropic Messages | `/anthropic/v1/messages` |
| Google v1beta | `/google/v1beta/models/{id}` |

`data/go_catalog.json` is the dated mapping. It is regenerated by
`scripts/build_go_catalog_snapshot.py` from the published table — never
hand-edited. **An undocumented model is refused, not guessed.**

**Request hygiene (required by the provider):**

* a **stable `x-opencode-session`** header per conversation. The provider
  refuses a request without it:
  `{"type":"error","error":{"type":"MissingSessionID", ...}}`.
  The gateway derives this server-side — see `gateway/session.py`.
* a **self-identifying user agent**, e.g. `personal-llm-gateway/2.0`, not a
  generic SDK name.

**Other facts:**

* Per-model monthly limits with windows of 5-hour 20%, weekly 50%, monthly
  100%. Free models (`beam-free`, `longcat-2.5-preview-free`,
  `minimax-m3.1-flash-free`) stay usable after the allowance is exhausted.
* **The provider's catalogue publishes no context fields.** A Go route gets
  a context window only when the Go endpoint table identifies the exact
  model **and** that model provider's official documentation publishes the
  window. The dated snapshot records MiMo-V2.6-Flash at 1,048,576 tokens
  (maximum completion 131,072) and MiniMax M3.1 Flash at 1,000,000 tokens.
  This is not inferred from a Go price tier. A model without both pieces of
  evidence remains Unknown; a documented Go service-side cap would take
  precedence over the model window.
* **Thinking is model-specific.** MiniMax M3.1 Flash documents
  `reasoning_effort` levels `low`, `medium`, `high`, `xhigh` and `max`
  (default `max`; it cannot be turned off), so those appear as OpenCode
  variants when it is the primary route. MiMo-V2.6-Flash documents a binary
  `thinking.type` control (`enabled` / `disabled`) and returns
  `reasoning_content`; it documents no effort vocabulary, so the gateway
  does not invent level variants for it. An effort that a fallback does not
  publish remains a preference: it is dropped only for the serving fallback
  and the ledger records the degradation.
* Retention: 30 days for the GPT/Luna, Grok, Claude and Beam models; 0 days
  for GLM, Kimi, MiMo, LongCat, Qwen and Hy; **training permitted** for the
  Gemini and Muse models. This is why cross-provider fallback is a privacy
  decision, not just a routing one.

---

## ChatGPT / Codex

**Established from** `https://learn.chatgpt.com/docs/auth`,
`https://learn.chatgpt.com/docs/enterprise/sign-in-with-chatgpt-through-a-gateway`,
`https://learn.chatgpt.com/docs/enterprise/gateway-compatibility`, and
OpenAI's own client source at `github.com/openai/codex`
(`codex-rs/login/src/device_code_auth.rs`,
`codex-rs/codex-api/src/endpoint/models.rs`,
`codex-rs/codex-api/src/common.rs`).

### The device-code flow

```text
POST https://auth.openai.com/api/accounts/deviceauth/usercode
     {"client_id": "app_EMoamEEZ73f0CkXaXp7hrann"}
->  {"device_auth_id": ..., "user_code": "7ZIV-3RO67", "interval": "5"}

POST https://auth.openai.com/api/accounts/deviceauth/token
     {"device_auth_id": ..., "user_code": ...}
->  403/404 = not yet, keep polling
->  200 = {"authorization_code", "code_challenge", "code_verifier"}

POST https://auth.openai.com/oauth/token   (form-encoded)
     grant_type=authorization_code
     client_id=app_EMoamEEZ73f0CkXaXp7hrann
     code=<authorization_code>
     redirect_uri=https://auth.openai.com/deviceauth/callback
     code_verifier=<code_verifier>
->  {"id_token", "access_token", "refresh_token"}
```

* The code expires in **15 minutes**; the polling interval comes from the
  provider (`interval`, default 5s).
* **Device-code sign-in must be enabled** for the account — ChatGPT security
  settings for a personal account, workspace permissions for a workspace
  account. Without it the endpoint answers 404.
* The verification URL is `https://auth.openai.com/codex/device`.
* The `chatgpt_account_id` claim of the `id_token` is forwarded as the
  `ChatGPT-Account-ID` header.
* The client id is the **public** client id used by OpenAI's own Codex
  client. It is an identifier, not a secret.

### The inference upstream

```text
POST https://chatgpt.com/backend-api/codex/responses
GET  https://chatgpt.com/backend-api/codex/models?client_version=<semver>
```

* `client_version` is **required**. The reference client appends it
  (`append_client_version_query` in `codex-rs/codex-api/src/endpoint/models.rs`).
  Without it the backend answers `400 invalid_request_error`. The gateway
  sends its own version — honestly identifying itself rather than
  impersonating the Codex client — and the value must be a whole semver.
* **The upstream is streaming only.** A Responses request without
  `"stream": true` is refused. Probed directly:

  | request | result |
  |---|---|
  | `stream: false`, bare | 400 |
  | `stream: true`, bare | 400 |
  | `stream: false`, Codex fields | 400 |
  | `stream: true`, Codex fields | **200** |

* **The Codex request fields are required.** The reference client always
  sends them, and the backend expects them:

  ```json
  {"model": "...", "input": [...], "stream": true,
   "tool_choice": "auto", "parallel_tool_calls": true,
   "store": false, "include": ["reasoning.encrypted_content"]}
  ```

  `store: false` means the provider is never asked to retain the response.
  A caller may override the defaults; the gateway sets `stream: true`
  itself, because it has to.

* **The model catalogue has its own shape.** It is a list under `models`,
  and a model is identified by its **`slug`**, not by an `id`:

  ```json
  {"models": [{"slug": "gpt-6-luna", "display_name": "...",
               "supported_in_api": true, "visibility": "list",
               "context_window": 272000, "max_context_window": 872000,
               "effective_context_window_percent": 95,
               "supported_reasoning_levels": [{"effort": "high", ...}]}]}
  ```

  A parser that looks for `id` finds nothing and silently reports an empty
  catalogue — "Catalog refreshed: 0 models", with no error. A model with
  `supported_in_api: false` is not offered.

* **The response stream events** (observed): `response.created`,
  `response.in_progress`, `response.output_item.added`,
  `response.content_part.added`, `response.output_text.delta`,
  `response.metadata`, `response.output_text.done`, `response.content_part.done`,
  `response.output_item.done`, `response.completed`. The terminal
  `response.completed` event carries the **complete final object**, including
  `output` and `usage` — which is what makes aggregation simple.

### Context windows

| Field | Meaning |
|---|---|
| `context_window` | The model's working window (272,000 for `gpt-6-luna`) |
| `max_context_window` | The ceiling for a **config override** (872,000) — not a competing limit |
| `effective_context_window_percent` | The share usable for input, after the provider reserves headroom (95 by default) |

The effective limit is `min(context_window, max_context_window) × percent/100`
= 258,400 for `gpt-6-luna`. At the **alias** level the limit is the smallest
limit among the routes that could serve the request, and it is **Unknown** if
any eligible route's limit is not published.

The limit is **reported, not enforced**: the gateway has no tokenizer, so it
does not count a prompt's tokens. The provider is the authority.

### What is not documented

OpenAI publishes no supported third-party proxying path for a consumer
ChatGPT subscription, and no machine-readable usage or allowance API for a
personal plan. The gateway therefore:

* calls **no undocumented endpoint** for usage or quota;
* reports ChatGPT limits as **Unknown**, with the published per-plan
  estimates labelled with their source and date;
* states the beta status of the device-code path in the console.

---

## Probing an upstream safely

When a contract is unclear, probe it **from inside the container**, using the
stored credential, and print only status codes and field names. The
credential never leaves the credential store and nothing is printed:

```sh
docker compose exec -T gateway python - <<'PY'
import json, os, sys
from pathlib import Path
sys.path.insert(0, "/app/src")
from gateway.adapters.chatgpt_codex import ChatGPTCodexAdapter
from gateway.credentials import CredentialStore
store = CredentialStore(Path(os.environ["GATEWAY_DATA_DIR"]))
adapter = ChatGPTCodexAdapter(store.provider_handle("chatgpt_codex"))
# ... one request; print only the status and the field names
PY
```

Probing from the host instead would use a different egress path and could
give a different answer — the 520s seen from one path and 200s from another
were the same request.

**Never** print a token, a device code, a prompt or a completion. **Never**
paste a real credential into a chat message, a card, a file or a commit.
