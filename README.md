# ClaudIO

**Cognitive Layer for Adaptive Universal Data & Intelligent Operations**

DSPy multi-agent system for scientific computing. **Programming LLMs, not prompting them.**

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![DSPy](https://img.shields.io/badge/DSPy-2.6+-green.svg)](https://dspy.ai)
[![UV](https://img.shields.io/badge/UV-enabled-orange.svg)](https://github.com/astral-sh/uv)

---

## What is ClaudIO?

ClaudIO is a **DSPy-powered multi-agent system** for scientific computing that demonstrates:

- **Declarative LLM Programming**: DSPy signatures instead of prompt engineering
- **ReAct Agents**: Reasoning + Acting with MCP scientific tools
- **Multi-Agent Orchestration**: ChainOfThought routes to domain experts
- **FastMCP Integration**: Standard protocol for HDF5, SLURM, analysis tools
- **Local LM Support**: LM Studio/Ollama for privacy-preserving HPC

### Architecture

```
User Question
    ↓
Orchestrator (DSPy ChainOfThought)
    → Analyzes & routes
    ↓
Expert (DSPy ReAct)
    Thought: "Need to analyze file"
    Action: call hdf5_analyze(filepath)
    Observation: {compression: "none"}
    Thought: "Recommend gzip-6"
    → Answer with recommendations
```

---

## Quick Start (Developers)

### Prerequisites

- **LM Studio** running at `http://100.127.255.164:1234` with gpt-oss-20b loaded
- **UV** installed (`curl -LsSf https://astral.sh/uv/install.sh | sh`)
- **Python 3.11+**

### Running

```bash
# 1. Test configuration
uv run src/claudio/config.py

# 2. Test orchestrator (5 routing tests)
uv run src/claudio/orchestrator.py

# 3. Test data expert (ReAct with tools)
uv run src/claudio/experts/data_expert.py

# 4. Run interactive CLI
uv run src/claudio/ui/cli.py
```

### Example Interaction

```
$ uv run src/claudio/ui/cli.py

You: How do I optimize my 100GB HDF5 file?

ClaudIO via data-expert:
Based on your 100GB HDF5 file, here are optimization recommendations:

1. **Compression**: Apply gzip-6 or blosc compression
   - gzip-6: 2-3x compression, widely compatible
   - blosc: 10-100x faster decompression for parallel I/O

2. **Chunking**: Enable automatic chunking for parallel access
   - Match chunk size to access patterns
   - Typical: 100KB - 10MB per chunk

3. **Parallel I/O**: Use MPI-IO collective writes if on cluster
```

---

## Project Structure

```
src/claudio/
├── config.py                 # LM configuration (LM Studio/Ollama/OpenAI)
├── orchestrator.py           # DSPy ChainOfThought router
├── signatures/               # DSPy signature definitions
│   ├── orchestrator_sig.py   # Routing signature
│   └── expert_sig.py         # Expert signatures (5 experts)
├── experts/                  # Domain expert agents
│   ├── data_expert.py        # HDF5, ADIOS (ReAct + tools) ✅
│   ├── hpc_expert.py         # SLURM, MPI
│   ├── analysis_expert.py    # Visualization, stats
│   ├── research_expert.py    # Papers, citations
│   └── workflow_expert.py    # Automation
├── tools/                    # FastMCP integration
│   ├── mcp_wrapper.py        # FastMCP client
│   ├── servers/
│   │   └── hdf5_server.py    # HDF5 MCP server ✅
│   ├── data_tools.py         # Tool wrappers
│   └── hpc_tools.py          # HPC tools
└── ui/
    ├── cli.py                # Rich TUI ✅
    └── api.py                # FastAPI server
```

**Total**: ~3,700 lines of DSPy agent code

---

## Key Features

### 1. DSPy Signatures (Programming, not Prompting)

```python
class DataExpertSignature(dspy.Signature):
    """Analyze and optimize scientific data files."""

    question: str = dspy.InputField(desc="User's data I/O question")
    context: str = dspy.InputField(desc="File metadata")

    answer: str = dspy.OutputField(desc="Analysis and recommendations")
```

**No manual prompts. DSPy generates optimal prompts from signatures.**

### 2. ReAct Agents (Reasoning + Tool Calling)

```python
class DataExpert(dspy.Module):
    def __init__(self):
        super().__init__()
        self.agent = dspy.ReAct(
            DataExpertSignature,
            tools=[hdf5_analyze, hdf5_optimize],  # MCP tools
            max_iters=5
        )

    def forward(self, question: str, context: str):
        return self.agent(question=question, context=context)
```

**Agent autonomously decides when/how to use tools.**

### 3. FastMCP Tool Servers

```python
from fastmcp import FastMCP

mcp = FastMCP("HDF5 Tools")

@mcp.tool
def hdf5_analyze(filepath: str) -> dict:
    """Analyze HDF5 file structure."""
    import h5py
    with h5py.File(filepath) as f:
        return {"compression_ratio": ..., "recommendations": [...]}
```

**Standard MCP protocol. Easy to extend.**

### 4. UV Self-Contained Scripts

```python
#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = ["dspy-ai>=2.6.0", "fastmcp>=0.1.0"]
# ///

# No venv, no pip install - just uv run!
```

---

## Development

### Testing Individual Components

```bash
# Configuration
uv run src/claudio/config.py

# Signatures
uv run src/claudio/signatures/orchestrator_sig.py
uv run src/claudio/signatures/expert_sig.py

# Experts
uv run src/claudio/experts/data_expert.py
uv run src/claudio/experts/hpc_expert.py

# Orchestrator
uv run src/claudio/orchestrator.py

# MCP Tools
uv run src/claudio/tools/mcp_wrapper.py

# HDF5 MCP Server
uv run src/claudio/tools/servers/hdf5_server.py --port 8000
```

### CLI Commands

```bash
# Default (LM Studio)
uv run src/claudio/ui/cli.py

# With verbose output
uv run src/claudio/ui/cli.py --verbose

# With Ollama
uv run src/claudio/ui/cli.py --ollama

# With OpenAI
export OPENAI_API_KEY=sk-...
uv run src/claudio/ui/cli.py --openai
```

### CLI Commands

```
/help      - Show available commands
/experts   - List available experts
/history   - Show conversation history
/verbose   - Toggle routing details
/clear     - Clear history
/quit      - Exit
```

---

## LM Configuration

### LM Studio (Default)

```python
from claudio.config import setup_dspy

lm = setup_dspy()  # Defaults to LM Studio
# URL: http://100.127.255.164:1234
# Model: openai/gpt-oss-20b
# Cost: FREE (local)
```

### Ollama

```python
lm = setup_dspy(use_ollama=True, use_lm_studio=False, model="llama3.1:8b")
# Fully local, privacy-preserving
```

### OpenAI

```python
lm = setup_dspy(use_openai=True, use_lm_studio=False)
# Cloud, requires API key
```

---

## DSPy Patterns Demonstrated

1. **Signatures**: Declarative LLM behavior specs
2. **ChainOfThought**: Orchestrator routing with reasoning
3. **ReAct**: Expert agents with tool calling
4. **Module Composition**: Orchestrator → Expert hierarchy
5. **Tool Integration**: MCP tools in ReAct agents

---

## Current Status

### ✅ Working

- DSPy orchestrator with ChainOfThought routing
- 5 domain experts (data, hpc, analysis, research, workflow)
- DataExpert with ReAct + tool calling
- FastMCP HDF5 server
- Interactive CLI
- LM Studio/Ollama/OpenAI support

### 🔨 In Progress

- More ReAct agents (upgrade remaining experts)
- Additional FastMCP servers (SLURM, Darshan)
- RAG for scientific context
- FastAPI server

### 📋 Future

- Multi-agent coordination (sequential, parallel)
- Optimization (BootstrapFewShot, MIPROv2) - optional
- Production deployment guides
- MLflow integration

---

## Documentation

- **docs/CLAUDIO_ARCHITECTURE.md** - Agent-centric system design
- **docs/DSPY_FOR_CLAUDIO.md** - DSPy implementation patterns
- **ai-docs/DSPY/** - Complete DSPy reference (7 guides)
- **ai-docs/example_poc/** - Original POC code
- **CLAUDE.md** - Developer quick reference

---

## Technologies

- **[DSPy](https://dspy.ai)** - Framework for programming LLMs
- **[FastMCP](https://github.com/jlowin/fastmcp)** - Model Context Protocol
- **[UV](https://github.com/astral-sh/uv)** - Fast Python package manager
- **[Rich](https://rich.readthedocs.io)** - Terminal UI
- **h5py** - HDF5 file operations

---

## Contributing

ClaudIO is research/development code. Not ready for production or public use.

For development:
1. Review `docs/CLAUDIO_ARCHITECTURE.md`
2. Study `src/claudio/experts/data_expert.py` for ReAct pattern
3. Check `src/claudio/tools/servers/hdf5_server.py` for FastMCP pattern
4. Test with: `uv run src/claudio/ui/cli.py`

---

## License

Apache 2.0 - See LICENSE

---

**ClaudIO**: Programming LLMs through DSPy signatures. ReAct agents with FastMCP tools. Built for scientific computing.
