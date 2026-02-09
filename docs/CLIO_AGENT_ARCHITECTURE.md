# CLIO Agent Architecture

Self-improving autonomous agent for scientific data management. Intelligence Layer (CEI) of IOWarp.

**Version**: 5.0 | **Updated**: 2026-02-09

---

## System Overview

```
User (CLI / REST API)
    |
    v
CLIO Main Agent (Tier 1)
    |-- ARC Memory (LRU cache + B-tree index + LSM tree)
    |-- Agent Registry (capability-based routing)
    |
    v
Expert Agents (Tier 2)
    |-- DataExpert (HDF5, Parquet, ADIOS)
    |-- HPCExpert (SLURM, Darshan) [Phase 2]
    |
    v
MCP Tool Servers (via FastMCP Gateway)
    |-- /hdf5 (list_datasets, analyze_dataset, optimize_chunking)
    |-- /parquet (analyze_schema, query_data) [Phase 2]
    |-- /slurm (submit_job, check_status) [Phase 2]
    |
    v
IOWarp CTE (persistent storage for ARC) [Phase 5]
```

---

## 3-Tier Agent Hierarchy

### Tier 1: Main Agent (Orchestrator)

The main ClioAgent module. Responsibilities:
- Parse user queries and extract intent
- Route to appropriate expert via Agent Registry
- Manage conversation context from ARC
- Assemble final responses

**Implementation**: `dspy.ReAct` module with expert-calling tools. Uses `dspy.ChatAdapter` for LM Studio compatibility.

```python
# Main agent wraps experts as callable tools
class ClioAgent(dspy.Module):
    def __init__(self):
        self.agent = dspy.ReAct(
            MainAgentSignature,
            tools=[ask_data_expert, ask_hpc_expert]
        )
```

### Tier 2: Expert Agents (Persistent Specialists)

Domain-specific agents with dedicated MCP tools and system prompts.

| Expert | Domain | Tools | Status |
|--------|--------|-------|--------|
| DataExpert | HDF5, Parquet, ADIOS | hdf5_*, parquet_* | Working (CoT mode) |
| HPCExpert | SLURM, Darshan | slurm_*, darshan_* | Phase 2 |

**Implementation**: `dspy.ReAct` module with MCP tools bridged via `dspy.Tool.from_mcp_tool()`.

Each expert has:
- 500+ word domain-specific system prompt in its signature docstring
- 5-7 curated tools (not every atomic operation)
- ARC integration for context retrieval and metric storage

### Tier 3: Parallel Sub-Tasks (Phase 6)

For parameter sweeps, compression testing, batch analysis. Uses `dspy.Parallel` instead of custom nanoagent pool.

---

## MCP Tool Layer (FastMCP 3.x)

### Gateway Pattern

All MCP servers are composed into a single gateway using FastMCP `mount()`:

```python
from fastmcp import FastMCP

gateway = FastMCP("clio-gateway")
gateway.mount("/hdf5", hdf5_server)      # Tools: hdf5_list_datasets, hdf5_analyze_dataset, ...
gateway.mount("/parquet", parquet_server)  # Tools: parquet_analyze_schema, ...
gateway.mount("/slurm", slurm_server)     # Tools: slurm_submit_job, ...
```

Tools are automatically namespaced. Experts connect to the gateway, not individual servers.

### DSPy Tool Bridge

MCP tools are bridged to DSPy ReAct agents natively:

```python
from fastmcp import Client
import dspy

async with Client(gateway) as client:
    mcp_tools = await client.list_tools()
    dspy_tools = [dspy.Tool.from_mcp_tool(t) for t in mcp_tools]
    expert = dspy.ReAct(DataExpertSignature, tools=dspy_tools)
```

This replaces the 789-line custom `mcp_connector.py` with ~5 lines of native code.

### Tool Design Principles

From industry research (OpenAI, Anthropic, Google ADK):

1. **Max 5-7 tools per expert** - Too many tools confuse the LM. Curate, don't enumerate.
2. **Composite over atomic** - `analyze_file` (does everything) instead of separate `get_shape`, `get_dtype`, `get_compression`, etc.
3. **Agent story documentation** - Each tool documents when/why an agent would use it.
4. **Dynamic discovery** - Gateway exposes `list_capabilities` tool. Main agent lazy-loads tool schemas, reducing context from ~47K tokens to ~400 tokens.

### In-Memory Testing

MCP servers are tested without subprocess or network:

```python
from fastmcp import Client

async def test_hdf5():
    async with Client(hdf5_server) as client:
        result = await client.call_tool("list_datasets", {"filepath": "test.h5"})
        assert len(result) > 0
```

---

## ARC Memory Layer

### Architecture

```
┌─────────────────────────────────────────────┐
│  In-Memory (Hot)                            │
│  LRU Cache: O(1) access, 1000 items        │
│  Active conversations, recent tool results  │
├─────────────────────────────────────────────┤
│  Index Layer                                │
│  B-Tree: O(log N) search on session_id,     │
│          timestamp, agent_id                │
│  LSM Tree: write-optimized metrics logging  │
├─────────────────────────────────────────────┤
│  Persistent (Cold)                          │
│  Local filesystem (current)                 │
│  IOWarp CTE namespace (Phase 5)             │
└─────────────────────────────────────────────┘
```

### What ARC Stores

| Namespace | Data | Access Pattern |
|-----------|------|----------------|
| `/conversations/<session_id>/` | Messages, routing decisions | O(log N) by session |
| `/invocations/<trace_id>/` | Expert execution traces, tool calls | O(log N) by agent+time |
| `/metrics/<agent_id>/` | Success rate, latency, cache hits | O(1) pre-computed |
| `/context/<domain>/` | Learned patterns, cached tool results | O(1) cache, O(log N) search |

### Three Memory Types

Based on research (Anthropic agent guide, Google ADK):

1. **Episodic Memory**: What happened in this session (conversations, tool calls)
2. **Semantic Memory**: Domain knowledge (learned patterns, retrieved docs)
3. **Procedural Memory**: What worked and what failed (optimization history, strategy rankings) - *added Phase 2*

### Context Compilation Pipeline (Phase 2)

Context is **compiled, not concatenated**. Raw ARC data goes through:

```
filter -> compact -> enrich -> assemble

1. Filter: Select relevant conversations, patterns for this query
2. Compact: Summarize long histories into key points
3. Enrich: Add procedural memory (what worked before for similar queries)
4. Assemble: Format into context budget (T1: 2K tokens, T2: 4K tokens)
```

This replaces the current pattern of dumping raw history into the prompt.

---

## Agent Registry

### Current Implementation

Keyword-based capability matching:

```python
registry.register_agent("data", data_expert, AgentCapability(
    keywords=["hdf5", "parquet", "compression", "chunking"],
    description="Data I/O optimization expert",
    tools=["analyze_hdf5", "optimize_chunks"],
    specialization="data_io"
))
```

### Target Implementation (Phase 2)

DSPy-optimizable typed routing:

```python
class RoutingSignature(dspy.Signature):
    """Route user query to the most appropriate expert."""
    question: str = dspy.InputField()
    selected_expert: Literal["data", "hpc", "none"] = dspy.OutputField()

router = dspy.ChainOfThought(RoutingSignature)
# Routing decisions stored in ARC for SIMBA optimization
```

Typed `Literal` outputs are optimizable by DSPy's SIMBA optimizer (Phase 3).

---

## Model-Role Fit

Different tiers use different models via `dspy.context(lm=...)`:

| Tier | Role | Model Class | Temperature | Why |
|------|------|------------|-------------|-----|
| T1 | Main/Router | Fast SLM | 0.3 | Quick routing decisions |
| T2 | Expert/Reasoner | Capable model | 1.0 | Creative problem-solving |
| T3 | Sub-tasks | Smallest available | 0.5 | Focused parameter work |

```python
# Per-request model selection (no global mutation)
with dspy.context(lm=routing_model):
    routing = router(question=query)

with dspy.context(lm=expert_model):
    result = expert(question=query)
```

Default: LM Studio at `http://127.0.0.1:1234` with auto-detected model.

---

## Optimizer Layer (Phase 3)

### Self-Improvement Architecture

```
ARC Metrics (invocations, success/failure, latency)
    |
    v
Training Data Generator (extract from ARC history)
    |
    v
SIMBA Optimizer (DSPy 3.x, designed for agentic tasks)
    |
    v
Optimized Variant (improved prompts, few-shot examples)
    |
    v
Variant Manager (store in ARC, A/B test, rollback)
```

### What Gets Optimized

1. **Expert prompts**: Signature docstrings, few-shot examples, reasoning chains
2. **Routing decisions**: Which expert for which query type
3. **Tool selection**: When to call which tool, parameter defaults

### Safety Gates

- Minimum 50 training examples before optimization
- Statistical significance test (p < 0.05) before deployment
- Rollback capability for every deployed variant
- Gradual rollout: 10% -> 50% -> 100% traffic

---

## IOWarp Integration (Phase 5)

CLIO Agent is the Intelligence Layer (CEI) in IOWarp's 3-tier architecture:

| IOWarp Layer | CLIO Component | Function |
|--------------|----------------|----------|
| CEI (Context Exploration) | Main Agent + Experts + ARC | Intelligence |
| CAE/PPI (Content Assimilation) | MCP Tool Servers | Tool execution |
| CTE (Context Transfer Engine) | ARC Persistent Storage | Multi-tier storage |

### ARC-CTE Integration

```python
# Phase 5: ARC persists to IOWarp CTE
arc_storage = IOWarpCTEBackend(
    namespace="/clio_agent/arc",
    tier_policy={
        "hot": {"storage": "nvme", "age_threshold": "1h"},
        "warm": {"storage": "pfs", "age_threshold": "24h"},
        "cold": {"storage": "object_store", "age_threshold": "30d"},
    }
)
```

Currently falls back to local filesystem when IOWarp is unavailable.

---

## Error Handling

**Graceful Degradation Chain**:

```
IOWarp CTE unavailable -> ARC uses local filesystem
ARC unavailable -> Agent works without memory (warns user)
MCP server down -> Expert uses pure reasoning (no tool calls)
LM timeout -> Retry once, then return partial answer
Optimizer fails -> Keep current variant (no changes)
```

No single failure should crash the system.

---

## Data Flow

```
1. User query arrives (CLI or API)
2. Main agent loads compiled context from ARC
3. Registry routes to best expert (typed routing)
4. Expert calls MCP tools via gateway (cache-first)
5. Expert generates answer using ReAct reasoning
6. Invocation metrics stored in ARC (LSM tree)
7. Conversation stored in ARC (B-tree indexed)
8. Answer returned to user
9. [Background] Optimizer checks if metrics degraded
```

---

## Related Documentation

- [PLAN.md](../PLAN.md) - Implementation phases and tasks
- [CLAUDE.md](../CLAUDE.md) - Development rules and patterns
- [ARC_MEMORY_LAYER.md](ARC_MEMORY_LAYER.md) - Memory architecture deep dive
- [MCP_TOOL_INTEGRATION.md](MCP_TOOL_INTEGRATION.md) - Tool server patterns
- [SYSTEM_IDENTITY.md](SYSTEM_IDENTITY.md) - CLIO Agent identity and behavior
- [EXPERT_SYSTEM_DESIGN.md](EXPERT_SYSTEM_DESIGN.md) - Expert agent patterns
- [SELF_IMPROVEMENT.md](SELF_IMPROVEMENT.md) - Optimizer Layer details
