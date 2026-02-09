# CLIO Agent: Agentic Landscape Research

> Compiled: 2026-02-09 | Sources: 19 vault articles + 12 web searches
> Purpose: Map industry best practices to CLIO Agent architecture

---

## Part 1: Cross-Cutting Patterns

What ALL sources agree on, regardless of framework or vendor.

### 1.1 Layered Context Compilation (Not Concatenation)

Every production agent system that works at scale uses **layered context** - not raw prompt concatenation. The consensus pattern emerges from Anthropic, OpenAI, Google ADK, and Cursor independently.

**OpenAI's 6-Layer Architecture** (from their internal data agent):

| Layer | Content | Refresh Rate |
|-------|---------|--------------|
| 1. Usage | Schema metadata, column types, historical queries | Daily offline |
| 2. Annotations | Domain expert descriptions, semantics, caveats | Manual + versioned |
| 3. Code Enrichment | Table definitions from Spark/Python, auto-freshness | Daily offline |
| 4. Institutional Knowledge | Slack, Docs, Notion - embedded with metadata/permissions | Periodic |
| 5. Memory | Corrections, filters, constraints (global + personal scope) | Agent-triggered |
| 6. Runtime | Live queries to data warehouse, metadata services | Per-request |

> "Highly prescriptive prompting degraded results. We gave the agent high-level guidance and let it reason." - OpenAI Data Agent team

**Token Budget Allocation** (production consensus):

| Component | Budget | Notes |
|-----------|--------|-------|
| System instructions | 10-15% | Persona, constraints, format |
| Tool descriptions | 15-20% | THE critical bottleneck |
| Knowledge context | 30-40% | Dynamic, RAG-retrieved |
| Conversation history | 15-25% | Compacted, not raw |
| Working space | 10-15% | For CoT reasoning |

**Key Insight**: Caching reduces context costs by 75-90%. Model cascading (routing simple queries to cheaper models) saves 60%.

**CLIO Mapping**: Adapt OpenAI's 6-layer for scientific data:
1. Dataset/file usage patterns (HDF5 structures, Parquet schemas)
2. Human annotations (domain expert dataset descriptions)
3. IOWarp connector enrichment (data transformations, pipeline logic)
4. Institutional knowledge (lab protocols, experiment logs, papers)
5. Memory (corrections, domain-specific filters, learned constraints)
6. Runtime (live data warehouse queries, IOWarp metadata service)

### 1.2 Tool Curation Over Generation

> "Auto-generating MCP servers from REST APIs = poisoning agents. Good REST API = generous, 100s of endpoints. Agent drowns in choice." - Jeremiah Lowin, FastMCP creator

Every source agrees: **fewer, better tools beats many tools**.

**The Numbers**:
- 6 MCP servers, 60 tools = ~47,000 tokens of tool descriptions loaded per request
- Dynamic discovery reduces this to ~400 tokens (99% reduction)
- OpenAI found overlapping tool functionality confused their agent
- Anthropic recommends max 5-7 tools per agent context

**The Philosophy Mismatch**:

| Human Developer | AI Agent |
|----------------|----------|
| Rich, composable, atomic parts | Ruthlessly curated, minimalist |
| Discovers via docs/IDE | Pays tokens for EVERY tool EVERY turn |
| Cheap iteration (click around) | Expensive iteration (full reasoning cycle) |
| Context from training/experience | Context ONLY from what's in prompt |

**Right Approach** (consensus):
1. Start with "Agent Story": "Given {context}, I use {tools} to achieve {outcome}"
2. Build ONLY the tools that story requires
3. Use `FastMCP.from_openapi()` for exploration, NOT production
4. Use `Tool.from_tool()` to transform: rename cryptic args, hide params with defaults
5. Self-contained tools: `analyze_hdf5()` NOT `read_hdf5()` + `parse_hdf5()` + `get_hdf5_metadata()`

**CLIO Mapping**: Each expert gets max 5-7 tools. DataExpert: `hdf5_analyze`, `hdf5_optimize`, `parquet_validate`, `adios_convert`, `compression_recommend`. NOT 20+ atomic operations.

### 1.3 Planner-Worker Hierarchy Over Flat Coordination

This is the strongest consensus across ALL sources. Flat agent coordination fails.

**Cursor's Evidence** (hundreds of agents, weeks of runtime):
- **Flat coordination FAILED**: Equal-status agents with shared file + locks. Agents held locks too long, became risk-averse, avoided hard problems. "Twenty agents would slow down to the effective throughput of two or three."
- **Optimistic concurrency FAILED**: Simpler but same deep problems. No agent took responsibility.
- **Planner-Worker SUCCEEDED**: Planners create tasks, Workers execute blindly, Judge validates.

**The Pattern** (confirmed by Anthropic, Google ADK, LangGraph, ROMA):

```
PLANNER (high-capability model)
  ├── Explores codebase/data
  ├── Creates task decomposition
  ├── Can spawn sub-planners (recursive)
  └── Doesn't execute

WORKERS (focused, potentially cheaper model)
  ├── Pick up assigned task
  ├── Execute to completion
  ├── Don't coordinate with other workers
  └── Push results

JUDGE (validates cycle)
  ├── Determines if continue or stop
  └── Next iteration starts fresh
```

**Key Learnings**:
- "Many improvements came from removing complexity, not adding it" (Cursor dropped "integrator" role)
- "The best system is often simpler than you'd expect"
- "Prompts matter more than harness and models"
- Periodic fresh starts combat drift and tunnel vision

**CLIO Mapping**: Tier 1 = Planner (orchestrates), Tier 2 = Expert Workers (execute domain tasks), Tier 3 = Nanoagent sub-workers (parallel sub-tasks). This hierarchy is already correct in CLIO's design.

### 1.4 Memory as Typed Events + Compaction

All production memory systems converge on **typed event stores with compaction**, not raw conversation logs.

**Memory Type Taxonomy** (consensus from Mem0, Zep, LangMem, Memary):

| Type | What It Stores | Access Pattern | CLIO ARC Mapping |
|------|---------------|----------------|------------------|
| Episodic | Specific interactions, analyses, experiments | "What happened last time I analyzed this file?" | `/invocations/<trace_id>/` |
| Semantic | Domain knowledge, schemas, format specs | "What are HDF5 compression best practices?" | `/context/<domain>/` |
| Procedural | Learned workflows, successful sequences | "How did I optimize that 100GB file before?" | `/metrics/<agent_id>/` + optimization history |
| Short-term | Current conversation, working context | Sliding window | LRU cache (hot tier) |

**Platform Comparison**:

| Platform | Architecture | Key Strength | CLIO Relevance |
|----------|-------------|-------------|----------------|
| Mem0 | Hybrid (vector + KG + KV) | +26% accuracy over OpenAI baseline | Best pattern for CLIO's multi-agent setup |
| Zep | Temporal knowledge graph | 90% latency reduction, +18.5% recall | Session memory pattern for conversations |
| LangMem | Summarization-centric | Minimal footprint | Compaction strategy for context windows |
| Memary | Knowledge-graph focus | Cross-agent memory sharing | Pattern for Tier 2 expert collaboration |

**Compaction Strategy** (for long-horizon support):
1. **Summarization**: Compress conversation while preserving hypotheses, intermediate results, architectural decisions
2. **Note-taking**: NOTES.md pattern - observations, TODOs, failed approaches
3. **Sub-agents**: Fresh context windows for specialized tasks
4. **Finer-grained tools**: Break complex tools into focused ones

**CLIO Mapping**: ARC's 3-tier architecture (LRU cache → B-tree index → IOWarp CTE) already implements the hot/warm/cold pattern. Add typed memory categories (episodic/semantic/procedural) to the schema layer.

### 1.5 Spec-First Development with Machine-Readable Validation

Multiple sources emphasize **specs before code, machine-readable validation over human review**.

**Agent OS Pattern**:
- Machine-readable specifications for every task
- Planning phase produces executable spec
- Validation runs automatically against spec
- No ambiguity about "done"

**OpenAI Data Agent Evaluation** (granular, not end-to-end):

| Stage | Metric | Why |
|-------|--------|-----|
| Tool Routing | F1 against oracle router | "Does it route correctly?" |
| Query Construction | Retrieval recall/precision | "Does it ask the right question?" |
| Retrieval | Pre-rerank vs post-rerank separately | "Does it find relevant context?" |
| Generation | Groundedness to sources | "Is the answer faithful?" |

> "End-to-end only evaluation = recipe for disaster. Failures cascade. Only granular visibility enables identifying which component to improve." - OpenAI

**CLIO Mapping**: Build evaluation into every phase:
- v0.2.0: Registry routing accuracy metric
- v0.3.0: Tool call success rate, cache hit rate
- v0.4.0: Optimizer improvement metrics (before/after)
- v0.5.0: Online learning A/B test framework

### 1.6 Model-Role Fit

Every multi-agent production system uses **different models for different roles**.

**Cursor's Finding** (from trillions of tokens):
- GPT-5.2 = better planner (follows instructions, keeps focus, avoids drift)
- GPT-5.1-Codex = better worker (coding-specific, despite being trained for code)
- "Opus 4.5 tends to stop earlier and take shortcuts when convenient"
- "We now use the model best suited for each role rather than one universal model"

**SLM Evidence**:
- 99% of use cases addressable with SLMs (<10B params) - Hugging Face CEO
- 7B model = 5% consumption of larger model
- Domain-specific SLMs OUTPERFORM GPT-4 in narrow tasks:
  - Diabetica-7B: 87.2% vs GPT-4 79.17%
  - Legal SLM (0.2B): outperforms GPT-3.5/GPT-4 on F1
  - H Company Runner H (3B): 67% vs Anthropic Computer Use 52%

**Multi-Model Agent Pattern**:

```python
# CLIO model-role mapping
TIER_1_PLANNER = "claude-sonnet-4-5"  # Best reasoning, routing
TIER_2_EXPERT = "granite-3.3-8b"     # Domain-tuned, local
TIER_3_NANO = "granite-3.3-2b"       # Fast, cheap, disposable
ROUTING = "dspy.context(lm=TIER_1)"  # Per-request override
```

**CLIO Mapping**: Already designed for multi-model via `dspy.context(lm=...)`. Phase 4 should implement model selection per tier. Local models (LM Studio, Ollama) for Tier 2/3, cloud models for Tier 1 planning.

---

## Part 2: Architecture Patterns Mapped to CLIO

### 2.1 Tier 1: Main Agent Patterns

**Current State**: `src/clio_agent/agent.py` - DSPy ReAct with experts as tool functions.

**Pattern: ReAct with Adaptive Tool Selection**

The main agent should NOT have all tools. It should have expert-calling tools only.

```python
# Current (correct pattern)
class ClioAgent(dspy.Module):
    def __init__(self):
        self.agent = dspy.ReAct(
            signature="question -> answer",
            tools=[self.ask_data_expert, self.ask_hpc_expert],
            max_iters=5
        )

# Enhancement: Add context retrieval as a tool
def retrieve_context(self, query: str) -> str:
    """Retrieve relevant context from ARC memory."""
    return self.arc.retrieve_relevant(query, top_k=5)
```

| Pattern | Affects | Phase | Change |
|---------|---------|-------|--------|
| Expert tools only (no raw MCP) | Tier 1 | v0.2.0 | Keep current pattern, don't expose MCP tools to main agent |
| Context retrieval as tool | Tier 1 | v0.2.0 | Add `retrieve_context` tool function |
| Per-request model override | Tier 1 | v0.4.0 | Use `dspy.context(lm=...)` for model selection |
| Adaptive iteration limit | Tier 1 | v0.4.0 | Optimizer tunes `max_iters` based on query complexity |

### 2.2 Tier 2: Expert Agent Patterns

**Current State**: `src/clio_agent/experts/data_expert.py` - DSPy ReAct with mock tools.

**Pattern: Domain-Specialized ReAct with Curated Tools**

Each expert gets max 5-7 tools, domain-specific system prompt, and its own model assignment.

```python
class DataExpert(dspy.Module):
    """Expert with curated tool set and domain context."""

    def __init__(self, mcp_tools: list[dspy.Tool]):
        super().__init__()
        # Max 5-7 tools, curated for this domain
        self.agent = dspy.ReAct(
            signature="task, context -> analysis, recommendations, commands",
            tools=mcp_tools[:7],  # Hard cap
            max_iters=5
        )

    def forward(self, task: str, context: str = "") -> dspy.Prediction:
        # Check ARC cache first
        cached = self.arc.get_cached_tool_result(task)
        if cached:
            return cached

        with dspy.context(lm=TIER_2_MODEL):  # Model-role fit
            result = self.agent(task=task, context=context)

        # Cache result
        self.arc.cache_tool_result(task, result)
        return result
```

| Pattern | Affects | Phase | Change |
|---------|---------|-------|--------|
| 5-7 tool cap per expert | Tier 2 | v0.3.0 | Enforce in expert constructors |
| Cache-first execution | Tier 2 | v0.3.0 | ARC cache check before every MCP call |
| Model-role fit | Tier 2 | v0.4.0 | `dspy.context(lm=...)` per expert |
| Domain system prompt | Tier 2 | v0.3.0 | Specialized signatures per expert |

### 2.3 Tier 3: Nanoagent Patterns

**Pattern: Single-File Agent (SFA) + dspy.Parallel**

Inspired by the SFA pattern and Cursor's worker agents:

```python
class NanoagentSpawner:
    """Spawn ephemeral, single-purpose nanoagents."""

    def spawn(self, template: str, task: str, model: str = TIER_3_MODEL):
        """Spawn a nanoagent from template."""
        nano = NanoagentTemplates[template]  # Pre-defined, minimal

        with dspy.context(lm=model):
            result = nano(task=task)

        self.arc.store_invocation(trace_id=uuid4(), result=result)
        return result

    def spawn_parallel(self, tasks: list[tuple[str, str]]):
        """Spawn multiple nanoagents concurrently."""
        nanos = [NanoagentTemplates[t](task=q) for t, q in tasks]
        return dspy.Parallel(nanos)  # Concurrent execution
```

Nanoagent templates (single-purpose, minimal):
- `hdf5_chunk_analyzer` - Analyze chunk layout and recommend sizes
- `compression_tester` - Test compression ratios across algorithms
- `parameter_validator` - Validate configuration parameters
- `metadata_extractor` - Extract and summarize file metadata

| Pattern | Affects | Phase | Change |
|---------|---------|-------|--------|
| Template-based spawning | Tier 3 | v0.4.0 | Implement spawner + templates |
| Parallel execution | Tier 3 | v0.4.0 | Use `dspy.Parallel` |
| SLM model assignment | Tier 3 | v0.4.0 | Local 2-3B models for nanoagents |
| Auto-termination + ARC tracking | Tier 3 | v0.4.0 | Store invocation in ARC, cleanup |

### 2.4 ARC Memory Patterns

**Current State**: `src/clio_agent/arc/memory.py` - LRU cache + B-tree index + LSM tree. 90% complete.

**Enhancement: Typed Memory Categories**

Add episodic/semantic/procedural typing to existing schema:

```python
# Enhancement to arc/schema.py
class MemoryType(Enum):
    EPISODIC = "episodic"    # Specific interactions, analyses
    SEMANTIC = "semantic"     # Domain knowledge, schemas
    PROCEDURAL = "procedural" # Learned workflows, sequences

@dataclass
class MemoryEntry:
    type: MemoryType
    content: str
    scope: str  # "global" | "user:<id>" | "project:<id>"
    created_at: float
    accessed_at: float
    access_count: int
    metadata: dict
```

**Enhancement: Memory Scoping** (inspired by OpenAI Data Agent):

| Scope | What | Example |
|-------|------|---------|
| Global | Dataset-specific knowledge | "HDF5 files from instrument X always use gzip-4" |
| User | Personal preferences | "User prefers chunk size 1MB for this workload" |
| Project | Experiment-specific | "This experiment uses custom coordinate system" |

| Pattern | Affects | Phase | Change |
|---------|---------|-------|--------|
| Typed memory categories | ARC | v0.2.0 | Add `MemoryType` enum to schema |
| Memory scoping | ARC | v0.3.0 | Add scope field to entries |
| Temporal knowledge graph | ARC | v0.5.0+ | Link analyses→experiments→datasets→papers |
| Compaction strategy | ARC | v0.3.0 | Summarize old conversations, preserve key facts |

### 2.5 Registry & Routing Patterns

**Current State**: `src/clio_agent/registry/registry.py` - Thread-safe, keyword-based matching.

**Enhancement: Capability Card Pattern** (inspired by A2A Agent Cards):

```python
@dataclass
class AgentCard:
    """A2A-compatible capability manifest."""
    name: str
    version: str
    description: str
    capabilities: list[str]
    tools: list[str]
    input_schema: dict      # What this agent accepts
    output_schema: dict     # What it returns
    model_preference: str   # Preferred model for this agent
    cost_tier: str          # "low" | "medium" | "high"
    latency_class: str      # "fast" | "medium" | "slow"
```

**Agent Registry Approaches (2025-2026 landscape)**:

| Approach | Mechanism | CLIO Relevance |
|----------|-----------|----------------|
| MCP Registry | Centralized mcp.json descriptors | Internal tool discovery |
| A2A Agent Cards | Decentralized JSON capability manifests | External agent integration |
| AGNTCY Directory | IPFS Kademlia DHT for semantic discovery | Future distributed CLIO |
| NANDA AgentFacts | Cryptographically verifiable registries | Enterprise deployment |

| Pattern | Affects | Phase | Change |
|---------|---------|-------|--------|
| Agent Cards with schemas | Registry | v0.2.0 | Add `AgentCard` dataclass |
| DSPy-optimizable routing | Registry | v0.4.0 | Make router a `dspy.ChainOfThought` module |
| A2A protocol compliance | Registry | v0.5.0 | Implement A2A Agent Card spec |
| Dynamic tool discovery | Registry | v0.3.0 | Lazy-load MCP tool schemas |

### 2.6 MCP Tool Server Patterns

**Current State**: Stubs only. Over-engineered `mcp_connector.py` (789 lines) to be replaced.

**Pattern: Curated FastMCP Servers with ARC Caching**

```python
from fastmcp import FastMCP

# One server per domain, minimal tools
hdf5_server = FastMCP("clio-hdf5")

@hdf5_server.tool
def analyze_hdf5(file_path: str) -> dict:
    """Analyze HDF5 file structure, compression, and chunk layout.

    Returns dataset inventory with sizes, dtypes, compression ratios,
    and optimization recommendations.
    """
    import h5py
    with h5py.File(file_path, "r") as f:
        datasets = []
        def visitor(name, obj):
            if isinstance(obj, h5py.Dataset):
                datasets.append({
                    "path": name,
                    "shape": obj.shape,
                    "dtype": str(obj.dtype),
                    "chunks": obj.chunks,
                    "compression": obj.compression,
                    "nbytes": obj.nbytes,
                })
        f.visititems(visitor)
    return {"datasets": datasets, "total_datasets": len(datasets)}

@hdf5_server.tool
def optimize_hdf5(file_path: str, target: str = "balanced") -> dict:
    """Recommend and apply HDF5 optimizations.

    Targets: 'size' (minimize storage), 'speed' (maximize I/O),
    'balanced' (compromise).
    """
    # Analyze current state, recommend chunk sizes + compression
    ...
```

**Server Composition** (mount pattern for multi-server):

```python
from fastmcp import FastMCP

main_server = FastMCP("clio-tools")
main_server.mount("/hdf5", hdf5_server)    # Auto-namespaced
main_server.mount("/parquet", parquet_server)
main_server.mount("/slurm", slurm_server)
```

**Dynamic Discovery** (MCP CLI pattern, 99% token reduction):

```python
# Instead of loading all 60 tool schemas upfront:
# Agent asks: "What servers exist?" → lightweight list
# Agent asks: "What does hdf5/analyze do?" → single tool schema
# Agent calls: hdf5/analyze with params → execute

# Implementation: meta-tool that discovers other tools
@main_server.tool
def list_available_tools(domain: str = "") -> list[str]:
    """List available tool servers and their capabilities."""
    ...
```

| Pattern | Affects | Phase | Change |
|---------|---------|-------|--------|
| One server per domain | MCP | v0.3.0 | Create hdf5_server, parquet_server, slurm_server |
| Mount composition | MCP | v0.3.0 | Use `FastMCP.mount()` for namespacing |
| ARC cache integration | MCP | v0.3.0 | Cache tool results with TTL |
| Dynamic discovery | MCP | v0.3.0 | Meta-tool for lazy schema loading |
| Tool.from_tool() curation | MCP | v0.3.0 | Rename/simplify MCP tool interfaces |

---

## Part 3: Anti-Patterns to Avoid

### 3.1 Context Pollution from Too Many Tools

**The Problem**: Each tool description costs tokens EVERY turn. 60 tools = ~47,000 tokens of overhead.

**Evidence**:
- OpenAI: "We exposed full tool set... overlapping functionality confused the agent"
- FastMCP creator: "Context pollution = silent killer. Toolkits inject 1000s tokens > custom system prompts"
- Agents become "obsessive API librarians" instead of helpful assistants

**CLIO Risk**: Current stubs plan 15+ MCP servers with 150+ tools. Loading all tool schemas into every agent context would consume the entire context window.

**Fix**:
- Max 5-7 tools per expert context
- Dynamic discovery for the rest
- Use `Tool.from_tool()` to hide irrelevant parameters

### 3.2 Flat Agent Coordination

**The Problem**: Equal-status agents self-coordinating leads to risk aversion, lock contention, and work duplication.

**Evidence**:
- Cursor: 20 agents → effective throughput of 2-3 with locks
- Agents avoided hard problems, made safe/small changes
- "No agent took responsibility for hard problems or end-to-end implementation"
- HACN research: flat consensus fails at scale, hybrid hierarchical-decentralized works

**CLIO Risk**: If Tier 2 experts are given equal authority to coordinate, same failure mode.

**Fix**: Strict hierarchy. Tier 1 plans and assigns. Tier 2 executes assigned domain. Tier 3 executes sub-tasks. No peer coordination between experts.

### 3.3 Auto-Generating MCP from OpenAPI

**The Problem**: REST APIs are designed for humans (generous, atomic). Agents need the opposite (curated, composite).

**Evidence**:
- FastMCP from_openapi() = exploration tool only
- GitHub issues: "LLM timed out deciding between create_invoice vs generate_invoice"
- "Hallucinated get_all_users_with_blue_eyes"
- Each atomic call = expensive round trip with full reasoning cycle

**CLIO Risk**: Temptation to auto-wrap IOWarp REST API as MCP tools.

**Fix**: Hand-craft each MCP tool. Start with "agent story", build only required tools. Use `from_openapi()` for exploration, never production.

### 3.4 Monolithic System Prompts

**The Problem**: Dumping everything into one giant system prompt wastes tokens and dilutes important instructions.

**Evidence**:
- "Lost in the middle" - models lose track of information in long contexts
- OpenAI: Layered context outperforms monolithic
- Anthropic: Structured context (persona → instructions → tools → examples → constraints)

**CLIO Risk**: Current `agent.py` builds context string by concatenation.

**Fix**: Implement layered context compilation. Each layer has a token budget. Dynamic selection based on query type.

### 3.5 Ignoring Procedural Memory

**The Problem**: Most agent memory systems store WHAT happened but not HOW to do things.

**Evidence**:
- OpenAI Data Agent: Memory stores corrections, filters, constraints - not just conversation history
- Mem0: Procedural memory = learned workflows, successful sequences
- Without it: agent repeats the same mistakes, can't learn "this HDF5 format always needs gzip-4"

**CLIO Risk**: ARC currently stores conversations and invocations but no explicit procedural memory.

**Fix**: Add procedural memory type to ARC schema. Store successful analysis workflows as templates. Learn tool-calling patterns from history.

### 3.6 End-to-End Only Evaluation

**The Problem**: Measuring only final output quality hides which component failed.

**Evidence**:
- OpenAI: "Recipe for disaster. Failures cascade."
- "Only granular visibility enables identifying which component to improve"
- Most enterprises rely on custom-built accuracy checks that lack real-time coverage

**CLIO Risk**: Currently no stage-wise evaluation. Tests cover units but not pipeline stages.

**Fix**: Implement granular metrics at every stage:
- Routing accuracy (did it pick the right expert?)
- Tool selection accuracy (did the expert use the right tool?)
- Tool execution success (did the MCP call work?)
- Answer quality (was the final response correct?)

### 3.7 Perfect Accuracy Obsession

**The Problem**: Pushing from 95% to 99% accuracy often triples cost/latency for negligible user-perceived improvement.

**Evidence**:
- Vector DB research: 95→99% recall might triple query time
- SLM research: 90-95% accuracy at 5% cost is the sweet spot
- "Well-tuned: search millions in milliseconds" with ANN (approximate) search

**CLIO Risk**: Over-engineering retrieval or optimization for diminishing returns.

**Fix**: Set pragmatic targets. Cache hit rate > 85% (not 99%). Routing accuracy > 90%. Optimization improvement > 5%. These are the targets in PLAN.md already - don't inflate them.

---

## Part 4: Technology Recommendations

### 4.1 FastMCP 3.x Patterns for CLIO's Tool Servers

**Recommended Architecture**:

```python
# Server composition with mount
from fastmcp import FastMCP

clio_tools = FastMCP("clio-tools")

# Domain-specific servers
hdf5 = FastMCP("hdf5")
parquet = FastMCP("parquet")
slurm = FastMCP("slurm")
darshan = FastMCP("darshan")

# Mount with auto-namespacing
clio_tools.mount("/hdf5", hdf5)
clio_tools.mount("/parquet", parquet)
clio_tools.mount("/slurm", slurm)
clio_tools.mount("/darshan", darshan)
```

**Tool-to-DSPy Bridge**:

```python
import dspy
from fastmcp import Client

# Native MCP → DSPy tool bridge
async with Client("clio-tools") as client:
    tools = await client.list_tools()
    dspy_tools = [dspy.Tool.from_mcp_tool(t) for t in tools]

    expert = dspy.ReAct(
        signature="task -> analysis",
        tools=dspy_tools[:7],  # Curated subset
    )
```

**Key Patterns**:
- `@mcp.tool` with rich docstrings (tool descriptions are critical)
- Structured return types (dict, not raw strings)
- Error messages with recovery suggestions
- Built-in input validation

### 4.2 DSPy 3.x Patterns for CLIO's Agent Modules

**ReAct with ChatAdapter** (enables local model tool-calling):

```python
import dspy

# Configure for local model
lm = dspy.LM("openai/granite-3.3-8b", api_base="http://localhost:1234/v1")
dspy.configure(lm=lm, adapter=dspy.ChatAdapter())

# ReAct agent with tool-calling via ChatAdapter
expert = dspy.ReAct(
    signature="task, context -> analysis, recommendations",
    tools=curated_tools,
    max_iters=5,
)
```

**CodeAct Alternative** (LLM writes Python instead of structured tool calls):

```python
# For complex analysis where structured tools are limiting
analyst = dspy.CodeAct(
    signature="dataset_path, analysis_goal -> code, result",
    tools=["h5py", "pandas", "matplotlib"],
)
# LLM generates and executes Python code directly
```

**Optimizer Selection**:

| Optimizer | Use Case | CLIO Application |
|-----------|----------|------------------|
| BootstrapFewShot | Quick prompt improvement | Tier 2 expert prompts |
| MIPROv2 | Multi-stage optimization | Routing + expert chain |
| SIMBA | Long-horizon agentic tasks | Full agent pipeline |

**MIPROv2 for CLIO**:

```python
from dspy.teleprompt import MIPROv2

optimizer = MIPROv2(
    metric=routing_accuracy_metric,
    num_candidates=10,
    num_threads=4,
)

# Optimize the routing module
optimized_router = optimizer.compile(
    router_module,
    trainset=arc_history_examples,  # From ARC memory
)
```

**SIMBA for Agent Optimization** (v0.4.0+):

```python
from dspy.teleprompt import SIMBA

# SIMBA designed specifically for agentic/long-horizon tasks
optimizer = SIMBA(
    metric=agent_success_metric,
    max_bootstrapped_demos=8,
    max_labeled_demos=16,
)

optimized_agent = optimizer.compile(
    clio_agent,
    trainset=successful_interactions,
)
```

### 4.3 Memory Architecture Recommendations

**Recommended Enhancements to ARC**:

1. **Temporal Knowledge Graph** (inspired by Zep):
   - Link analyses → experiments → datasets → papers
   - Enable queries like "What analyses used this dataset?"
   - Timestamps on all edges for temporal reasoning

2. **Memory Compaction Pipeline** (inspired by LangMem):
   ```python
   class MemoryCompactor:
       def compact_conversation(self, messages: list[Message]) -> str:
           """Summarize conversation preserving key facts."""
           # Keep: hypotheses, results, errors, decisions
           # Drop: greetings, clarifications, retries
           ...

       def extract_procedural(self, invocations: list[Invocation]) -> list[Procedure]:
           """Extract reusable workflows from successful invocation chains."""
           ...
   ```

3. **Scoped Memory** (inspired by OpenAI):
   ```python
   # Global: "HDF5 gzip level 4 is optimal for climate data"
   arc.store_memory("gzip-4-climate", scope="global", type=MemoryType.SEMANTIC)

   # User: "Dr. Smith prefers 1MB chunks"
   arc.store_memory("chunk-pref", scope="user:smith", type=MemoryType.EPISODIC)

   # Project: "Experiment 42 uses custom coordinate system"
   arc.store_memory("coord-sys", scope="project:exp42", type=MemoryType.SEMANTIC)
   ```

### 4.4 Local SLM Deployment for Tier 3 Nanoagents

**Recommended Stack**:

```bash
# llama.cpp + Unsloth Dynamic GGUFs
llama-server \
  --model granite-3.3-2b-instruct-UD-Q4_K_XL.gguf \
  --port 8080 \
  --ctx-size 8192 \
  --flash-attn on \
  --cache-type-k q8_0 --cache-type-v q8_0 \
  --jinja  # Required for tool calling
```

**Model Recommendations for CLIO Tiers**:

| Tier | Model | Size | Where | Why |
|------|-------|------|-------|-----|
| Tier 1 (Planner) | Claude Sonnet 4.5 / GPT-5.2 | Cloud | Cloud API | Best reasoning, routing |
| Tier 2 (Expert) | Granite 3.3 8B | 8B | Local (LM Studio) | Domain knowledge, tool calling |
| Tier 3 (Nano) | Granite 3.3 2B | 2B | Local (llama.cpp) | Fast, cheap, focused tasks |
| Classification | TF-IDF + classifier | <100MB | In-process | Query routing, 0.95 F1, instant |

**Integration via DSPy**:

```python
# Multi-model setup
TIER_1 = dspy.LM("anthropic/claude-sonnet-4-5-20250929")
TIER_2 = dspy.LM("openai/granite-3.3-8b", api_base="http://localhost:1234/v1")
TIER_3 = dspy.LM("openai/granite-3.3-2b", api_base="http://localhost:8080/v1")

# Per-tier model assignment
with dspy.context(lm=TIER_2):
    result = data_expert(task="Analyze HDF5 compression")

with dspy.context(lm=TIER_3):
    chunks = nanoagent(task="Check chunk alignment")
```

### 4.5 Agentic RAG for Context Retrieval

**Pattern: 4-Stage Intelligent Retrieval for CLIO**

```python
class ClioRetriever:
    """Agentic RAG with 4-stage decision pipeline."""

    def retrieve(self, query: str, context: dict) -> list[str]:
        # Stage 1: IF - Does this need retrieval?
        if self._is_factual_or_computational(query):
            return []  # No retrieval needed

        # Stage 2: WHAT - Construct optimal query
        enhanced_query = self._enhance_query(
            query, context["user_role"], context["dataset_type"]
        )

        # Stage 3: WHERE & HOW - Pick strategy
        if self._is_code_query(enhanced_query):
            results = self._lexical_search(enhanced_query)  # grep/glob
        elif self._has_figures(enhanced_query):
            results = self._multimodal_search(enhanced_query)  # Vision RAG
        else:
            results = self._hybrid_search(enhanced_query)  # Semantic + lexical

        # Stage 4: GENERATE - From smallest faithful context
        return self._rerank_and_truncate(results, max_tokens=2000)
```

**Scientific Data RAG Specifics**:
- HDF5 files are self-describing: extract metadata without loading full file
- Parquet: predicate pushdown, column pruning, row group skipping
- MIQS pattern: in-memory metadata indexing for self-describing formats
- Datatractor pattern: curated registry of extraction tools with standardized schema

**When to Use What**:

| Data Type | Retrieval Strategy | Why |
|-----------|-------------------|-----|
| Code (Python, config) | Lexical (grep/glob) | Exact matches, function names |
| Scientific text, papers | Semantic (embeddings) | Meaning-based similarity |
| Plots, diagrams | Multimodal (vision) | "Cannot grep a diagram" |
| Structured metadata | Direct query (SQL/API) | Exact field lookups |
| HDF5/Parquet schemas | File introspection | Self-describing formats |

---

## Part 5: Future Vision (v0.5.0+)

### 5.1 Online Learning Patterns

**Pattern: Continuous Self-Improvement via ARC Metrics**

```python
class OnlineLearner:
    """Continuously improve agent from production metrics."""

    def check_triggers(self):
        """Check if optimization is needed."""
        metrics = self.arc.get_recent_metrics(window="7d")

        if metrics.success_rate < 0.80:
            self.trigger_optimization("success_rate_drop")
        if metrics.avg_latency > metrics.baseline_latency * 1.2:
            self.trigger_optimization("latency_increase")

    def ab_test(self, current: dspy.Module, candidate: dspy.Module):
        """Run A/B test with gradual rollout."""
        # 10% traffic → candidate
        # Compare metrics over 100 interactions
        # If candidate wins with statistical significance (p < 0.05):
        #   50% → 100% rollout
        # Else: rollback
        ...
```

**Statistical Significance** (required before deployment):

```python
from scipy import stats

def is_significant(control_scores, treatment_scores, alpha=0.05):
    """Welch's t-test for variant comparison."""
    t_stat, p_value = stats.ttest_ind(
        control_scores, treatment_scores, equal_var=False
    )
    return p_value < alpha
```

### 5.2 A2A Protocol Patterns

**Google Agent-to-Agent Protocol** (April 2025):

```python
# A2A Agent Card (JSON manifest for discovery)
agent_card = {
    "name": "clio-data-expert",
    "version": "0.3.0",
    "capabilities": ["hdf5", "parquet", "adios", "compression"],
    "endpoint": "http://localhost:8000/a2a",
    "authentication": {"type": "bearer"},
    "input_schema": {"type": "object", "properties": {"task": {"type": "string"}}},
    "output_schema": {"type": "object", "properties": {"analysis": {"type": "string"}}},
}

# A2A communication between CLIO and external agents
class A2AAdapter:
    async def call_external(self, agent_card: dict, task: str) -> dict:
        """Call external agent via A2A protocol."""
        ...

    async def handle_incoming(self, request: dict) -> dict:
        """Handle incoming A2A requests to CLIO experts."""
        ...
```

**Integration Points**:
- LangChain agents → CLIO via A2A adapter
- CrewAI crews → CLIO via A2A adapter
- AutoGen agents → CLIO via A2A adapter
- CLIO experts exposed as A2A endpoints

### 5.3 Evolutionary Optimization

**Pattern: Curriculum Learning for Agent Improvement**

```python
class CurriculumOptimizer:
    """Progressive difficulty optimization."""

    def generate_curriculum(self):
        """Create training curriculum from ARC history."""
        # Easy: queries agent got right with high confidence
        # Medium: queries that required multiple tool calls
        # Hard: queries that failed or required human correction
        easy = self.arc.query(success=True, confidence=">0.9")
        medium = self.arc.query(tool_calls=">2", success=True)
        hard = self.arc.query(success=False)
        return [easy, medium, hard]

    def evolve(self, agent: dspy.Module, curriculum: list):
        """Train agent on progressively harder examples."""
        for difficulty_level in curriculum:
            optimized = SIMBA(metric=self.metric).compile(
                agent, trainset=difficulty_level
            )
            if self.evaluate(optimized) > self.evaluate(agent):
                agent = optimized
        return agent
```

**Meta-Agent Pattern** (from ROMA):
- Agent that generates and improves other agents
- Recursive self-modification with safety constraints
- Bootstrapping from minimal capabilities to complex workflows

### 5.4 Community Marketplace

**Pattern: Optimizer & Expert Marketplace**

```python
# Community optimizer template
class BioinformaticsOptimizer:
    """Community-contributed optimizer for genomics workflows."""
    domain = "bioinformatics"
    version = "1.0.0"
    author = "community"

    training_data = [
        # Curated examples for genomics analysis patterns
        {"task": "Align FASTQ files", "tools": ["bwa_align"], ...},
        ...
    ]

    def optimize(self, expert: dspy.Module) -> dspy.Module:
        return MIPROv2(metric=genomics_metric).compile(
            expert, trainset=self.training_data
        )
```

**Registry for Community Contributions**:
- Optimizer templates (bioinformatics, climate, chemistry, physics)
- Expert definitions (new Tier 2 specialists)
- MCP server implementations (domain-specific tools)
- Training datasets (curated examples per domain)

---

## Appendix: Source Index

### Vault Articles Read
1. AI Agent Architectures Comparison (Hierarchical, Swarm, Meta Learning, Modular, Evolutionary)
2. Building Effective Agents (Anthropic)
3. Agent Development Kit (Google ADK)
4. Agent OS (buildermethods)
5. Recursive Open Meta-Agent v0.1 Beta (ROMA)
6. LangGraph Swarm Patterns
7. Scaling Long-Running Autonomous Coding (Cursor)
8. Single-File Agents (disler)
9. Advanced Context Engineering for Agents (Dexter Horthy)
10. Context Engineering (Anthropic)
11. How Memory Transforms AI Agents (2025)
12. Inside OpenAI's In-House Data Agent
13. Traditional RAG vs Agentic RAG (NVIDIA)
14. Complete Guide to Vector Databases
15. Stop Converting REST APIs to MCP
16. Introducing MCP CLI
17. Small Language Models (SLMs)
18. How to Run Local LLMs with Claude Code & OpenAI Codex
19. Future of Storage for AI and HPC

### Web Research Topics
1. DSPy multi-agent patterns 2025-2026
2. MCP server best practices scientific computing
3. Agent memory architecture production 2025-2026
4. Self-improving AI agents DSPy optimization
5. Agentic RAG vs traditional RAG 2026
6. Context engineering LLM agents
7. Tool curation AI agents anti-patterns
8. Agent registry capability routing patterns
9. Hierarchical agent coordination consensus
10. Token budget context allocation production
11. A2A protocol Google agent-to-agent
12. Mem0 agent memory architecture
