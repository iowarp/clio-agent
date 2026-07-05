# 07 — Providers + Config

> Everything the TUI (or an operator) needs to set before talking to CLIO. Source: `src/clio_agent/config.py`, `tools/file_policy.py`, `pyproject.toml`.

## LM provider matrix

CLIO's `LMProviderConfig` (`config.py:75-115`) ships with four built-in providers. Switch via `CLIO_LM_PROVIDER`:

| `CLIO_LM_PROVIDER` | Default `API_BASE` | Default `MODEL` | `API_KEY` source |
|---|---|---|---|
| `lm_studio` (default) | `http://127.0.0.1:1234/v1` | auto-discovered from `/v1/models` when blank | literal `"lm-studio"` |
| `ollama` | `http://127.0.0.1:11434/v1` | `granite3.1-dense:8b` | literal `"ollama"` |
| `openai` | `https://api.openai.com/v1` | `gpt-4o-mini` | `OPENAI_API_KEY` env |
| `anthropic` | `https://api.anthropic.com/v1` | `claude-sonnet-4-20250514` | `ANTHROPIC_API_KEY` env |
| `argonne` | `https://inference-api.alcf.anl.gov/resource_server/sophia/vllm/v1` | `openai/gpt-oss-120b` | Globus Auth (lazy) or `CLIO_ARGONNE_TOKEN` env |

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

# 3. Discover the currently loaded model ids:
python scripts/list_alcf_models.py

# 4. Point CLIO at Sophia with a currently loaded modern model:
export CLIO_LM_PROVIDER=argonne
export CLIO_LM_API_BASE=https://inference-api.alcf.anl.gov/resource_server/sophia/vllm/v1
export CLIO_LM_MODEL=openai/gpt-oss-120b
clio-agent
```

ALCF model availability is dynamic because hosted models are tied to running
gateway jobs. Prefer currently loaded modern models such as
`openai/gpt-oss-120b`, `openai/gpt-oss-20b`, `gpt-oss-120b` on Metis, or Llama
4 variants. Treat older Llama 3.1 ids as compatibility examples, not as the
recommended stress-test baseline.

The TUI's Settings → Model picker also exposes "ALCF Sophia (Globus
Auth)", "ALCF Metis (Globus Auth)", and "vLLM (localhost)" presets.
The ALCF presets issue `PUT /v1/providers/lm` with `provider=argonne`,
and the backend resolves a token via `providers.argonne_auth` if the
request body leaves `api_key` blank. The local vLLM preset is plain
OpenAI-compatible localhost configuration and does not use Globus.

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
| `CLIO_LM_TEMPERATURE` | `1.0` | sampling temperature for reasoner/chat paths |
| `CLIO_LM_PLANNER_TEMPERATURE` | `0.3` | sampling temperature for planner action selection |
| `CLIO_LM_PLANNER_MAX_TOKENS` | `CLIO_LM_MAX_TOKENS` | planner-only token cap for structured JSON actions |
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

- **Planner LM** — low temperature (`0.3`), model defaults to `ibm/granite-4-h-tiny`. Drives `AgentActionSignature` planning for direct tools, experts, chat answers, or explicit no-action decisions. Known local reasoning models such as Qwopus/Qwen use a deterministic planner profile (`planner_temperature=0`, planner token floor 4096) so hidden reasoning tokens do not starve JSON action output.
- **Reasoner LM** — default temperature `1.0`, same model by default. Drives expert ReAct loops, chat answers, and synthesis after tool observations.

Both are scoped per request via `dspy.context()` — no global model mutation (`CLAUDE.md` L30–133).

### Validated local reasoning profile

`qwopus3.5-9b-v3` has been validated through LM Studio's ROCm runtime on an AMD Radeon RX 6950 XT with 32k context loaded. CLIO applies the local reasoning-model planner profile automatically when the LM Studio/Ollama model id contains `qwopus`, `qwen3`, `qwen-3`, `qwen35`, or `qwen-3.5`.

Validation baseline:

- direct LM Studio smoke returned `LMSTUDIO_QWOPUS_OK`
- CLIO smoke returned `CLIO_QWOPUS_OK` with `error_info=null`
- long-context push used 26,056 prompt tokens and returned `CTX32K_QWOPUS_OK`

If a reasoning model still cannot produce valid planner JSON, CLIO should surface a structured `routing_error` with retry/reconfigure/exit actions rather than returning a fallback answer.

## Deployment modes

| Mode | Start | State |
|---|---|---|
| Interactive CLI | `clio-agent` or `uv run src/clio_agent/ui/cli.py` | live |
| REST API | `clio-agent-gact --host 0.0.0.0 --port 8100` | live |
| Docker | `docker compose up` | live (Uvicorn on :8100, health every 30 s) |
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
