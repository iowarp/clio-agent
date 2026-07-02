# CLIO Agent - Next Milestone Vision Document

> Active-spec note (2026-04-23): this document is historical vision context.
> Use [PLAN.md](PLAN.md) as the current source of truth when it conflicts with
> this file. Source code and tests still outrank both documents.

## What This Is

CLIO Agent is an autonomous, self-improving AI agent specialized in scientific data management within the IOWarp HPC ecosystem. It is NOT a framework for building agents -- it IS the agent. It helps scientists optimize HDF5 files, analyze I/O traces, convert data formats, run statistical analysis, and make their scientific computing workflows faster.

This document originally described the milestone that took the early baseline
from a single-expert agent into a multi-expert, tool-using alpha. That milestone
has largely shipped for local HDF5, Parquet, CSV, visualization, CLI/API, ARC,
doctor reporting, and offline optimization support. The active next milestone is
the v0.3 Integration-Ready Harness in `PLAN.md`.

## Current Source-Verified Snapshot (Apr 2026)

### Working Components
- **Main Agent (Tier 1)**: `ClioAgent` routes to Data, Analysis, Visualization, Chat, or out-of-scope handlers, with deterministic local-tool shortcuts for explicit file paths.
- **Experts (Tier 2)**: DataExpert, AnalysisExpert, and VisualizationExpert are wired with curated tool sets.
- **Tools**: real local HDF5 and Parquet FastMCP servers, safe CSV inspection, matplotlib chart generation, and a FastMCP gateway with namespace compatibility.
- **ARC Memory Layer**: conversations, invocations, metrics, dataset profiles, procedural memory, context compilation, cache, index, local storage, and LSM metrics.
- **Runtime Interfaces**: Rich CLI, `doctor` reporting, FastAPI `/health`, `/query`, `/experts`, `/metrics`, and SSE streaming.
- **Optimization**: instrumentation, training-set generation, SIMBA runner, variant management, compare, deploy, and rollback scaffolding.
- **Deployment**: Dockerfile, Docker Compose, Singularity definition, multi-provider LM configuration, and local-first runtime defaults.

### Still Not Product Capabilities
- ADIOS2/BP inspection, Darshan analysis, SLURM/PBS integration, real CTE backend, A2A, nanoagent execution, production auth, online learning, and guarded mutating HPC workflows remain future work.
- `MCPToolBridge` is now a compatibility shim over explicit sync/async MCP execution boundaries; new execution paths should depend on those executor interfaces directly.
- Historical `.planning/` files record the v0.2 milestone and should not override `PLAN.md`.

## Historical State (Feb 2026, Superseded)

### Working Components
- **Main Agent (Tier 1)**: `ClioAgent` class using DSPy `ChainOfThought`, routes to experts, stores context in ARC memory. Works with LM Studio local models. ~665 lines.
- **DataExpert (Tier 2)**: Single expert. Falls back to `ChainOfThought` because ReAct had compatibility issues with older DSPy/LM Studio. ~340 lines.
- **ARC Memory Layer**: LRU cache (TTL), B-tree index (O(log N)), LSM tree (93% coverage, background compaction). msgspec schemas (630+ lines). ~90% complete.
- **Agent Registry**: Thread-safe, keyword-based capability matching. 1 expert registered.
- **CLI**: Rich TUI with `/help`, `/history`, `/experts`, `/memory`, `/registry` commands.
- **Config**: LM Studio model auto-detection, Granite preference, multi-model selection.
- **Tests**: 25 passing, 35% coverage. LSM tree at 93%.

### What Exists But Doesn't Work
- **MCP Connector** (`mcp_connector.py`, 789 lines): Over-engineered async/sync bridge. Replace with native DSPy/FastMCP.
- **MCP servers**: All empty stubs (HDF5, ADIOS, Parquet, SLURM, Darshan).
- **Expert stubs**: HPCExpert, ResearchExpert, WorkflowExpert (empty).
- **Optimizer Layer**: All empty stubs (base, prompt_opt, routing_opt, metrics, etc.).
- **Nanoagents**: All empty stubs (spawner, pool, templates).
- **A2A Protocol**: Empty adapters.
- **REST API**: 5-line stub.

### Codebase Stats
- ~8,244 lines Python, 60+ modules
- Python >= 3.12 (locked)
- Build: UV + Hatchling
- 25 tests passing, no CI/CD

## Technology Capabilities (Verified Feb 2026)

### DSPy 3.x (dspy-ai 3.1.3)

| Feature | Impact on CLIO |
|---------|---------------|
| `dspy.Tool.from_mcp_tool()` | Eliminates 789-line mcp_connector.py |
| `dspy.ChatAdapter` | Makes ReAct work with LM Studio (fixes CoT fallback) |
| `dspy.SIMBA` | Optimizer designed for agentic/long-horizon tasks |
| `dspy.CodeAct` | LLM generates Python code to call tools |
| `dspy.Parallel` | Replaces custom nanoagent pool concept |
| `dspy.context(lm=...)` | Per-request model selection (no global mutation) |
| `dspy.streamify()` | Token streaming from any module |
| `Literal` typed outputs | Routing decisions become optimizable |
| Thread-safe `configure()` | Safe concurrent usage |
| Native async (`acall()`) | No manual async/sync bridges needed |

### Historical FastMCP Reference Snapshot

| Feature | Impact on CLIO |
|---------|---------------|
| `mount()` + namespacing | Gateway pattern for multi-server composition |
| `Client(server)` in-memory | Test MCP servers without subprocess |
| `Depends()` | Hide ARC injection from LLM schema |
| `@lifespan` | Shared state across tool calls |
| Transforms (Namespace, Enabled) | Access control and capability filtering |
| Streamable HTTP transport | Production deployment |
| ASGI integration | Mount in FastAPI apps |
| `as_proxy()` | Unified gateway from config |

### Key Research Insights

From analysis of 19 vault articles + 12 web searches covering OpenAI data agent, Anthropic's agent guide, Google ADK, Cursor's multi-agent, and others:

1. **Context is compiled, not concatenated** - Filter -> compact -> enrich -> assemble. Separate storage from presentation. Budget tokens per tier.
2. **Tool curation over generation** - Max 5-7 tools per expert. Hide atomicity behind composite operations. "Agent story" documentation.
3. **Planner-Worker hierarchy** - T1 = Judge/Router, T2 = Planners + Workers. Avoid equal-status agents.
4. **Memory needs 3 types** - Episodic (what happened), Semantic (domain knowledge), Procedural (what worked/failed). ARC currently only has episodic + semantic.
5. **Specialized system prompts** - Each expert needs 500+ word domain-specific prompt, not generic "helpful assistant."
6. **Dynamic MCP discovery** - Lazy-load tool schemas. Reduce 47K tokens to ~400 tokens context overhead.
7. **Model-role fit** - Different models for different tiers (SLMs for routing, capable models for reasoning).
8. **Spec-first validation** - Store task specs in ARC, validate against spec post-execution, feed into memory.

### Anti-Patterns to Avoid (from research)

- Context pollution (dumping raw history into prompts)
- Flat coordination (equal-status agents instead of hierarchy)
- Auto-generating tools from OpenAPI specs (too many, too granular)
- Monolithic prompts (shared across all agents)
- Ignoring procedural memory (not tracking what worked/failed)
- End-to-end only evaluation (no intermediate metrics)

## Implementation Phases

See [PLAN.md](PLAN.md) for detailed task breakdown. Summary:

| Phase | Goal | Key Deliverables |
|-------|------|-----------------|
| **1. Foundation Reset** | Real tools, correct DSPy patterns | HDF5 MCP server, gateway, ReAct+ChatAdapter, delete mcp_connector.py, clean stubs |
| **2. Data Lifecycle** | Close storage→analytics→viz cycle | AnalysisExpert, VisualizationExpert, Parquet server, context compilation |
| **3. Self-Improvement** | Get better with use | SIMBA optimizer, training data collection, variant management, `--tune` CLI |
| **4. Production** | Deployable | REST API, CI/CD, containers, 80%+ coverage |
| **5. IOWarp** | CTE integration | ARC-CTE backend, tier migration, Darshan+ADIOS servers |
| **6. Advanced** | Future capabilities | Online learning, A2A protocol, additional experts |

## Key Design Principles

1. **DSPy is internal**: Users see CLIO, not DSPy. Exception: `AGENTS.md` and code comments for contributors.
2. **All data through ARC**: Conversations, invocations, metrics, cached tool results -- all in ARC.
3. **Graceful degradation**: IOWarp down -> filesystem. MCP down -> reasoning only. Optimizer fails -> keep current variant.
4. **Cache first**: Check ARC before every tool call. Target >85% hit rate.
5. **Real tools, not mocks**: Every MCP server does real work (reads real HDF5 files, submits real SLURM jobs).
6. **Tool curation**: Max 5-7 tools per expert. Composite operations with agent stories.
7. **Context compilation**: Filter -> compact -> enrich -> assemble. Never concatenate raw history.
8. **Optimizable routing**: Router is a DSPy module with Literal typed outputs -- optimizable by SIMBA.
9. **Test before commit**: Coverage gates per phase (50% -> 60% -> 70% -> 80%).

## Constraints

- Python >= 3.12 (locked)
- Must work with local LM Studio models AND cloud APIs
- CLI is primary interface (REST API is secondary)
- No database ORMs -- ARC handles all persistence
- IOWarp integration is optional (agent works standalone)
- Single-machine deployment for v1

## What Makes CLIO Different

1. **Self-improving**: Gets measurably better with use via DSPy SIMBA optimizer
2. **Science-native**: Built for HDF5, ADIOS, Parquet, SLURM, Darshan -- not a general chatbot
3. **Memory-first**: ARC with O(log N) retrieval + context compilation, not just chat history
4. **Tool-using**: Real MCP servers that read files, submit jobs, analyze traces
5. **Locally deployable**: Works on a workstation with LM Studio, no cloud required
6. **Context-engineered**: Compiled context windows with budget per tier, not raw concatenation
