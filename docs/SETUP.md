# Setting up CLIO + GACT TUI

A short, opinionated guide for getting CLIO Agent running with the
GACT TUI front-end. Aimed at lab users who want to point a terminal
UI at CLIO and start asking scientific-data questions.

## What you're installing

- **clio-agent** (Python) — the agent itself, plus a FastAPI server
  (`clio-agent-gact`) that speaks the GACT v0.2 contract.
- **gact** (Go binary) — the TUI front-end. Connects to any
  GACT-compliant backend via REST + SSE.

You can use either piece independently — the TUI works against any
backend that implements the contract; CLIO works against any GACT
client. This guide covers the common case: both together.

## Prerequisites

- Python 3.12+ and [`uv`](https://github.com/astral-sh/uv) — `pip
  install uv` works in a pinch but the lab usually has it.
- Go 1.26.2+ for the TUI binary (or grab a release artefact when we
  ship them).
- An LM endpoint. Any of these:
  - **OpenAI / ChatGPT** — needs `OPENAI_API_KEY` (most lab
    members; same key Codex CLI uses).
  - **Anthropic Claude direct** — needs `ANTHROPIC_API_KEY`.
  - **Claude Max via Meridian** — Meridian proxies your Claude Max
    OAuth as an OpenAI-compatible API. Cheapest if you have a Max
    subscription. See [Meridian](https://github.com/rynfar/meridian).
  - **OpenRouter** — single key, many providers (incl. free
    tier models for testing).
  - **Local: LM Studio / Ollama** — fully on-device.

## Install — one-line script (recommended)

```sh
# Linux / macOS
curl -fsSL https://raw.githubusercontent.com/iowarp/clio-agent/main/install/install.sh | sh

# Windows (PowerShell)
irm https://raw.githubusercontent.com/iowarp/clio-agent/main/install/install.ps1 | iex
```

Installs both repos under `~/.local/share/clio` (or `%LOCALAPPDATA%\clio`),
builds the TUI, and drops a `clio` launcher into `~/.local/bin`. The
launcher boots the server on `:17800` if it isn't already running and
attaches the TUI. Run `clio` and you're chatting.

Pin a specific tag: `CLIO_REF=v0.3.1 GACT_REF=v0.2.1 curl … | sh`. See
[install/README.md](../install/README.md) for the full env-override
table.

## Install — manual (if you want to control where everything lives)

```bash
# Pull both repos somewhere convenient.
git clone https://github.com/iowarp/clio-agent.git
git clone https://github.com/iowarp/gact-tui.git

# Install CLIO with the API extra (pulls FastAPI + uvicorn).
cd clio-agent
uv pip install -e '.[api]'

# Build the TUI.
cd ../gact-tui/tui
go build -o gact .
```

## Configure providers from inside the TUI (no env vars needed)

The first time you run the TUI against an unconfigured backend, the
**LM Provider** modal pops automatically — pick a preset (Meridian /
Claude Max via Meridian / Anthropic API / OpenAI / OpenRouter / LM
Studio / Ollama), paste an API key if needed, save. CLIO is wired
in-place; the next message uses the new provider.

To swap mid-session: **Ctrl+S** → Settings → Model → **Change provider…**
The same modal opens, you re-pick, save, and the next turn uses the new
provider. No restart, no env-var dance.

## Boot CLIO

```bash
cd clio-agent
uv run clio-agent-gact --port 17800
```

The server boots without an LM wired — the TUI will pop a config
modal on first connect. Or pre-configure via env vars:

```bash
export CLIO_LM_PROVIDER=openai
export CLIO_LM_API_BASE=https://api.openai.com/v1
export CLIO_LM_MODEL=gpt-4o-mini
export CLIO_LM_API_KEY=sk-...
uv run clio-agent-gact --port 17800
```

## Connect the TUI

```bash
GACT_BACKEND=http://127.0.0.1:17800 ./gact
```

If CLIO has no LM, the modal pops automatically:
1. **Pick a preset** with `←/→` (OpenAI is at the top of the list).
2. **Tab to Model** — defaults are sensible (`gpt-4o-mini`, `claude-haiku-4-5-20251001`).
3. **Tab to API key** — paste yours.
4. **Optional: Tab to Temperature / Max tokens** — leave blank for
   server defaults (1.0 / 32000).
5. **Tab to Save and connect — Enter**.

The modal dismisses, and you're ready to chat.

## Provider-specific notes

### OpenAI / ChatGPT (most common in the lab)

Same key Codex CLI uses. The modal preset wires:
- `provider: openai`
- `api_base: https://api.openai.com/v1`
- `suggested_model: gpt-4o-mini` (cheap; bump to `gpt-4o` or
  `gpt-4-turbo` for heavier reasoning).

Cost shows in the TUI footer per turn (e.g. `$0.0021  150 in / 800 out`).
The cost-meter price table tracks `gpt-4o-mini` ($0.15/$0.60 per M)
and `gpt-4o` ($2.5/$10 per M); other OpenAI models fall through to
zero — file an issue if you need a model added to the table.

### Meridian (Claude Max)

If you have a Claude Max subscription, install Meridian once
(`npm install -g @rynfar/meridian` then `meridian start`) and use:
- `provider: openai-compatible`
- `api_base: http://127.0.0.1:3456/v1`
- `model: claude-haiku-4-5-20251001` (or sonnet/opus)
- `api_key: anything` (Meridian doesn't validate)

### OpenRouter

Free-tier models work without spend but are heavily rate-limited:
- `openai/gpt-oss-120b:free`
- `qwen/qwen3-next-80b-a3b-instruct:free` (often rate-limited)
- `google/gemma-4-31b-it:free` (often rate-limited)

### LM Studio / Ollama

Fully local, no key required. Pick the preset; the TUI auto-discovers
models from the local server.

## Mid-conversation provider swap

You can change providers/models on the fly without losing the
session:

1. `Ctrl+S` (settings) → swap provider, model, temperature, or max
   tokens.
2. Save and continue chatting.

The next turn uses the new LM. The cost-meter recalculates with the
new model's prices.

## Smoke test

```bash
# Boot server.
cd clio-agent
uv run clio-agent-gact --port 17800 &

# Configure the LM.
curl -X PUT http://127.0.0.1:17800/v1/providers/lm \
  -H "Content-Type: application/json" \
  -d '{
    "provider": "openai",
    "model": "gpt-4o-mini",
    "api_base": "https://api.openai.com/v1",
    "api_key": "'"$OPENAI_API_KEY"'"
  }'

# Ask a question.
SID=$(curl -s -X POST http://127.0.0.1:17800/v1/sessions -H "Content-Type: application/json" -d '{"title":"smoke"}' | python3 -c 'import json,sys;print(json.load(sys.stdin)["id"])')
curl -X POST "http://127.0.0.1:17800/v1/sessions/$SID/messages" \
  -H "Content-Type: application/json" \
  -d '{"parts":[{"type":"text","text":"hi"}]}'

# Wait a few seconds, then read the answer.
sleep 10
curl -s "http://127.0.0.1:17800/v1/sessions/$SID/messages" | python3 -m json.tool
```

You should see the assistant's reply with a populated `tokens` and
`cost_usd` envelope.

## Try one of these to see CLIO routing in action

Each prompt below exercises a different code path. Run them through
the TUI (or via curl) once you have the LM configured. Each should
produce the documented behaviour — see
[`docs/CAPABILITIES_MATRIX.md`](CAPABILITIES_MATRIX.md) for the full
end-to-end verification matrix.

| Prompt | What it exercises |
|---|---|
| `"What is the schema of /tmp/clio-demo/clio_demo.parquet?"` | analysis expert (direct Parquet inspection) |
| `"Inspect /tmp/clio-demo/clio_demo.h5"` | data expert (direct HDF5 inspection) |
| `"validate parquet schema and statistics in parallel"` | analysis expert spawns 2 nanoagents (#9) — child sessions appear under the parent in the sidebar |
| `"propose an edit to /path/to/file.py — replace string concat with f-string"` | chat path → fs.propose_edit → file_diff Part rendered inline |
| `"hello, who are you?"` | chat agent (CLIO identity reply) |

## Try a third-party MCP server

CLIO can install + call any MCP from npm/pypi/wherever. Example with
the canonical "everything" server:

```bash
# Install (npx auto-resolves the package).
curl -X POST http://127.0.0.1:17800/v1/mcp/servers \
  -H "Content-Type: application/json" \
  -d '{
    "name": "everything",
    "transport": "stdio",
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/server-everything", "stdio"]
  }'

# Call a tool on it.
curl -X POST "http://127.0.0.1:17800/v1/mcp/servers/<id-from-install>/call" \
  -H "Content-Type: application/json" \
  -d '{"tool": "echo", "args": {"message": "hi from CLIO"}}'

# Confirm it shows in the catalog (TUI: type `/mcp`).
curl -s http://127.0.0.1:17800/v1/mcp/servers | python3 -m json.tool
```

The TUI's `/mcp` slash command shows bundled (fs/hdf5/parquet) AND
any installed third-party server in one list. Tools called through
the TUI's interactive chat OR via the `/v1/mcp/servers/{id}/call`
endpoint both register in `tools_called` metadata + emit
`tool.call.started/completed` SSE events.

## Troubleshooting

- **TUI shows "no LM configured"** — open the modal: `Ctrl+S` →
  swap to a preset, paste API key, save.
- **`/v1/providers/lm` returns 400 with `dspy.configure(...)` error**
  — known DSPy 3.x quirk. Fixed in clio-agent ≥ 0.3.0; bump if
  you're on an older version.
- **Tokens always 0** — the server is using DSPy's LM cache by
  default. clio-agent ≥ 0.3.0 turns it off (`cache=False`); upgrade.
- **ChatAdapter format markers (`[[ ## answer ## ]]`) leak into the
  reply** — fixed in clio-agent ≥ 0.3.0 via `StreamListener`.
- **"connecting…" flickers in the footer** — known polish item.
  Doesn't affect functionality.

## Open `/doctor` to check status

In the TUI, type `/doctor` to see:
- **Health tab**: every integration's status (api, sessions, agent,
  arc, lm).
- **Capabilities tab**: which v0.2 features the backend honours
  (28/30 supported by current CLIO; LSP + voice intentionally out).

Use this to debug "is the agent actually wired?" questions.
