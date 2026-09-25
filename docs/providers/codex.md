# Codex subscription provider

Use a Codex subscription as a CLIO language-model provider directly from
CLIO's own process -- no Codex CLI, no Codex SDK, no OpenAI API credential
inside CLIO.

## Architecture

CLIO talks OAuth, HTTP/SSE, and WebSocket directly to the Codex backend
(`chatgpt.com/backend-api`) from `src/clio_agent/providers/codex/`. There is
no subprocess, no vendored CLI binary, and no second SDK layer between CLIO
and the wire.

- **Auth** (`oauth.py`, `login_flow.py`, `credentials.py`): PKCE OAuth with
  three login methods racing to the same code exchange -- a loopback browser
  callback, a paste-only fallback (for a browser CLIO cannot open, or a
  headless/`clio-relay` host), and a device code for headless hosts. Rotating
  refresh tokens are persisted atomically at `0600`, refreshed proactively
  (under 5 minutes remaining) and on a 401.
- **Transport** (`transport_ws.py`, `transport_sse.py`): WebSocket is the
  default, with delta continuation -- a follow-up turn reuses the open
  connection and sends only the new input suffix plus `previous_response_id`
  when eligible, rather than replaying the whole conversation. SSE is the
  automatic fallback for any pre-stream WebSocket failure.
- **LiteLLM adapter** (`litellm_adapter.py`): a `CustomLLM` registered under
  the internal name `codex_direct` -- deliberately NOT the catalog id
  `codex`, because litellm ships its own native `chatgpt` provider (a
  device-code OAuth client against `auth.openai.com`) that this provider
  historically collided with when it was registered under a matching name.
  See `constants.py::LITELLM_PROVIDER` for the full story.
- **Model catalog** (`model_discovery/codex_catalog.py`): data-driven
  (`catalogs/codex-models.json`), since the Codex backend offers no account
  model-enumeration RPC. `discover_codex` trusts the catalog's own
  context-window/output-limit values rather than an external lookup.

CLIO remains the only agent loop and the only owner of tool execution --
the Codex backend is used purely as an inference endpoint, the same as any
other LM provider.

## Sign in

Sign in from either surface -- both call the same generic
`POST /v1/providers/codex/auth` (start/complete/status/logout) endpoint and
the same UI component:

- **Model picker.** Open the picker, hover/click the Codex row to open its
  submenu, and follow the inline sign-in section (browser link, device code,
  or paste). The submenu switches to Codex's model list as soon as sign-in
  completes, without leaving the picker.
- **Settings → Providers.** The full sign-in panel, for setting up Codex
  without first choosing a model.

## Configure

```sh
export CLIO_LM_PROVIDER=codex
export CLIO_LM_MODEL=gpt-5.6-sol
export CLIO_CODEX_TRANSPORT=websocket
```

`CLIO_CODEX_TRANSPORT` may be omitted -- `websocket` is the default. `sse`
forces the automatic-fallback transport for the whole session (useful behind
a proxy that blocks WebSocket upgrades).

## Streaming and reasoning truth

The Responses API exposes assistant text deltas, provider reasoning deltas
(when the model reports them), and a `reasoning_output_tokens` usage count.
The transcript labels these separately from CLIO's own hidden-reasoning
bridge -- a provider-reported reasoning delta is never invented text.

## Failure behavior

Every failure is one of the typed errors in `errors.py`, never a bare
exception: a 429 whose body names the account's plan window is terminal
(`CodexPlanLimitError`, never retried); other retryable statuses back off
honoring `retry-after-ms`/`retry-after`; a 401 refreshes the credential once
and retries the whole turn; a pre-stream WebSocket failure falls back to SSE
for the rest of that turn; a mid-stream failure is a hard error, not silently
downgraded.

## Related source

- `src/clio_agent/providers/codex/` -- OAuth, credentials, transports, the
  LiteLLM adapter
- `src/clio_agent/providers/model_discovery/codex.py` /
  `codex_catalog.py` -- model discovery
- `src/clio_agent/gact/routes/provider_auth.py` -- the generic sign-in API
- `src/clio_agent/providers/catalog.py` -- the catalog entry
