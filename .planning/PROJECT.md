# CLIO Agent

## What This Is

CLIO Agent is an autonomous, self-improving AI agent specialized in scientific data management within the IOWarp HPC ecosystem. It helps scientists optimize HDF5 files, analyze I/O traces, convert data formats, run statistical analysis, and make scientific computing workflows faster. It runs locally with LM Studio and uses DSPy 3.x internally for agent patterns and FastMCP 3.x for real tool servers.

## Core Value

Three specialized small-model experts (Data, Analysis, Visualization) complete the storage-to-insight cycle cheaper and better than one large generalist LLM — and the system gets measurably better with use.

## Requirements

### Validated

<!-- Shipped and confirmed valuable. Inferred from existing codebase. -->

- ✓ Main agent processes user queries via DSPy ChainOfThought — existing
- ✓ DataExpert handles HDF5/data optimization queries — existing (CoT fallback)
- ✓ ARC Memory provides LRU cache (O(1)), B-tree index (O(log N)), LSM tree — existing (~90%)
- ✓ Agent Registry discovers experts via keyword-based capability matching — existing
- ✓ CLI provides interactive Rich TUI with /help, /history, /experts, /memory commands — existing
- ✓ Config auto-detects LM Studio models with Granite preference — existing
- ✓ msgspec schemas for typed serialization (630+ lines) — existing
- ✓ 25 tests passing with pytest — existing

### Active

<!-- Current scope: Phases 1-4. Building toward multi-expert pipeline + production readiness. -->

- [ ] Replace mcp_connector.py with native DSPy/FastMCP patterns
- [ ] Build real HDF5 MCP server with h5py (not stubs)
- [ ] Build FastMCP gateway with mount() and namespacing
- [ ] Switch DataExpert from CoT fallback to ReAct with ChatAdapter
- [ ] Split main agent into Router (Literal typed) + Chat Agent
- [ ] Build Parquet MCP server for analytics
- [ ] Implement AnalysisExpert with ReAct + Parquet tools
- [ ] Implement VisualizationExpert with matplotlib file output
- [ ] Context compilation pipeline (filter → compact → enrich → assemble)
- [ ] Procedural memory type in ARC (what worked/failed)
- [ ] Expert collaboration via ARC shared context
- [ ] Dynamic tool discovery (lazy-load schemas, ~400 tokens vs ~47K)
- [ ] SIMBA optimizer integration with training data collection
- [ ] Variant management with rollback capability
- [ ] Offline tuning CLI mode (--tune flag)
- [ ] REST API with FastAPI (POST /query, GET /experts, GET /metrics, GET /health)
- [ ] CI/CD pipeline (GitHub Actions: ruff, mypy, pytest, coverage gate)
- [ ] Container deployment (Dockerfile, Singularity, Docker Compose)
- [ ] Test coverage progression: 50% → 60% → 70% → 80%
- [ ] Graceful degradation chain (MCP down → reasoning only, etc.)
- [ ] Multi-provider LM support (LM Studio, Ollama, OpenAI, Anthropic)

### Out of Scope

<!-- Deferred to future milestone: IOWarp Integration + Advanced Features -->

- IOWarp CTE backend for ARC — requires CTE runtime availability (Phase 5)
- Multi-tier data migration (NVMe → PFS → object store) — IOWarp dependency (Phase 5)
- Darshan MCP server — IOWarp ecosystem (Phase 5)
- ADIOS MCP server — IOWarp ecosystem (Phase 5)
- Online learning / A/B testing — requires stable optimizer first (Phase 6)
- A2A Protocol — external agent integration deferred (Phase 6)
- HPCExpert — depends on IOWarp integration (Phase 6)
- ResearchExpert — literature search, deferred (Phase 6)
- WorkflowExpert — pipeline orchestration, deferred (Phase 6)
- Nanoagent pool — use dspy.Parallel when needed, not premature (Phase 6)
- Cost comparison proof — aspirational, not gating this milestone

## Context

- **Codebase**: ~8,244 lines Python, 60+ modules, Python >= 3.12
- **Build system**: UV + Hatchling, pyproject.toml
- **Branch**: v0.2.0
- **Working**: Main agent (CoT), DataExpert (CoT), ARC memory (90%), CLI, registry
- **Stubs to replace**: All MCP servers, optimizers, nanoagents, A2A, REST API
- **Legacy to delete**: mcp_connector.py (789 lines, over-engineered async/sync bridge)
- **Test files**: Need synthetic HDF5 files (no real data available)
- **LM provider**: LM Studio at 127.0.0.1:1234 (local only during dev)

### Technology Capabilities (Verified)

- **DSPy 3.x**: Tool.from_mcp_tool(), ChatAdapter, SIMBA, CodeAct, Parallel, context(lm=...), Literal outputs, native async
- **FastMCP 3.x**: mount() gateway, Client(server) in-memory testing, Depends() injection, @lifespan, Transforms, Streamable HTTP

### Research Insights

- Context compiled not concatenated (filter → compact → enrich → assemble)
- Max 5-7 tools per expert with agent story docs
- Planner-Worker hierarchy (T1 = Judge/Router, T2 = Planners + Workers)
- 3 memory types needed: episodic, semantic, procedural
- 500+ word domain-specific prompts per expert
- Dynamic MCP discovery reduces 47K tokens to ~400 tokens
- Model-role fit: different models for different tiers

## Constraints

- **Python**: >= 3.12 (locked)
- **LM Provider**: LM Studio local models only during development; multi-provider in Phase 4
- **CLI Primary**: CLI is primary interface, REST API is secondary (Phase 4)
- **No ORMs**: ARC handles all persistence, no SQLAlchemy or similar
- **Standalone**: Must work without IOWarp (IOWarp integration is future milestone)
- **Single machine**: Single-machine deployment for this milestone
- **Baseline**: CLI can break during Phase 1 rewrites but must work by phase end
- **DSPy internal**: Never expose DSPy in user-facing interfaces or error messages
- **Tool curation**: Max 5-7 tools per expert, composite operations, agent stories

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| Phases 1-4 scope (not 1-6) | IOWarp CTE not available, Phase 6 is speculative | — Pending |
| Allow CLI breakage during Phase 1 | Faster rewrites, must work by phase end | — Pending |
| Local LM Studio only for dev | Simplifies testing, multi-provider in Phase 4 | — Pending |
| Synthetic HDF5 test files | No real scientific data available for testing | — Pending |
| Visualization = matplotlib file output | CLI agent, charts as PNG/SVG files to disk | — Pending |
| Cost comparison is aspirational | Focus on working pipeline, not proving savings | — Pending |
| Replace mcp_connector.py entirely | 789 lines of over-engineered async/sync bridge, native DSPy/FastMCP is better | — Pending |

---
*Last updated: 2026-02-10 after initialization*
