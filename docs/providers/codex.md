# Codex subscription provider

Use a ChatGPT/Codex subscription as a CLIO language-model provider without
placing an OpenAI API credential inside CLIO.

## Architecture

CLIO imports the official `openai_codex` Python SDK. The SDK owns its pinned
Codex runtime, authentication, and typed event stream. CLIO does not execute a
`codex` shell command, implement app-server JSON-RPC, or maintain a second
fallback transport.

DSPy routes the `codex/` model prefix to
`src/clio_agent/providers/codex_litellm.py`. That adapter:

1. serializes the DSPy messages into one role-preserving prompt;
2. starts an ephemeral, read-only SDK thread with tools, plugins, MCP servers,
   browser, memory, and internal multi-agent behavior disabled;
3. streams typed assistant deltas, provider reasoning events, and token usage;
4. rejects any unexpected internal Codex tool or agent action as a typed error.

CLIO remains the only agent loop and the only owner of tool execution.

## Install and authenticate

The project dependency installs `openai-codex` and its pinned runtime. Complete
Codex authentication once on the machine using the supported Codex login flow.
The SDK reuses that authentication; CLIO never receives the credential.

## Configure

```sh
export CLIO_LM_PROVIDER=codex
export CLIO_LM_MODEL=gpt-5.6-luna
export CLIO_CODEX_TRANSPORT=sdk
```

`CLIO_CODEX_TRANSPORT` may be omitted because `sdk` is the default and only
accepted value. Any legacy transport value is a hard configuration error; it is
never downgraded or used as a fallback.

## Streaming and reasoning truth

The SDK currently exposes assistant text deltas, provider-generated reasoning
summary deltas, and a `reasoning_output_tokens` count. It also defines a raw
reasoning-delta event, which CLIO preserves in a separate lane whenever the
runtime emits it.

A provider summary is not raw hidden reasoning. The transcript must label these
separately:

- provider reasoning: raw reasoning text actually emitted by the SDK;
- provider reasoning summary: a summary actually emitted by the SDK;
- hidden reasoning tokens: the reported token count, without invented text.

## Failure behavior

- No model activity for 120 seconds is a typed timeout.
- A total turn timeout is a typed failure.
- An unexpected internal tool, command, browser, or child-agent item is a typed
  isolation failure.
- SDK/auth/model errors are surfaced; there is no CLI or batch fallback.
- Empty assistant output is a typed error.

## Related source

- `src/clio_agent/providers/codex_stream.py`
- `src/clio_agent/providers/codex_litellm.py`
- `src/clio_agent/providers/model_discovery/codex.py`
- `src/clio_agent/providers/catalog.py`
