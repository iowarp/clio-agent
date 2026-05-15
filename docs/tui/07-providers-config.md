# 07 — Providers + Config

> Everything the TUI (or an operator) needs to set before talking to CLIO. Source: `src/clio_agent/config.py`, `tools/file_policy.py`, `pyproject.toml`.

## LM provider matrix

CLIO's `LMProviderConfig` (`config.py:75-115`) ships with four built-in providers. Switch via `CLIO_LM_PROVIDER`:

| `CLIO_LM_PROVIDER` | Default `API_BASE` | Default `MODEL` | `API_KEY` source |
|---|---|---|---|
| `lm_studio` (default) | `http://127.0.0.1:1234/v1` | `ibm/granite-4-h-tiny` | literal `"lm-studio"` |
| `ollama` | `http://127.0.0.1:11434/v1` | `granite3.1-dense:8b` | literal `"ollama"` |
| `openai` | `https://api.openai.com/v1` | `gpt-4o-mini` | `OPENAI_API_KEY` env |
| `anthropic` | `https://api.anthropic.com/v1` | `claude-sonnet-4-20250514` | `ANTHROPIC_API_KEY` env |
| `argonne` | `https://inference-api.alcf.anl.gov/resource_server/sophia/vllm/v1` | `meta-llama/Meta-Llama-3.1-8B-Instruct` | Globus Auth (lazy) or `CLIO_ARGONNE_TOKEN` env |

(`config.py:40-72`)

### Argonne / ALCF native models

Set `CLIO_LM_PROVIDER=argonne` to talk to ALCF's hosted vLLM gateway
(Sophia / Polaris). Authentication is a Globus Auth bearer token tied
to the user's `anl.gov` / `alcf.anl.gov` identity, minted on demand and
refreshed for ~6 months from a single OAuth flow.

```sh
# 1. Install the optional dep:
pip install 'clio-agent[argonne]'

# 2. Run the OAuth flow once per machine:
python -m clio_agent.providers.argonne_auth authenticate

# 3. Point CLIO at Sophia (default) — or Polaris:
export CLIO_LM_PROVIDER=argonne
export CLIO_LM_API_BASE=https://inference-api.alcf.anl.gov/resource_server/sophia/vllm/v1
export CLIO_LM_MODEL=meta-llama/Meta-Llama-3.1-8B-Instruct
clio-agent
```

The TUI's Settings → Model picker also exposes "ALCF Sophia (Globus
Auth)", "ALCF Polaris (Globus Auth)", and "ALCF local vLLM (compute-
node)" presets — picking one issues `PUT /v1/providers/lm` with
`provider=argonne`, and the backend resolves a token via
`providers.argonne_auth` if the request body leaves `api_key` blank.

`/health` and `/doctor` report on Argonne separately:

- `globus-sdk` not installed → `unavailable`, with a `pip install`
  hint.
- No tokens on disk → `misconfigured`, with the `authenticate` hint.
- Tokens present → `skipped` (we don't live-probe the gateway since
  that would refresh tokens on every health hit).

All overridable with `CLIO_LM_API_BASE`, `CLIO_LM_MODEL`, `CLIO_LM_API_KEY`.

## Full env-var reference

| Env var | Default | Purpose |
|---|---|---|
| `CLIO_LM_PROVIDER` | `lm_studio` | which provider to use |
| `CLIO_LM_API_BASE` | provider-specific | endpoint URL |
| `CLIO_LM_MODEL` | provider-specific | model ID |
| `CLIO_LM_API_KEY` | provider-specific or env | API key |
| `CLIO_LM_TEMPERATURE` | `1.0` | sampling temperature (reasoner); router is forced to `0.3` |
| `CLIO_LM_MAX_TOKENS` | `32000` | per-response cap |
| `CLIO_ENVIRONMENT` | `dev` | `dev` / `staging` / `production` |
| `CLIO_ARC_BACKEND` | `local` | `local` or `cte` (future) |
| `CLIO_LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| **File policy** | | |
| `CLIO_ALLOWED_ROOTS` | `$(pwd):$(pwd)/tmp` | colon-separated whitelist of paths tools may read |
| `CLIO_MAX_FILE_SIZE_BYTES` | `1073741824` (1 GB) | per-file cap |
| `CLIO_ALLOW_SYMLINKS` | `false` | allow symlinks in tool paths |
| **Secrets** | | |
| `OPENAI_API_KEY` | — | fallback for `openai` provider |
| `ANTHROPIC_API_KEY` | — | fallback for `anthropic` provider |

## DSPy LM setup

`setup_dspy(model: str | None, verbose: bool)` (`config.py:459-512`) wires a **multi-provider** DSPy `LM` with `ChatAdapter` support — that's the adapter that makes DSPy's `ReAct` work against local models (LM Studio / Ollama), not just OpenAI.

Two LMs exist:

- **Router LM** — low temperature (`0.3`), model defaults to `ibm/granite-4-h-tiny`. Drives `RouterSignature` classification.
- **Reasoner LM** — default temperature `1.0`, same model by default. Drives all expert ReAct loops.

Both are scoped per request via `dspy.context()` — no global model mutation (`CLAUDE.md` L30–133).

## Provider path for Claude Max via Meridian

DSPy's built-in Anthropic provider expects a pay-as-you-go API key. For dev on a **Claude Max subscription** (OAuth), use [Meridian](https://github.com/rynfar/meridian) — a TypeScript/Bun proxy that bridges Anthropic's official SDK (OAuth) to an OpenAI-compatible endpoint. CLIO + Meridian looks identical to "CLIO + any openai-compatible backend":

```sh
# 1. Run meridian (follow its README for OAuth bootstrap):
meridian serve --port 4141 &

# 2. Point CLIO at it:
export CLIO_LM_PROVIDER=openai
export CLIO_LM_API_BASE=http://127.0.0.1:4141/v1
export CLIO_LM_API_KEY=any-placeholder      # meridian owns real auth
export CLIO_LM_MODEL=claude-sonnet-4-5

# 3. Launch CLIO as usual:
clio-agent-api --host 127.0.0.1 --port 8000
```

Meridian's README lists Crush / OpenCode / Aider / Cline as known-good clients; CLIO slots into the same integration pattern. No DSPy or CLIO changes required — the proxy is invisible to both ends.

Tradeoffs vs a native DSPy/Anthropic integration:

- **+** No API-key cost during development.
- **+** Reuses the user's existing Claude Max subscription session.
- **+** Same knobs as any other openai-compatible setup (`CLIO_LM_PROVIDER=openai`, swap `API_BASE`).
- **−** One extra process to supervise. If we ship this in a `gact agent deploy clio` adapter, the adapter should spawn/supervise meridian the same way it supervises `clio-agent-api`.
- **−** Latency adds ~10–30 ms per hop (negligible relative to LM inference itself).

For CI and non-interactive use where OAuth flow isn't viable, fall back to `openai` / `anthropic` with a real API key.

## Deployment modes

| Mode | Start | State |
|---|---|---|
| Interactive CLI | `clio-agent` or `uv run src/clio_agent/ui/cli.py` | live |
| REST API | `clio-agent-api --host 0.0.0.0 --port 8000` | live |
| Docker | `docker compose up` | live (Uvicorn on :8000, health every 30 s) |
| Python library | `from clio_agent import ClioAgent` | live (sync) |
| A2A task API | — | v0.8 |

Docker mount: `clio-data` volume → `.clio_agent/` (ARC local persistence). (`Dockerfile:1-18`, `docker-compose.yml:1-23`)

## Python + install

- Python **≥ 3.12** locked (`pyproject.toml:14`).
- Dev install: `uv sync --extra dev --extra optimizers`
- Prod install: `uv sync --frozen`

## Key runtime deps

| Dep | Min version | Purpose |
|---|---|---|
| `dspy-ai` | `≥3.1.0` | signatures, modules, ReAct, optimisers (internal) |
| `fastmcp` | `≥3.0.0` (tests pin `≥2.14`) | MCP gateway + tool mounting |
| `h5py` | `≥3.10.0` | HDF5 server |
| `pyarrow` | `≥14.0.0` | Parquet server |
| `rich` | `≥14.2.0` | terminal formatting (CLI) |
| `prompt-toolkit` | `≥3.0.0` | CLI input handling |
| `sortedcontainers`, `lru-dict`, `msgspec` | — | ARC internals |

## Doctor / health probe

`clio-agent` CLI: `/doctor` command. REST API: `GET /health`. Both emit integration status for:

- `lm` — ready / unavailable, provider, model, endpoint, latency
- `gateway` — MCP server availability (HDF5, Parquet, …)
- `arc` — ARC memory status, backend, metrics collection
- `file_policy` — `CLIO_ALLOWED_ROOTS` + `max_file_size` + symlink behaviour
- `api` — HTTP health if the API is running
- `clio_core` — IOWarp integration status (non-destructive probe)

**The TUI should surface `/health` prominently** — same role as `/doctor` in CLIO's own CLI. A "Clio is degraded: LM unavailable" banner is infinitely more useful than a generic error at first message.

## Config-error handling

Invalid cloud-provider config (e.g. `CLIO_LM_PROVIDER=openai` but no `OPENAI_API_KEY`) raises `ConfigError` at `load_config_from_env()` time (`config.py:118-173`). The REST API returns a 503 with the structured error shape so the TUI can show a helpful Settings hint.

## Summary table for the adapter

| Thing the TUI asks | Where it comes from |
|---|---|
| "Which LM is Clio using?" | `GET /health` → `provider`, or `LMProviderConfig` fields in-process |
| "Why is X tool failing?" | `tool_result.error` → `{type, code, message, details}` (05-tools.md) |
| "What paths can Clio read?" | `CLIO_ALLOWED_ROOTS` (echoed in `/health`'s `file_policy` integration) |
| "Which provider is best for my workload?" | User choice; LM Studio / Ollama for local dev; OpenAI / Anthropic for cloud |
| "Retry with a different model" | Re-set `CLIO_LM_*` + restart the API, or hit `/config/reload` (future) |
