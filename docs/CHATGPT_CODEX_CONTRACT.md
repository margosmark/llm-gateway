# ChatGPT / Codex adapter contract

The normative contract for the ChatGPT/Codex adapter, as implemented. It is
written so the behaviour can be checked against the cited sources without
re-reading the web.

**Sources**

* Device-code sign-in: <https://learn.chatgpt.com/docs/auth> — officially
  documented, and labelled **beta**.
* The gateway forwarding target:
  <https://learn.chatgpt.com/docs/enterprise/sign-in-with-chatgpt-through-a-gateway>.
* Gateway compatibility requirements:
  <https://learn.chatgpt.com/docs/enterprise/gateway-compatibility>.
* The request contract: OpenAI's own Codex client, `codex-rs/login/`
  (<https://github.com/openai/codex>), which is the reference implementation
  of the documented flow.

**Not documented by OpenAI:** that a third-party gateway may serve a
consumer ChatGPT subscription to arbitrary LAN clients. The mechanism below
is implemented from provider-integration behaviour. It is not an OpenAI
endorsement, and the console says so.

---

## 1. Device-code authorisation

### 1.1 Request a user code

```
POST https://auth.openai.com/api/accounts/deviceauth/usercode
Content-Type: application/json

{"client_id": "app_EMoamEEZ73f0CkXaXp7hrann"}
```

Response:

```json
{"device_auth_id": "...", "user_code": "ABCD-EFGH", "interval": 5}
```

* `client_id` is the public OAuth client id used by OpenAI's own Codex
  client for device-code sign-in. It is an identifier, not a secret.
* `interval` is the polling interval in seconds.
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

The start response also carries an **opaque poll handle** — a random value
the gateway minted. The provider's own `device_auth_id` never leaves the
process: the browser is given the handle, and the gateway uses it to find
the pending record again. The pending record lives in memory only and
expires with the code.

### 1.3 Poll for approval

The console polls the gateway with the opaque handle, and the gateway polls
the provider with its own `device_auth_id`:

```
POST https://auth.openai.com/api/accounts/deviceauth/token
Content-Type: application/json

{"device_auth_id": "...", "user_code": "ABCD-EFGH"}
```

| Status | Meaning |
|---|---|
| `2xx` | Approved. The body carries `authorization_code`, `code_challenge` and `code_verifier`. |
| `403`, `404` | Not yet. Keep polling, sleeping `interval` seconds. |
| anything else | The attempt failed. Report it and stop. |

The console polls at the interval the provider asked for. The poll response
never repeats the one-time code.

Maximum wait: **15 minutes**.

### 1.4 Exchange the authorization code

```
POST https://auth.openai.com/oauth/token
Content-Type: application/x-www-form-urlencoded

grant_type=authorization_code
&client_id=app_EMoamEEZ73f0CkXaXp7hrann
&code=<authorization_code>
&redirect_uri=https://auth.openai.com/deviceauth/callback
&code_verifier=<code_verifier>
```

Response: `{"id_token": "...", "access_token": "...", "refresh_token": "..."}`.

The `chatgpt_account_id` claim of the `id_token` is read and stored, because
the upstream expects it to be forwarded.

### 1.5 Refresh

```
POST https://auth.openai.com/oauth/token
Content-Type: application/x-www-form-urlencoded

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

## 2. Inference

### 2.1 The upstream

```
https://chatgpt.com/backend-api/codex/models
https://chatgpt.com/backend-api/codex/responses
```

The OpenAI gateway documentation states that a gateway's Codex-compatible
base URL must proxy `/models` and `/responses` to this base.

### 2.2 The headers

```
Authorization: Bearer <access_token>
ChatGPT-Account-ID: <chatgpt_account_id>     # when known
Content-Type: application/json
Accept: text/event-stream                     # for a streaming request
User-Agent: personal-llm-gateway/2.0
```

The account id is forwarded unchanged. No gateway credential is added, and
none is ever forwarded upstream.

### 2.3 The dialect

**Responses API only.** OpenAI's own compatibility requirements state that
`wire_api = "chat"` and a Chat Completions-only endpoint are not supported.
The gateway therefore converts a Chat Completions request into a Responses
request before calling this upstream — see `src/gateway/bridge/`.

### 2.4 Streaming

The upstream is streamed incrementally, never buffered:

* SSE frames are forwarded as they arrive;
* the terminal `response.completed` event is preserved;
* error and failure events are forwarded, so a failed response is
  distinguishable from a stalled connection.

---

## 3. What the adapter never does

* It never re-displays the device code, the access token, the refresh token,
  or the id token after they are stored.
* It never calls an undocumented endpoint. In particular it does **not**
  probe for usage or quota state: there is no official machine-readable
  allowance API for a personal subscription, so the console reports
  **Unknown** and links the official dashboard.
* It never guesses. A model with no documented dialect, and a request with a
  capability the target cannot express, are refused with an actionable
  message.

---

## 4. Conformance checklist

Each line is a test.

| # | Requirement | Where |
|---|---|---|
| 1 | The user-code request uses the documented URL and body | `tests/test_adapters.py` |
| 2 | The verification URL is the official one | `tests/test_adapters.py` |
| 3 | `403`/`404` while polling means "not yet", not failure | `tests/test_adapters.py` |
| 4 | The token exchange uses the documented form and redirect URI | `tests/test_adapters.py` |
| 5 | The stored credential is never re-displayed | `tests/test_credentials.py`, `tests/test_end_to_end.py` |
| 6 | The inference upstream is the documented base | `tests/test_adapters.py` |
| 7 | The account id is forwarded when known | `tests/test_adapters.py` |
| 8 | Disconnect removes only the local credential | `tests/test_adapters.py` |
| 9 | A device-code failure is classified, not guessed | `tests/test_adapters.py` |
| 10 | The error message never echoes the upstream body | `tests/test_adapters.py` |
