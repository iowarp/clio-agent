# Architecture

**Analysis Date:** 2026-02-09

## Pattern Overview

**Overall:** 3-Tier Hierarchical Agent System with ARC Memory Layer

**Key Characteristics:**
- Tier 1 (Main Agent) → Tier 2 (Domain Experts) → Tier 3 (Nanoagents via DSPy Parallel)
- ReAct pattern (Reasoning + Acting) with tool-augmented expert chains
- DSPy 3.x internal engine with FastMCP 3.x tool servers
- ARC Memory (Adaptive Retrieval Cache) for context, invocations, conversations, metrics
- Capability-based routing with typed outputs (no keyword-match routing)

## Layers

**Tier 1 - Main Agent:**
- Purpose: Entry point for user queries, orchestrates expert delegation
- Location: `src/clio_agent/agent.py` (ClioAgent class)
- Contains: ReAct-based query processor, context compilation, result assembly
- Depends on: ARC Memory, DataExpert, Registry
- Used by: CLI (`src/clio_agent/ui/cli.py`), REST API (`src/clio_agent/ui/api.py`)

**Tier 2 - Domain Experts:**
- Purpose: Specialized reasoning and tool execution for specific domains
- Location: `src/clio_agent/experts/`
  - `data_expert.py` (DataExpert - HDF5/ADIOS/Parquet optimization, working)
  - `hpc_expert.py` (HPCExpert - stub)
  - `research_expert.py` (ResearchExpert - stub)
  - `workflow_expert.py` (WorkflowExpert - stub)
- Contains: DSPy ChainOfThought or ReAct modules with MCP tool functions
- Depends on: Signatures, MCP tools, ARC Memory
- Used by: Main agent as tool functions

**Tier 3 - Nanoagents:**
- Purpose: Lightweight, single-task agents spawned by experts for parallelization
- Location: `src/clio_agent/nanoagents/`
  - `spawner.py` (NanoagentSpawner - stub)
  - `pool.py` (NanoagentPool - stub)
  - `templates/` (nanoagent templates - stubs)
- Contains: dspy.Parallel composition of simple agents
- Depends on: Signatures
- Used by: Experts for parallel sub-tasks (Phase 3)

**ARC Memory Layer:**
- Purpose: Unified storage, caching, and retrieval for all agent data
- Location: `src/clio_agent/arc/`
  - `memory.py` (ARCMemory - main interface)
  - `cache.py` (LRUCache - hot data, O(1) access)
  - `index.py` (BTreeIndex - disk fallback, O(log N) retrieval)
  - `lsm.py` (LSMTree - high-throughput metrics)
  - `retrieval.py` (ContextRetriever - relevance scoring)
  - `schema.py` (msgspec schemas - serialization)
  - `storage.py` (IOWarp CTE backend, local FS fallback)
- Contains: Thread-safe multi-layer storage with cache statistics
- Depends on: sortedcontainers, lru-dict, msgspec
- Used by: All agents, main agent for context retrieval

**Tools Layer:**
- Purpose: MCP servers exposing domain-specific tools
- Location: `src/clio_agent/tools/`
  - `mcp_connector.py` (IOWarpMCPTools - MCP client, legacy)
  - `mcp_wrapper.py` (wrapper utilities)
  - `servers/` (FastMCP server implementations)
    - `hdf5_server.py` (analyze_hdf5, optimize_chunks)
    - `adios_server.py` (ADIOS tools)
    - `parquet_server.py` (Parquet tools)
    - `slurm_server.py` (Job submission)
    - `darshan_server.py` (I/O trace analysis)
- Contains: FastMCP servers with @mcp.tool() decorators
- Depends on: fastmcp>=3.0.0
- Used by: Experts via dspy.Tool.from_mcp_tool()

**Registry Layer:**
- Purpose: Agent discovery, capability management, routing validation
- Location: `src/clio_agent/registry/`
  - `registry.py` (AgentRegistry - thread-safe registration)
  - `capability_matcher.py` (future: semantic matching)
  - `a2a_adapter.py` (future: external agent adapters)
  - `external_compiler.py` (future: LangChain/CrewAI bridges)
- Contains: Agent metadata, keyword→agent mapping, capability validation
- Depends on: None (dataclasses only)
- Used by: Main agent for expert discovery

**UI/Integration Layer:**
- Purpose: User interfaces and system integrations
- Location: `src/clio_agent/ui/`
  - `cli.py` (Rich-based interactive CLI)
  - `api.py` (REST API endpoints - stub)
  - `a2a_endpoint.py` (Agent-to-agent protocol - stub)
  - `tuning_ui.py` (Optimizer tuning interface - stub)
- Contains: Input handling, output formatting, HTTP servers
- Depends on: rich, dspy
- Used by: End users, external systems

## Data Flow

**User Query → Answer:**

1. **Entry**: User sends query via CLI (`src/clio_agent/ui/cli.py`) or API
2. **Retrieval**: Main agent retrieves context from ARC (ContextRetriever)
   - Loads conversation history, learned patterns, relevant invocations
   - Compiles context (filter → compact → enrich → assemble)
   - Returns Context object with max 5 learned patterns
3. **Reasoning**: ClioAgent ReAct agent processes question
   - Chains together with session_context from ARC
   - Decides which expert tool to call (or no tool)
   - Calls expert tools as dspy.Tool functions
4. **Expert Execution**: DataExpert (or other expert) executes tool
   - Expert checks ARC cache for previous results (1-hour TTL)
   - Calls MCP tool if not cached
   - Returns analysis + recommendations
5. **Storage**: Results stored in ARC (LSM + index)
   - Invocation record: query, duration, success, tools used
   - Conversation record: user message + assistant response
   - Metrics: written to LSM tree for analytics
6. **Response**: ClioAgent returns to user
   - Includes answer, trajectory (tool calls), statistics (hit rate, duration)

**State Management:**
- **Hot Path** (recent calls): LRUCache in ARCMemory (O(1))
- **Warm Path** (historical): BTreeIndex on disk (O(log N))
- **Metrics**: LSMTree for time-series (flushable)
- **Fallback**: Local filesystem if IOWarp unavailable

## Key Abstractions

**Signature (dspy.Signature):**
- Purpose: Declarative I/O contracts for agent reasoning
- Examples: `src/clio_agent/signatures/main_agent_sig.py`, `src/clio_agent/signatures/expert_sig.py`
- Pattern: Input/output fields with descriptions; DSPy optimizes via SIMBA

**Module (dspy.Module):**
- Purpose: Composable agent logic with forward() method
- Examples: `ClioAgent`, `DataExpert`
- Pattern: Inherit from dspy.Module; compose signatures with ChainOfThought/ReAct

**Tool Function:**
- Purpose: Bridge between DSPy and MCP tools
- Examples: `ask_data_expert()` in `src/clio_agent/agent.py` (module-level tool)
- Pattern: Use dspy.Tool.from_mcp_tool() to wrap MCP tools; ReAct introspects via function signature

**Memory Records:**
- Purpose: Typed data storage in ARC
- Examples: Conversation, Invocation, Metrics (in `src/clio_agent/arc/schema.py`)
- Pattern: msgspec-serializable dataclasses for efficient disk I/O

## Entry Points

**CLI Entry Point:**
- Location: `src/clio_agent/ui/cli.py` (`run_cli()` function)
- Triggers: Direct user execution (`uv run src/clio_agent/ui/cli.py`)
- Responsibilities:
  - Initialize ClioAgent
  - Loop on user input with Rich prompts
  - Format and display responses with syntax highlighting
  - Commands: `/experts` (list), `/history` (show), `/clear` (reset), `/quit` (exit)

**Agent Entry Point:**
- Location: `src/clio_agent/agent.py` (`ClioAgent.forward()`)
- Triggers: Programmatic calls from UI or external systems
- Responsibilities:
  1. Retrieve context from ARC (ContextRetriever.retrieve_context_for_query)
  2. Execute ReAct agent with session_context
  3. Store invocation metrics (LSMTree.write)
  4. Store conversation (ARCMemory.store_conversation)
  5. Return Prediction with answer, trajectory, statistics

**Expert Entry Point:**
- Location: `src/clio_agent/experts/data_expert.py` (`DataExpert.forward()`)
- Triggers: Called by Main Agent via ask_data_expert() tool function
- Responsibilities:
  - Parse question and file_context
  - Check ARC cache for previous analysis (1-hour TTL)
  - Call MCP tools (analyze_hdf5, optimize_chunks) if not cached
  - Format analysis + recommendations
  - Store tool results in ARC

## Error Handling

**Strategy:** Graceful degradation with fallbacks

**Patterns:**
- **Missing Context**: "No prior context" fallback (ContextRetriever)
- **Tool Failure**: Expert returns error message; Main agent continues
- **ARC Unavailable**: Continue without memory; warn user (agent.py line 337-338)
- **MCP Server Down**: Pure reasoning mode (no tool calls)
- **LM Timeout**: Retry once, then return partial answer
- **Optimizer Fails**: Keep current variant (no rollback)

## Cross-Cutting Concerns

**Logging:**
- Strategy: LSMTree for structured metrics (not text logs)
- Where: agent.py calls `lsm.write()` after each invocation
- What: query, duration_ms, success, error, tool_count

**Validation:**
- Strategy: Registry.register_agent() validates agent_id and capabilities
- Where: `src/clio_agent/registry/registry.py` (lines 125-133)
- What: Non-null agent_id, non-duplicate registration

**Authentication:**
- Strategy: Not implemented (Phase 4)
- Placeholder: `src/clio_agent/ui/api.py` (stub)

**Caching:**
- Strategy: 3-layer caching in ARC
  - L1: LRUCache (hot, 1000 entries)
  - L2: BTreeIndex (warm, disk-backed)
  - L3: LSMTree (metrics, high-throughput)
- TTL: 1 hour for tool results (configurable in expert)

---

*Architecture analysis: 2026-02-09*
