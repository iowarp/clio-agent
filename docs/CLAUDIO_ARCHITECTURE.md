---
title: "ClaudIO Architecture: DSPy Multi-Agent System for Scientific Computing"
category: architecture
priority: critical
version: "2.0"
focus: "Agent Patterns, MCP Tools, Multi-Agent Coordination"
---

# ClaudIO Architecture

ClaudIO is a **DSPy-powered multi-agent system** for scientific computing, built on the principle of **programming LLMs, not prompting them**.

## Core Philosophy

> **"Program the LLM through DSPy signatures and modules, let agents coordinate through ReAct patterns, integrate tools via FastMCP"**

ClaudIO is NOT:
- ❌ A prompt engineering framework
- ❌ An optimization-first system
- ❌ A simple chatbot wrapper

ClaudIO IS:
- ✅ A programmatic multi-agent system using DSPy
- ✅ A tool-augmented reasoning system via ReAct
- ✅ An MCP-integrated scientific computing platform
- ✅ A self-contained UV-native Python application

---

## System Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    ClaudIO Architecture                      │
│                  (DSPy + FastMCP + UV)                       │
└─────────────────────────────────────────────────────────────┘

User Input
    │
    ▼
┌──────────────────────────────────────────────┐
│   Orchestrator (DSPy ChainOfThought)         │
│   Signature: question, experts -> routing    │
│   Analyzes query, selects best expert(s)     │
└───────────────────┬──────────────────────────┘
                    │
        ┌───────────┼───────────┬─────────────┐
        │           │           │             │
        ▼           ▼           ▼             ▼
┌─────────────┐ ┌─────────────┐ ┌──────────┐ ┌──────────┐
│DataExpert   │ │HPCExpert    │ │Analysis  │ │Research  │
│(ReAct)      │ │(ReAct)      │ │Expert    │ │Expert    │
│             │ │             │ │(ReAct)   │ │(ReAct)   │
│Tools:       │ │Tools:       │ │Tools:    │ │Tools:    │
│- hdf5_*     │ │- slurm_*    │ │- plot_*  │ │- arxiv_* │
│- adios_*    │ │- darshan_*  │ │- stats_* │ │- paper_* │
│- parquet_*  │ │- mpi_*      │ │          │ │          │
└──────┬──────┘ └──────┬──────┘ └────┬─────┘ └────┬─────┘
       │               │              │            │
       └───────────────┴──────────────┴────────────┘
                            │
                            ▼
                ┌───────────────────────┐
                │   FastMCP Tools       │
                │   (MCP Protocol)      │
                │                       │
                │   - HDF5 Server       │
                │   - SLURM Server      │
                │   - Darshan Server    │
                │   - Analysis Server   │
                │   - Research Server   │
                └───────────────────────┘
```

---

## Core Components

### 1. DSPy Signatures (Programming the LLM)

**What**: Declarative input/output specifications that define LLM behavior

**Example**:
```python
class DataExpertSignature(dspy.Signature):
    """Scientific data I/O expert: analyze and optimize HDF5/ADIOS/Parquet files."""

    question: str = dspy.InputField(desc="User's data I/O question")
    context: str = dspy.InputField(desc="File metadata, system info")

    answer: str = dspy.OutputField(desc="Analysis and recommendations")
```

**Key Point**: We **declare** what we want, DSPy handles how to achieve it.

### 2. DSPy Modules (Agents & Reasoning)

**Core Modules Used**:

#### ChainOfThought (Orchestrator)
```python
class ClaudIOOrchestrator(dspy.Module):
    def __init__(self):
        super().__init__()
        # Uses reasoning to select expert
        self.router = dspy.ChainOfThought(OrchestratorSignature)
```

#### ReAct (Domain Experts)
```python
class DataExpert(dspy.Module):
    def __init__(self):
        super().__init__()
        # Reasoning + Acting with tools
        self.agent = dspy.ReAct(
            DataExpertSignature,
            tools=[hdf5_analyze, hdf5_optimize, adios_convert],
            max_iters=10
        )
```

**Module Hierarchy**:
- **Predict**: Basic input → output
- **ChainOfThought**: Adds reasoning steps (orchestrator)
- **ReAct**: Reasoning + tool calling (experts)
- **ProgramOfThought**: Generates executable code (HPC workflows)

### 3. MCP Tools (via FastMCP)

**Integration Pattern**:
```python
from fastmcp import FastMCP

# Define MCP server
mcp = FastMCP("Scientific Computing Tools")

@mcp.tool
def hdf5_analyze(filepath: str) -> dict:
    """Analyze HDF5 file structure and performance."""
    return {
        "compression_ratio": analyze_compression(filepath),
        "chunking": get_chunking_info(filepath),
        "recommendations": generate_recommendations(filepath)
    }

# Wrap for DSPy ReAct
def hdf5_analyze_tool(filepath: str) -> dict:
    """DSPy-compatible tool wrapper."""
    # Call MCP server
    return call_mcp_tool("hdf5_analyze", {"filepath": filepath})
```

**Available MCP Servers**:
- `hdf5-server`: HDF5 file operations
- `slurm-server`: SLURM job management
- `darshan-server`: I/O profiling analysis
- `analysis-server`: Data analysis & visualization
- `research-server`: ArXiv/paper search

### 4. Expert Agents (ReAct Pattern)

Each expert is a **ReAct agent** that can:
1. **Reason** about the problem
2. **Act** by calling tools
3. **Observe** tool results
4. **Iterate** until completion

**DataExpert ReAct Flow**:
```
User: "Optimize my 100GB HDF5 file"
    ↓
Thought: "Need to analyze current compression first"
    ↓
Action: call hdf5_analyze("/data/file.h5")
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

---

## Multi-Agent Coordination Patterns

### Pattern 1: Single Expert (Most Common)
```python
orchestrator = ClaudIOOrchestrator()
result = orchestrator(question="How do I optimize HDF5?")
# Routes to DataExpert, executes, returns
```

### Pattern 2: Sequential Experts
```python
# Example: Profile → Analyze → Optimize
result = orchestrator(
    question="Profile my simulation, find bottlenecks, optimize I/O",
    strategy="sequential"
)
# HPCExpert → DataExpert → WorkflowExpert
```

### Pattern 3: Parallel Experts (Future)
```python
# Example: Multiple format conversions
result = orchestrator(
    question="Convert all simulation outputs to analysis-ready formats",
    strategy="parallel"
)
# Multiple DataExperts work in parallel
```

### Pattern 4: Hierarchical (Expert delegates to sub-expert)
```python
# DataExpert decides HDF5 vs ADIOS optimization path
# Then delegates to specialized sub-modules
```

---

## LM Configuration

### Local Development (LM Studio)
```python
from claudio.config import setup_dspy

lm = setup_dspy(use_lm_studio=True)
# Model: openai/gpt-oss-20b
# Location: http://100.127.255.164:1234 (WSL2 → Windows)
# Cost: FREE
```

### Local Production (Ollama)
```python
lm = setup_dspy(use_ollama=True, model="llama3.1:8b")
# Fully local, zero-cost inference
# Privacy-preserving for sensitive HPC data
```

### Cloud (OpenAI) - Optional
```python
lm = setup_dspy(use_openai=True)
# For benchmarking or when local models insufficient
```

---

## UV Script Integration

**Every ClaudIO module is a self-contained UV script**:

```python
#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "dspy-ai>=2.6.0",
#   "fastmcp>=0.1.0",
# ]
# ///

import dspy
from fastmcp import Client

# Module implementation
```

**Benefits**:
- No virtual environments
- Self-documenting dependencies
- Direct execution: `uv run expert.py`
- Reproducible across environments

---

## Data Flow

```
1. User Question
   └─> Orchestrator analyzes via ChainOfThought
       └─> Selects expert based on capabilities

2. Expert Receives Task
   └─> ReAct agent activates
       └─> Reasons about approach
           └─> Calls MCP tools as needed
               └─> Iterates until completion

3. Result Assembly
   └─> Expert formats response
       └─> Orchestrator adds routing metadata
           └─> User receives answer + trace
```

---

## Implementation Phases

### Phase 1: Core Foundation ✅ (Current)
- [x] DSPy signatures for all experts
- [x] Orchestrator with ChainOfThought
- [x] Basic expert modules
- [x] LM configuration (LM Studio, Ollama, OpenAI)
- [x] UV-native structure

### Phase 2: ReAct & Tools (Next)
- [ ] Upgrade experts from ChainOfThought → ReAct
- [ ] Implement FastMCP tool servers
- [ ] Wrap MCP tools for DSPy compatibility
- [ ] Test tool calling end-to-end
- [ ] RAG integration for context

### Phase 3: Advanced Patterns
- [ ] Multi-agent coordination (sequential, parallel)
- [ ] Hierarchical expert delegation
- [ ] Context management & memory
- [ ] Streaming responses
- [ ] Error recovery & fallbacks

### Phase 4: Production Features
- [ ] FastAPI server deployment
- [ ] Monitoring & observability
- [ ] Performance optimization (if needed)
- [ ] Documentation & examples

---

## Key Design Decisions

### Why DSPy ChainOfThought for Orchestrator?
- **Reasoning transparency**: See why expert was selected
- **Composable**: Easy to extend with more experts
- **No tool calling needed**: Pure routing logic

### Why DSPy ReAct for Experts?
- **Tool integration**: Natural MCP tool calling
- **Iterative refinement**: Multi-step problem solving
- **Observable**: Full reasoning trace
- **Autonomous**: Decides when to use tools

### Why FastMCP for Tools?
- **Standard protocol**: MCP is emerging standard
- **Easy authoring**: Pythonic tool definition
- **Server/client model**: Clean separation
- **Rich ecosystem**: Growing tool library

### Why UV for Scripts?
- **Zero config**: No requirements.txt management
- **Inline dependencies**: Self-documenting
- **Fast**: 10-100x faster than pip
- **Reproducible**: Locked dependencies per script

---

## Comparison: Before vs After DSPy

| Aspect | Before (Manual) | After (DSPy) |
|--------|----------------|--------------|
| Expert routing | Hardcoded rules | ChainOfThought learns |
| Tool calling | Manual if/else | ReAct autonomous |
| Prompt engineering | Hours of tweaking | Signature definition |
| Multi-step tasks | Complex orchestration | ReAct handles |
| Adding new expert | Update routing logic | Add signature + register |
| Context management | Manual tracking | DSPy handles |
| Error handling | Custom code | Built-in retry logic |

---

## Next Steps

1. **Review this architecture** - ensure alignment with vision
2. **Implement ReAct experts** - upgrade from ChainOfThought
3. **Build FastMCP servers** - wrap scientific tools
4. **Test end-to-end** - with LM Studio running
5. **Add RAG** - for scientific computing knowledge
6. **Deploy** - FastAPI + production config

---

## Related Documentation

- [DSPy for ClaudIO](DSPY_FOR_CLAUDIO.md) - Deep DSPy implementation guide
- [Expert System Design](EXPERT_SYSTEM_DESIGN.md) - Expert capabilities & signatures
- [MCP Tool Integration](MCP_TOOL_INTEGRATION.md) - FastMCP patterns
- [ai-docs/DSPY/](../ai-docs/DSPY/) - Complete DSPy reference materials

**Version**: 2.0 (Agent-Centric)
**Last Updated**: 2025-01-18
**Focus**: DSPy agents, MCP tools, multi-agent coordination
