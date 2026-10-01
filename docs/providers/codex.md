# Codex provider

Use a Codex subscription as a CLIO language-model provider. The `codex`
provider has ONE transport, **direct**: CLIO sends the Responses API request
to the Codex backend (`chatgpt.com/backend-api`) from its own process over a
kept WebSocket. No Codex SDK and no Codex CLI process is involved.

The provider catalog still reports Codex's transport as a one-row
`transports` list (`id: "direct"`), and each model row carries
`transport: "direct"`, which clients echo back as `ModelRef.variant`.

## Architecture

`src/clio_agent/providers/codex/direct_engine.py` is a DSPy 3.4 engine: lm15's
`OpenAICodexLM` builds the Responses payload (native function tools, reasoning,
images and PDFs, `prompt_cache_key`) and parses the event stream; the engine owns
only the transport.

- **Auth** (`oauth.py`, `login_flow.py`, `credentials.py`): CLIO's own sign-in
  is PKCE OAuth with three login methods racing to the same code exchange -- a
  loopback browser callback, a paste-only fallback (for a browser CLIO cannot
  open, or a headless/`clio-relay` host), and a device code for headless hosts.
  Rotating refresh tokens are persisted atomically at `0600` and refreshed
  proactively (under 5 minutes remaining); every call reads a fresh token.
  Without a CLIO sign-in the engine uses the local Codex CLI login at
  `$CODEX_HOME/auth.json` (default `~/.codex/auth.json`), read and refreshed by
  lm15 under the CLI's own lock; CLIO never rotates or writes that file
  (`credentials.codex_cli_auth_path()` is the one place the path is resolved).
- **Transport:** one WebSocket per conversation (GACT session + agent scope)
  inside an agent loop, with delta continuation: a call whose messages repeat
  everything already sent plus the model's own reply sends only the new
  messages' input items with `previous_response_id` (the state lives on the
  connection, so it works with `store: false`). An edit, a different system
  prompt or tool list, an idle (5 min) or aged (55 min) socket, or a backend
  that lost the previous response sends the full input on a new socket, typed
  on the `provider.stateful` audit row. The conversation's `prompt_cache_key`
  routes every call to one prompt cache. Measured (B3, 2026-09-29): median
  TTFT 1.18 s vs 1.49 s for stateless HTTP on a 4-turn conversation, same
  ~70% cache hit rate.
- **Model list** (live): the Codex backend's account model list
  (`codex/model_list.py`:
  `GET https://chatgpt.com/backend-api/codex/models?client_version=<v>`, the
  endpoint the official Codex CLI reads, with the same sign-in as the engine).
  The backend gates models on `minimal_client_version`; CLIO presents the
  version of the bundled `openai-codex-cli-bin` runtime, which is the provider
  panel's user-updatable Codex component. The result is cached in the
  model-catalog overlay (TTL `providers.model_catalog_ttl_s`, last-good kept on
  a failed ask, typed staleness). There is no maintained or bundled Codex model
  list.
- **PDF input:** PDF attachments are delivered as Responses `input_file` parts
  (verified live; the model list itself reports only text/image), so Codex rows
  carry `pdf` with evidence source `codex_direct_input_file`.

CLIO remains the only agent loop and the only owner of tool execution --
the Codex backend is used purely as an inference endpoint, the same as any
other LM provider.

## Sign in

Either sign in from a CLIO surface, or rely on an existing Codex CLI login
(`codex login`, stored at `$CODEX_HOME/auth.json`). CLIO's own sign-in wins
when both exist. Both CLIO surfaces call the same generic
`POST /v1/providers/codex/auth` (start/complete/status/logout) endpoint and the
same UI component:

- **Model picker.** Open the picker, hover/click the Codex row to open its
  submenu, and follow the inline sign-in section (browser link, device code,
  or paste). The submenu switches to Codex's model list as soon as sign-in
  completes, without leaving the picker.
- **Settings → Providers.** The full sign-in panel, for setting up Codex
  without first choosing a model.

Binding Codex (`PUT /v1/providers/lm`) with neither sign-in answers a typed
`401 codex_auth_required`.

## Configure

```sh
export CLIO_LM_PROVIDER=codex
export CLIO_LM_MODEL=gpt-5.6-sol
export CLIO_CODEX_TRANSPORT=websocket  # or "sse"
```

`CLIO_CODEX_TRANSPORT` (`lm.codex_transport`) is the delivery mechanism:
`websocket` (default, delta continuation) or `sse`, an explicit stateless-HTTP
mode for a network whose proxy blocks WebSocket upgrades (every call sends the
full input). There is no automatic fallback between them.

**Removed:** the Codex SDK transport. A leftover `lm.codex_variant` in a config
file or `CLIO_CODEX_VARIANT` in the environment is a typed configuration error
(`config_key_removed`) naming every place it is set -- delete it. A bind
request or model reference naming a transport other than `direct` is refused in
plain language; choose the model again from the picker.

## Streaming and reasoning truth

The Responses API exposes assistant text deltas, provider reasoning deltas
(when the model reports them), and a `reasoning_output_tokens` usage count.
The transcript labels these separately from CLIO's own hidden-reasoning
bridge -- a provider-reported reasoning delta is never invented text.

## Failure behavior

Every failure is typed, never a bare exception: an exhausted plan window is
`CodexPlanLimitError` (terminal, never retried, a plain-language message for
the user); a refused sign-in is a typed auth error; a lost continuation resends
in full once; other backend and transport failures are lm15's typed errors,
which DSPy retries when they are retryable (never after anything streamed).

## Related source

- `src/clio_agent/providers/codex/` -- OAuth (`oauth.py`, `login_flow.py`),
  credentials (`credentials.py`), the engine (`direct_engine.py`) and its
  stream-audit rows (`audit.py`), the live model list (`model_list.py`)
- `src/clio_agent/providers/model_discovery/codex.py` -- discovery rows from
  the live model list
- `src/clio_agent/gact/routes/codex_readiness.py` -- the bind readiness gate
- `src/clio_agent/gact/provider_catalog.py` -- the one-row `transports`
  catalog entry (`_codex_direct_transport_row`)
- `src/clio_agent/gact/routes/provider_auth.py` -- the generic sign-in API
- `src/clio_agent/providers/catalog.py` -- the catalog entry
