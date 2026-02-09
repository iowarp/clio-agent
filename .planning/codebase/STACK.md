# Technology Stack

**Analysis Date:** 2026-02-09

## Languages

**Primary:**
- Python 3.12+ - All source code and agent implementation

**Secondary:**
- JSON - Configuration and MCP server definitions
- msgpack - Binary serialization for ARC memory persistence

## Runtime

**Environment:**
- Python 3.12+ (enforced in `pyproject.toml`)

**Package Manager:**
- UV (PEP 440, PEP 660 compliant) - Primary dependency manager
- Pip compatible

**Lockfile:**
- `uv.lock` - UV-generated lock file for reproducible builds

## Frameworks

**Core Agent Framework:**
- DSPy 3.0.3+ - Agent patterns, signatures, chain-of-thought, ReAct (INTERNAL)
- FastMCP 2.13.0+ - MCP protocol client, tool server communication

**UI/CLI:**
- rich 14.2.0+ - Terminal UI, formatting, panels, tables
- prompt-toolkit 3.0.0+ - Interactive REPL and prompting

**Memory Layer:**
- sortedcontainers 2.4.0+ - B-tree index for O(log N) retrieval
- lru-dict 1.3.0+ - LRU cache for hot data
- msgspec 0.18.0+ - Efficient msgpack serialization

**Testing:**
- pytest 7.4.0+ - Test runner
- pytest-cov 4.1.0+ - Coverage reporting
- pytest-asyncio 0.21.0+ - Async test support

**Development:**
- ruff 0.1.0+ - Linting and formatting
- mypy 1.7.0+ - Type checking

**Build/Distribution:**
- hatchling - Build backend (specified in `pyproject.toml`)

## Key Dependencies

**Critical - Agent Core:**
- `dspy-ai>=3.0.3` - Internal DSPy engine for signatures, modules, optimizers (NEVER expose to users)
- `fastmcp>=2.13.0` - MCP client for tool calling and server communication
- `requests>=2.31.0` - HTTP client for LM Studio API calls

**Infrastructure:**
- `sortedcontainers>=2.4.0` - B-tree index for memory retrieval
- `lru-dict>=1.3.0` - LRU cache layer
- `msgspec>=0.18.0` - Serialization with msgpack backend

**Scientific Data Tools (Optional):**
- `h5py>=3.10.0` - HDF5 file operations (tools extra)
- `adios2>=2.9.0` - ADIOS BP file operations (tools extra)
- `pyarrow>=14.0.0` - Parquet analytics (tools extra)

**IOWarp Integration (Optional):**
- `pyzmq>=25.0.0` - ZeroMQ for IOWarp CTE runtime communication (iowarp extra)

**REST API (Optional, Phase 4+):**
- `fastapi>=0.104.0` - REST API framework (api extra)
- `uvicorn>=0.24.0` - ASGI server for FastAPI
- `sse-starlette>=1.6.0` - Server-Sent Events for streaming

**Optimization (Optional, Phase 3+):**
- `scipy>=1.11.0` - Statistical tests and optimization
- `numpy>=1.24.0` - Numerical operations

## Configuration

**Environment Variables:**
- `IOWARP_ENDPOINT` - ZeroMQ endpoint for IOWarp CTE runtime (default: `tcp://localhost:5555`)
- `AGENT_TOOLKIT_<SERVER>_URL` - HTTP/SSE endpoint overrides for MCP servers:
  - `AGENT_TOOLKIT_HDF5_URL` - HDF5 MCP server endpoint
  - `AGENT_TOOLKIT_ADIOS_URL` - ADIOS MCP server endpoint
  - `AGENT_TOOLKIT_PARQUET_URL` - Parquet MCP server endpoint
  - `AGENT_TOOLKIT_SLURM_URL` - SLURM MCP server endpoint
  - `AGENT_TOOLKIT_DARSHAN_URL` - Darshan MCP server endpoint
  - `AGENT_TOOLKIT_COMPRESSION_URL` - Compression MCP server endpoint
  - `AGENT_TOOLKIT_PANDAS_URL` - Pandas MCP server endpoint
  - `AGENT_TOOLKIT_PLOT_URL` - Plot MCP server endpoint

**LM Configuration:**
- LM Studio at `http://127.0.0.1:1234` (default, configurable in `src/clio_agent/config.py`)
- OpenAI-compatible API endpoint (`/v1` suffix)
- API key: `lm-studio` (default placeholder)

**ARC Memory Storage:**
- Default directory: `.clio_agent/arc/`
- Subdirectories: `conversations/`, `invocations/`, `metrics/`, `context/`
- Storage tiers (IOWarp fallback): `warm/`, `cold/`, `archive/`

**Build:**
- `pyproject.toml` - PEP 621 project metadata and dependencies
- Build system: `hatchling` (specified in `[build-system]`)
- Package build target: `src/clio_agent/` (wheel packages)

**Linting & Formatting:**
- `.ruff` config in `pyproject.toml` - Line length 100, targets py312, rules E/W/F/I/B/C4
- `.black` config in `pyproject.toml` - Line length 100, targets py312
- `.mypy` config in `pyproject.toml` - Python 3.12, warn_return_any enabled

**Testing:**
- `pytest` config in `pyproject.toml` - `tests/` directory, test discovery pattern `test_*.py`
- Coverage threshold: Reports term-missing and HTML
- Async support enabled via `pytest-asyncio`

## Platform Requirements

**Development:**
- Python 3.12+
- UV package manager
- LM Studio (for local LLM development) OR compatible OpenAI API endpoint
- For IOWarp integration: ZeroMQ runtime at `tcp://localhost:5555`

**Production:**
- Python 3.12+ runtime
- LM (cloud API, local LM Studio, or custom OpenAI-compatible endpoint)
- FastMCP MCP servers (Agent Toolkit - `iowarp-mcps` package)
- Optional: IOWarp CTE runtime for persistent storage
- Optional: Docker/Kubernetes for containerized deployment

**Optional Scientific Tools:**
- HDF5 library (system) for `h5py` compilation
- ADIOS2 library (system) for `adios2` compilation
- Arrow library (system) for `pyarrow` compilation

## Entry Points

**CLI:**
- Command: `clio-agent` (defined in `pyproject.toml`)
- Implementation: `src/clio_agent/ui/cli.py:run_cli()`

**REST API:**
- Command: `clio-agent-api` (defined in `pyproject.toml`)
- Implementation: `src/clio_agent/ui/api.py:main()` (stub, Phase 4+)

**Programmatic:**
- Main agent: `from clio_agent import ClioAgent`
- Expert: `from clio_agent.experts import DataExpert`
- Memory: `from clio_agent.arc import ARCMemory`

---

*Stack analysis: 2026-02-09*
