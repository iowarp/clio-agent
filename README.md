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

**Experimental v0.2 note:**
The reliable path today is a local-filesystem-first harness for `HDF5`, `Parquet`, and `CSV`
inspection through the FastMCP gateway, CLI/API, explicit file policy, and `doctor` runtime
reporting. Full `clio-core`/CTE runtime control and broader A2A integration remain future work
behind explicit external-process boundaries.

**Core Capabilities:**
- 🤖 **Multi-Expert Orchestration**: Main agent → DataExpert / AnalysisExpert / VisualizationExpert
- 🧠 **Native Memory (ARC)**: O(log N) context retrieval with local-first persistence
- 📈 **Self-Improving**: Offline tuning + instrumentation via the Optimizer Layer
- 🔌 **Agent Registry**: Route built-in experts today, with explicit external integration boundaries
- 🩺 **Runtime Doctor**: Report LM, gateway, HDF5, Parquet, API, file policy, and `clio-core` truth
- 🛠️ **FastMCP Tools**: Access real HDF5 and Parquet tools through a namespaced gateway
- 🗄️ **IOWarp Integration Boundary**: Probe `clio-core` repo/config/binary readiness without starting services
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
│  • CLI (current)  • REST API (current)  • A2A (future)     │
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
│  │Data  │ │Analysis│ │Visualization│ │External   │       │
│  │Expert│ │Expert  │ │Expert       │ │Agents     │       │
│  │(T2)  │ │(T2)    │ │(T2)         │ │(future)   │       │
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
│ FastMCP     │  │ • Prompt     │  │ Local-first ARC /  │
│ local tools │  │   Optimizers │  │ future CTE         │
│ gateway     │  │ • Routing    │  │                    │
│ (current)   │  │   Optimizers │  │ • Conversations    │
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

# Install dependencies
uv sync --extra dev --extra api --extra optimizers

# Optional homelab profile for local LM Studio on Dynamo
source scripts/homelab-env.sh
clio_homelab_use dynamo-lms

# Constrain local file access for tool execution
export CLIO_ALLOWED_ROOTS=/home/akougkas/iowarp/clio-agent:/tmp

# Inspect runtime truth before using the agent
uv run src/clio_agent/ui/cli.py doctor

# Create deterministic demo data
uv run scripts/create_demo_data.py --output-dir /tmp/clio-agent-demo
```

### Running CLIO Agent

```bash
# Launch interactive CLI
uv run src/clio_agent/ui/cli.py

# Launch REST API
uv run src/clio_agent/ui/api.py --host 127.0.0.1 --port 8000
```

### Example Interaction

```
$ uv run src/clio_agent/ui/cli.py

╭─ CLIO Agent - IOWarp Intelligence Layer ─╮
│ Autonomous Data Management Agent      │
│ Type /help for commands                │
╰────────────────────────────────────────╯

You: What datasets are in /tmp/clio-agent-demo/clio_demo.h5?

CLIO Agent via DataExpert:

Found 3 datasets:

1. `/simulation/temperature` - shape `(120, 80)`, gzip-6 compressed
2. `/simulation/pressure` - shape `(120, 80)`, chunked
3. `/time_step` - shape `(120,)`
```

---

## From-scratch deploy: TUI + Agent on a fresh machine

End-to-end recipe for a box that has nothing installed yet. Three things to
install (`uv`, Go, an LLM), two repos to clone (`clio-agent` on
`tui-integration`, `gact-tui` on `clio`), one command to launch the TUI.

### 0. System prerequisites

A real terminal (256-color), a working network, and:

```sh
# Python ≥ 3.12 — most current distros ship 3.12. Verify:
python3 --version

# Go ≥ 1.25 (for building gact-tui)
#   Debian/Ubuntu:   sudo apt install -y golang
#   Fedora/RHEL:     sudo dnf install -y golang
#   macOS:           brew install go
#   anywhere:        https://go.dev/dl/   (or asdf / gvm)
go version
```

If `python3 --version` reports < 3.12, install 3.12 first (`uv python install
3.12` works once `uv` is on disk).

### 1. Install `uv`

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"   # add to ~/.bashrc / ~/.zshrc
```

### 2. Install `clio-agent` (tui-integration branch)

```sh
git clone -b tui-integration https://github.com/iowarp/clio-agent
cd clio-agent
uv venv --python 3.12
source .venv/bin/activate
uv pip install -e '.[api]'             # or '.[all]' for optimizers + argonne + iowarp
```

You should now have three console scripts on `$PATH`:

| Script              | Purpose                                              |
| ------------------- | ---------------------------------------------------- |
| `clio-agent`        | Interactive CLI (smoke test)                         |
| `clio-agent-api`    | REST API on its own port                             |
| `clio-agent-gact`   | GACT v0.2 server — the surface `gact-tui` talks to   |

Verify: `which clio-agent-gact` should point inside the venv you just created.

### 3. Install `gact-tui` (clio branch)

```sh
cd ..
git clone -b clio https://github.com/iowarp/gact-tui
cd gact-tui
make build && make install            # → ~/.local/bin/{gact,emulator-server}
gact version
```

### 4. Pick an LLM (one of three)

Set the env in the **same shell** you'll deploy from — `gact agent deploy`
inherits your env when spawning the detached `clio-agent-gact` process.

**A) Local LM Studio** — free, private, runs on your box.

```sh
# 1. Launch LM Studio, load a model, start its local server (default :1234).
# 2. Point CLIO at it:
export CLIO_LM_PROVIDER=lm_studio
export CLIO_LM_API_BASE=http://127.0.0.1:1234/v1
export CLIO_LM_MODEL=<id-shown-in-lm-studio>
```

**B) Claude Max via Meridian** — bring your own Anthropic subscription. Full
recipe in [docs/providers/meridian.md](docs/providers/meridian.md). Short form:

```sh
npm install -g @rynfar/meridian
CLAUDE_CONFIG_DIR="$HOME/.claude" meridian &
export CLIO_LM_PROVIDER=openai
export CLIO_LM_API_BASE=http://127.0.0.1:3456/v1
export CLIO_LM_MODEL=claude-haiku-4-5-20251001
export CLIO_LM_API_KEY=x               # any non-empty string
```

**C) Argonne ALCF inference gateway** — Sophia vLLM via Globus Auth. Needs
`uv pip install -e '.[argonne]'` and an ALCF account.

```sh
export CLIO_LM_PROVIDER=argonne
export CLIO_LM_API_BASE=https://inference-api.alcf.anl.gov/resource_server/sophia/vllm/v1
export CLIO_LM_MODEL=meta-llama/Meta-Llama-3.1-70B-Instruct
# First call triggers Globus device-flow login; the bearer is refreshed
# automatically on 401. See src/clio_agent/providers/argonne_auth.py.
```

### 5. Smoke-test the agent on its own

```sh
clio-agent --query "hello, name yourself"
```

A greeting plus an expert label means the LLM + agent are wired up. If this
fails, the TUI step will fail too — fix it here first.

### 6. Deploy through the TUI

```sh
gact agent deploy clio my-clio        # spawns clio-agent-gact detached
gact connect my-clio                  # opens the TUI
```

Inside the TUI:

- **Ctrl+S** → Settings → Model tab → *Change provider…* swaps the running
  session to a different provider/model without restarting the agent.
- **Ctrl+Z** detaches the TUI; the agent keeps running. `gact resume` to
  reattach where you left off.
- **`/help`** in the input bar shows the full slash palette
  (`/doctor`, `/memory`, `/experts`, `/metrics`, …).

When you're done:

```sh
gact agent stop my-clio
```

### Troubleshooting

| Symptom                                          | Likely cause                                                                                                                  |
| ------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------ |
| `/doctor` shows `agent: unavailable`             | `CLIO_LM_*` env wasn't set in the shell that ran `gact agent deploy`. `env \| grep CLIO_LM_` to confirm; re-export and redeploy. |
| `gact: command not found`                        | `~/.local/bin` isn't on `$PATH`. Add it to your shell rc.                                                                       |
| `clio-agent-gact: command not found`             | Wrong venv active. `source clio-agent/.venv/bin/activate` then `which clio-agent-gact`.                                          |
| TUI shows old behaviour after editing source     | `clio-agent-gact` runs as a detached child. `gact agent stop my-clio && gact agent deploy clio my-clio` to pick up edits.        |
| 400 from provider on certain model ids           | Provider-specific quirk — see `PROVIDER_DEFAULTS` in `src/clio_agent/config.py`. Add a capability flag, don't branch on name.    |

---

## Core Concepts

### 1. Agent Registry (Capability-Based Coordination)

The **Agent Registry** is CLIO Agent's coordination layer for discovering and routing to agents:

- **Native Experts**: Built-in specialists (DataExpert, AnalysisExpert, VisualizationExpert)
- **External Agents**: Future explicit integration boundary, not the default v0.2 path
- **Capability Matching**: Route queries based on agent capabilities, not hardcoded rules
- **Dynamic Discovery**: Agents register at runtime with their capabilities

**How it works:**
```
1. User query arrives
2. CLIO Agent extracts needed capabilities (e.g., "HDF5", "optimization")
3. Query registry for agents matching capabilities
4. Rank by capability overlap + agent tier
5. Route to the best built-in expert for the current runtime
```

### 2. A2A Protocol (Agent-to-Agent Communication)

The **A2A Protocol** remains a future integration boundary for CLIO Agent.
It is not the primary runtime path in the current experimental `v0.2` branch.

- **Current focus**: built-in experts + explicit local filesystem workflows
- **Future direction**: external agent communication via stable process/protocol boundaries
- **Constraint**: no broad external-agent runtime is enabled by default today

**Future Example - CLIO Agent as Sidekick:**
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
- **Tier 2**: Expert Agents (persistent specialists like DataExpert, AnalysisExpert, VisualizationExpert)
- **Tier 3**: Future ephemeral workers for specialized sub-tasks

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
- **Local-First Persistence**: Persistent storage under `.clio_agent/` today, with CTE later
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
├── agent.py                  # Main agent orchestrator
├── runtime/                  # Doctor and runtime status reporting
├── signatures/               # Input/output specifications
├── experts/                  # Data, analysis, visualization experts
├── registry/                 # Agent registry and capability routing
├── arc/                      # Memory layer
├── optimizer/                # Instrumentation, training, variants, runner
├── tools/                    # Gateway, file policy, execution boundary
│   └── servers/              # HDF5 and Parquet MCP servers
└── ui/
    ├── cli.py                # Interactive CLI and `doctor`
    └── api.py                # FastAPI REST API

docs/
├── CLIO_AGENT_ARCHITECTURE.md   # Full architecture documentation
├── CONTRIBUTOR_QUICKSTART.md    # Fast contributor path
├── MCP_TOOL_INTEGRATION.md   # MCP integration patterns
└── SYSTEM_IDENTITY.md        # CLIO Agent identity and capabilities

tests/
├── test_core/                # Core functionality tests
├── test_arc/                 # Memory layer tests
├── test_experts/             # Expert agent tests
├── test_tools/               # Gateway and tool server tests
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
/tools     - Show available MCP tools
/doctor    - Show runtime integration status
/metrics   - Show agent performance metrics
/verbose   - Toggle reasoning trace display
/history   - Show conversation history
/clear     - Clear history
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

### ✅ Working Now (Apr 2026)

- **Main Agent + Experts**: DataExpert, AnalysisExpert, and VisualizationExpert are wired into routing
- **FastMCP Gateway**: Real HDF5 and Parquet tools plus safe CSV inspection paths
- **Runtime Doctor**: CLI/API health reporting for LM, gateway, HDF5, Parquet, API, file policy, and `clio-core`
- **File Policy**: Explicit local file access boundaries via `CLIO_ALLOWED_ROOTS`
- **API + CLI**: Interactive CLI, `/health`, `/query`, `/experts`, and `/metrics`
- **LM Config**: LM Studio, Ollama, OpenAI-compatible, Anthropic, and OpenAI providers

### 🚧 Current Boundaries

- **Local-filesystem-first**: current reliable product path is local HDF5/Parquet/CSV inspection
- **clio-core probe only**: repo/config/binary discovery is non-destructive and does not start services
- **A2A later**: external agent integration remains future work
- **No broad HPC mutation path yet**: scheduler and service-control workflows remain out of scope for v0.2

### 📋 Near-Term Direction

- deeper external-process/config integration with `clio-core`
- more live health probes and degraded-state reporting
- broader tool coverage after the local harness path is hardened

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

### 3. REST API (Current)
```bash
# Start API server
uv run src/clio_agent/ui/api.py --host 127.0.0.1 --port 8000

# Query via HTTP
curl -X POST http://localhost:8000/query \
  -H "Content-Type: application/json" \
  -d '{"question": "What datasets are in /tmp/clio-agent-demo/clio_demo.h5?"}'
```

### 4. Container (Experimental)
```dockerfile
FROM python:3.12-slim
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
COPY . /app
WORKDIR /app
CMD ["uv", "run", "src/clio_agent/ui/api.py"]
```

### 5. A2A Integration External Agents (Future)
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

- **Data Analysis**: Local Parquet/CSV statistics and schema inspection
- **Visualization**: Generate local charts and summaries from supported datasets
- **Reproducibility**: Generate workflow specifications, track provenance

---

## Documentation

- **[PLAN.md](PLAN.md)** - Development roadmap and current phase tasks
- **[docs/SYSTEM_IDENTITY.md](docs/SYSTEM_IDENTITY.md)** - CLIO Agent capabilities, identity, design principles
- **[docs/CLIO_AGENT_ARCHITECTURE.md](docs/CLIO_AGENT_ARCHITECTURE.md)** - Full architecture, 3-tier orchestration, ARC memory
- **[AGENTS.md](AGENTS.md)** - Repository guidance for contributors and coding agents
- **[docs/CONTRIBUTOR_QUICKSTART.md](docs/CONTRIBUTOR_QUICKSTART.md)** - Fast contributor path

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

For day-to-day contributor workflow, start with:
- [docs/CONTRIBUTOR_QUICKSTART.md](docs/CONTRIBUTOR_QUICKSTART.md)

Recommended background reading:
1. [docs/CLIO_AGENT_ARCHITECTURE.md](docs/CLIO_AGENT_ARCHITECTURE.md)
2. [docs/SYSTEM_IDENTITY.md](docs/SYSTEM_IDENTITY.md)
3. [src/clio_agent/experts/data_expert.py](src/clio_agent/experts/data_expert.py)

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
