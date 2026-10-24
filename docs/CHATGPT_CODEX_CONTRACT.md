# The ChatGPT / Codex contract

The normative contract for the ChatGPT/Codex adapter, as implemented. It
says where each fact came from, so a future agent can tell an observed fact
from an inference.

**Sources**

* `https://learn.chatgpt.com/docs/auth` — the device-code sign-in flow.
* `https://learn.chatgpt.com/docs/enterprise/sign-in-with-chatgpt-through-a-gateway`
  — the forwarding target and its headers.
* `https://learn.chatgpt.com/docs/enterprise/gateway-compatibility` — the
  Responses requirements a gateway must preserve.
* OpenAI's own client, `github.com/openai/codex`:
  `codex-rs/login/src/device_code_auth.rs` (the device flow),
  `codex-rs/codex-api/src/endpoint/models.rs` (the model list),
  `codex-rs/codex-api/src/common.rs` (the request and response shapes),
  `codex-rs/codex-api/src/endpoint/responses.rs` (the responses endpoint).
* Probing the live endpoints, from inside the container, printing only
  status codes and field names.

The cross-provider view, and the traps, are in
[`PROVIDER_CONTRACTS.md`](PROVIDER_CONTRACTS.md).

---

## 1. Device-code authorisation

### 1.1 Request a user code

```text
POST https://auth.openai.com/api/accounts/deviceauth/usercode
Content-Type: application/json

{"client_id": "app_EMoamEEZ73f0CkXaXp7hrann"}
```

Response:

```json
{"device_auth_id": "...", "user_code": "7ZIV-3RO67", "interval": "5"}
```

* `client_id` is the **public** OAuth client id used by OpenAI's own Codex
  client for device-code sign-in. It is an identifier, not a secret, and it
  is overridable.
* A `404` means device-code sign-in is **not enabled** for the account or
  workspace. The setup UI states the prerequisite before the flow starts.

### 1.2 Show the owner the code

The owner console shows:

* the verification URL `https://auth.openai.com/codex/device`;
* the one-time code;
* a countdown — the code expires in **15 minutes**;
* the anti-phishing warning: *"Enter the code only at the official OpenAI
  URL shown above. If a website or another person gave you this code,
  cancel."*

The code is shown once, as part of the explicit "Connect ChatGPT" action. It
is never logged, never written to the ledger, and never returned by a later
read.

**The browser never holds the provider's `device_auth_id`.** The start
action returns an **opaque poll handle** that the gateway minted, and the
pending record is keyed by that handle. The provider's own identifier never
leaves the process.

### 1.3 Poll for approval

```text
POST https://auth.openai.com/api/accounts/deviceauth/token
Content-Type: application/json

{"device_auth_id": "...", "user_code": "ABCD-EFGH"}
```

| Status | Meaning |
|---|---|
| `2xx` | Approved. The body carries `authorization_code`, `code_challenge` and `code_verifier`. |
| `403`, `404` | Not yet. Keep polling, sleeping `interval` seconds. |
| anything else | The attempt failed. Report it and stop. |

Maximum wait: **15 minutes**. The console polls at the provider's interval
and keeps polling until the state is terminal.

### 1.4 Exchange the authorization code

```text
POST https://auth.openai.com/oauth/token
Content-Type: application/x-www-form-urlencoded

grant_type=authorization_code
&client_id=app_EMoamEEZ73f0CkXaXp7hrann
&code=<authorization_code>
&redirect_uri=https://auth.openai.com/deviceauth/callback
&code_verifier=<code_verifier>
```

Response: `{"id_token", "access_token", "refresh_token"}`.

The `chatgpt_account_id` claim of the `id_token` is read and stored, because
the upstream expects it to be forwarded.

### 1.5 Refresh

```text
POST https://auth.openai.com/oauth/token

grant_type=refresh_token
&client_id=app_EMoamEEZ73f0CkXaXp7hrann
&refresh_token=<refresh_token>
```

Refresh happens on a `401`/`403` from the inference path, once per request.
A second failure is reported to the caller.

### 1.6 Disconnect

Deleting the local credential removes it from this machine. **Provider-side
session revocation is the account owner's action**, and the console says so.

---

## 2. The model catalogue

```text
GET https://chatgpt.com/backend-api/codex/models?client_version=<semver>
Authorization: Bearer <access_token>
ChatGPT-Account-ID: <chatgpt_account_id>     # when known
```

* **`client_version` is required.** The reference client appends it
  (`append_client_version_query` in `codex-rs/codex-api/src/endpoint/models.rs`)
  and the backend expects it. Without it the backend answers
  `400 invalid_request_error`. The gateway sends its own version — honestly
  identifying itself rather than impersonating the Codex client — and the
  value is validated to be a whole semantic version.

* **The catalogue has its own shape.** It is a list under `models`, and a
  model is identified by its **`slug`**, not by an `id`:

  ```json
  {"models": [{
     "slug": "gpt-6-luna",
     "display_name": "GPT-6 Luna",
     "supported_in_api": true,
     "visibility": "list",
     "context_window": 272000,
     "max_context_window": 872000,
     "effective_context_window_percent": 95,
     "supported_reasoning_levels": [{"effort": "high", "description": "..."}]
  }]}
  ```

  A parser that looks for `id` finds nothing and silently reports an empty
  catalogue. A model with `supported_in_api: false` is not offered, because
  the gateway would only fail on it later.

* **Context windows.** `context_window` is the working window;
  `max_context_window` is the ceiling for a *config override*, not a
  competing limit; `effective_context_window_percent` is the share usable
  for input after the provider reserves headroom. The effective limit is
  `min(context_window, max_context_window) × percent / 100` — 258,400 for
  `gpt-6-luna`. See `ARCHITECTURE.md` for the alias-level rule.

* **The catalogue publishes no image or vision field.** The only capability
  fields are `supported_in_api`, `supported_reasoning_levels` and
  `visibility`. Image-input support therefore cannot be discovered from this
  endpoint and is **transcribed per exact slug** from OpenAI's own model
  documentation, in `IMAGE_INPUT_SOURCES` in
  `adapters/chatgpt_codex.py`. `gpt-5.6-terra` is listed there because
  OpenAI's model page states "Input modalities: text, image" and lists
  `image_input` under Supported features (read 2026-10-23). A slug absent
  from that map stays **false** — image support is never inferred from the
  Responses dialect, a model family, or a tier name. The source URL is
  appended to the model's `notes` so an operator can review the conclusion.

---

## 3. Inference

### 3.1 The upstream

```text
POST https://chatgpt.com/backend-api/codex/responses
```

### 3.2 The headers

```text
Authorization: Bearer <access_token>
ChatGPT-Account-ID: <chatgpt_account_id>     # when known
Content-Type: application/json
Accept: text/event-stream
User-Agent: personal-llm-gateway/2.0
```

No gateway credential is added, and none is ever forwarded upstream.

### 3.3 The request

The upstream is **streaming only**, and it expects the same fields the
reference client sends:

```json
{
  "model": "gpt-6-luna",
  "input": [...],
  "stream": true,
  "tool_choice": "auto",
  "parallel_tool_calls": true,
  "store": false,
  "include": ["reasoning.encrypted_content"]
}
```

* `stream` is always `true` upstream. The backend refuses anything else.
* `store: false` means the provider is never asked to retain the response.
* A caller may override `tool_choice` and `parallel_tool_calls`; the
  gateway sets `stream` itself, because it has to.

The request's `model` is the **upstream model id** of the chosen route.
An alias never reaches the provider.

### 3.3.1 Request fields the upstream rejects

The backend answers a list of ordinary Chat Completions fields with
`400 {"detail": [{"msg": "Unsupported parameter: <field>"}]}`. Confirmed
against the live backend, one field at a time, each added to an
otherwise-accepted body:

```text
temperature       top_p             n                 stop
stop_sequences    presence_penalty  frequency_penalty logit_bias
logprobs          top_logprobs      seed              user
metadata          response_format   service_tier      max_tokens
max_completion_tokens                suffix
```

`service_tier` is reported as `Unsupported service_tier: <value>` rather
than `Unsupported parameter`, but is rejected the same way.

Two properties of this rule matter operationally:

* **Validation runs before the allowance check.** The identical body
  *without* the field returns the account-allowance error, so a 400 here is
  a genuine shape rejection, not an exhausted account. That is what makes
  the list above trustworthy evidence.
* **The rejection is image-independent.** The same sweep run with an
  `input_image` item present produces the identical set, so an image
  request does not change which parameters are legal.

This was the dominant cause of the `malformed` failure class. `malformed`
is **not** a fallbackable class, so a single innocent `temperature` turned
a servable request into a 502 with no fallback attempted — which is why it
hit the image path hardest: Hermes' vision and summariser calls are exactly
the requests that set `temperature`, and they are non-streaming.

The fix keeps the knowledge on the adapter. `ChatGPTCodexAdapter` declares
`UNSUPPORTED_PAYLOAD_FIELDS`, and the router — which holds no provider
branch — reads it through the same seam as `dialect` and drops those fields
**for that route only**. The substitution is recorded in the ledger as a
degradation naming the dropped fields; it is never silent. An adapter that
declares nothing is stripped of nothing, and a genuine 400 still fails
closed.

### 3.4 The response stream

Events observed from the backend:

```text
response.created            response.output_text.delta
response.in_progress        response.metadata
response.output_item.added  response.output_text.done
response.content_part.added response.content_part.done
response.output_item.done   response.completed
```

The terminal `response.completed` event carries the **complete final
object**, including `output` and `usage`. That is what makes aggregation
simple.

### 3.5 A single object for a non-streaming client

Because the upstream only streams, a client that asks for one object is
served by consuming the stream and presenting the result as one object.
`gateway.bridge.aggregate` takes the final object from
`response.completed`, and the result is then converted into the caller's
dialect.

---

## 4. What the adapter never does

* It never re-displays the device code, the access token, the refresh token
  or the id token after they are stored.
* It never calls an undocumented endpoint. In particular it does **not**
  probe for usage or quota: there is no official machine-readable allowance
  API for a personal ChatGPT plan, so the console reports those limits as
  **Unknown** and links the official dashboard.
* It never guesses. A model with no documented dialect, and a request with a
  capability the target cannot express, are refused with an actionable
  message.
* It never sends the device auth id to the browser, and it never follows a
  redirect upstream.

---

## 5. Conformance checklist

Each line is a test in `tests/`.

| # | Requirement | Test |
|---|---|---|
| 1 | The user-code request uses the documented URL and body | `test_adapters.py` |
| 2 | The verification URL is the official one | `test_device_flow.py` |
| 3 | `403`/`404` while polling means "not yet" | `test_adapters.py` |
| 4 | The token exchange uses the documented form and redirect URI | `test_adapters.py` |
| 5 | The stored credential is never re-displayed | `test_credentials.py`, `test_end_to_end.py` |
| 6 | The inference upstream is the documented base | `test_adapters.py` |
| 7 | The model list carries the client version | `test_adapter_contract.py` |
| 8 | The catalogue's `slug` shape is parsed | `test_adapter_contract.py` |
| 9 | The upstream is always asked to stream | `test_streaming_only_upstream.py` |
| 10 | The Codex request fields are sent | `test_streaming_only_upstream.py` |
| 11 | A non-streaming client gets one object | `test_streaming_only_upstream.py` |
| 12 | A failed authorisation is reported, not guessed | `test_device_flow.py` |
| 13 | The upstream body is never echoed | `test_adapters.py` |

---

## 6. What is not documented by OpenAI

* No supported third-party proxying path for a consumer ChatGPT
  subscription. The device-code path is documented as a **beta**; this
  gateway's use of it is not endorsed by OpenAI, and the console says so.
* No machine-readable usage, quota or reset API for a personal plan.
  Published per-plan estimates exist; actual remaining allowance is
  **Unknown** to the gateway.
* No published per-model context window for the public API that matches the
  Codex backend's own profile. The gateway reports the backend's numbers,
  which are what that upstream will actually accept.
