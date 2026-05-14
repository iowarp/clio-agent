# Using OpenAI Codex as a CLIO provider

CLIO speaks the OpenAI chat-completions wire shape (Meridian, OpenRouter,
OpenAI direct, LM Studio, Ollama all plug in this way). To add the
[OpenAI Codex](https://github.com/openai/codex) app-server SDK as a
fourth pluggable provider — alongside Meridian, OpenRouter, and the
direct Anthropic / OpenAI APIs — we ship a tiny HTTP shim:
`scripts/codex_bridge.py`.

## One-time setup

Clone openai/codex (skip if you already have it locally), then install
the Python SDK in editable mode:

```sh
git clone https://github.com/openai/codex.git
cd codex/sdk/python
uv pip install -e .

# The SDK launches `codex` as a subprocess for each request. Make sure
# the codex binary is on PATH:
which codex
```

Install the bridge's HTTP deps into the same venv:

```sh
uv pip install 'fastapi>=0.104' 'uvicorn>=0.24'
```

## Running the bridge

```sh
python /path/to/clio-agent/scripts/codex_bridge.py --port 18900
```

You should see:

```
codex-openai-bridge listening on http://127.0.0.1:18900
  POST /v1/chat/completions  ← OpenAI-compatible
  GET  /v1/models             ← Codex models
```

Verify it's reachable:

```sh
curl -s http://127.0.0.1:18900/v1/models | jq '.data[].id'
```

## Wiring it up to CLIO from the TUI

The `OpenAI Codex (via bridge)` preset is now part of the LM-config
modal. Two ways to reach it:

- **First connect**: the modal pops automatically.
- **Mid-session**: `Ctrl+S → Settings → Model → Change provider…`

Pick `OpenAI Codex (via bridge)`, accept the defaults (model `gpt-5.4`,
api_base `http://127.0.0.1:18900/v1`), save. The next chat turn routes
through Codex.

## Wiring it via the wire (for scripting)

```sh
curl -X PUT http://127.0.0.1:17800/v1/providers/lm \
  -H 'Content-Type: application/json' \
  -d '{
    "provider":"openai-compatible",
    "model":"gpt-5.4",
    "api_base":"http://127.0.0.1:18900/v1",
    "api_key":"",
    "temperature":1.0,
    "max_tokens":2048
  }'
```

## Caveats

- The bridge runs each chat-completion request as a fresh Codex thread,
  using the most recent user message as the input. Multi-turn memory
  isn't preserved across chat-completion calls — Codex CLI is the right
  tool when that matters.
- System prompts are concatenated upfront then divided by `---`. Codex
  has its own personality and reasoning settings; expect it to lean on
  those over our injected system text. (Same caveat as Meridian.)
- Streaming SSE is not implemented; the bridge returns the full response
  in one POST. CLIO falls back to non-stream for any provider that
  doesn't surface deltas, so the TUI works either way (no per-token
  animation).
- Token usage is not surfaced — Codex reports it asynchronously and we
  haven't wired the notification path yet. Cost meter shows zero for
  Codex turns. Track this in a future bridge release.
