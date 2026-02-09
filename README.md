# CLIO Agent

**Cognitive Layer for Adaptive Universal Data & Intelligent Operations**

Autonomous agent for scientific data management. **IOWarp Intelligence Layer (CEI).**

[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![UV](https://img.shields.io/badge/UV-enabled-orange.svg)](https://github.com/astral-sh/uv)
[![FastMCP](https://img.shields.io/badge/FastMCP-enabled-purple.svg)](https://github.com/jlowin/fastmcp)
[![IOWarp](https://img.shields.io/badge/IOWarp-Intelligence%20Layer-teal.svg)](https://iowarp.org)

---

## What is CLIO Agent?

CLIO Agent is an **autonomous agent specialized in scientific data management** for HPC and research workflows. It operates as the Intelligence Layer (CEI) within IOWarp's 3-tier architecture.

**Core Capabilities:**
- 🤖 **3-Tier Agent Orchestration**: Main agent → Expert agents → Ephemeral nanoagents
- 🧠 **Native Memory (ARC)**: O(log N) context retrieval, IOWarp-backed persistent storage
- 📈 **Self-Improving**: Offline tuning + online learning via Optimizer Layer
- 🔌 **Agent Registry**: Integrate agents from ANY framework (LangChain, CrewAI, AutoGen)
- 🔗 **A2A Protocol**: Agent-to-agent communication for external collaboration
- 🛠️ **FastMCP Tools**: Access 15+ MCP servers for scientific infrastructure
- 🗄️ **IOWarp Integration**: CEI (Intelligence + ARC) → CAE/PPI (Tools) → CTE (Storage + ARC Persistence)
- 🏠 **Model Flexibility**: Any LLM API (cloud, local, custom models)

### Why CLIO Agent?

**CLIO Agent is NOT** a framework for building agents - it **IS** the agent.

Think of CLIO Agent as a specialized colleague for scientific data:
- Works standalone via CLI
- Collaborates with general agents (Claude Code, Gemini) as a science sidekick
- Integrates experts built with any framework through the Agent Registry
- Spawns ephemeral nanoagents for parallelized sub-tasks

---

## Architecture

### The Big Picture

```
┌────────────────────────────────────────────────────────────┐
│  USER INTERFACES (AI Gateway)                              │
│  • CLI (current)  • REST API (planned)  • A2A Protocol     │
└───────────────────────────┬────────────────────────────────┘
                            │
┌───────────────────────────▼────────────────────────────────┐
│  INTELLIGENCE LAYER (CEI) - CLIO Agent + ARC Memory           │
│                                                             │
│  ┌──────────────────────────────────────────────────────┐ │
│  │  CLIO Agent Orchestrator (Tier 1)                       │ │
│  │   → Agent Registry coordination                      │ │
│  │   → ARC Memory queries (O(log N))                    │ │
│  │   → Capability-based routing                         │ │
│  └────────────┬─────────────────────────────────────────┘ │
│               │                                            │
│    ┌──────────┼──────────┬──────────────┐                 │
│    ▼          ▼          ▼              ▼                 │
│  ┌──────┐ ┌──────┐ ┌──────────┐  ┌──────────────┐        │
│  │Data  │ │HPC   │ │Research  │  │External      │        │
│  │Expert│ │Expert│ │Expert    │  │Agents        │        │
│  │(T2)  │ │(T2)  │ │(T2)      │  │(via A2A)     │        │
│  └──┬───┘ └──┬───┘ └──┬───────┘  └──────────────┘        │
│     │        │        │                                   │
│     │  All tiers read/write ARC for coordination          │
│     └────────┴────────┘                                   │
│              │                                             │
│        ┌─────┴──────┐                                     │
│        ▼            ▼                                     │
│   ┌─────────────────────────┐   ┌────────────────────┐   │
│   │ Nanoagents (Tier 3)     │   │ ARC Memory         │   │
│   │ • Ephemeral workers     │   │ • In-mem cache     │   │
│   └─────────────────────────┘   │ • B-tree index     │   │
│                                  │ • O(log N) search  │   │
│                                  └────────────────────┘   │
└───────────────────────┬────────────────────────────────────┘
                        │
    ┌───────────────────┼──────────────────┐
    ▼                   ▼                  ▼
┌─────────────┐  ┌──────────────┐  ┌────────────────────┐
│ CAE/PPI     │  │ OPTIMIZER    │  │ ARC Persistent     │
│ (Tools)     │  │ LAYER        │  │ Store (CTE)        │
│             │  │              │  │                    │
│ FastMCP     │  │ • Prompt     │  │ IOWarp Namespace:  │
│ 15+ servers │  │   Optimizers │  │ /clio_agent/arc/*     │
│ 150+ tools  │  │ • Routing    │  │                    │
│ (Phase 4+)  │  │   Optimizers │  │ • Conversations    │
│             │  │ • Offline    │  │ • Metrics          │
│             │  │   Tuning     │  │ • Invocations      │
│             │  │ • Online     │  │ • Context          │
│             │  │   Learning   │  │                    │
└─────────────┘  └──────────────┘  └────────────────────┘
                        │
                        ▼
        ┌───────────────────────────────────┐
        │  CTE - Hermes Multi-Tier Storage  │
        │  GPU → NVMe → PFS → Object Store  │
        └───────────────────────────────────┘
```

### Example Flow: Data Optimization

```
User: "Optimize my 100GB HDF5 file"
    ↓
CLIO Agent Main Agent (Tier 1)
    → Query: "Need HDF5 optimization capability"
    → Agent Registry: Find DataExpert
    ↓
DataExpert (Tier 2) - ReAct Pattern
    Thought: "Need to analyze current state"
    Action: hdf5_analyze("/data/file.h5") via CAE/PPI
    Observation: {"compression": "none", "size": "100GB"}

    Thought: "No compression! Should apply gzip-6"
    Action: hdf5_optimize(strategy="gzip-6")
    Observation: {"new_size": "45GB", "ratio": "2.2x"}
    ↓
Result: "Applied gzip-6 compression: 100GB → 45GB (2.2x reduction)"
```

---

## Quick Start

### Prerequisites

- **Python 3.12+**
- **UV** package manager (recommended):
  ```bash
  curl -LsSf https://astral.sh/uv/install.sh | sh
  ```
- **LM Studio or Ollama** (for local models) OR API key for cloud LLM

### Installation & Setup

```bash
# Clone repository
git clone https://github.com/iowarp/clio-agent
cd clio-agent

# Option 1: Local Development (LM Studio)
# Start LM Studio and load a model (e.g., gpt-oss-20b, granite-4-h-tiny)

# Option 2: Local Production (Ollama)
ollama run gpt-oss-20b

# Option 3: Cloud (OpenAI, Anthropic, etc.)
export OPENAI_API_KEY="your-key-here"
```

### Running CLIO Agent

```bash
# Test configuration
uv run src/clio_agent/config.py

# Test main agent
uv run src/clio_agent/agent.py

# Test data expert
uv run src/clio_agent/experts/data_expert.py

# Launch interactive CLI
uv run src/clio_agent/ui/cli.py
```

### Example Interaction

```
$ uv run src/clio_agent/ui/cli.py

╭─ CLIO Agent - IOWarp Intelligence Layer ─╮
│ Autonomous Data Management Agent      │
│ Type /help for commands                │
╰────────────────────────────────────────╯

You: How do I optimize my 100GB HDF5 file for parallel I/O?

CLIO Agent via DataExpert:

Based on analysis, here are optimization recommendations:

1. **Compression** (Priority: High)
   • Apply gzip-6 compression (2-3x reduction expected)
   • Alternative: blosc for faster parallel decompression
   • Expected size: 33-50GB

2. **Chunking** (Priority: High)
   • Enable automatic chunking for parallel access
   • Recommended chunk size: 1-10MB (match access pattern)
   • Benefits: Enables MPI-IO collective operations

3. **Parallel I/O Strategy**
   • Use HDF5's MPI-IO mode on HPC cluster
   • Configure collective buffering
   • Expected improvement: 5-10x faster parallel writes

Would you like me to generate the optimization script?
```

---

## Core Concepts

### 1. Agent Registry (Capability-Based Coordination)

The **Agent Registry** is CLIO Agent's coordination layer for discovering and routing to agents:

- **Native Experts**: Built-in specialist agents (DataExpert, HPCExpert, etc.)
- **External Agents**: Agents from ANY framework (LangChain, CrewAI, AutoGen)
- **Capability Matching**: Route queries based on agent capabilities, not hardcoded rules
- **Dynamic Discovery**: Agents register at runtime with their capabilities

**How it works:**
```
1. User query arrives
2. CLIO Agent extracts needed capabilities (e.g., "HDF5", "optimization")
3. Query registry for agents matching capabilities
4. Rank by capability overlap + agent tier
5. Route to best agent (native OR external via A2A)
```

### 2. A2A Protocol (Agent-to-Agent Communication)

The **A2A Protocol** enables CLIO Agent to integrate with any agent framework:

- **Standardized Interface**: All agents communicate via A2A protocol
- **Framework Agnostic**: Works with LangChain, CrewAI, AutoGen, custom agents
- **Bidirectional**: CLIO Agent can call external agents OR be called as a sidekick
- **Compilation**: Registry compiles external agents into CLIO Agent-compatible instances

**Example - CLIO Agent as Sidekick:**
```
Claude Code (general agent) receives science question
    ↓ (A2A Request to CLIO Agent)
CLIO Agent DataExpert handles HDF5 optimization
    ↓ (A2A Response)
Claude Code assembles final answer with CLIO Agent's expertise
```

### 3. 3-Tier Agent Hierarchy

CLIO Agent uses a **3-tier architecture** for scalability and efficiency:

- **Tier 1**: CLIO Agent Main Agent (orchestrator, routing, context management)
- **Tier 2**: Expert Agents (persistent specialists like DataExpert, HPCExpert)
- **Tier 3**: Nanoagents (ephemeral workers spawned for specific tasks)

**Why 3 tiers?**
- **Separation of concerns**: Orchestration vs. expertise vs. execution
- **Scalability**: Spawn hundreds of nanoagents for parallel tasks
- **Resource efficiency**: T3 agents auto-terminate after completion
- **Flexibility**: Experts decide when to spawn nanoagents vs. execute directly

### 4. IOWarp Integration (CEI/CAE/CTE)

CLIO Agent is the **Intelligence Layer (CEI)** of IOWarp's full stack:

- **CEI (Context Exploration Interface)**: CLIO Agent main agent + experts + ARC memory
- **CAE/PPI (Content Assimilation + Plugin Interface)**: FastMCP tools layer
- **CTE (Context Transfer Engine)**: Hermes multi-tier storage layer + ARC persistence

This integration enables CLIO Agent to:
- Call scientific tools via MCP (HDF5, SLURM, etc.)
- Optimize data movement across storage tiers (GPU → NVMe → PFS)
- Coordinate with IOWarp's intelligent prefetching and caching
- Persist ARC memory across IOWarp storage hierarchy

### 5. ARC (Agent Runtime Context) - Memory Layer

The **ARC Memory Layer** is CLIO Agent's native, high-performance memory system:

- **O(log N) Retrieval**: B-tree indexing for fast context search
- **In-Memory Cache**: LRU cache for hot data (active conversations)
- **IOWarp CTE Integration**: Persistent storage in `/clio_agent/arc/*` namespace
- **Multi-Tier Storage**: Hot (GPU) → Warm (NVMe) → Cold (PFS) → Archive (Object)

**What ARC Stores:**
```
/conversations/<session_id>/   # Full conversation history
/invocations/<trace_id>/        # Expert & nanoagent execution traces
/metrics/<agent_id>/            # Performance metrics, success rates
/context/<domain>/              # Retrieved docs, tool results, patterns
```

**Why ARC Matters:**
- **Continuous Learning**: Track what works, what doesn't
- **Context Preservation**: Resume conversations seamlessly
- **Performance Analytics**: Identify optimization opportunities
- **Agent Coordination**: Experts share context via ARC

### 6. Optimizer Layer - Self-Improvement

The **Optimizer Layer** is CLIO Agent's learning system for continuous improvement:

- **Offline Tuning Mode**: User runs optimization sessions to tune agents
- **Online Learning Mode**: Automatic improvement during operation
- **Community Optimizers**: User-contributed and domain-specific optimizers

**Optimizer Types:**
```
1. Prompt Optimizers
   - Few-shot example selection
   - Reasoning chain construction
   - Signature refinement

2. Routing Optimizers
   - Expert selection logic tuning
   - Capability matching rules

3. Tool Selection Optimizers
   - When to use which MCP tool
   - Tool parameter tuning
```

**How It Works:**
```
1. Offline Tuning:
   User: "uv run src/clio_agent/ui/cli.py --tune"
   → Provide training examples
   → Select optimizer (BootstrapFewShot, MIPRO, etc.)
   → Run optimization (minutes to hours)
   → Deploy optimized prompts

2. Online Learning:
   → Capture metrics during operation (stored in ARC)
   → A/B test prompt variants
   → Gradual improvement based on success rates
   → Automatic optimization triggers
```

**Why Optimizers Matter:**
- **Super Tunable**: Customize CLIO Agent for your specific domain
- **Self-Improving**: Gets better with use
- **Data-Driven**: Optimizations based on actual performance
- **Extensible**: Add custom optimizers for unique needs

---

## Project Structure

```
src/clio_agent/
├── config.py                 # LM configuration (any provider)
├── agent.py                # Main agent orchestrator (Tier 1)
├── signatures/               # Input/output specifications
│   ├── main_agent_sig.py     # Routing signature
│   └── expert_sig.py         # Expert signatures
├── experts/                  # Domain expert agents (Tier 2)
│   └── data_expert.py        # DataExpert (HDF5, ADIOS, Parquet) ✅
├── registry/                 # Agent Registry
│   ├── registry.py           # Capability-based routing
│   └── capability_matcher.py # Query -> expert matching
├── arc/                      # Memory layer
│   ├── memory.py             # Core ARC implementation
│   ├── cache.py              # LRU cache
│   ├── index.py              # B-tree indexing for O(log N)
│   ├── lsm.py                # LSM tree for metrics
│   ├── schema.py             # Data schemas
│   ├── storage.py            # IOWarp CTE integration
│   ├── retrieval.py          # Context retrieval
│   └── coordinator.py        # Multi-agent coordination
├── optimizers/               # Learning layer (Phase 3+)
│   ├── base.py               # CLIO AgentOptimizer base class
│   ├── prompt_opt.py         # Prompt optimization
│   ├── routing_opt.py        # Routing optimization
│   ├── tool_opt.py           # Tool selection optimization
│   └── metrics.py            # Performance tracking
├── tools/                    # FastMCP integration (CAE/PPI)
│   ├── mcp_connector.py      # MCP bridge (being replaced Phase 1)
│   └── servers/              # MCP server implementations
│       └── hdf5_server.py    # HDF5 operations (stub)
└── ui/
    ├── cli.py                # Interactive CLI ✅
    └── api.py                # REST API (Phase 4)

docs/
├── SYSTEM_IDENTITY.md        # CLIO Agent identity and capabilities
├── CLIO_AGENT_ARCHITECTURE.md   # Full architecture documentation
├── MCP_TOOL_INTEGRATION.md   # MCP integration patterns
└── EXPERT_SYSTEM_DESIGN.md   # Expert agent design guide

tests/
├── test_core/                # Core functionality tests
├── test_arc/                 # Memory layer tests
├── test_optimizers/          # Optimizer tests
├── test_experts/             # Expert agent tests
└── test_integration/         # End-to-end tests
```

**Total**: ~8,200+ lines of intelligent, self-improving agent code

---

## CLI Commands

```
/help      - Show available commands
/experts   - List registered agents (native + external)
/registry  - Show Agent Registry status
/memory    - Show ARC memory statistics
/metrics   - Show agent performance metrics
/verbose   - Toggle reasoning trace display
/history   - Show conversation history
/clear     - Clear history
/tune      - Enter offline tuning mode (Phase 3)
/quit      - Exit
```

**Offline Tuning Mode** (Phase 3):
```bash
# Launch in tuning mode
uv run src/clio_agent/ui/cli.py --tune

# Interactive tuning workflow:
# 1. Select component to optimize (routing, expert prompts, tools)
# 2. Provide training examples
# 3. Choose optimizer (BootstrapFewShot, MIPRO, custom)
# 4. Run optimization session
# 5. Evaluate and deploy
```

---

## Model Configuration

CLIO Agent supports **any LLM provider**:

### Local Development (LM Studio)
```python
from clio_agent.config import setup_dspy

lm = setup_dspy(provider="lm_studio")
# Models: gpt-oss-20b, granite-4-h-tiny, etc.
# Cost: FREE
# Privacy: Data never leaves your machine
```

### Local Production (Ollama)
```python
lm = setup_dspy(provider="ollama", model="llama3.1:8b")
# Fully local, zero-cost inference
# Privacy-preserving for sensitive HPC data
```

### Cloud Providers
```python
# OpenAI
lm = setup_dspy(provider="openai", model="gpt-4")

# Anthropic
lm = setup_dspy(provider="anthropic", model="claude-3-5-sonnet-20241022")

# Google
lm = setup_dspy(provider="google", model="gemini-pro")
```

### Custom/Fine-tuned Models
```python
lm = setup_dspy(provider="custom", endpoint="http://my-model:8000")
# Support for domain-specific fine-tuned models
```

**Dual-LM Configuration**:
- Router (Tier 1): Lower temperature (0.3), deterministic
- Reasoner (Tier 2): Higher temperature (1.0), creative

---

## Current Status

### ✅ Working Now (Feb 2026)

- **Main Agent (Tier 1)**: CoT mode orchestration with conversation management
- **DataExpert (Tier 2)**: CoT mode specialist for HDF5, ADIOS, Parquet
- **ARC Memory (90% complete)**: LRU cache, B-tree index, LSM tree for metrics
- **Agent Registry**: Keyword-based capability routing (being upgraded to typed routing)
- **CLI**: Rich TUI interactive interface
- **LM Config**: LM Studio, Ollama, OpenAI, Anthropic, custom endpoints

### 🚧 Phase 1: Foundation Reset (Current)

**Goals**: Real MCP integration, DSPy 3.x patterns, clean up stubs

- Replace mcp_connector.py with FastMCP 3.x gateway + mount()
- Implement real HDF5 MCP server (FastMCP 3.x)
- Integrate DSPy 3.x ReAct + ChatAdapter for LM Studio compatibility
- Delete mcp_connector.py (789 lines), clean up stub code
- Raise test coverage to 50%

**See PLAN.md for detailed task list**

### 📋 Upcoming Phases

**Phase 2: Multi-Expert System**
- HPCExpert (SLURM, PBS, resource management)
- Typed routing with Literal outputs (optimizable by DSPy)
- Context compilation pipeline (filter -> compact -> enrich -> assemble)
- Tool curation (max 5-7 tools per expert with agent stories)

**Phase 3: Self-Improvement Layer**
- SIMBA optimizer for agentic workflows
- Training data collection in ARC
- Offline tuning mode via CLI
- Statistical validation before deploying optimized variants

**Phase 4: Production Hardening**
- REST API (FastAPI)
- CI/CD pipeline
- Docker/Singularity containers
- Test coverage to 80%

**Phase 5: IOWarp CTE Integration**
- ARC persistent storage in IOWarp namespace
- Multi-tier data movement (GPU → NVMe → PFS → Object)
- Tool result caching across storage tiers

**Phase 6: Advanced Features**
- Online learning mode (A/B testing, auto-optimization)
- A2A protocol for external agent integration
- Additional experts (ResearchExpert, VisualizationExpert, etc.)

---

## Deployment Modes

CLIO Agent supports multiple deployment patterns:

### 1. Standalone CLI
```bash
uv run src/clio_agent/ui/cli.py
# Interactive command-line interface
```

### 2. Python Library (Planned)
```python
from clio_agent import ClioAgent

agent = ClioAgent()
response = agent.query("How do I optimize this HDF5 file?")
print(response)
```

### 3. REST API (Planned)
```bash
# Start API server
uv run src/clio_agent/ui/api.py --port 8000

# Query via HTTP
curl -X POST http://localhost:8000/query \
  -H "Content-Type: application/json" \
  -d '{"query": "Optimize HDF5 file", "context": {}}'
```

### 4. Container (Planned)
```dockerfile
FROM python:3.12-slim
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
COPY . /app
WORKDIR /app
CMD ["uv", "run", "src/clio_agent/ui/api.py"]
```

### 5. A2A Integration External Agents (Planned)
```python
# CLIO Agent as sidekick to another agent
from external_agent import ClaudeCode

claude_code = ClaudeCode()
claude_code.register_sidekick("clio-agent", a2a_endpoint="http://localhost:8000/a2a")

# ClaudeCode can now delegate science questions to CLIO Agent
```

---

## Use Cases

### Scientific Computing

- **HDF5 Optimization**: Analyze and optimize large scientific datasets
- **I/O Performance**: Profile and improve data access patterns
- **Workflow Automation**: Coordinate SLURM jobs, data processing pipelines
- **Data Migration**: Convert between formats (HDF5 ↔ ADIOS ↔ Parquet)

### HPC Operations

- **Job Management**: SLURM job submission, monitoring, optimization
- **Performance Analysis**: Darshan I/O profiling integration
- **Resource Allocation**: Intelligent node/core selection
- **Workflow Orchestration**: Nextflow, Parsl, CWL integration

### Research Assistance

- **Literature Search**: ArXiv, PubMed integration (via ResearchExpert)
- **Data Analysis**: Statistical analysis, visualization recommendations
- **Reproducibility**: Generate workflow specifications, track provenance

---

## Documentation

- **[PLAN.md](PLAN.md)** - Development roadmap and current phase tasks
- **[docs/SYSTEM_IDENTITY.md](docs/SYSTEM_IDENTITY.md)** - CLIO Agent capabilities, identity, design principles
- **[docs/CLIO_AGENT_ARCHITECTURE.md](docs/CLIO_AGENT_ARCHITECTURE.md)** - Full architecture, 3-tier orchestration, ARC memory
- **[CLAUDE.md](CLAUDE.md)** - Developer quick reference

**External Resources:**
- [IOWarp Architecture](https://iowarp.ai/docs) - Full IOWarp Docs
- [MCP Protocol](https://modelcontextprotocol.io) - Model Context Protocol specification
- [UV Documentation](https://github.com/astral-sh/uv) - UV package manager

---

## Technologies

**Core Stack:**
- **[FastMCP 3.x](https://github.com/jlowin/fastmcp)** - Model Context Protocol for tool integration
- **[UV](https://github.com/astral-sh/uv)** - Fast Python package manager (10-100x faster than pip)
- **[DSPy 3.x](https://github.com/stanfordnlp/dspy)** - DSPy: Programming—not prompting—Foundation Models
- **[Rich](https://rich.readthedocs.io)** - Terminal UI framework
- **[IOWarp](https://github.com/iowarp/iowarp-install)** - Context Management Platform Infrastructure for Autonomous AI Agents

**Agent Frameworks Suggested for Integration in the Future (via A2A Protocol):**
- **[LangChain](https://langchain.com)** - External agents via A2A adapter
- **[CrewAI](https://crewai.com)** - Multi-agent teams integration
- **[AutoGen](https://microsoft.github.io/autogen/)** - Microsoft AutoGen agents

**LLM Providers Supported:**
- LM Studio (local)
- Ollama (local)
- OpenAI (cloud)
- Anthropic (cloud)
- Google (cloud)
- Custom endpoints

---

## Contributing

CLIO Agent is research/development code as part of the IOWarp project.

For development:
1. Read [docs/CLIO_AGENT_ARCHITECTURE.md](docs/CLIO_AGENT_ARCHITECTURE.md) for architecture overview
2. Study [docs/SYSTEM_IDENTITY.md](docs/SYSTEM_IDENTITY.md) for design principles
3. Explore [src/clio_agent/experts/data_expert.py](src/clio_agent/experts/data_expert.py) for agent patterns
4. Test with: `uv run src/clio_agent/ui/cli.py`

**Development Priorities (Phase 1):**
- Real HDF5 MCP server (FastMCP 3.x)
- DSPy 3.x ReAct + ChatAdapter integration
- Delete mcp_connector.py, clean up stubs
- Test coverage to 50%

---

## Citation

If you use CLIO Agent in your research, please cite:

```bibtex
@software{clioagent2025,
  title={CLIO Agent: Autonomous Agent for Scientific Data Management},
  author={IOWarp Team},
  year={2025},
  url={https://github.com/iowarp/clio-agent}
}
```

---

**CLIO Agent**: Autonomous agent for scientific data management. Intelligence Layer (CEI) of IOWarp. Built for researchers, by researchers.

**Core Innovation**: 3-Tier Orchestration + ARC Memory + Optimizer Layer + FastMCP Tools + IOWarp Integration
