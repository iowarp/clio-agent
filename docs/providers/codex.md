# Codex provider

Use a Codex subscription as a CLIO language-model provider. The `codex`
provider has TWO transports, and the provider is READY (green) whenever
EITHER is available:

- **`sdk`** -- the official `openai_codex` Python SDK, run against the
  user's OWN `CODEX_HOME` (default `~/.codex`). The Codex runtime itself
  owns login/refresh. Available when the SDK/runtime is installed and its own
  `account()` call reports a signed-in account.
- **`direct`** -- CLIO sends the Responses API request to the Codex backend
  (`chatgpt.com/backend-api`) from its own process over a kept WebSocket, with
  CLIO's own OAuth credential or, without one, the local Codex CLI login
  (`~/.codex/auth.json`) -- no Codex SDK for this transport.

Duplicate model ids across the two transports are expected -- picking a
model also picks which transport serves it (`variant: "sdk" | "direct"` on
the bind request / `ModelRef`).

## Architecture

### sdk transport

`src/clio_agent/providers/codex/sdk_client.py` hosts the persistent
`openai_codex` SDK client on its own event-loop thread; `sdk_engine.py` is the
DSPy 3.4 engine over it (`dspy.LM("codex_sdk/<model>", engine=CodexSDKEngine(...),
async_engine=AsyncCodexSDKEngine(...))`, built by `create_lm` for the sdk
variant -- no LiteLLM); `sdk_discovery.py` asks the SDK itself (`account()` /
`models()`) for availability and the live model list -- never a file check.

**Requests and tools.** The engine takes a typed `dspy.lm15.Request` and returns
a typed `Response`. The SDK takes one prompt string and has no native tools, so
the request's tools and messages are rendered as text
(`lm/engines/text_tools.py`): the model ends a message with one fenced
`tool_calls` block, which comes back as typed tool calls. A block that does not
parse is not repaired: it becomes one `clio_invalid_tool_calls` call whose
observation is the parse error. Text before the block streams live; thinking
streams as thinking; errors raise `dspy.lm15` types so DSPy owns retries.

**Stateful threads.** Inside an agent loop the engine keeps ONE Codex thread per
conversation (GACT session + agent scope, model, cwd, effort) and continues it
with only the messages after what the thread already holds (its own reply
included). Any other call opens a new thread and sends in full, with a typed
reset reason (`first_call` / `prefix_mismatch` / `ops_reset` /
`session_evicted` / `provider_error` / `provider_compacted`) on the
`provider.stateful` audit row. Codex auto-compaction and Codex's own web search
are disabled for these threads (clio-core is the context system; clio owns
tools); a compaction that happens anyway resets the thread typed. Superseded
threads are archived. Cached input tokens flow into the usage totals
(`Usage.cache_read_tokens`). Capacity: `providers.codex.stateful_capacity` /
`CLIO_CODEX_STATEFUL_CAPACITY`. No `env` override is ever passed to the SDK's
`CodexConfig`, so the spawned `codex` runtime inherits CLIO's own process
environment (the user's real `CODEX_HOME`) verbatim.

### direct transport

`src/clio_agent/providers/codex/direct_engine.py` is a DSPy 3.4 engine: lm15's
`OpenAICodexLM` builds the Responses payload (native function tools, reasoning,
images and PDFs, `prompt_cache_key`) and parses the event stream; the engine owns
only the transport.

- **Auth** (`oauth.py`, `login_flow.py`, `credentials.py`): PKCE OAuth with
  three login methods racing to the same code exchange -- a loopback browser
  callback, a paste-only fallback (for a browser CLIO cannot open, or a
  headless/`clio-relay` host), and a device code for headless hosts. Rotating
  refresh tokens are persisted atomically at `0600` and refreshed proactively
  (under 5 minutes remaining); every call reads a fresh token. Without a CLIO
  sign-in the engine uses the local Codex CLI login (`~/.codex/auth.json`).
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
- **Model lists** (live, per transport): the `direct` transport asks the
  Codex backend's account model list (`codex/model_list.py`:
  `GET https://chatgpt.com/backend-api/codex/models?client_version=<v>`, the
  endpoint the official Codex CLI reads, with CLIO's own credential); the
  `sdk` transport asks the SDK's `model/list` RPC (`codex/sdk_discovery.py`).
  The backend gates models on `minimal_client_version`, and both transports
  present the version of the bundled `openai-codex-cli-bin` runtime, so they
  see the same models. Both results are cached in the model-catalog overlay
  (TTL `providers.model_catalog_ttl_s`, last-good kept on a failed ask, typed
  staleness). There is no maintained or bundled Codex model list.
- **PDF input:** the `direct` transport delivers PDF attachments as Responses
  `input_file` parts (verified live; the model list itself reports only
  text/image), so its rows carry `pdf` with evidence source
  `codex_direct_input_file`. The `sdk` transport cannot carry files (the SDK's
  `UserInput` has no file variant), so its rows do not.

CLIO remains the only agent loop and the only owner of tool execution --
the Codex backend is used purely as an inference endpoint, the same as any
other LM provider.

## Sign in

**sdk transport:** sign in with the Codex CLI itself, outside CLIO (the same
sign-in a locally-installed Codex/`codex` CLI already uses). CLIO only asks
the running SDK/runtime whether an account is signed in -- it never
participates in that login and never touches `~/.codex/auth.json`.

**direct transport:** sign in from either CLIO surface -- both call the same
generic `POST /v1/providers/codex/auth` (start/complete/status/logout)
endpoint and the same UI component:

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
export CLIO_CODEX_VARIANT=direct       # or "sdk"; default "direct"
export CLIO_CODEX_TRANSPORT=websocket  # direct transport's OWN delivery choice
```

`CLIO_CODEX_VARIANT` selects WHICH of the two transports above this config
binds (`sdk` | `direct`, default `direct`); it may also be omitted and set
per bind request instead (`variant` on `PUT /v1/providers/lm`). It is
unrelated to `CLIO_CODEX_TRANSPORT`, which is the DIRECT transport's own
delivery mechanism: `websocket` (default, delta continuation) or `sse`, an
explicit stateless-HTTP mode for a network whose proxy blocks WebSocket
upgrades (every call sends the full input). There is no automatic fallback
between them.

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

- `src/clio_agent/providers/codex/` -- OAuth, credentials, both transports,
  (`sdk_client.py`/`sdk_engine.py`/`sdk_discovery.py` for `sdk`;
  `oauth.py`/`login_flow.py`/`credentials.py`/`direct_engine.py` for `direct`)
- `src/clio_agent/providers/model_discovery/codex.py` /
  `providers/codex/model_list.py` -- the direct transport's live model list
- `src/clio_agent/gact/routes/codex_variant.py` -- the sdk-transport
  readiness probe + the sdk/direct bind dispatch
- `src/clio_agent/gact/provider_catalog.py` -- the `transports` catalog row
  (`_codex_sdk_transport_row` / `_codex_direct_transport_row`)
- `src/clio_agent/gact/routes/provider_auth.py` -- the generic sign-in API
  (direct transport only)
- `src/clio_agent/providers/catalog.py` -- the catalog entry
