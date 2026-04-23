# CLIO Agent

CLIO Agent is an experimental scientific-data harness for IOWarp.

The current `v0.2` line is intentionally local-first:

- inspect local `HDF5`, `Parquet`, and `CSV` files safely
- route to real in-process tools through the FastMCP gateway
- report runtime truth with `doctor` and API `/health`
- run against local or remote LM Studio, Ollama, or OpenAI-compatible endpoints
- keep `clio-core` integration behind explicit external-process and config boundaries

It is not yet a full `clio-core`/CTE-backed production runtime. The active plan
for getting there lives in [PLAN.md](PLAN.md).

## Current Capabilities

- `DataExpert`: HDF5 inspection, compression review, chunking guidance
- `AnalysisExpert`: Parquet schema/statistics plus CSV inspection
- `VisualizationExpert`: local chart generation for supported file workflows
- `ARC` local persistence for conversations, invocations, metrics, and profiles
- CLI with expert routing, history, metrics, tools, and `doctor`
- FastAPI service with `/health`, `/query`, `/experts`, `/metrics`, and SSE
- Runtime doctor output for LM, gateway, HDF5, Parquet, API, file policy, and `clio-core`

## Current Boundaries

The repository deliberately does **not** claim these are production-ready yet:

- real `clio-core` storage/runtime control
- CTE-backed ARC persistence as the default mode
- scheduler mutation workflows
- broad autonomous job execution
- complete HPC tool coverage beyond the current local harness path

For now, the reliable product path is:

1. configure a reachable language model
2. constrain file access with `CLIO_ALLOWED_ROOTS`
3. use explicit local file paths for deterministic inspection flows
4. treat `doctor` and `/health` as the source of truth for what is actually live

## Quick Start

### 1. Install dependencies

```bash
uv sync --extra dev --extra api --extra optimizers
```

### 2. Configure a model

#### Local homelab profile

```bash
source scripts/homelab-env.sh
clio_homelab_use dynamo-lms
export CLIO_ALLOWED_ROOTS=/home/akougkas/iowarp/clio-agent:/tmp
```

The `dynamo-lms` profile is pinned to:

- `CLIO_LM_PROVIDER=lm_studio`
- `CLIO_LM_API_BASE=http://192.168.86.143:1234/v1`
- `CLIO_LM_MODEL=nemotron-cascade-2-30b-a3b-i1`

#### Manual configuration

```bash
export CLIO_LM_PROVIDER=lm_studio
export CLIO_LM_API_BASE=http://127.0.0.1:1234/v1
export CLIO_LM_API_KEY=lm-studio
export CLIO_LM_MODEL=granite-4.0-h-small
export CLIO_ALLOWED_ROOTS=/home/akougkas/iowarp/clio-agent:/tmp
```

You can also use:

- `CLIO_LM_PROVIDER=ollama`
- `CLIO_LM_PROVIDER=openai`
- any local OpenAI-compatible backend reachable via `CLIO_LM_API_BASE`

### 3. Check runtime truth

```bash
uv run src/clio_agent/ui/cli.py doctor
```

### 4. Create demo data

```bash
uv run scripts/create_demo_data.py --output-dir /tmp/clio-agent-demo
```

This writes:

- `/tmp/clio-agent-demo/clio_demo.h5`
- `/tmp/clio-agent-demo/clio_demo.parquet`

### 5. Run the CLI

```bash
uv run src/clio_agent/ui/cli.py
```

Example:

```text
You: What datasets are in /tmp/clio-agent-demo/clio_demo.h5?
```

### 6. Run the API

```bash
uv run src/clio_agent/ui/api.py --host 127.0.0.1 --port 8000
```

Example:

```bash
curl -sS http://127.0.0.1:8000/health

curl -sS -X POST http://127.0.0.1:8000/query \
  -H 'Content-Type: application/json' \
  -d '{"question":"What datasets are in /tmp/clio-agent-demo/clio_demo.h5?"}'
```

## Runtime Modes

### Local filesystem harness

This is the primary supported mode today.

- ARC persists locally under `.clio_agent/`
- tool servers inspect real local files under `CLIO_ALLOWED_ROOTS`
- `doctor` and `/health` expose degraded vs ready integrations explicitly

### `clio-core` probe mode

CLIO can probe a local `clio-core` checkout and report:

- repo path detection
- config candidates
- binary candidates
- degraded/unavailable state

It does **not** start `chimaera`, `wrp_cae_omni`, or related services by default.

## Project Layout

```text
src/clio_agent/
  agent.py                   main orchestrator
  config.py                  LM provider configuration
  runtime/                   doctor and runtime status reporting
  arc/                       local memory, cache, retrieval, storage
  experts/                   data, analysis, visualization experts
  tools/                     execution boundary, file policy, gateway, servers
  ui/                        CLI and FastAPI entry points
  optimizer/                 offline instrumentation and variant support

tests/
  test_core/
  test_arc/
  test_experts/
  test_tools/
  test_integration/

scripts/
  create_demo_data.py
  homelab-env.sh
```

## Validation

Local validation used for the current experimental branch:

```bash
uv run ruff check src/ tests/ scripts/create_demo_data.py
uv run pytest tests/
```

GitHub Actions currently runs:

- `uv sync --extra dev --extra api --extra optimizers`
- `uv run ruff check src/ tests/ scripts/create_demo_data.py`
- `bash -n scripts/homelab-env.sh`
- `uv run mypy src/`
- `uv run pytest tests/ -m "not integration" --cov-fail-under=80`

Integration tests remain part of the local release gate because they depend on
available local runtimes and model endpoints.

## Documentation

- [PLAN.md](PLAN.md): active development plan and target architecture
- [AGENTS.md](AGENTS.md): repository guidance for coding agents and contributors
- [docs/CONTRIBUTOR_QUICKSTART.md](docs/CONTRIBUTOR_QUICKSTART.md): shortest contributor path

## Release Notes for Experimental v0.2

This branch is suitable for:

- local scientific file inspection demos
- API/CLI smoke tests
- runtime truth reporting
- validating LM Studio and OpenAI-compatible deployments

It is not yet the full IOWarp-integrated production harness.
