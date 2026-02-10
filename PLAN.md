# CLIO Agent Implementation Plan

Dependency-ordered phases for building CLIO Agent into a production-ready autonomous science agent.

**Current State** (Feb 2026): ~8,200 lines Python, 60+ modules. Main agent + DataExpert work in ChainOfThought mode. ARC memory 90% complete. All MCP servers are stubs. Optimizer layer is stubs. Nanoagents are stubs.

---

## Phase 1: Foundation Reset

**Goal**: Replace over-engineered custom infrastructure with native DSPy 3.x + FastMCP 3.x capabilities. Get one expert (DataExpert) working end-to-end with real tools.

**Why first**: Everything else depends on a working tool layer and correct DSPy patterns.

### Tasks

1.1 **Upgrade dependencies**
- Pin `dspy-ai>=3.1.0` and `fastmcp>=3.0.0` in pyproject.toml
- Configure `dspy.ChatAdapter` globally so ReAct works with LM Studio
- Verify ReAct agent can call tools with local models
- Remove `JSONAdapter` workarounds in agent.py

1.2 **Delete mcp_connector.py (789 lines)**
- Replace with `dspy.Tool.from_mcp_tool()` for DSPy tool bridging
- Replace with `fastmcp.Client` for direct MCP server communication
- Update DataExpert to use native tool bridge
- Remove `IOWarpMCPConnector`, `IOWarpMCPTools`, `create_iowarp_tool_function`

1.2a **Define async boundary**
- CLI stays sync, agent/expert/tool pipeline async internally
- FastMCP Client interactions use `async with Client(server)`
- DSPy ReAct calls use `await react.acall()` for MCP tool execution
- No custom sync/async bridge code — use native DSPy + FastMCP async patterns

1.3 **Build real HDF5 MCP server**
- Implement `tools/servers/hdf5_server.py` using FastMCP 3.x
- Tools: `list_datasets`, `analyze_dataset`, `optimize_chunking`, `check_compression` (4 tools, not 10)
- Use `h5py` for actual file operations
- Test with `Client(server)` in-memory pattern (no subprocess needed)
- Remove mock `hdf5_analyze` / `hdf5_optimize` from data_expert.py

1.4 **Build CLIO Gateway server**
- Create `tools/gateway.py` using FastMCP `mount()` with namespacing
- Mount HDF5 server: `gateway.mount(hdf5_server, namespace="hdf5")`
- Add `analyze_file` gateway tool with format auto-detection
- Keep gateway extensible for Phase 2 servers

1.5 **Fix DataExpert to use real tools**
- Switch from ChainOfThought fallback to ReAct with ChatAdapter
- Connect to HDF5 MCP server via gateway
- Limit to 5-7 tools (curated, not exhaustive)
- Add 500+ word domain-specific system prompt to DataExpertSignature
- Verify: expert can analyze a real HDF5 file end-to-end

1.6 **Split main agent into Router + Chat Agent**
- Router (ChainOfThought + Literal, smallest model):
  - Intent detection: classify query as chat/data/analysis/visualization/none
  - Runs on fast SLM for low latency
  - Optimizable with MIPROv2 later
- Chat Agent (conversational, user-facing):
  - Handles direct conversation, context management, follow-ups
  - Delegates to experts via router when domain expertise needed
  - Returns expert results to user with conversational framing
- Use `dspy.context(lm=...)` for per-agent model selection
- Remove module-level `_data_expert_instance` global
- Verify: user question → router → DataExpert (ReAct) → HDF5 tools → chat agent → answer

1.7 **Clean up codebase**
- Delete dead code files:
  - `experts/hpc_expert.py`, `experts/research_expert.py`, `experts/workflow_expert.py`
  - `ui/a2a_endpoint.py`, `ui/tuning_ui.py`
  - `registry/a2a_adapter.py`, `registry/external_compiler.py`
  - `tools/mcp_wrapper.py`
- Clean up nanoagents/ interfaces to match target architecture (mark as planned, not implemented)
- Clean up optimizers/ interfaces to match target architecture (mark as planned, not implemented)
- Update imports everywhere

1.8 **Test coverage to 50%**
- Write tests for HDF5 MCP server using `Client(server)` in-memory testing
- Write tests for gateway tool routing
- Write tests for DataExpert with mocked MCP tools
- Write tests for main agent forward() flow
- Fix any broken existing tests

### Success Criteria
- [ ] Router (CoT + Literal) correctly dispatches to chat agent or DataExpert
- [ ] Chat Agent handles conversation and expert delegation
- [ ] DataExpert (ReAct) analyzes a real `.h5` file through MCP server
- [ ] mcp_connector.py deleted, replaced by native DSPy/FastMCP
- [ ] Async boundary defined: CLI sync, pipeline async internally
- [ ] No stub files claiming false functionality
- [ ] `pytest tests/` passes with >50% coverage
- [ ] `uv run src/clio_agent/ui/cli.py` works end-to-end

### Dependencies
- None (this is the foundation)

---

## Phase 2: Complete Data Management Workflow

**Goal**: Add AnalysisExpert + VisualizationExpert to close the data lifecycle: storage → analytics → visualization. Prove multi-agent thesis with two new experts.

**Why second**: Foundation must work before adding more experts. Closing the data management cycle proves that specialized small-model agents can match or beat expensive generalist LLMs at lower cost.

### Tasks

2.1 **Build Parquet MCP server**
- `tools/servers/parquet_server.py`: `analyze_schema`, `query_data`, `compute_statistics`
- Mount: `gateway.mount(parquet_server, namespace="parquet")`
- Test with `Client(server)` in-memory

2.2 **Implement AnalysisExpert**
- `experts/analysis_expert.py` with ReAct pattern
- Tools: Parquet server tools + statistical analysis utilities
- 500+ word domain-specific system prompt for data analysis
- Register in agent registry with capabilities
- Update router Literal to include "analysis"

2.3 **Implement VisualizationExpert**
- `experts/visualization_expert.py` with ReAct pattern
- Tools: chart generation, data formatting, summary tables
- Register in agent registry
- Update router Literal to include "visualization"

2.4 **Extend router for multi-expert dispatch**
- Router Literal: `Literal["chat", "data", "analysis", "visualization", "none"]`
- Store routing decisions in ARC for future optimization

2.5 **Add context compilation pipeline to ARC**
- Implement `arc/context_compiler.py`: filter -> compact -> enrich -> assemble
- Replace raw history concatenation with compiled context windows
- Add procedural memory type (what worked/failed) alongside episodic and semantic
- Max context budget per tier (T1: 2K tokens, T2: 4K tokens)

2.6 **Expert collaboration via ARC shared context**
- DataExpert stores dataset profile in ARC
- AnalysisExpert reads dataset profile to tailor analysis approach
- VisualizationExpert reads analysis results for chart selection

2.7 **Dynamic tool discovery**
- Gateway exposes `list_capabilities` tool
- Main agent lazy-loads tool schemas (reduce context from ~47K to ~400 tokens)
- Only inject tool descriptions for the selected expert's tools

2.8 **End-to-end workflow demo**
- User provides dataset → DataExpert analyzes format → AnalysisExpert runs statistics → VisualizationExpert presents results
- Prove: 3 small specialized agents complete this cheaper than one large generalist LLM

2.9 **Test coverage to 60%**
- Tests for AnalysisExpert, VisualizationExpert, Parquet server
- Integration tests for multi-expert routing
- Tests for context compilation pipeline

### Success Criteria
- [ ] 3 experts (DataExpert + AnalysisExpert + VisualizationExpert) working with real tools
- [ ] Router correctly dispatches to the right expert based on query intent
- [ ] Experts share context via ARC (dataset profile flows through pipeline)
- [ ] End-to-end data lifecycle: storage → analytics → visualization
- [ ] Context compilation reduces token usage by >50% vs raw concatenation
- [ ] `pytest tests/` passes with >60% coverage

### Dependencies
- Phase 1 complete

---

## Phase 3: Self-Improvement (Optimizer Layer)

**Goal**: Implement the Optimizer Layer using DSPy 3.x SIMBA optimizer. Collect training data, optimize prompts, deploy improved variants.

**Why third**: Need working experts + ARC metrics to have data for optimization.

### Tasks

3.1 **Training data collection**
- Instrument all expert calls to log (input, output, success/failure) to ARC
- Build training set generator from ARC invocation history
- Implement `optimizers/training_data.py` with data extraction + formatting
- Minimum 50 examples per expert for meaningful optimization

3.2 **Selective optimization**
- Router: optimize with MIPROv2 if routing accuracy < 90%
- Experts: optimize with SIMBA only for experts with measurable underperformance
- Not everything needs optimization — if it works, leave it alone
- DSPy provides the infrastructure; using it is a tool, not a requirement
- Statistical significance testing before deployment (p < 0.05)

3.3 **Variant management**
- `optimizers/variant_manager.py`: store optimized variants in ARC
- Load best variant on startup
- Rollback capability if new variant degrades performance
- Version tracking: variant_id, training_examples, improvement_delta

3.4 **Offline tuning CLI mode**
- `--tune` flag on CLI for interactive tuning session
- Show current performance metrics
- Run optimization with progress reporting
- Before/after comparison display
- Deploy or rollback decision

3.5 **Metrics dashboard in CLI**
- `/metrics` command shows: success_rate, avg_latency, cache_hit_rate per expert
- `/compare` command shows variant A vs B performance
- Stored in ARC, computed from LSM tree data

3.6 **Test coverage to 70%**
- Tests for training data extraction
- Tests for optimizer integration (mock LM calls)
- Tests for variant management
- Tests for metrics computation

### Success Criteria
- [ ] Training data collected from ARC (50+ examples per expert)
- [ ] SIMBA optimization improves expert success_rate by >5%
- [ ] `uv run src/clio_agent/ui/cli.py --tune` runs optimization workflow
- [ ] Optimized variants stored in ARC with rollback capability
- [ ] `pytest tests/` passes with >70% coverage

### Dependencies
- Phase 2 complete (need multi-expert + ARC metrics data)

---

## Phase 4: Production Hardening

**Goal**: REST API, CI/CD pipeline, container deployment, 80%+ test coverage. Make CLIO Agent deployable.

### Tasks

4.1 **REST API**
- `ui/api.py` using FastAPI
- `POST /query` - send question, get answer
- `GET /experts` - list registered experts
- `GET /metrics` - performance metrics
- `GET /health` - health check
- SSE streaming for long-running queries

4.2 **CI/CD pipeline**
- GitHub Actions: lint (ruff), type check (mypy), test (pytest), coverage gate (80%)
- Pre-commit hooks: ruff format + ruff check
- Automated release workflow on tag push

4.3 **Container deployment**
- Dockerfile for CLIO Agent API server
- Singularity definition file for HPC environments
- Docker Compose with CLIO Agent + LM Studio
- Health checks and graceful shutdown

4.4 **Test coverage to 80%**
- Unit tests for all modules
- Integration tests for API endpoints
- End-to-end tests for CLI and API
- Performance benchmarks for ARC operations

4.5 **Error handling hardening**
- Structured error responses (not raw tracebacks)
- Graceful degradation: MCP server down -> reasoning-only mode
- Timeout handling for LM calls and tool calls
- Rate limiting on API endpoints

4.6 **Configuration management**
- Environment-based config (dev, staging, production)
- Support for multiple LM providers (LM Studio, Ollama, OpenAI, Anthropic)
- MCP server configuration via config file or environment variables

### Success Criteria
- [ ] REST API functional with OpenAPI docs
- [ ] CI/CD pipeline runs on every PR
- [ ] Docker image builds and runs CLIO Agent
- [ ] `pytest tests/` passes with >80% coverage
- [ ] Graceful degradation verified (kill MCP server, agent still responds)

### Dependencies
- Phase 3 complete (optimizer layer needs to be stable before production)

---

## Phase 5: IOWarp Integration

**Goal**: Connect ARC persistent storage to IOWarp CTE backend. Enable multi-tier data migration and intelligent prefetching.

### Tasks

5.1 **ARC-CTE storage backend**
- Implement real IOWarp CTE connection in `arc/storage.py` (currently falls back to local FS)
- Register `/clio_agent/arc/*` namespace in IOWarp
- Read/write conversations, invocations, metrics to CTE

5.2 **Multi-tier migration**
- Configure tier policy: hot (NVMe) -> warm (PFS) -> cold (object store)
- Automatic migration based on access patterns
- Prefetching for predicted queries

5.3 **Additional MCP servers for IOWarp ecosystem**
- Darshan server: `analyze_log`, `get_io_summary`
- ADIOS server: `analyze_bp_file`, `convert_format`
- Mount all in gateway

5.4 **IOWarp-aware optimizations**
- DataExpert considers storage tier when recommending strategies
- Tool results cached at appropriate IOWarp tier
- ARC retrieval uses IOWarp prefetching hints

### Success Criteria
- [ ] ARC data persisted in IOWarp CTE
- [ ] Data migrates across tiers based on access patterns
- [ ] 4+ MCP servers working (HDF5, Parquet, Darshan, ADIOS)
- [ ] IOWarp CTE connection failure degrades gracefully to local FS

### Dependencies
- Phase 4 complete (need production-quality code before IOWarp integration)
- IOWarp CTE runtime available

---

## Phase 6: Advanced Features

**Goal**: Online learning, A2A protocol, additional experts, community features.

### Tasks

6.1 **Online learning**
- A/B testing framework for prompt variants
- Automatic optimization triggers based on metric degradation
- Gradual rollout (10% -> 50% -> 100%)
- Rollback on degradation detection

6.2 **A2A Protocol**
- Implement Google A2A protocol for external agent integration
- CLIO Agent as provider (other agents call CLIO for science tasks)
- CLIO Agent as consumer (CLIO delegates to external agents)
- Agent Cards for capability advertisement

6.3 **Additional experts**
- HPCExpert: HPC job management, cluster optimization (when IOWarp integration is ready)
- ResearchExpert: literature search, citation management
- WorkflowExpert: pipeline orchestration (Nextflow, Parsl)

6.4 **Nanoagent pool** (if needed)
- Use `dspy.Parallel` for parallel sub-task execution
- Ephemeral workers for parameter sweeps, compression testing
- Pool management with resource limits

6.5 **Model-role fit**
- Configure different models per agent tier
- T1 (main): fast model for routing (SLM)
- T2 (experts): capable model for reasoning
- T3 (nanoagents): smallest model for focused tasks
- `dspy.context(lm=...)` per-request model switching

### Success Criteria
- [ ] Online learning automatically improves performance
- [ ] A2A protocol allows external agents to call CLIO Agent
- [ ] 4+ experts working
- [ ] Model-role fit reduces cost while maintaining quality

### Dependencies
- Phase 5 complete

---

## Cross-Cutting Requirements (All Phases)

- Main agent + DataExpert + CLI must always work (never break baseline)
- All data flows through ARC (no separate storage systems)
- DSPy is internal implementation detail (never exposed in user-facing interfaces)
- Tool curation: max 5-7 tools per expert (curated, not exhaustive)
- Type hints on all functions, Google-style docstrings
- Commit format: `<type>: <description>` (feat, fix, refactor, test, docs)

---

**Last Updated**: 2026-02-10
