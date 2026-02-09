# Codebase Structure

**Analysis Date:** 2026-02-09

## Directory Layout

```
clio-agent/                    # Repository root
├── src/clio_agent/            # Main source code
│   ├── agent.py               # ClioAgent main Tier 1 agent (ReAct pattern)
│   ├── config.py              # LM Studio configuration + model selection
│   ├── conversation_manager.py # Conversation orchestration (partial)
│   │
│   ├── signatures/            # DSPy signature definitions
│   │   ├── main_agent_sig.py  # Main agent routing signature
│   │   └── expert_sig.py      # Expert reasoning signature
│   │
│   ├── experts/               # Tier 2 domain experts
│   │   ├── data_expert.py     # DataExpert: HDF5/ADIOS/Parquet (working)
│   │   ├── hpc_expert.py      # HPCExpert: SLURM/compute (stub)
│   │   ├── research_expert.py # ResearchExpert: domain-specific (stub)
│   │   └── workflow_expert.py # WorkflowExpert: DAG orchestration (stub)
│   │
│   ├── nanoagents/            # Tier 3 lightweight agents
│   │   ├── spawner.py         # NanoagentSpawner: lifecycle (stub)
│   │   ├── pool.py            # NanoagentPool: concurrent execution (stub)
│   │   └── templates/         # Nanoagent templates
│   │       ├── compression_tester.py
│   │       ├── hdf5_chunk_analyzer.py
│   │       └── parameter_validator.py
│   │
│   ├── arc/                   # Adaptive Retrieval Cache memory layer
│   │   ├── memory.py          # ARCMemory: main interface (90% complete)
│   │   ├── cache.py           # LRUCache: hot data layer
│   │   ├── index.py           # BTreeIndex: disk retrieval layer
│   │   ├── lsm.py             # LSMTree: metrics storage
│   │   ├── retrieval.py       # ContextRetriever: relevance scoring
│   │   ├── schema.py          # msgspec dataclass schemas
│   │   ├── storage.py         # IOWarp CTE backend (fallback: local FS)
│   │   └── coordinator.py     # Multi-agent coordination (stub)
│   │
│   ├── registry/              # Agent discovery and routing
│   │   ├── registry.py        # AgentRegistry: thread-safe registration
│   │   ├── capability_matcher.py # (future) semantic matching
│   │   ├── a2a_adapter.py     # (future) external agent adapters
│   │   └── external_compiler.py # (future) LangChain/CrewAI bridges
│   │
│   ├── tools/                 # MCP tool servers and connectors
│   │   ├── mcp_connector.py   # IOWarpMCPTools: MCP client (legacy)
│   │   ├── mcp_wrapper.py     # Tool wrapper utilities
│   │   └── servers/           # FastMCP server implementations
│   │       ├── hdf5_server.py
│   │       ├── adios_server.py
│   │       ├── parquet_server.py
│   │       ├── slurm_server.py
│   │       └── darshan_server.py
│   │
│   ├── optimizers/            # DSPy optimizer implementations (stubs)
│   │   ├── base.py            # BaseOptimizer abstract class
│   │   ├── prompt_opt.py      # System prompt optimization
│   │   ├── routing_opt.py     # Routing decision optimization
│   │   ├── tool_opt.py        # Tool selection optimization
│   │   ├── metrics.py         # Optimization metrics
│   │   ├── evaluator.py       # Metric evaluation
│   │   ├── deployer.py        # Variant deployment
│   │   ├── online_learning.py # Online learning loop
│   │   └── community/         # Domain-specific optimizers (stubs)
│   │       ├── bioinformatics.py
│   │       ├── chemistry.py
│   │       └── climate.py
│   │
│   └── ui/                    # User interfaces
│       ├── cli.py             # Interactive Rich TUI (working)
│       ├── api.py             # REST API endpoints (stub)
│       ├── a2a_endpoint.py    # Agent-to-agent protocol (stub)
│       └── tuning_ui.py       # Optimizer tuning interface (stub)
│
├── tests/                     # Test suite (35% coverage)
│   ├── test_core/             # Core agent tests
│   │   ├── test_agent.py      # ClioAgent tests
│   │   └── test_config.py     # Configuration tests
│   ├── test_experts/          # Expert tests
│   │   └── test_data_expert.py
│   ├── test_arc/              # Memory layer tests
│   │   └── test_lsm.py
│   ├── test_tools/            # Tool tests
│   ├── test_integration/      # Integration tests
│   └── __init__.py
│
├── docs/                      # Architecture documentation
│   ├── CLIO_AGENT_ARCHITECTURE.md # System architecture (Feb 2026)
│   ├── MCP_TOOL_INTEGRATION.md    # FastMCP 3.x integration guide
│   ├── ARC_MEMORY_LAYER.md        # Memory subsystem design
│   └── ...
│
├── .planning/                 # GSD planning documents
│   ├── codebase/              # This directory (arch analysis)
│   │   ├── ARCHITECTURE.md    # (you are here)
│   │   └── STRUCTURE.md
│   └── SESSION_PROMPTS.md
│
├── pyproject.toml             # Project metadata, dependencies (Python >=3.12)
├── uv.lock                    # Locked dependency versions
├── README.md                  # Project overview
├── PLAN.md                    # Implementation phase checklist
├── CLAUDE.md                  # Development rules for AI agents
├── CLIO_VISION.md             # Research insights and aligned phases
└── .gitignore
```

## Directory Purposes

**src/clio_agent/:**
- Purpose: All Python source code for CLIO Agent
- Contains: 59 files, ~8,244 lines
- Entry point: `agent.py` (ClioAgent class) and `ui/cli.py` (CLI)

**src/clio_agent/signatures/:**
- Purpose: DSPy signature definitions (I/O contracts)
- Contains: Input/output field definitions for agent reasoning
- Pattern: Inherit from dspy.Signature, define fields with .InputField()/.OutputField()

**src/clio_agent/experts/:**
- Purpose: Tier 2 domain-specific agents
- Working: data_expert.py (HDF5/ADIOS/Parquet)
- Stubs: hpc_expert.py, research_expert.py, workflow_expert.py
- Pattern: Inherit from dspy.Module; use ChainOfThought or ReAct

**src/clio_agent/arc/:**
- Purpose: Unified memory layer (90% complete)
- Contains: Cache, index, LSM tree, schemas, storage
- Key file: memory.py (ARCMemory - main interface)
- Usage: All agents use arc.store_invocation(), arc.get_conversation(), etc.

**src/clio_agent/tools/:**
- Purpose: MCP tool servers and client utilities
- Status: mcp_connector.py (legacy), servers/*.py (working)
- Note: Will be replaced with FastMCP 3.x mount() gateway in Phase 1

**src/clio_agent/registry/:**
- Purpose: Agent discovery and capability routing
- Main file: registry.py (AgentRegistry)
- Usage: Main agent uses registry to validate/discover experts

**src/clio_agent/optimizers/:**
- Purpose: DSPy optimizer implementations (Phase 3+)
- Status: All stubs (base.py framework only)
- Plan: SIMBA optimizer, metrics evaluation, online learning

**src/clio_agent/ui/:**
- Purpose: User interfaces (CLI, REST API, A2A, tuning)
- Working: cli.py (Rich-based interactive interface)
- Stubs: api.py (REST), a2a_endpoint.py (agent protocol), tuning_ui.py

**tests/:**
- Purpose: Unit, integration, and MCP server tests
- Coverage: 35% (target 80% by Phase 4)
- Pattern: pytest with unittest.mock for mocking LM calls

**docs/:**
- Purpose: Architecture and integration documentation
- Key files:
  - CLIO_AGENT_ARCHITECTURE.md (system design)
  - MCP_TOOL_INTEGRATION.md (FastMCP 3.x guide)
  - ARC_MEMORY_LAYER.md (memory subsystem)

## Key File Locations

**Entry Points:**
- `src/clio_agent/ui/cli.py`: Interactive CLI (run with `uv run src/clio_agent/ui/cli.py`)
- `src/clio_agent/agent.py`: Programmatic agent (call ClioAgent().forward(question))
- `src/clio_agent/ui/api.py`: REST API (stub, Phase 4)

**Configuration:**
- `src/clio_agent/config.py`: LM Studio URL, model selection
- `pyproject.toml`: Dependencies (dspy>=3.0.3, fastmcp>=2.13.0)

**Core Logic:**
- `src/clio_agent/agent.py`: ClioAgent.forward() (lines 270-447)
- `src/clio_agent/experts/data_expert.py`: DataExpert.forward() (expert reasoning)
- `src/clio_agent/arc/memory.py`: ARCMemory (context/invocation/metrics storage)
- `src/clio_agent/registry/registry.py`: AgentRegistry (expert discovery)

**Testing:**
- `tests/test_core/test_agent.py`: Agent initialization and registry tests
- `tests/test_experts/test_data_expert.py`: DataExpert tests
- `tests/test_arc/test_lsm.py`: LSM tree tests

**Schemas and Models:**
- `src/clio_agent/arc/schema.py`: msgspec dataclasses (Conversation, Invocation, Metrics)
- `src/clio_agent/signatures/main_agent_sig.py`: Main agent I/O contract
- `src/clio_agent/signatures/expert_sig.py`: Expert I/O contract

## Naming Conventions

**Files:**
- snake_case: `data_expert.py`, `mcp_connector.py`
- Modules: Single-purpose per file
- Tests: `test_<module>.py` or `<module>.py` in tests/

**Directories:**
- Functional domains: `experts/`, `tools/`, `arc/`, `registry/`, `ui/`
- Layer names match architecture: `nanoagents/`, `optimizers/`
- Tests mirror source: `tests/test_experts/` for `src/clio_agent/experts/`

**Functions:**
- camelCase actions: `ask_data_expert()`, `store_conversation()`
- snake_case utilities: `fetch_lm_studio_models()`, `select_models_for_agents()`

**Classes:**
- PascalCase: `ClioAgent`, `DataExpert`, `ARCMemory`, `ContextRetriever`
- Suffixes for patterns: `...Signature` (DSPy), `...Expert` (Tier 2), `...Server` (MCP)

**Variables:**
- snake_case: `session_id`, `arc_memory`, `tool_results`
- Prefixes for private: `_data_expert_instance`, `_cache`, `_lock`

## Where to Add New Code

**New Feature:**
- Core logic: `src/clio_agent/experts/<domain>_expert.py`
- Tests: `tests/test_experts/test_<domain>_expert.py`

**New Expert (Tier 2):**
- Implementation: `src/clio_agent/experts/<expert_name>.py`
  - Inherit from dspy.Module
  - Use ChainOfThought or ReAct pattern
  - Register in ClioAgent.__init__()
- Signature: `src/clio_agent/signatures/<expert_name>_sig.py`
- Tests: `tests/test_experts/test_<expert_name>.py`

**New Tool (MCP Server):**
- Server: `src/clio_agent/tools/servers/<tool_name>_server.py`
  - Implement using FastMCP 3.x (@mcp.tool() decorators)
  - Mount via gateway.mount() in Phase 1
- Tests: `tests/test_tools/test_<tool_name>_server.py`

**New Optimizer (Phase 3+):**
- Implementation: `src/clio_agent/optimizers/<optimizer_name>.py`
- Extend BaseOptimizer (base.py)
- Use SIMBA pattern from DSPy
- Tests: `tests/test_optimizers/test_<optimizer_name>.py`

**Utilities/Helpers:**
- Shared helpers: `src/clio_agent/<module>_utils.py` (if used by multiple modules)
- Module-specific: Keep in module file (e.g., config.py helper functions)

## Special Directories

**src/clio_agent/.clio_agent/ (Runtime):**
- Purpose: Runtime storage for ARC, LSM tree, conversations
- Generated: Yes (created by ARCMemory.__init__())
- Committed: No (in .gitignore)
- Structure:
  ```
  .clio_agent/
  ├── arc/
  │   ├── conversations/   # Conversation JSON files
  │   ├── invocations/     # Invocation records
  │   ├── metrics/         # Historical metrics
  │   ├── context/         # Context compilations
  │   └── lsm/             # LSM tree data
  ```

**docs/ (Documentation):**
- Purpose: Architecture and integration guides
- Committed: Yes
- Key files: CLIO_AGENT_ARCHITECTURE.md, MCP_TOOL_INTEGRATION.md

**.planning/codebase/ (GSD Analysis):**
- Purpose: Codebase analysis for /gsd commands
- Generated: By /gsd:map-codebase
- Committed: Yes (reference for future phases)

**tests/ (Testing):**
- Purpose: Unit, integration, and MCP server tests
- Committed: Yes
- Pattern: pytest with mocks for LM calls

---

*Structure analysis: 2026-02-09*
