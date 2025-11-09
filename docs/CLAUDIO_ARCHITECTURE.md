---
title: "ClaudIO Architecture: Self-Improving Autonomous Agent for Scientific Data Management"
category: architecture
priority: critical
version: "4.0"
focus: "3-Tier Orchestration + ARC Memory + Optimizer Layer + Agent Registry + A2A Protocol + IOWarp Integration"
---

# ClaudIO Architecture

ClaudIO is a **self-improving autonomous agent for scientific data management** that orchestrates specialized expert agents and nanoagents, maintains persistent memory (ARC), learns from experience (Optimizer Layer), integrates external agents via A2A protocol, and serves as the Intelligence Layer (CEI) within IOWarp's 3-tier architecture.

## Core Philosophy

> **"Self-improving autonomous agent for science. Native memory (ARC). Continuous learning (Optimizers). Registry-based orchestration. A2A protocol for collaboration. IOWarp Intelligence Layer (CEI). Gets better with use."**

ClaudIO is NOT:
- ❌ A framework for building agents
- ❌ A prompt engineering toolkit
- ❌ A monolithic LLM wrapper
- ❌ A simple chatbot
- ❌ A static, non-learning system

ClaudIO IS:
- ✅ A self-improving autonomous data management agent specialized in scientific workflows
- ✅ A 3-tier orchestration system (main agent → experts → nanoagents)
- ✅ A native memory system (ARC) with O(log N) retrieval and IOWarp CTE persistence
- ✅ A continuous learning system (Optimizer Layer) with offline tuning + online learning
- ✅ An agent registry coordinator for external agent integration (LangChain, CrewAI, AutoGen)
- ✅ The Intelligence Layer (CEI) of IOWarp's 3-tier architecture
- ✅ A tool-augmented reasoning system via FastMCP (CAE/PPI layer)
- ✅ A multi-modal deployment platform (CLI, library, REST API, container)

---

## System Architecture

### Full Stack View: ClaudIO + ARC + Optimizers + IOWarp Integration

```
┌────────────────────────────────────────────────────────────────────┐
│                    USER INTERFACES (AI Gateway)                    │
│  CLI (current) | REST API (planned) | A2A Protocol (external)      │
└────────────────────────────────┬───────────────────────────────────┘
                                 │
┌────────────────────────────────▼───────────────────────────────────┐
│  INTELLIGENCE LAYER (CEI) - Context Exploration Interface          │
│                                          + ARC Memory (In-Memory)  │
│                                                                     │
│  ┌──────────────────────────────────────────────────────────────┐ │
│  │  ClaudIO Main Agent (Tier 1 - Orchestrator)                  │ │
│  │   • Query analysis & intent extraction                       │ │
│  │   • ARC Memory queries (O(log N) retrieval)                  │ │
│  │   • Agent Registry coordination                              │ │
│  │   • Capability-based routing                                 │ │
│  │   • Conversation & context management                        │ │
│  └────────────────┬─────────────────────────────────────────────┘ │
│                   │                                                │
│     ┌─────────────┼─────────────┬────────────────┐                │
│     ▼             ▼             ▼                ▼                │
│  ┌─────────┐  ┌─────────┐  ┌─────────┐  ┌──────────────────┐    │
│  │Data     │  │HPC      │  │Research │  │External Agents   │    │
│  │Expert   │  │Expert   │  │Expert   │  │(via A2A Protocol)│    │
│  │(Tier 2) │  │(Tier 2) │  │(Tier 2) │  │  • LangChain     │    │
│  │         │  │(Planned)│  │(Planned)│  │  • CrewAI        │    │
│  │         │  │         │  │         │  │  • AutoGen       │    │
│  └────┬────┘  └────┬────┘  └────┬────┘  └────┬─────────────┘    │
│       │            │            │             │                  │
│       │  All tiers read/write ARC for coordination               │
│       └────────────┴────────────┴─────────────┘                  │
│                    │                                              │
│      ┌─────────────┴─────────────┐                               │
│      ▼                           ▼                               │
│  ┌─────────────────┐      ┌──────────────┐   ┌────────────────┐ │
│  │  Nanoagents     │      │Agent Registry│   │ ARC Memory     │ │
│  │  (Tier 3)       │      │ • Discovery  │   │ • LRU Cache    │ │
│  │  • Ephemeral    │      │ • Compilation│   │ • B-tree Index │ │
│  │  • Task-focused │      │ • Routing    │   │ • O(log N)     │ │
│  │  (tracked in ARC)      └──────────────┘   └────────────────┘ │
│  └─────────────────┘                                             │
└─────────────────────────────────┬──────────────────────────────────┘
                                  │
          ┌───────────────────────┼──────────────────┐
          ▼                       ▼                  ▼
┌─────────────────┐  ┌────────────────────┐  ┌──────────────────────┐
│ CAE/PPI (Tools) │  │ OPTIMIZER LAYER    │  │ ARC Persistent Store │
│                 │  │ (Self-Improvement) │  │ (CTE Integration)    │
│ FastMCP Servers │  │                    │  │                      │
│ • HDF5/ADIOS    │  │ • Prompt Optimizers│  │ IOWarp Namespace:    │
│ • SLURM/PBS     │  │ • Routing Optimizers│  │ /claudio/arc/*      │
│ • Nextflow/Parsl│  │ • Tool Optimizers  │  │                      │
│ • 15+ servers   │  │ • Offline Tuning   │  │ • /conversations/    │
│ • 150+ tools    │  │ • Online Learning  │  │ • /invocations/      │
│ (v0.5.0)        │  │ • Metrics Analysis │  │ • /metrics/          │
│                 │  │ (reads ARC metrics)│  │ • /context/          │
└─────────────────┘  └────────────────────┘  └──────────────────────┘
                              │
                              ▼
          ┌───────────────────────────────────────────┐
          │  CTE - Context Transfer Engine            │
          │  Hermes Multi-Tier Storage                │
          │  • Hot: GPU memory (active sessions)      │
          │  • Warm: NVMe (recent 24h)                │
          │  • Cold: Parallel FS (historical)         │
          │  • Archive: Object store (long-term)      │
          └───────────────────────────────────────────┘
```

### 3-Tier Agent Hierarchy (Detail)

```
TIER 1: ClaudIO Main Agent
┌────────────────────────────────────────┐
│  Responsibilities:                     │
│  • Parse user queries                  │
│  • Extract required capabilities       │
│  • Query Agent Registry                │
│  • Route to native OR external agents  │
│  • Manage conversation context         │
│  • Assemble final responses            │
└─────────────────┬──────────────────────┘
                  │
      ┌───────────┼───────────┐
      ▼           ▼           ▼
┌────────────┬──────────┬─────────────┐
│            │          │             │
▼            ▼          ▼             ▼

TIER 2: Expert Agents (Persistent Specialists)
┌─────────────┐  ┌──────────────┐  ┌──────────────┐
│ DataExpert  │  │  HPCExpert   │  │ External Agents│
│  (Current)  │  │  (Planned)   │  │ (via Registry)│
│             │  │              │  │               │
│ Capabilities│  │ Capabilities │  │ Capabilities  │
│ • HDF5      │  │ • SLURM      │  │ • Custom      │
│ • ADIOS     │  │ • MPI        │  │ • LangChain   │
│ • Parquet   │  │ • Darshan    │  │ • CrewAI      │
│             │  │              │  │ • AutoGen     │
└──────┬──────┘  └──────┬───────┘  └───────────────┘
       │                │
       └────────────────┘
                │
        ┌───────┴────────┐
        ▼                ▼

TIER 3: Nanoagents (Ephemeral Workers)
┌──────────────────────────────────────┐
│  • Spawned by Tier 2 experts         │
│  • Short-lived, task-specific        │
│  • Examples:                         │
│    - "Analyze this HDF5 chunk"       │
│    - "Convert this ADIOS timestep"   │
│    - "Optimize compression params"   │
│  • Auto-terminated after completion  │
└──────────────────────────────────────┘
```

---

## Core Components

### 1. Agent Registry (Capability-Based Coordination)

**Purpose**: Central registry for agent discovery, capability matching, and routing.

**Key Functions**:
- **Registration**: Native experts and external agents register their capabilities
- **Discovery**: Query available agents by capability (e.g., "HDF5 optimization")
- **Compilation**: Translate external agents (LangChain, CrewAI, AutoGen) into ClaudIO-compatible instances
- **Routing**: Match user queries to appropriate agents based on declared capabilities

**Agent Metadata Structure**:
```
Agent Entry:
  - name: "DataExpert"
  - tier: 2 (expert) or 3 (nanoagent)
  - capabilities: ["HDF5", "ADIOS", "Parquet", "compression", "chunking"]
  - tools: ["hdf5_analyze", "hdf5_optimize", "adios_convert"]
  - source: "native" | "langchain" | "crewai" | "autogen" | "custom"
  - interface: "A2A" (for external agents)
```

**Routing Algorithm**:
```
1. Parse user query → extract required capabilities
2. Query registry for agents matching capabilities
3. Rank by:
   - Capability overlap score
   - Agent tier (prefer Tier 2 over Tier 3)
   - Source (native > external for performance)
4. Route to top-ranked agent
5. If external agent: use A2A protocol communication
```

### 2. A2A Protocol (Agent-to-Agent Communication)

**Purpose**: Standardized communication interface enabling ClaudIO to integrate agents from any framework.

**Protocol Structure**:
```
Request:
  - query: string (user's question)
  - context: dict (conversation history, file metadata, etc.)
  - capabilities_needed: list[string]
  - constraints: dict (timeout, model preferences, etc.)

Response:
  - answer: string (agent's response)
  - reasoning_trace: list[dict] (thought process, optional)
  - tools_used: list[string] (MCP tools called)
  - metadata: dict (execution time, model used, etc.)
```

**Supported Agent Frameworks**:
- **LangChain**: Agents built with LangChain SDK register via A2A adapter
- **CrewAI**: Crew agents compile to A2A-compatible instances
- **AutoGen**: AutoGen agents expose A2A interface
- **Custom**: Any agent implementing A2A protocol spec

**Communication Flow**:
```
ClaudIO Main Agent
    ↓ (A2A Request)
External Agent (e.g., LangChain)
    ↓ (executes internally)
External Agent Response
    ↓ (A2A Response)
ClaudIO Main Agent (assembles final response)
```

### 3. ARC Memory Layer (Agent Runtime Context)

**Purpose**: ClaudIO's native, high-performance memory system for persistent context, fast retrieval, and agent coordination.

**Architecture**:

```
┌────────────────────────────────────────────────────────────┐
│  ARC MEMORY ARCHITECTURE                                   │
├────────────────────────────────────────────────────────────┤
│                                                             │
│  TIER 1: In-Memory Layer (Hot Data, Fast Access)          │
│  ┌──────────────────────────────────────────────────────┐ │
│  │  LRU Cache                                           │ │
│  │  • Active conversations                              │ │
│  │  • Recent tool results                               │ │
│  │  • User preferences                                  │ │
│  │  • O(1) access for cached items                      │ │
│  │  • Size: Configurable (default 1000 items)           │ │
│  └──────────────────────────────────────────────────────┘ │
│                        ↕                                   │
│  TIER 2: Index Layer (Fast Search)                         │
│  ┌──────────────────────────────────────────────────────┐ │
│  │  B-Tree Index                                        │ │
│  │  • Index on: session_id, timestamp, agent_id         │ │
│  │  • O(log N) retrieval                                │ │
│  │  • Range queries supported                           │ │
│  │                                                       │ │
│  │  LSM Tree (Log-Structured Merge Tree)                │ │
│  │  • Write-heavy metrics collection                    │ │
│  │  • Optimized for high write throughput               │ │
│  └──────────────────────────────────────────────────────┘ │
│                        ↕                                   │
│  TIER 3: Persistent Layer (IOWarp CTE Integration)         │
│  ┌──────────────────────────────────────────────────────┐ │
│  │  IOWarp Namespace: /claudio/arc/*                    │ │
│  │                                                       │ │
│  │  Tier Policy:                                        │ │
│  │  • Hot (GPU memory): Active sessions (< 1 hour)      │ │
│  │  • Warm (NVMe): Recent sessions (< 24 hours)         │ │
│  │  • Cold (Parallel FS): Historical (< 30 days)        │ │
│  │  • Archive (Object Store): Long-term (> 30 days)     │ │
│  └──────────────────────────────────────────────────────┘ │
└────────────────────────────────────────────────────────────┘
```

**Data Schema**:

```python
# /conversations/<session_id>/
Conversation = {
    "session_id": "uuid-v4",
    "user_id": "user@example.com",
    "created_at": "2025-01-09T...",
    "updated_at": "2025-01-09T...",
    "messages": [
        {
            "role": "user" | "assistant",
            "content": "...",
            "timestamp": "2025-01-09T...",
        }
    ],
    "routing_decisions": [
        {
            "timestamp": "2025-01-09T...",
            "query": "...",
            "selected_agent": "DataExpert",
            "reasoning": "...",
            "confidence": 0.95,
        }
    ],
    "metadata": {
        "user_preferences": {...},
        "domain": "scientific_computing",
        "total_tokens": 1234,
    }
}

# /invocations/<trace_id>/
Invocation = {
    "trace_id": "uuid-v4",
    "session_id": "uuid-v4",
    "agent_id": "DataExpert",
    "tier": 2,  # 1=Main, 2=Expert, 3=Nanoagent
    "started_at": "2025-01-09T...",
    "completed_at": "2025-01-09T...",
    "duration_ms": 1247,
    "status": "success" | "failure" | "timeout",
    "tools_called": [
        {
            "tool": "hdf5_analyze",
            "params": {...},
            "result": {...},
            "duration_ms": 342,
        }
    ],
    "nanoagents_spawned": [
        {
            "nanoagent_id": "uuid-v4",
            "task": "analyze_chunk",
            "duration_ms": 123,
        }
    ],
    "performance": {
        "latency_ms": 1247,
        "success": true,
        "user_satisfied": true,  # implicit/explicit
    }
}

# /metrics/<agent_id>/
Metrics = {
    "agent_id": "DataExpert",
    "period": "2025-01-01/2025-01-31",
    "total_invocations": 1234,
    "success_rate": 0.967,
    "avg_latency_ms": 1523,
    "p50_latency_ms": 1200,
    "p95_latency_ms": 2500,
    "p99_latency_ms": 4200,
    "user_satisfaction": 0.89,
    "optimization_history": [
        {
            "timestamp": "2025-01-15T...",
            "optimizer": "PromptOptimizer",
            "variant": "v2.3.1",
            "improvement": "+12% success_rate",
        }
    ]
}

# /context/<domain>/
Context = {
    "domain": "hdf5_optimization",
    "retrieved_docs": [...],  # RAG results
    "cached_tool_results": {...},
    "learned_patterns": [
        {
            "pattern": "large_files_need_compression",
            "confidence": 0.92,
            "examples_seen": 47,
        }
    ]
}
```

**API Interface**:

```python
class ARC:
    """Agent Runtime Context - Memory Layer"""

    # Read Operations (O(log N))
    def get_conversation(self, session_id: str) -> Conversation
    def get_invocations(self, agent_id: str, limit: int = 100) -> List[Invocation]
    def get_metrics(self, agent_id: str, period: str) -> Metrics
    def search_context(self, query: str, domain: str) -> List[Context]

    # Write Operations
    def store_message(self, session_id: str, message: Message) -> None
    def store_invocation(self, invocation: Invocation) -> None
    def update_metrics(self, agent_id: str, metrics: Metrics) -> None
    def cache_tool_result(self, tool: str, params: dict, result: Any) -> None

    # Coordination
    def get_shared_context(self, session_id: str) -> dict  # For multi-agent coordination
    def update_shared_context(self, session_id: str, context: dict) -> None
```

**Performance Characteristics**:
- **Cache Hit Rate**: 85-95% for recent conversations
- **O(1) Access**: For cached items (hot data)
- **O(log N) Search**: For indexed queries
- **Write Throughput**: 10,000+ ops/sec (LSM tree)
- **Storage Tiers**: Automatic migration based on access patterns

**Integration with Optimizer Layer**:
```
ARC stores all metrics → Optimizer Layer reads metrics →
Identifies improvement opportunities → Tunes prompts/routing →
Improved performance stored back in ARC → Continuous cycle
```

### 4. Optimizer Layer (Self-Improvement Engine)

**Purpose**: ClaudIO's learning system for continuous improvement through offline tuning and online learning.

**Architecture**:

```
┌────────────────────────────────────────────────────────────┐
│  OPTIMIZER LAYER ARCHITECTURE                              │
├────────────────────────────────────────────────────────────┤
│                                                             │
│  ┌──────────────────────────────────────────────────────┐ │
│  │  OFFLINE TUNING MODE (User-Driven)                   │ │
│  │                                                       │ │
│  │  User initiates: uv run cli.py --tune                │ │
│  │       ↓                                               │ │
│  │  1. Select Component                                 │ │
│  │     - Main agent routing                             │ │
│  │     - Expert prompts (DataExpert, HPCExpert, etc.)   │ │
│  │     - Tool selection logic                           │ │
│  │       ↓                                               │ │
│  │  2. Provide Training Examples                        │ │
│  │     - Built-in example sets                          │ │
│  │     - Custom user examples                           │ │
│  │     - Generated from ARC history                     │ │
│  │       ↓                                               │ │
│  │  3. Choose Optimizer                                 │ │
│  │     - BootstrapFewShot (fast, simple)                │ │
│  │     - MIPRO (comprehensive, slower)                  │ │
│  │     - Custom/Community optimizers                    │ │
│  │       ↓                                               │ │
│  │  4. Run Optimization Session                         │ │
│  │     - Try prompt variations                          │ │
│  │     - Measure performance                            │ │
│  │     - Find local optima                              │ │
│  │     - Progress shown in real-time                    │ │
│  │       ↓                                               │ │
│  │  5. Evaluate & Deploy                                │ │
│  │     - Compare before/after metrics                   │ │
│  │     - Test on validation set                         │ │
│  │     - Deploy if satisfactory                         │ │
│  │     - Store optimized variant in ARC                 │ │
│  └──────────────────────────────────────────────────────┘ │
│                                                             │
│  ┌──────────────────────────────────────────────────────┐ │
│  │  ONLINE LEARNING MODE (Automatic)                    │ │
│  │                                                       │ │
│  │  While ClaudIO operates:                             │ │
│  │       ↓                                               │ │
│  │  1. Capture Metrics                                  │ │
│  │     - Every invocation tracked in ARC                │ │
│  │     - Success rates, latency, satisfaction           │ │
│  │       ↓                                               │ │
│  │  2. A/B Testing                                      │ │
│  │     - Randomly select prompt variant (10% traffic)   │ │
│  │     - Compare performance to baseline                │ │
│  │     - Track statistical significance                 │ │
│  │       ↓                                               │ │
│  │  3. Automatic Optimization Triggers                  │ │
│  │     - When success_rate < threshold (e.g., 0.80)     │ │
│  │     - When latency degrades > 20%                    │ │
│  │     - When user satisfaction drops                   │ │
│  │       ↓                                               │ │
│  │  4. Gradual Improvement                              │ │
│  │     - Roll out better variants incrementally         │ │
│  │     - 10% → 50% → 100% traffic shift                 │ │
│  │     - Rollback if performance degrades               │ │
│  └──────────────────────────────────────────────────────┘ │
└────────────────────────────────────────────────────────────┘
```

**Optimizer Types**:

```python
# 1. Prompt Optimizers
class PromptOptimizer(ClaudIOOptimizer):
    """
    Optimize agent prompts (signatures, few-shot examples, reasoning chains)

    Methods:
    - BootstrapFewShot: Automatically select best few-shot examples
    - MIPRO: Multi-step instruction proposal and refinement
    - SignatureOptimizer: Refine input/output field descriptions
    """

    def optimize(self, agent: str, training_set: List[Example]) -> OptimizedPrompt:
        # Internal: Uses DSPy teleprompters (BootstrapFewShot, MIPROv2)
        pass

# 2. Routing Optimizers
class RoutingOptimizer(ClaudIOOptimizer):
    """
    Optimize Agent Registry routing logic

    Learns:
    - Which expert to select for which query types
    - Capability matching rules
    - Confidence thresholds
    """

    def optimize(self, routing_history: List[RoutingDecision]) -> OptimizedRouter:
        # Analyzes ARC routing_decisions, improves selection logic
        pass

# 3. Tool Selection Optimizers
class ToolOptimizer(ClaudIOOptimizer):
    """
    Optimize when/how to use MCP tools

    Learns:
    - When to call which tool
    - Optimal tool parameters
    - Tool execution ordering
    """

    def optimize(self, tool_traces: List[Invocation]) -> OptimizedToolStrategy:
        # Analyzes ARC invocation tool_calls, improves strategy
        pass

# 4. Community/Custom Optimizers
class CustomOptimizer(ClaudIOOptimizer):
    """
    User-contributed or domain-specific optimizers

    Examples:
    - ChemistryOptimizer: For chemistry-specific queries
    - BioinformaticsOptimizer: For genomics workflows
    - TransferLearning: Learn from similar tasks
    """
    pass
```

**Optimization Workflow (Offline)**:

```
Step 1: Component Selection
  User selects: DataExpert prompts

Step 2: Training Set
  Option A: Use built-in examples (HDF5 optimization tasks)
  Option B: Generate from ARC history (last 1000 invocations)
  Option C: Upload custom examples

Step 3: Optimizer Selection
  User chooses: MIPRO (comprehensive)

Step 4: Optimization Run
  MIPRO runs:
    - Proposes 20 prompt variations
    - Tests each against training set (80%) and validation set (20%)
    - Measures: success_rate, latency, reasoning_quality
    - Selects best variant based on composite score

  Progress:
    [=====>            ] 25% (5/20 variants tested)
    Current best: variant_7 (success: 0.94, latency: 1203ms)

Step 5: Evaluation
  Before:  success_rate=0.87, avg_latency=1523ms
  After:   success_rate=0.94, avg_latency=1203ms
  Improvement: +8% success, -21% latency

  User decision: Deploy ✓

  Deployment:
    - Store variant_7 as active prompt in ARC
    - Tag as "optimized_2025-01-09_mipro"
    - Can rollback to previous version if needed
```

**Metrics Tracked in ARC**:

```
Optimization Metrics:
- variant_id: "v2.3.1"
- optimizer_used: "MIPRO"
- training_examples: 856
- validation_examples: 214
- optimization_duration: "2h 15m"
- improvements:
    - success_rate: 0.87 → 0.94 (+8%)
    - avg_latency: 1523ms → 1203ms (-21%)
    - user_satisfaction: 0.84 → 0.91 (+8%)
- deployed_at: "2025-01-09T15:30:00Z"
- rollback_available: true
```

**Why Optimizers Matter**:
- **Super Tunable**: Customize ClaudIO for specific domains (chemistry, genomics, climate science)
- **Data-Driven**: Not guesswork - based on actual performance metrics from ARC
- **Continuous**: Gets better over time with both offline tuning and online learning
- **Community-Driven**: Share optimizers across users, transfer learning from similar tasks
- **Measurable**: Clear before/after metrics, statistically significant improvements

**Integration with ARC**:
```
Metrics Flow:
  Agent Invocation → Store in ARC (/invocations/, /metrics/)
                    ↓
  Optimizer Layer ← Read metrics from ARC
                    ↓
  Analyze Performance → Identify improvement opportunities
                    ↓
  Generate Optimized Variant → Test against validation set
                    ↓
  Deploy Best Variant → Store in ARC (/metrics/optimization_history)
                    ↓
  Future Invocations → Use optimized variant → Better performance → Store in ARC
                    ↓
  Continuous Improvement Cycle
```

### 5. MCP Tools (via CAE/PPI Layer)

**Integration with IOWarp Tool Layer**:

Expert agents call MCP tools through IOWarp's Content Assimilation Engine (CAE) and Platform Plugin Interface (PPI). Tool results are cached in ARC for faster subsequent access.

**Tool Execution Flow (with ARC Caching)**:
```
Expert Agent (Tier 2)
    ↓ (tool request)
ARC Cache Check
    ├─ Cache Hit → Return cached result (O(1))
    └─ Cache Miss ↓
CAE/PPI Layer (IOWarp)
    ↓ (MCP protocol)
MCP Server (e.g., hdf5-server)
    ↓ (execute operation)
Result
    ↓ (cache in ARC + return via CAE/PPI)
Expert Agent (process result)
```

**Planned MCP Ecosystem** (15+ servers):
- `hdf5-server`: HDF5 file analysis & optimization
- `adios-server`: ADIOS2 operations
- `parquet-server`: Parquet file management
- `slurm-server`: SLURM job management
- `darshan-server`: I/O profiling analysis
- `nextflow-server`: Workflow execution
- `parsl-server`: DAG execution
- `cwl-server`: Common Workflow Language
- `instrument-server`: Scientific instrument control
- Plus network, database, monitoring servers

**Tool Philosophy**:
- Tools are optional (graceful degradation)
- Experts work without tools (pure reasoning)
- Tools enhance capabilities when available
- MCP servers provide standardized scientific infrastructure interfaces

### 6. Expert Agents (ReAct Pattern - Tier 2)

**Reasoning + Acting Pattern**:

Each expert agent uses the ReAct pattern:
1. **Reason** about the problem
2. **Act** by calling MCP tools (via CAE/PPI)
3. **Observe** tool results
4. **Iterate** until completion

**DataExpert Example Flow**:
```
User: "Optimize my 100GB HDF5 file"
    ↓
Thought: "Need to analyze current compression first"
    ↓
Action: call hdf5_analyze("/data/file.h5") via CAE/PPI
    ↓
Observation: {"compression": "none", "size": "100GB"}
    ↓
Thought: "No compression! Should apply gzip-6"
    ↓
Action: call hdf5_optimize("/data/file.h5", strategy="gzip-6")
    ↓
Observation: {"new_size": "45GB", "ratio": "2.2x"}
    ↓
Answer: "Applied gzip-6 compression, reduced from 100GB to 45GB (2.2x)"
```

**Current Expert**: DataExpert (HDF5, ADIOS, Parquet)
**Planned Experts**: HPCExpert (SLURM, MPI), ResearchExpert (papers, citations), WorkflowExpert (automation)

---

## Multi-Agent Coordination Patterns

### Pattern 1: Single Expert (Current)
```
User Query → ClaudIO Main Agent → Agent Registry
    → DataExpert (Tier 2) → MCP Tools (CAE/PPI)
    → Result → User
```

### Pattern 2: Sequential Experts (Planned)
```
User: "Profile my simulation, find bottlenecks, optimize I/O"
    ↓
ClaudIO: Routes sequentially
    → HPCExpert (profile)
    → DataExpert (analyze I/O)
    → WorkflowExpert (optimize)
```

### Pattern 3: Parallel Experts (Planned)
```
User: "Convert all simulation outputs to analysis-ready formats"
    ↓
ClaudIO: Spawns multiple DataExpert instances in parallel
    → Expert 1 (file1.h5)
    → Expert 2 (file2.h5)
    → Expert 3 (file3.h5)
    → Aggregate results
```

### Pattern 4: Hierarchical (Expert → Nanoagents)
```
DataExpert receives complex task
    ↓
Spawns Nanoagents (Tier 3):
    → Nanoagent 1: "Analyze HDF5 chunk"
    → Nanoagent 2: "Test compression strategies"
    → Nanoagent 3: "Validate chunking parameters"
    ↓
Expert aggregates nanoagent results
    ↓
Returns final recommendation
```

### Pattern 5: External Agent Collaboration (A2A)
```
User asks general question with science component
    ↓
ClaudIO: "I'm specialized in science data, let me collaborate"
    ↓ (A2A Request to Claude Code)
Claude Code: Handles general aspects
    ↓ (A2A Request back to ClaudIO)
ClaudIO DataExpert: Handles HDF5 optimization
    ↓
Combined response to user
```

---

## LM Configuration (Model Flexibility)

ClaudIO supports **any LLM API** for maximum flexibility:

### Local Development (LM Studio)
```python
from claudio.config import setup_lm

lm = setup_lm(provider="lm_studio")
# Model: openai/gpt-oss-20b or granite-4-h-tiny
# Location: http://100.127.255.164:1234 (WSL2 → Windows)
# Cost: FREE, Privacy-preserving
```

### Local Production (Ollama)
```python
lm = setup_lm(provider="ollama", model="llama3.1:8b")
# Fully local, zero-cost inference
# Privacy-preserving for sensitive HPC data
```

### Cloud (OpenAI, Anthropic, etc.)
```python
lm = setup_lm(provider="openai")  # or "anthropic", "google", etc.
# For benchmarking or when local models insufficient
```

### Custom/Fine-tuned Models
```python
lm = setup_lm(provider="custom", endpoint="http://my-model:8000")
# Support for custom fine-tuned models
# Domain-specific scientific LMs
```

**Dual-LM Configuration**:
- **Main Agent / Router**: Lower temperature (0.3), deterministic
- **Expert / Reasoner**: Higher temperature (1.0), creative
- Can use different models for different tiers

---

## UV Script Integration (Deployment)

**Every ClaudIO module is a self-contained UV script**:

```python
#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "fastmcp>=0.1.0",
#   "rich>=13.0.0",
# ]
# ///

# Module implementation (agent logic is internal)
```

**Benefits**:
- No virtual environments needed
- Self-documenting dependencies
- Direct execution: `uv run expert.py`
- Reproducible across environments
- Fast: 10-100x faster than pip

**Deployment Modes**:
1. **Standalone CLI**: `uv run src/claudio/ui/cli.py`
2. **Python Library**: `from claudio import ClaudIO; agent = ClaudIO()`
3. **REST API**: FastAPI server (planned)
4. **Container**: Docker/Singularity with UV runtime

---

## Data Flow (with ARC Memory Integration)

```
1. User Query
   └─> AI Gateway (CLI / REST API / A2A)
       └─> ClaudIO Main Agent (Tier 1, CEI Layer)
           ├─> Load conversation from ARC (O(log N))
           ├─> Load user preferences from ARC
           └─> Analyzes query, extracts capabilities

2. Agent Registry Coordination
   └─> Query registry for matching agents
       ├─> Check ARC for routing history (learns from past)
       ├─> Rank by capability overlap + historical success
       └─> Select best agent (native or external)
           └─> Store routing decision in ARC

3. Expert Execution
   └─> Route to Expert Agent (Tier 2, CEI Layer)
       ├─> Load relevant context from ARC
       ├─> Use optimized prompts (from Optimizer Layer)
       └─> Expert uses ReAct pattern
           ├─> Calls MCP tools (CAE/PPI Layer)
           │   ├─> Check ARC cache for tool results (O(1) if cached)
           │   └─> Execute tool if not cached, store result in ARC
           ├─> May spawn nanoagents (Tier 3, tracked in ARC)
           └─> Iterates until completion

4. Tool Execution (if needed, with ARC caching)
   └─> MCP request via CAE/PPI Layer
       ├─> Check ARC tool result cache first
       ├─> Cache miss → Execute MCP server (e.g., HDF5 analysis)
       │   └─> May interact with CTE Layer (storage)
       ├─> Cache result in ARC (TTL configurable)
       └─> Returns results

5. Response Assembly & Metrics Collection
   └─> Expert formats response
       ├─> Store invocation trace in ARC (/invocations/)
       ├─> Update performance metrics in ARC (/metrics/)
       └─> Main agent adds metadata & context
           ├─> Store full conversation in ARC (/conversations/)
           └─> User receives answer + trace

6. Continuous Learning (Background)
   └─> Optimizer Layer periodically analyzes ARC metrics
       ├─> Identify performance degradation or improvement opportunities
       ├─> In online learning mode: A/B test new variants
       └─> Update routing/prompt strategies based on data
```

---

## Implementation Phases

### Phase 1: Core Foundation ✅ (v0.1.0 - Implemented)
- [x] Main agent orchestration (Tier 1) with conversation management
- [x] DataExpert with ReAct pattern (Tier 2)
- [x] LM configuration for any provider (LM Studio, Ollama, OpenAI, Anthropic, Google, custom)
- [x] UV self-contained script architecture
- [x] Interactive CLI with Rich TUI
- [x] Dual-LM support (separate models for routing vs. reasoning)

### Phase 2: Intelligence + Memory 🚧 (v0.2.0 - In Progress)
- [ ] **Agent Registry implementation**: Capability-based discovery and routing
- [ ] **A2A protocol specification & adapters**: External agent integration (LangChain, CrewAI, AutoGen)
- [ ] **External agent compilation**: SDK-agnostic agent registration
- [ ] **ARC Memory Layer foundation**: In-memory cache + B-tree indexing for O(log N) retrieval
- [ ] **Conversation storage**: Persistent context management
- [ ] Multi-agent coordination patterns

### Phase 3: Tools & IOWarp Integration (v0.3.0)
- [ ] FastMCP server implementations (HDF5, SLURM, Darshan, etc.)
- [ ] CAE/PPI layer integration with IOWarp
- [ ] **ARC-CTE integration**: Persistent storage in IOWarp namespace (/claudio/arc/*)
- [ ] CTE layer coordination for multi-tier storage (GPU → NVMe → PFS → Archive)
- [ ] **Tool result caching in ARC**: Faster repeated tool calls
- [ ] Tool calling end-to-end testing

### Phase 4: Advanced Patterns + Learning (v0.4.0)
- [ ] Nanoagent spawning (Tier 3)
- [ ] Hierarchical expert delegation (Expert → Nanoagents)
- [ ] Parallel expert execution
- [ ] **Optimizer Layer implementation**: Offline tuning mode
- [ ] **Performance tracking**: Metrics collection in ARC (/metrics/)
- [ ] **Prompt optimizers**: BootstrapFewShot, MIPRO
- [ ] **Routing optimizers**: Learn from ARC routing history
- [ ] **Tool optimizers**: Optimize MCP tool selection
- [ ] AgentLog audit trails (Spring 2025)

### Phase 5: Pre-Production Public Beta (v0.5.0)
- [ ] Improved CLI with `/tune`, `/memory`, `/metrics` commands
- [ ] REST API (FastAPI server)
- [ ] **Online learning mode**: A/B testing, automatic optimization triggers
- [ ] **Community optimizer marketplace**: User-contributed optimizers
- [ ] Agent Evals performance benchmarking
- [ ] Container deployment (Docker, Singularity)
- [ ] Integrated Agent Toolkit (15+ MCP servers, 150+ tools)
- [ ] Comprehensive documentation & examples

---

## Key Design Decisions

### Why Agent Registry?
- **Extensibility**: Add new experts or external agents without code changes
- **Capability matching**: Intelligent routing based on agent capabilities
- **Framework agnostic**: Integrate agents from ANY SDK (LangChain, CrewAI, etc.)
- **Discovery**: Runtime agent discovery and selection

### Why A2A Protocol?
- **Standardization**: Common interface for all agents
- **Interoperability**: ClaudIO can work with or as a sidekick to any agent
- **Framework independence**: Not locked into a single agent framework
- **Future-proof**: New frameworks can integrate via A2A

### Why 3-Tier Hierarchy?
- **Separation of concerns**: Orchestration (T1) vs. expertise (T2) vs. execution (T3)
- **Scalability**: Nanoagents enable massive parallelism
- **Resource efficiency**: Ephemeral T3 agents minimize overhead
- **Flexibility**: Experts can decide when to delegate vs. execute

### Why IOWarp Integration?
- **Complete stack**: Intelligence (CEI) + Tools (CAE/PPI) + Storage (CTE)
- **Scientific focus**: Built for HPC and scientific computing workflows
- **Performance**: Leverage IOWarp's optimized storage layer
- **Ecosystem**: Access to 15+ MCP servers for scientific tools

### Why FastMCP for Tools?
- **Standard protocol**: MCP is emerging industry standard
- **Easy authoring**: Pythonic tool definition
- **Server/client model**: Clean separation
- **Rich ecosystem**: Growing tool library

### Why UV for Scripts?
- **Zero config**: No requirements.txt management
- **Inline dependencies**: Self-documenting
- **Fast**: 10-100x faster than pip
- **Reproducible**: Locked dependencies per script

### Why Model Flexibility?
- **Privacy**: Local models (LM Studio, Ollama) for sensitive HPC data
- **Cost**: Zero-cost local inference vs. cloud APIs
- **Performance**: Choose appropriate model for task (small for routing, large for reasoning)
- **Custom models**: Support for domain-specific fine-tuned models

### Why ARC Memory Layer?
- **Fast Retrieval**: O(log N) search via B-tree indexing, not linear scan
- **Persistent Context**: Resume conversations, preserve history across sessions
- **Agent Coordination**: All tiers share context via ARC, no duplication
- **IOWarp Integration**: Leverage multi-tier storage (GPU → NVMe → PFS → Archive)
- **Performance Analytics**: Store metrics for optimizer learning
- **Scalability**: Write-optimized (LSM tree) for high-throughput metrics collection

### Why Optimizer Layer?
- **Super Tunable**: Customize ClaudIO for specific domains (chemistry, genomics, climate)
- **Data-Driven**: Optimizations based on real metrics from ARC, not guesswork
- **Continuous Improvement**: Gets better with use (offline tuning + online learning)
- **Community-Driven**: Share optimizers across users, transfer learning
- **Measurable**: Clear before/after metrics, statistically significant improvements
- **Extensible**: Users can add custom optimizers for unique needs

---

## Architecture Benefits

| Aspect | Traditional Approach | ClaudIO Architecture |
|--------|---------------------|---------------------|
| Agent discovery | Hardcoded if/else routing | Agent Registry with capability matching |
| External integration | Framework lock-in | A2A protocol (any framework) |
| Scalability | Single-tier agents | 3-tier hierarchy with nanoagents |
| **Memory & Context** | **Stateless or slow DB queries** | **ARC with O(log N) retrieval + IOWarp CTE** |
| **Learning & Improvement** | **Static, no optimization** | **Optimizer Layer: offline tuning + online learning** |
| Tool calling | Manual tool management | MCP protocol via CAE/PPI layer + ARC caching |
| Storage optimization | Application-level | Integrated with IOWarp CTE + ARC persistence |
| Model selection | Fixed provider | Any LLM API (cloud, local, custom) |
| Adding new expert | Rewrite routing logic | Register capability in registry |
| Deployment | Complex setup | UV scripts (self-contained) |
| Collaboration | Isolated agents | A2A protocol for agent-to-agent |
| **Performance tracking** | **Manual logs, hard to analyze** | **Automatic metrics collection in ARC** |
| **Tuning** | **Trial and error prompt engineering** | **Data-driven optimization via Optimizer Layer** |

---

## Future Roadmap

### Spring 2025: AgentLog Integration
- Audit trails for compliance and reproducibility
- Track agent decision-making process
- Provenance tracking for scientific workflows
- AgentLog API integration

### Summer 2025: Agent Evals Framework
- Performance benchmarking for agents
- Accuracy and efficiency metrics
- Comparative analysis across agent configurations
- Automated optimization recommendations
- **Integration with Optimizer Layer** for continuous improvement

### Fall 2025: Expanded MCP Ecosystem
- 15+ MCP servers fully implemented (150+ tools total)
- Community-contributed MCP servers
- Domain-specific tool libraries
- Integration with major HPC centers
- **Community Optimizer Marketplace**: Share optimized variants across users

---

## Implementation Details (Internal)

ClaudIO's internal implementation uses lightweight libraries for agent orchestration and optimization:
- **Agent patterns**: Implemented via lightweight Python library (internal detail)
- **Reasoning patterns**: Chain-of-Thought and ReAct (architecture, not library)
- **Signatures**: Declarative input/output specs (concept, not tied to specific library)
- **ARC Memory**: Custom implementation with LRU cache, B-tree index, IOWarp CTE integration
- **Optimizers**: Internally leverage DSPy teleprompters (BootstrapFewShot, MIPROv2) wrapped in ClaudIOOptimizer API

These are implementation details - users interact with ClaudIO through:
- CLI commands (including `/tune` for offline optimization)
- Python API (`from claudio import ClaudIO`, `from claudio.arc import ARC`)
- REST API (planned for v0.5.0)
- A2A protocol (for agent-to-agent communication)

---

## Related Documentation

- [System Identity](SYSTEM_IDENTITY.md) - ClaudIO identity, capabilities, and design principles
- [ARC Memory Layer](ARC_MEMORY_LAYER.md) - Deep dive on memory architecture, indexing, IOWarp integration
- [Optimizer Guide](OPTIMIZER_GUIDE.md) - Self-improvement, tuning workflows, community optimizers
- [A2A Protocol Specification](A2A_PROTOCOL.md) - Agent-to-Agent communication (v0.2.0+)
- [IOWarp Architecture](https://iowarp.ai/docs) - Full IOWarp 3-tier architecture (CEI/CAE/CTE)
- [MCP Protocol Specification](https://modelcontextprotocol.io) - Model Context Protocol docs

---

**Version**: 4.0 (ClaudIO Self-Improving Autonomous Agent)
**Last Updated**: 2025-01-09
**Focus**: 3-Tier Orchestration + **ARC Memory** + **Optimizer Layer** + Agent Registry + A2A Protocol + IOWarp Integration
