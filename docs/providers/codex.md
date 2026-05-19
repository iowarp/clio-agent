# Codex (subscription)

Use your ChatGPT / Codex subscription as a clio LM provider — no
per-token cost.

## How it works

clio-agent ships a LiteLLM `CustomLLM`
(`src/clio_agent/providers/codex_litellm.py`) that registers the
`codex/` model prefix. When you set `CLIO_LM_PROVIDER=codex`, DSPy
constructs `dspy.LM(model="codex/<your-model>")`, LiteLLM routes
that to our handler, and the handler dispatches to either:

- **`codex exec` subprocess** (default, no extra deps), or
- **`openai_codex` Python SDK** in-process (faster, opt-in via the
  `[codex]` extra).

Auth is whatever your `codex` CLI uses (ChatGPT login or API key) —
clio never sees the credential. We just shell out (or use the SDK,
which talks to the same local app-server daemon).

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
export CLIO_LM_MODEL=gpt-5            # or gpt-5-codex, gpt-5.4, gpt-4.1
clio                                  # launcher boots server + TUI
```

### TUI (in-session swap)

`Ctrl+S` → Settings → Model → Change provider → pick **OpenAI Codex
(subscription)**.

## Transport modes

| Mode | Selected by | Pros | Cons |
|---|---|---|---|
| `exec` (default) | always works | Pure subprocess, no Python deps | ~1-2s cold start per call |
| `sdk` | `CLIO_CODEX_TRANSPORT=sdk` *or* `optional_params["codex_transport"]="sdk"` | Faster after the daemon warms; lower latency | Requires `pip install 'clio-agent[codex]'` (git-pinned, not on PyPI) |

Default is fine for casual use. Switch to `sdk` if you're driving a
hot path (many turns per session). Install + select:

```sh
uv sync --extra codex
export CLIO_CODEX_TRANSPORT=sdk
clio
```

## What clio passes to Codex

The CustomLLM:

1. Flattens DSPy's messages to a single `ROLE: content\n\n…` prompt.
2. Spawns `codex exec --skip-git-repo-check --sandbox read-only
   --model <model> -o <tempfile> -` (exec path), or calls
   `Codex().thread_start(model=..., ephemeral=True,
   sandbox="read-only").run(prompt)` (sdk path).
3. Returns the final assistant message as a LiteLLM `ModelResponse`
   with `usage` stubbed at zero (Codex headless mode doesn't surface
   token counts).

The sandbox is locked to **read-only** so Codex's built-in
shell/filesystem tools are inert — the agent loop terminates after
one model response. clio's planner drives the real orchestration.

## Troubleshooting

**`codex` not on PATH.** The CustomLLM raises `CodexCLIUnavailableError`
with the install hint. Re-run after `npm install -g @openai/codex`.

**`codex exec` timeout.** Default is 120s. Set via
`optional_params["timeout"]` from your DSPy LM kwargs, or raise the
`max_tokens` config if you're hitting Codex's natural completion
boundary.

**SDK transport says "openai_codex SDK is not installed".** You need
the optional extra:
```sh
uv sync --extra codex          # contributors
pip install 'clio-agent[codex]' # users
```

**Wrong stdout format / no output file.** Verify `codex --version` ≥
0.128.0. Earlier CLIs may not support `--output-last-message`.

**Cost shows as $0 in the TUI footer.** Expected — Codex's headless
mode doesn't report usage. Cost lives in your ChatGPT subscription
dashboard, not in clio's per-turn meter.

## Known limitations

- **No streaming.** DSPy's `Predict` doesn't need it; we return
  non-streaming `ModelResponse`. The TUI synthesizes the typing
  animation locally.
- **Subprocess per-call (exec mode).** ~1-2s overhead. Use the SDK
  transport for hot paths.
- **No tool calling.** Sandbox is locked read-only; Codex's own tools
  are intentionally inert. clio's planner does tool routing via MCP.

## Related

- [Providers overview](README.md)
- [Adding a new provider](ADDING_A_PROVIDER.md)
- Source: [`src/clio_agent/providers/codex_litellm.py`](../../src/clio_agent/providers/codex_litellm.py)
- Source: [`src/clio_agent/providers/registry.py`](../../src/clio_agent/providers/registry.py) (`codex` entry)
