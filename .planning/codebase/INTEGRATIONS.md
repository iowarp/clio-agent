# External Integrations

**Analysis Date:** 2026-02-09

## APIs & External Services

**Language Models (Chat):**
- LM Studio (local development)
  - Endpoint: `http://127.0.0.1:1234/v1` (configurable)
  - Protocol: OpenAI-compatible (chat completion)
  - Configuration: `src/clio_agent/config.py` - `LMStudioConfig`, `RouterLMConfig`, `ReasonerLMConfig`
  - Used by: Main agent (routing), Data expert (reasoning)

**MCP Tool Servers (Agent Toolkit - iowarp-mcps):**
- HDF5 Server
  - Connection: `uvx iowarp-mcps hdf5` (local) or `AGENT_TOOLKIT_HDF5_URL` (HTTP)
  - MCP client: `fastmcp>=2.13.0` (via `IOWarpMCPConnector`)
  - Tools: hdf5_analyze, hdf5_optimize, hdf5_check_compression

- ADIOS Server
  - Connection: `uvx iowarp-mcps adios` (local) or `AGENT_TOOLKIT_ADIOS_URL` (HTTP)
  - Tools: adios_analyze_bp, adios_convert

- Parquet Server
  - Connection: `uvx iowarp-mcps parquet` (local) or `AGENT_TOOLKIT_PARQUET_URL` (HTTP)
  - Tools: parquet_analyze, parquet_optimize

- SLURM Server
  - Connection: `uvx iowarp-mcps slurm` (local) or `AGENT_TOOLKIT_SLURM_URL` (HTTP)
  - Tools: slurm_submit, slurm_status, slurm_cancel

- Darshan Server
  - Connection: `uvx iowarp-mcps darshan` (local) or `AGENT_TOOLKIT_DARSHAN_URL` (HTTP)
  - Tools: darshan_analyze, darshan_io_summary

- Compression Server
  - Connection: `uvx iowarp-mcps compression` (local) or `AGENT_TOOLKIT_COMPRESSION_URL` (HTTP)
  - Tools: compression_test, compression_recommend

- Pandas Server
  - Connection: `uvx iowarp-mcps pandas` (local) or `AGENT_TOOLKIT_PANDAS_URL` (HTTP)
  - Tools: pandas_analyze, pandas_transform

- Plot Server
  - Connection: `uvx iowarp-mcps plot` (local) or `AGENT_TOOLKIT_PLOT_URL` (HTTP)
  - Tools: plot_generate, plot_interactive

**MCP Connector Implementation:**
- Location: `src/clio_agent/tools/mcp_connector.py`
- Class: `IOWarpMCPConnector` - Async/sync bridge with persistent clients
- Features: Thread-safe, long-lived event loop, ARC caching integration
- Connection types: Stdio (local `uvx` commands) or HTTP/SSE (remote servers)
- Cache: Tool results cached via ARC with 1-hour TTL

## Data Storage

**Databases:**
- None (no relational databases used)

**File Storage:**
- Local filesystem: `.clio_agent/arc/` (default directory for ARC memory)
- Tier structure:
  - `conversations/` - Conversation history
  - `invocations/` - Tool invocations and trajectories
  - `metrics/` - Performance metrics
  - `context/` - Compiled context snapshots
  - Storage tiers (when IOWarp unavailable): `warm/`, `cold/`, `archive/`

- Scientific Data Files (read-only via MCP servers):
  - HDF5 files (via HDF5 Server)
  - ADIOS BP files (via ADIOS Server)
  - Parquet files (via Parquet Server)
  - SLURM job traces (via SLURM Server)
  - Darshan I/O logs (via Darshan Server)

**Caching:**
- L1 Cache: `LRUCache` (in `src/clio_agent/arc/cache.py`) - In-memory hot data
- L2 Index: `BTreeIndex` (in `src/clio_agent/arc/index.py`) - O(log N) disk index
- L3 Store: `LSMTree` (in `src/clio_agent/arc/lsm.py`) - High-throughput metrics log
- Serialization: msgpack (via `msgspec>=0.18.0`)

## Authentication & Identity

**Auth Provider:**
- LM Studio: Placeholder API key (`lm-studio`) - development only
- MCP Servers: No authentication (assumes trusted network or private deployment)
- IOWarp: ZeroMQ endpoint (assumes secured via network policy)

**API Credentials:**
- LM configuration: `api_key` field in `LMStudioConfig` (default: `lm-studio`)
- Env-based overrides: None currently (model selection is hardcoded or auto-detected)

**Secrets Location:**
- None enforced - LM Studio credentials are development placeholders
- Production: Use environment variables or external secret management
- No `.env` file tracking in this codebase

## Monitoring & Observability

**Error Tracking:**
- None (no external error tracking service)

**Logs:**
- Console output via `print()` and `rich` formatting
- Location: `src/clio_agent/ui/cli.py` - Rich panels and logging
- No centralized log aggregation
- ARC metrics logged to LSM tree: `src/clio_agent/arc/lsm.py`

**Performance Metrics:**
- ARC memory statistics: Cache hit rate, retrieval latency
- Tool execution metrics: Duration, success/failure, cache hits
- Stored via LSMTree in ARC memory system
- Access via `ARCMemory.get_cache_stats()` and `ARCMemory.get_invocation_metrics()`

## CI/CD & Deployment

**Hosting:**
- Local development: LM Studio (CLI tool on developer machine)
- Production: Custom deployment (Docker, K8s, or bare metal)
- No built-in cloud provider integration

**CI Pipeline:**
- None (not configured in repository)
- Recommended: GitHub Actions or similar with `pytest`, `ruff check`, `mypy`

**Containerization:**
- No `Dockerfile` in current codebase
- Can be containerized with Python 3.12 base image
- Requires: LM Studio endpoint or OpenAI API endpoint
- Optional: IOWarp CTE runtime for persistent memory tier migration

## Environment Configuration

**Required Environment Variables:**
- None mandatory (all have sensible defaults)

**Optional Environment Variables:**
- `IOWARP_ENDPOINT` - ZeroMQ endpoint for IOWarp CTE runtime
  - Format: `tcp://host:port` (default: `tcp://localhost:5555`)
  - Check: `src/clio_agent/arc/storage.py` - `IOWarpCTEBackend._check_iowarp()`

- `AGENT_TOOLKIT_*_URL` - HTTP/SSE endpoints for MCP servers
  - Format: `http://host:port/mcp`
  - Examples:
    - `AGENT_TOOLKIT_HDF5_URL=http://hpc-cluster:8000/mcp`
    - `AGENT_TOOLKIT_ADIOS_URL=http://hpc-cluster:8001/mcp`
  - Location: `src/clio_agent/tools/mcp_connector.py` - `_initialize_agent_toolkit_servers()`

**Development Configuration:**
- LM Studio defaults to `http://127.0.0.1:1234`
- Model selection: Auto-detect from LM Studio `/v1/models` endpoint
  - Prefers: Granite chat models
  - Fallback: First available chat model
  - Location: `src/clio_agent/config.py` - `select_models_for_agents()`

## Webhooks & Callbacks

**Incoming:**
- None (agent is pull-based, not event-driven)

**Outgoing:**
- None currently
- Future: REST API endpoints for external agent collaboration (A2A protocol - Phase 5)

## MCP Tool Architecture

**Tool Integration Pattern:**
```
DSPy Agent (sync)
    ↓
IOWarpMCPTools (sync wrapper)
    ↓
IOWarpMCPConnector (async/sync bridge)
    ↓
FastMCP Client (async)
    ↓
MCP Server (Agent Toolkit)
    ↓
Scientific Tool (HDF5, ADIOS, SLURM, etc.)
```

**Connection Types:**
1. **Stdio (Local)**: Agent spawns `uvx iowarp-mcps <server>` subprocess
2. **HTTP/SSE (Remote)**: Client connects to `http://host:port/mcp` endpoint

**Tool Caching:**
- All tool results cached in ARC with 1-hour TTL
- Cache key: `(server_name, tool_name, arguments)` hash
- Location: `src/clio_agent/tools/mcp_connector.py` - `_call_tool_async()` (lines 385-410)

**Thread Safety:**
- Long-lived event loop in daemon thread
- FastMCP Client context managers kept alive across calls
- Thread-safe via `asyncio.run_coroutine_threadsafe()`
- Lock protects client dictionary during connection creation

## IOWarp Integration

**CTE Backend (Convergent Tiered Environment):**
- Location: `src/clio_agent/arc/storage.py` - `IOWarpCTEBackend`
- Purpose: Multi-tier storage with automatic migration
- Tiers: Hot (cache) → Warm (SSD) → Cold (NFS) → Archive (tape)
- Protocol: ZeroMQ at `tcp://localhost:5555` (default)

**Graceful Degradation:**
- If IOWarp unavailable: Falls back to local filesystem storage
- Check: Socket connect to ZeroMQ port with 1-second timeout
- Fallback directory: `.clio_agent/arc/` (same as primary)

**Tier Migration Policy:**
- Hot → Warm: 1 day (handled by LRU eviction)
- Warm → Cold: 7 days (infrequent access)
- Cold → Archive: 30 days (historical data)
- Access tracking: `access_metadata.msgpack` in data directory

**Namespace:**
- IOWarp namespace: `/clio_agent/arc` (in `src/clio_agent/arc/storage.py`)
- Registered on first connection with tier policy

## Data Flow & External Systems

**Query Execution Flow:**
```
CLI Input (user question)
    ↓
LM Studio (route query via DSPy)
    ↓
ARC Memory (retrieve context)
    ↓
Expert Agent Selection
    ↓
DataExpert / HPCExpert / etc.
    ↓
MCP Tool Calls (via IOWarpMCPConnector)
    ↓
Agent Toolkit Servers (HDF5, ADIOS, SLURM, etc.)
    ↓
ARC Memory (cache tool results)
    ↓
LSMTree Metrics (log invocation)
    ↓
IOWarp CTE (optional: tier migration)
    ↓
LM Studio (format response)
    ↓
CLI Output
```

## Network & Protocol Stack

**Protocols:**
- OpenAI-compatible API (HTTP) - LM Studio communication
- MCP (Model Context Protocol) - Tool server communication
- ZeroMQ (REQ/REP pattern) - IOWarp CTE runtime communication
- HTTP/SSE (optional) - Remote MCP server endpoints

**Ports & Endpoints:**
- LM Studio: `http://127.0.0.1:1234/v1` (development)
- IOWarp ZeroMQ: `tcp://localhost:5555` (default)
- MCP HTTP Servers: Configurable via `AGENT_TOOLKIT_*_URL` env vars
- REST API (future): `http://0.0.0.0:8000` (when implemented)

---

*Integration audit: 2026-02-09*
