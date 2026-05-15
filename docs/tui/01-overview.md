# 01 — CLIO Agent Overview

> **Scope.** First in a series of notes documenting CLIO's external surface for frontend integrators — specifically the upcoming gact-tui face-lift. Written for somebody driving CLIO from a terminal frontend without reading the whole source tree first. File paths cite this repo's layout (relative from `docs/tui/`).

## What CLIO is

**One-liner**: CLIO Agent is a self-improving autonomous agent specialised in scientific data management inside HPC environments — the **Intelligence Layer (CEI)** of IOWarp's 3-tier architecture.

**Elevator pitch**: CLIO is *not* a framework for building agents — it IS the agent. It helps researchers and HPC operators inspect and optimise HDF5 files, analyse I/O traces, convert data formats, and reason over scientific-computing workflows. It routes work to specialised Experts, maintains persistent memory (ARC), learns from past runs, and can call external agents via the A2A protocol. Unlike a general-purpose chatbot, CLIO is context-engineered for scientific data, memory-backed, and self-improving (`../../README.md` L14–43; `CLIO_VISION.md` L1–7).

## Intended users

- **Primary:** HPC researchers, data scientists, cluster admins who need to inspect, optimise, transform, profile, and reason about scientific workflows using real tools in their real environment.
- **Secondary:** General-purpose agents (Claude Code, Gemini) that delegate domain work to CLIO as a "science sidekick"; coding agents embedding CLIO into larger automation pipelines.
  (`README.md` L38–42, `PLAN.md` L113–122)

## High-level agent graph

CLIO is a three-tier hierarchy:

```
TIER 1 — CLIO Main Agent (orchestrator)
  · Parses user query
  · Extracts required capabilities
  · Queries the Agent Registry for expert matches
  · Routes to a native Expert or an external agent

TIER 2 — Expert Agents (persistent specialists)
  · DataExpert      HDF5 / ADIOS / Parquet  (live)
  · HPCExpert       SLURM / MPI / Darshan   (planned)
  · ResearchExpert  papers / citations      (planned)
  · External agents LangChain, CrewAI, AutoGen via A2A protocol

TIER 3 — Nanoagents (ephemeral workers, future)
  · Spawned by Tier 2 via dspy.Parallel for sub-tasks
```

(`docs/CLIO_AGENT_ARCHITECTURE.md` L110–157; `README.md` L50–85)

### Single-turn flow (with memory)

```
User query (CLI / REST / A2A)
  → Main Agent: check ARC memory (O(log N)), extract capability list
  → Agent Registry: rank matching experts by capability + history, pick one
  → Expert (ReAct loop): Thought → Action (MCP tool) → Observation → repeat
  → Tool exec (FastMCP): ARC cache hit first (O(1)), else HDF5 / Parquet / SLURM server
  → Response: store invocation + metrics + conversation in ARC
  → (Background) Optimizer reads ARC metrics, deploys improved variants
```

(`CLIO_AGENT_ARCHITECTURE.md` L874–923; `README.md` L110–129)

## Key concepts — glossary

| Term | What it means | Why the TUI cares |
|---|---|---|
| **Expert** | A Tier-2 persistent specialist (currently `DataExpert`), given ≤7 curated MCP tools, runs a ReAct loop, registers capabilities in the Registry. | TUI should show *which expert is active*, stream its Thought / Action / Observation steps. (`README.md` L203–220) |
| **ARC** (Agent Runtime Context) | CLIO's native memory: LRU cache (hot, O(1)) + B-tree index (search, O(log N)) + LSM tree (write-heavy metrics). Stores conversations, invocations, metrics, cached tool results. Integrates with IOWarp CTE multi-tier storage. | TUI can surface memory stats (`/memory`) + expose cached-vs-cold responses. (`README.md` L269–289) |
| **DSPy** | Stanford framework for *programming* LMs (signatures, modules, `dspy.Tool.from_mcp_tool`, SIMBA optimiser). Implementation detail — not user-facing. | Don't expose in TUI UI. (`README.md` L570–572, `CLAUDE.md` L30–133) |
| **FastMCP** | MCP 3.x protocol / gateway CLIO uses to mount tool namespaces (`hdf5_list_datasets`, `parquet_analyze_schema`…) and inject dependencies like ARC. | Sources of truth for what tools exist. (`CLAUDE.md` L135–180) |
| **Agent Registry** | Coordination layer for discovery + routing; ranks agents by capability overlap, tier, history. Also compiles external (LangChain/CrewAI/AutoGen) agents into CLIO-compatible instances. | TUI's `/experts` listing comes from here. (`README.md` L203–220) |
| **Optimizer Layer** | Self-improvement: **offline** (user runs `--tune`, picks component, gives examples, picks optimiser) + **online** (auto A/B with statistical gate, gradual rollout, rollback). | TUI can surface "this model uses variant v3" or a tuning affordance later. (`README.md` L290–335) |
| **A2A Protocol** | Contract for CLIO to integrate agents from any framework. Request = query + context + capabilities + constraints; response = answer + reasoning_trace + tools_used + metadata. Future v0.2 boundary. | Not a default runtime path yet. (`README.md` L221–237) |
| **CEI / CAE / CTE** | IOWarp 3-layer split — **CEI**: CLIO + experts + ARC; **CAE/PPI**: FastMCP tool gateway; **CTE**: Hermes multi-tier storage. | TUI lives inside / above CEI. (`README.md` L253–265) |

## Deployment surface (v0.2.0)

| Mode | Entry point | Protocol | State |
|---|---|---|---|
| Interactive CLI | `uv run src/clio_agent/ui/cli.py` | Rich terminal UI | live |
| REST API | `uv run src/clio_agent/ui/api.py --host 127.0.0.1 --port 8000` | HTTP (FastAPI/Uvicorn) | live, basic endpoints |
| Docker | `docker-compose up` / `Dockerfile` | HTTP :8000 | live |
| Python library | `from clio_agent import ClioAgent` | in-process | planned |
| A2A | future HTTP task API | HTTP / formal A2A | v0.8 |

(`README.md` L476–525; `Dockerfile` L1–18; `docker-compose.yml` L1–23)

### REST endpoints today (v0.2)

| Endpoint | Method | Body | Returns | Status |
|---|---|---|---|---|
| `/health` | GET | — | `{"status":"ok","timestamp":...}` | ✅ |
| `/query` | POST | `{"question": str}` | `{"answer": str, "trace": [...]}` | ✅ |
| `/experts` | GET | — | `[{"name":"DataExpert","capabilities":[...]}]` | ✅ |
| `/metrics` | GET | — | `{"agent_id":"...","success_rate":0.95,...}` | ✅ |
| `/doctor` | GET | — | `{"lm":"ready","gateway":"ready","clio_core":"unavailable",...}` | ✅ |

Future (v0.4+): `/task/submit`, `/task/{id}/status`, `/task/{id}/cancel`, `/artifacts/{id}`, `/a2a`. (`PLAN.md` L149–150, L339–350)

## How users drive CLIO today

### Interactive CLI (primary)

```
$ uv run src/clio_agent/ui/cli.py
```

Rich-based REPL. Slash commands (`README.md` L374–401):

- `/help` — commands
- `/experts` — registered agents (native + external)
- `/registry` — Agent Registry status
- `/memory` — ARC stats
- `/tools` — available MCP tools
- `/doctor` — runtime integration health (LM, gateway, HDF5, Parquet, API, file policy, clio-core)
- `/metrics` — agent performance
- `/verbose` — toggle reasoning-trace display
- `/history` / `/clear` / `/quit`

Natural-language prompts route through the Main Agent → Expert → tools, streaming back.

### REST API (secondary)

```
$ uv run src/clio_agent/ui/api.py --host 127.0.0.1 --port 8000
$ curl -X POST http://localhost:8000/query \
    -H "Content-Type: application/json" \
    -d '{"question": "What datasets are in /tmp/clio-agent-demo/clio_demo.h5?"}'
```

## Config / environment

### LM provider (required)

```sh
# LM Studio (local default)
export CLIO_LM_PROVIDER=lm_studio
export CLIO_LM_API_BASE=http://127.0.0.1:1234/v1
export CLIO_LM_MODEL=openai/gpt-oss-20b   # auto-detected if empty

# Ollama
export CLIO_LM_PROVIDER=ollama
export CLIO_LM_API_BASE=http://127.0.0.1:11434/v1
export CLIO_LM_MODEL=llama3.1:8b

# OpenAI / Anthropic / Google / Custom — pattern is the same:
#   CLIO_LM_PROVIDER, CLIO_LM_API_KEY (or CLIO_LM_ENDPOINT), CLIO_LM_MODEL
```

(`src/clio_agent/config.py`; `README.md` L405–442)

### File-access policy

```sh
export CLIO_ALLOWED_ROOTS=/home/me/iowarp/clio-agent:/tmp
```

Empty → falls back to CWD (dev) or `/tmp` (prod). Prevents tools touching arbitrary paths. (`README.md` L159; `PLAN.md` L169–173)

### Runtime knobs

```sh
export CLIO_ENVIRONMENT=production        # or development
export CLIO_ARC_BACKEND=local             # or cte (future)
export CLIO_LOG_LEVEL=INFO                # or DEBUG / WARNING / ERROR
```

### Install

```sh
# Dev
uv sync --extra dev --extra optimizers
# Prod (minimal)
uv sync --frozen
```

Python **≥3.12** locked (`pyproject.toml` L14).

### Key deps

| Dep | Version | Purpose |
|---|---|---|
| `dspy-ai` | ≥3.1.0 | Agent patterns + optimisers (internal) |
| `fastmcp` | ≥3.0.0 | MCP protocol / gateway |
| `h5py` | ≥3.10.0 | HDF5 server |
| `pyarrow` | ≥14.0.0 | Parquet server |
| `rich` | ≥14.2.0 | Terminal UI (Rich-based CLI) |
| `sortedcontainers`, `lru-dict`, `msgspec` | — | ARC internals |
| `prompt-toolkit` | ≥3.0.0 | CLI input |

(`pyproject.toml` L37–49)

## Doctor reporting

`uv run src/clio_agent/ui/cli.py doctor` emits runtime truth for:

- **LM** — ready / unavailable, provider, model, endpoint, latency
- **Gateway** — MCP server availability (HDF5, Parquet, SLURM…)
- **ARC** — status, backend, metrics collection active
- **File policy** — `CLIO_ALLOWED_ROOTS` in force, max file size, symlink behaviour
- **API** — HTTP endpoint health if running
- **clio-core** — IOWarp integration status (non-destructive probe)

Reports degraded/misconfigured state clearly rather than crashing — this is the **integration-debug endpoint** the TUI should surface prominently. (`PLAN.md` L175–186, L410–421; `CONTRIBUTOR_QUICKSTART.md` L33–34)

## TUI-integrator cheat sheet

| Aspect | Reality |
|---|---|
| **What to call** | REST API (`:8000`) today; Python lib later; MCP for tool-level peek |
| **Endpoints used** | `POST /query`, `GET /health`, `GET /experts`, `GET /metrics`, `GET /doctor` |
| **Latency** | 1–5 s per expert invocation (tool exec + LM inference) |
| **Parallelism** | REST is async-safe; CLI is single-threaded REPL |
| **Memory** | ARC cache hit target >85% — TUI can show hit/miss |
| **Required config** | `CLIO_LM_*` + `CLIO_ALLOWED_ROOTS` |
| **Error handling** | Graceful degrade (MCP down → pure-LM reasoning; LM timeout → retry + partial) |
| **Artifacts** | Plots/profiles stored in ARC today; `/artifacts/{id}` retrieval endpoint future |
| **Auth** | None v0.2; future RBAC for mutating ops (SLURM, file write) |

## Next docs in this series

- `02-agent-graph.md` — message-loop internals, conversation model
- `03-experts.md` — expert lifecycle, registration, ReAct trace shape
- `04-arc-memory.md` — ARC layout + what the TUI can surface from it
- `05-tools.md` — catalogued tool surface (HDF5, Parquet, future SLURM…)
- `06-endpoints.md` — REST + MCP API reference with request/response shapes
- `07-providers-config.md` — LM provider matrix + env-var reference
- `08-semantics-and-lifecycle.md` — streaming, cancellation, error paths (extracted from the test suite)
- `09-integration-plan.md` — where gact-tui hooks in, what needs an adapter, what's missing upstream
