# CLIO Agent

**Autonomous AI agent for scientific data management.** Intelligence Layer (CEI) of the IOWarp platform.

[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![UV](https://img.shields.io/badge/UV-enabled-orange.svg)](https://github.com/astral-sh/uv)
[![IOWarp](https://img.shields.io/badge/IOWarp-Intelligence%20Layer-teal.svg)](https://iowarp.org)

---

## What is CLIO Agent?

CLIO Agent is an autonomous agent specialized in scientific data management for HPC and research workflows. It helps researchers optimize data files (HDF5, Parquet), manage HPC jobs (SLURM), and analyze I/O performance (Darshan).

**Key design principles:**
- **3-Tier Agent Hierarchy**: Main orchestrator -> Expert agents -> Parallel sub-tasks
- **ARC Memory**: O(log N) context retrieval with persistent storage
- **Self-Improving**: Collects performance metrics and optimizes its own prompts
- **MCP Tools**: Scientific tool servers via FastMCP protocol
- **Any LLM**: Works with local models (LM Studio, Ollama) or cloud APIs

CLIO Agent is NOT a framework for building agents - it IS the agent.

---

## Current Status

### Working Now
- Main agent orchestration with conversation management
- DataExpert agent for HDF5/data optimization advice (ChainOfThought mode)
- ARC Memory Layer: LRU cache, B-tree index, LSM tree, context retrieval (90% complete)
- Agent Registry with capability-based routing
- Interactive CLI with Rich TUI
- LM Studio integration (local, zero-cost inference)

### In Development (Phase 1: Foundation Reset)
- Real HDF5 MCP server (replacing mock tools)
- FastMCP 3.x gateway with mount() composition
- DSPy 3.x ReAct pattern with ChatAdapter (replacing ChainOfThought fallback)
- Stub cleanup (removing non-functional code)

### Planned
- **Phase 2**: HPCExpert + multi-expert routing + context compilation
- **Phase 3**: Optimizer Layer (SIMBA, training data, offline tuning)
- **Phase 4**: REST API, CI/CD, containers, 80%+ coverage
- **Phase 5**: IOWarp CTE storage backend
- **Phase 6**: Online learning, A2A protocol, additional experts

See [PLAN.md](PLAN.md) for full roadmap.

---

## Quick Start

### Prerequisites

- **Python 3.12+**
- **UV** package manager:
  ```bash
  curl -LsSf https://astral.sh/uv/install.sh | sh
  ```
- **LM Studio** with a model loaded (e.g., granite-4-h-tiny) at `http://127.0.0.1:1234`

### Install & Run

```bash
# Clone
git clone https://github.com/iowarp/clio-agent
cd clio-agent

# Install dependencies
uv sync --extra ui --extra memory

# Start LM Studio and load a model, then:
uv run src/clio_agent/ui/cli.py
```

### Verify Setup

```bash
# Test LM connection
uv run src/clio_agent/config.py

# Test main agent
uv run src/clio_agent/agent.py

# Run tests
uv run pytest tests/
```

### Example Interaction

```
$ uv run src/clio_agent/ui/cli.py

You: How do I optimize my 100GB HDF5 file for parallel I/O?

CLIO: Based on analysis, here are recommendations:

1. Compression: Apply gzip-6 (2-3x reduction expected)
2. Chunking: Enable automatic chunking (1-10MB chunks)
3. Parallel I/O: Use HDF5's MPI-IO mode with collective buffering

Would you like me to generate the optimization script?
```

---

## Architecture

```
User (CLI / API)
    |
    v
CLIO Main Agent (Tier 1) --- ARC Memory (cache + index + LSM)
    |
    v
Expert Agents (Tier 2)
    |--- DataExpert (HDF5, Parquet, ADIOS)
    |--- HPCExpert (SLURM, Darshan) [planned]
    |
    v
MCP Tool Servers (via FastMCP Gateway)
    |--- /hdf5 (analyze, optimize, compress)
    |--- /parquet (schema, query, stats) [planned]
    |--- /slurm (submit, status, cancel) [planned]
```

---

## Project Structure

```
src/clio_agent/
├── config.py              # LM Studio configuration
├── agent.py               # Main agent orchestrator (Tier 1)
├── signatures/            # DSPy signature definitions
├── experts/
│   └── data_expert.py     # DataExpert (Tier 2)
├── registry/
│   ├── registry.py        # Agent registry
│   └── capability_matcher.py
├── arc/                   # ARC Memory Layer
│   ├── memory.py          # Core API
│   ├── cache.py           # LRU cache
│   ├── index.py           # B-tree index
│   ├── lsm.py             # LSM tree
│   ├── retrieval.py       # Context retrieval
│   ├── schema.py          # Data schemas
│   ├── storage.py         # Persistent storage
│   └── coordinator.py     # Multi-agent coordination
├── tools/                 # MCP tool servers
│   └── mcp_connector.py   # MCP bridge (being replaced)
└── ui/
    └── cli.py             # Interactive CLI
```

---

## CLI Commands

```
/help      - Show available commands
/experts   - List registered agents
/registry  - Show Agent Registry status
/memory    - Show ARC memory statistics
/metrics   - Show performance metrics
/verbose   - Toggle reasoning trace
/history   - Show conversation history
/clear     - Clear history
/quit      - Exit
```

---

## Model Configuration

CLIO Agent connects to LM Studio by default at `http://127.0.0.1:1234`. Configure in `src/clio_agent/config.py`.

```python
# Default: LM Studio with auto-detected model
from clio_agent.config import setup_dspy
lm = setup_dspy()

# Custom model
lm = setup_dspy(model="ibm/granite-4-h-tiny")
```

Supports any OpenAI-compatible endpoint (LM Studio, Ollama, vLLM, cloud APIs).

---

## Development

```bash
# Run tests
uv run pytest tests/ -v

# Lint
uv run ruff check src/

# Type check
uv run mypy src/clio_agent/

# Run CLI
uv run src/clio_agent/ui/cli.py
```

Read [CLAUDE.md](CLAUDE.md) for development rules. Read [PLAN.md](PLAN.md) for current phase.

---

## Technologies

- **[DSPy](https://github.com/stanfordnlp/dspy)** - Agent patterns, signatures, optimizers (internal)
- **[FastMCP](https://github.com/jlowin/fastmcp)** - MCP protocol for tool servers
- **[UV](https://github.com/astral-sh/uv)** - Python package manager
- **[Rich](https://rich.readthedocs.io)** - Terminal UI
- **[IOWarp](https://github.com/iowarp/iowarp-install)** - Storage platform integration

---

## Citation

```bibtex
@software{clioagent2025,
  title={CLIO Agent: Autonomous Agent for Scientific Data Management},
  author={IOWarp Team},
  year={2025},
  url={https://github.com/iowarp/clio-agent}
}
```

---

**CLIO Agent**: Autonomous science agent. Built for researchers, by researchers.
