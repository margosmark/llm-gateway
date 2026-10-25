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
levels at all** (3 of 40), so every OpenCode Go route was excluded from any
request that asked for one, and a configured fallback was effectively dead.

The rule now is:

| Requirement | Treatment |
|---|---|
| wire dialect, streaming, function tools, built-in tools, image input, previous-response continuation | **Hard.** The route is excluded with a capability-specific reason. Dropping tools, images, or continuation would change the request contract. |
| `reasoning_effort` | **A preference.** The route stays eligible, the effort is dropped *for that route*, and the substitution is recorded. Never silent. |

Image input is detected in Chat Completions (`image_url`), Responses
(`input_image`) and Anthropic Messages - the last in two spellings: an
`image` block with a `source`, an `input_image` block, and an image nested
inside a `tool_result`. Capability detection and conversion run the same
block parser, so routing can never call something an image that rendering
then drops, or rendering can never invent an image routing excluded.
Image URLs are validated without fetching
anything: only absolute HTTP(S) URLs without embedded userinfo, or base64 data
URLs for PNG/JPEG/GIF/WebP, are accepted. Data URLs are limited to 4 MiB of
decoded image bytes (and a 6 MiB encoded URL ceiling). The gateway never
dereferences image URLs, so URL validation does not create a gateway-side
fetch or SSRF path. Unsupported/malformed content parts fail with a safe 4xx
instead of being silently discarded or downgraded to text - on the Anthropic
surface that covers an image whose `source` cannot be read, a
`tool_use.input` that is not an object, and an image inside a `tool_result`
on a dialect whose function output is a single string (refused with the
refusal recorded, or passed through whole by a native Anthropic route).
A Responses
`input` array carries *items* (messages, function calls, their outputs) -
never content parts at the top level - so a root-level `input_image`-like
item is malformed for every route and is refused with a 4xx before route
selection, in capability detection, dialect conversion **and** same-dialect
pass-through alike. Non-function tool types require
`supports_builtin_tools`; function tools continue to require
`supports_function_tools`. A `previous_response_id` requires
`supports_previous_response_id`. That metadata is not currently represented
on `ModelEntry`, so it defaults to unsupported and fails closed; continuation
also remains untranslatable to Chat Completions and is refused by the bridge.
When no route is eligible, the gateway returns each excluded route and its
reason in the routing error detail; when a fallback serves, skipped-route
reasons are retained in attribution/ledger.

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

## Reasoning effort: one vocabulary, per-model support, and the `none` trap

**Established from** OpenAI's official reasoning guide
(`https://developers.openai.com/api/docs/guides/reasoning`, read
2026-10-23), MiniMax's model documentation
(`https://platform.minimax.io/docs/guides/text-generation`, read
2026-10-23), and the dated OpenCode Go catalogue snapshot
(`data/go_catalog.json`, observed 2026-10-22).

OpenAI publishes the whole set the gateway recognises, in one sentence:

> Supported values are model-dependent and can include `none`, `minimal`,
> `low`, `medium`, `high`, `xhigh`, and `max`.

That sentence carries three contract facts the gateway is built around:

1. **"Model-dependent" is literal.** The same page documents that GPT-6
   Astra **rejects `none` with HTTP 400** in both dialects
   (`reasoning.effort` and `reasoning_effort`), and that GPT-6.1 Sol
   accepts neither `none` nor `minimal`. So a level is never a property of
   a dialect: it is a property of a model, and the gateway's rule (R19) is
   that the route stays eligible, the level is dropped *for that route*,
   and the substitution is recorded.
2. **The gateway's vocabulary is that published set, and nothing else.**
   `gateway.thinking.CANONICAL_LEVELS` is
   `none, minimal, low, medium, high, xhigh, max`; `gateway.core.capabilities.REASONING_EFFORTS`
   *is* that tuple. `ultra` is deliberately absent — no provider this
   gateway serves or fronts publishes it, so the gateway does not claim it.
   A client may still ask for it: an unknown level is forwarded and the
   router decides, because a provider may publish a level the gateway has
   not seen.
3. **`none` is the trap.** It is a published level *and* the gateway's
   spelling of "off", and it is **not portable**. In the dated catalogue
   only the two Responses-dialect models (`gpt-5.6-luna`, `gpt-6-luna`)
   publish it; the one chat-dialect model that publishes any levels
   (`minimax-m3.1-flash`) publishes `low..max`, and the OpenCode Go chat
   endpoint rejected the value in practice — that is what the commit
   `fix: strip disabled reasoning before upstream chat call` (e03eadf) was
   written for. The bridge therefore renders "off" **per target
   dialect**: `reasoning: {"effort": "none"}` for Responses, and the
   control *omitted* for Chat Completions. Sending `none` to a model that
   does not publish it is a provider 400.

**Budgets.** Neither served dialect has a budget parameter; an effort
level is the whole control. `thinking.budget_tokens` and
`thinkingBudget` are Anthropic and Google concepts, and those dialects are
not served yet (T5/T6), so the gateway publishes no budget table and
invents no budget. Such a control is refused on a converted path.

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

The gateway exposes Jev through its native `GET /v1/systemone/models` and
`POST /v1/systemone` surfaces. It does not present Jev as an OpenAI chat or
Responses model. The native System One endpoint accepts JSON only and refuses
image assets and `box` questions. That restriction is specific to System One:
the OpenAI-compatible inference API is not categorically text-only. The gateway
parses image-bearing chat and Responses requests, but a request can be served
only by a route whose exact model has documented image-capability evidence;
the router excludes incapable routes and fails closed if none remain. The
capability is not inferred from a model family or wire dialect. See the
provider-specific image evidence below. Catalogue refresh, connection test,
and credential enrollment remain explicit owner actions.

A direct System One request intentionally sends its submitted state and
questions to OpenCode Console. That content is never stored in gateway
configuration, logs, or the ledger. Separately, the gateway supports explicit,
per-alias Jev phase inference for phase routing; it does not use Jev to select a
completion model outside that opt-in policy.

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

## Anthropic Messages API

The native API-key adapter uses Anthropic's documented `POST /v1/messages`
endpoint and `GET /v1/models` catalogue. Requests carry `x-api-key`,
`anthropic-version: 2023-06-01`, JSON content type, and an event-stream Accept
header for streaming. Messages streaming uses SSE; the adapter passes complete
frames from the shared upstream client without translating their event shape.
The exact available model identifiers depend on the account and catalogue
response. Context limits, quotas, retention, and model-specific features are
left Unknown unless separately established; the adapter makes no inferred
capability claims. Calls occur only on explicit provider test, model refresh,
or inference actions.

## OpenCode Go

**Established from** `https://opencode.ai/docs/go/` and from probing
`https://opencode.ai/inference/go/v1/models`.

| Fact | Value |
|---|---|
| Catalogue endpoint | `GET https://opencode.ai/inference/go/v1/models` — answers **without** authentication |
| Live catalogue | 38 model ids (observed 2026-10-25). The snapshot holds 40 rows: the other two were served on 2026-10-22, are no longer returned, and are marked deprecated |
| Published table | Of the 38 live ids, 29 publish an endpoint, 36 publish a price, 28 publish a limit |
| Price provenance | `data/go_catalog.json` preserves each model's own data-page URL (`https://opencode.ai/data/<vendor>/<id>`) and its observation date; the token prices are **list-price estimates**, so a local cost estimate uses them only and stays Unknown when a field is absent. The Go plan bills its own discounted amount and serves `beam-free` free of charge, so an estimate is never a bill |
| Undocumented ids | 9 — `glm-5.1`, `grok-4.6`, `iceman-contributor-free`, `kimi-k2.6`, `minimax-m2.5`, `minimax-m2.7`, `omen-alpha`, `qwen3.6-plus`, `qwen3.7-max` |
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
`scripts/build_go_catalog_snapshot.py` from three sources, each keeping its
own URL and read date: the published endpoint/price/limit table on
`https://opencode.ai/docs/go/`, the live catalogue above, and each model's
own data page at `https://opencode.ai/data/<vendor>/<id>` (389 pages read
2026-10-25) — never hand-edited. **An undocumented model is refused, not
guessed.**

Serving drift as observed on 2026-10-25: `kimi-k3.1-pro` and
`gemini-4-argon` were served and documented but missing from the snapshot
(now added); `iceman-contributor-free` is served with neither a data page
nor a published row (added fail-closed — no dialect, no prices, no image
verdict); `gemini-4-argon-contributor` and `longcat-2.5-preview-free` were
catalogued but are no longer returned (kept, marked deprecated).

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
  `longcat-2.5-preview-free` is no longer served (observed 2026-10-25) and
  its row is deprecated.
* **Context comes from the model's own data page, not from the catalogue
  endpoint.** `GET /v1/models` publishes no context fields, so each row's
  `context_window` and `max_output_tokens` are transcribed per exact slug
  from that slug's page (read 2026-10-25; 36 of the 40 rows have one —
  `omen-alpha`'s page carries no payload and three ids have no page). The
  page URL is the row's `context_source`. A **`context_limit`** is separate
  and rarer: it is recorded only where the model provider's own
  documentation publishes the window, and today only two rows carry one —
  MiMo-V2.6-Flash at 1,048,576 tokens (maximum completion 131,072) and
  MiniMax M3.1 Flash at 1,000,000. Neither is inferred from a Go price tier;
  a row without evidence remains Unknown, and a documented Go service-side
  cap would take precedence over a model window.
* **Thinking is model-specific.** MiniMax M3.1 Flash documents
  `reasoning_effort` levels `low`, `medium`, `high`, `xhigh` and `max`
  (default `max`; it cannot be turned off), so those appear as OpenCode
  variants when it is the primary route. MiMo-V2.6-Flash documents a binary
  `thinking.type` control (`enabled` / `disabled`) and returns
  `reasoning_content`; it documents no effort vocabulary, so the gateway
  does not invent level variants for it. An effort that a fallback does not
  publish remains a preference: it is dropped only for the serving fallback
  and the ledger records the degradation.
* **Image input is a hard capability, recorded per exact model ID.** MiniMax
  documents that `MiniMax-M3.1-Flash` and `MiniMax-M3` accept OpenAI
  `image_url` content parts over the Chat Completions compatibility
  endpoint (`platform.minimax.io/docs/api-reference/text-openai-api`, read
  2026-10-23), and the published Go endpoint table establishes that the
  exact `minimax-m3.1-flash` ID is served over that endpoint. OpenCode's
  own per-model data pages — the platform of record behind the Go
  catalogue, `https://opencode.ai/data/xiaomi/<slug>`, read 2026-10-24 —
  record `mimo-v2.6-flash`, `mimo-v2.6-pro` and `mimo-v2.5` as accepting
  Text, Image, Audio and Video, and `mimo-v2.5-pro` as accepting **Text
  only**. Those five slugs are the whole evidenced set. The snapshot
  records the flag, its source URL and the read date together
  (`supports_image_input` / `image_input_source` /
  `image_input_observed_at`), and a regression test asserts the three
  always agree. The same pages list Audio and Video: the gateway capability
  model is a single image boolean, so those modalities are deliberately
  **not** transcribed and no field in the API may be read as an audio or
  video guarantee. **No capability is inferred from a model name, a model
  family, or a wire dialect** - `mimo-v2.5` accepts images while its sibling
  `mimo-v2.5-pro` does not, so a family-level rule would be wrong for one
  of them. A model without evidence stays false and sourceless;
  `/v1/alias-catalog` reports
  an alias as image-capable when **any** route that can serve it has the
  evidence. That is deliberately looser than the all-routes rule the tools
  flag uses, because the router enforces image capability per request: an
  image is routed to a route that can see it, a text-only route is excluded
  for that request with the reason recorded, and an image request with no
  capable route fails closed at the gateway. An alias whose routes are all
  unevidenced therefore stays text-only.

  A verdict that exists only in the catalogue is indistinguishable from
  `false` until it reaches the stored row, because the router, the alias
  catalogue and every client read the row — that drop is what R46 covers.
  Every boot therefore re-applies each adapter's evidence inside the boot
  transaction: promotion only, exact slug only, no provider call, no row
  added, enabled or demoted.
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

### Image input

The Codex catalogue publishes **no image or vision field** — the only
capability fields are `supported_in_api`, `supported_reasoning_levels` and
`visibility`. Image support therefore cannot be discovered from the
endpoint and is **transcribed per exact slug** from OpenAI's own model
documentation, in `IMAGE_INPUT_SOURCES` in `adapters/chatgpt_codex.py`.

`gpt-5.6-terra` is listed there: OpenAI's model page states "Input
modalities: text, image" and lists `image_input` under Supported features
(`developers.openai.com/api/docs/models/gpt-5.6-terra`, read 2026-10-23).
A slug absent from that map stays **false** — image support is never
inferred from the Responses dialect, a model family, or a tier name. The
source URL is appended to the model's `notes` so an operator can review the
conclusion.

This is a *separate* mechanism from the OpenCode Go snapshot's
`IMAGE_INPUT_SOURCES`, which lives in `scripts/build_go_catalog_snapshot.py`
and feeds `data/go_catalog.json`. The two providers are served by different
adapters and reach the capability from different places; adding a model to
one map has no effect on the other.

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
