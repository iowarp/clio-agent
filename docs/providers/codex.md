# Codex (subscription)

Use your ChatGPT / Codex subscription as a clio LM provider — no
per-token cost.

## How it works

clio-agent ships a LiteLLM `CustomLLM`
(`src/clio_agent/providers/codex_litellm.py`) that registers the
`codex/` model prefix. When you set `CLIO_LM_PROVIDER=codex`, DSPy
constructs `dspy.LM(model="codex/<your-model>")`, LiteLLM routes
that to our handler, and the handler drives the native **`codex
app-server`** — a warm subprocess per `(model, cwd)` speaking JSON-RPC
over stdio, with true token streaming and live usage.

Auth is whatever your `codex` CLI uses (ChatGPT login or API key) —
clio never sees the credential.

## Prerequisites

1. **Codex CLI installed**:
   ```sh
   npm install -g @openai/codex
   # or
   brew install --cask codex
   ```
2. **One-time login**:
   ```sh
   codex login
   ```
   Pick "Sign in with ChatGPT" to use your Plus / Pro / Business /
   Edu / Enterprise plan, or paste an OpenAI API key.

Verify:
```sh
codex --version    # 0.128.0 or later
```

## Use it

### Quick (env vars)

```sh
export CLIO_LM_PROVIDER=codex
export CLIO_LM_MODEL=gpt-5.5          # or gpt-5-codex, gpt-4.1
clio                                  # launcher boots server + TUI
```

### TUI (in-session swap)

`Ctrl+S` → Settings → Model → Change provider → pick **OpenAI Codex
(subscription)**.

## Transport

One transport since v0.8.0: the native **app-server** (`codex app-server`
JSON-RPC over stdio) — a warm subprocess per `(model, cwd)` with true token
streaming and live usage reporting. The legacy `exec` batch subprocess and the
`sdk` transport were deleted; setting `CLIO_CODEX_TRANSPORT` /
`lm.codex_transport` to anything but `app_server` is a hard configuration
error (unset it).

## What clio passes to Codex

The CustomLLM:

1. Flattens DSPy's messages to a single role-hardened JSON-Lines prompt.
2. Runs one turn on the warm app-server thread (read-only sandbox),
   streaming `item/agentMessage/delta` chunks live.
3. Returns the final assistant message as a LiteLLM `ModelResponse`
   with real `usage` from `thread/tokenUsage/updated`.

The sandbox is locked to **read-only** so Codex's built-in
shell/filesystem tools are inert — the agent loop terminates after
one model response. clio's planner drives the real orchestration.

## Troubleshooting

**`codex` not on PATH.** The CustomLLM raises `CodexCLIUnavailableError`
with the install hint. Re-run after `npm install -g @openai/codex`.

**Turn timeout.** Default is 120s. Set via
`optional_params["timeout"]` from your DSPy LM kwargs, or raise the
`max_tokens` config if you're hitting Codex's natural completion
boundary.

**`CLIO_CODEX_TRANSPORT` errors.** `exec` and `sdk` were removed in the
v0.8.0 cleanup; unset the variable (or set `app_server`).

**App-server protocol errors.** Verify `codex --version` ≥ 0.128.0 —
earlier CLIs may not ship the `codex app-server` surface.

**Cost.** The app-server reports live token usage
(`thread/tokenUsage/updated`), so per-turn tokens are real; dollar cost
still lives in your ChatGPT subscription, not clio's meter.

## Known limitations

- **No tool calling.** Sandbox is locked read-only; Codex's own tools
  are intentionally inert. clio's planner does tool routing via MCP.

## Related

- [Providers overview](README.md)
- [Adding a new provider](ADDING_A_PROVIDER.md)
- Source: [`src/clio_agent/providers/codex_litellm.py`](../../src/clio_agent/providers/codex_litellm.py)
- Source: [`src/clio_agent/providers/catalog.py`](../../src/clio_agent/providers/catalog.py) (`codex` entry)
