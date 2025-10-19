# ClaudIO v0.1.0 - Student Handoff Guide

**Welcome!** You're taking over ClaudIO, a multi-agent system for scientific computing.

---

## What is ClaudIO?

ClaudIO routes scientific computing questions to specialized AI experts:
- **data** expert: HDF5, ADIOS, Parquet I/O optimization
- **hpc** expert: SLURM, MPI, cluster performance
- **analysis** expert: Visualization, statistics, code
- **research** expert: Papers, citations, literature
- **workflow** expert: Automation, pipelines

**No prompt engineering.** Declarative agent programming with signatures.

---

## Quick Start (5 minutes)

### 1. Install & Run

```bash
# Ensure LM Studio is running at http://100.127.255.164:1234
# Then run:
./RUN_CLAUDIO.sh
```

### 2. Try Commands

```
You: /experts          # See all 5 experts
You: /help             # Show commands
You: /verbose          # Toggle routing details
```

### 3. Ask Questions

```
You: How do I optimize my 100GB HDF5 file?
→ Routes to DATA expert
→ Returns analysis + recommendations

You: My SLURM job is slow, help debug it
→ Routes to HPC expert  
→ Returns diagnosis + solution

You: Visualize simulation time-series data
→ Routes to ANALYSIS expert
→ Returns approach + code example
```

---

## Architecture (Simple Version)

```
Your Question
    ↓
Orchestrator (smart routing)
    ↓
Expert (specialized agent)
    ↓
Answer (structured output)
```

**Orchestrator** picks the right expert for each question.
**Experts** provide domain-specific answers.

---

## Key Files to Understand

### Start Here
1. `src/claudio/orchestrator.py` - How routing works
2. `src/claudio/experts/data_expert.py` - Example expert (has tool calling!)
3. `src/claudio/ui/cli.py` - The interactive interface

### Then Study
4. `src/claudio/signatures/expert_sig.py` - How experts are defined
5. `src/claudio/tools/servers/hdf5_server.py` - Example tool server
6. `docs/CLAUDIO_ARCHITECTURE.md` - Full system design

---

## How to Extend ClaudIO

### Add a New Expert

**Example: Adding a "CodeExpert" for programming help**

1. **Define signature** (`src/claudio/signatures/expert_sig.py`):
```python
class CodeExpertSignature(dspy.Signature):
    """Programming and debugging expert."""
    
    question: str = dspy.InputField(desc="Programming question")
    code_context: str = dspy.InputField(desc="Code context", default="")
    
    explanation: str = dspy.OutputField(desc="Explanation")
    code: str = dspy.OutputField(desc="Code example")
```

2. **Create expert module** (`src/claudio/experts/code_expert.py`):
```python
import dspy
from claudio.signatures.expert_sig import CodeExpertSignature

class CodeExpert(dspy.Module):
    def __init__(self):
        super().__init__()
        self.generate = dspy.ChainOfThought(CodeExpertSignature)
    
    def forward(self, question: str, code_context: str = ""):
        return self.generate(question=question, code_context=code_context)
    
    @staticmethod
    def get_capabilities():
        return {
            "name": "Code Expert",
            "description": "Programming and debugging assistance",
            "keywords": ["code", "programming", "python", "debug", "function"],
            "priority": 2
        }
```

3. **Register in** `src/claudio/experts/__init__.py`:
```python
from claudio.experts.code_expert import CodeExpert

def get_all_experts():
    return {
        "data": DataExpert(),
        "hpc": HPCExpert(),
        "analysis": AnalysisExpert(),
        "research": ResearchExpert(),
        "workflow": WorkflowExpert(),
        "code": CodeExpert(),  # Add here
    }

def get_expert_capabilities():
    return {
        # ... existing ...
        "code": CodeExpert.get_capabilities(),  # Add here
    }
```

4. **Update orchestrator** (`src/claudio/orchestrator.py` line ~240):
```python
elif expert_id == "code":
    result = expert(question=question, code_context=expert_context)
    answer = f"{result.explanation}\n\n**Code:**\n```python\n{result.code}\n```"
```

5. **Test it**:
```bash
uv run src/claudio/experts/code_expert.py
uv run src/claudio/orchestrator.py
```

### Add an MCP Tool Server

**Example: SLURM job management tools**

1. **Create server** (`src/claudio/tools/servers/slurm_server.py`):
```python
#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = ["fastmcp>=0.1.0"]
# ///

from fastmcp import FastMCP

mcp = FastMCP("SLURM Tools")

@mcp.tool
def slurm_analyze(job_script: str) -> dict:
    """Analyze SLURM job script for issues."""
    # Parse script, check resource allocation
    return {
        "issues": ["Using more nodes than needed"],
        "recommendations": ["Reduce to 32 nodes"]
    }

if __name__ == "__main__":
    mcp.run(transport="sse", port=8001)
```

2. **Test server**:
```bash
uv run src/claudio/tools/servers/slurm_server.py
```

3. **Use in expert** - upgrade HPCExpert to ReAct with this tool

### Upgrade Expert to Use Tools (ReAct Pattern)

Change expert from ChainOfThought to ReAct:

```python
# Before (pure reasoning)
self.generate = dspy.ChainOfThought(HPCExpertSignature)

# After (reasoning + tool calling)
from claudio.tools.mcp_wrapper import create_dspy_tool

slurm_analyze = create_dspy_tool("slurm", "slurm_analyze")
self.agent = dspy.ReAct(
    HPCExpertSignature,
    tools=[slurm_analyze],
    max_iters=5
)
```

---

## Testing Your Changes

```bash
# Test specific expert
uv run src/claudio/experts/your_expert.py

# Test orchestrator (includes all experts)
uv run src/claudio/orchestrator.py

# Test in CLI
uv run src/claudio/ui/cli.py
```

---

## Common Tasks

### Change LM Provider

```bash
# Use Ollama instead of LM Studio
uv run src/claudio/ui/cli.py --ollama

# Use OpenAI
export OPENAI_API_KEY=sk-...
uv run src/claudio/ui/cli.py --openai
```

### Add Scientific Knowledge

Future: Add RAG (Retrieval Augmented Generation) to provide scientific computing context to experts.

### Debug Issues

```bash
# Verbose mode shows routing reasoning
uv run src/claudio/ui/cli.py --verbose

# Check LM Studio connection
curl http://100.127.255.164:1234/v1/models

# Test config
uv run src/claudio/config.py
```

---

## Project Structure

```
src/claudio/
├── config.py              # LM setup
├── orchestrator.py        # Routes to experts
├── experts/               # 5 domain experts
│   ├── data_expert.py     # ← Study this (ReAct example)
│   ├── hpc_expert.py
│   ├── analysis_expert.py
│   ├── research_expert.py
│   └── workflow_expert.py
├── signatures/            # Expert definitions
├── tools/                 # MCP integration
│   ├── mcp_wrapper.py     # FastMCP client
│   └── servers/           # Tool servers
│       └── hdf5_server.py # ← Study this (MCP example)
└── ui/
    ├── cli.py             # Interactive CLI
    └── api.py             # FastAPI server
```

---

## Learning Resources

### In This Repo
- `docs/CLAUDIO_ARCHITECTURE.md` - System design
- `ai-docs/DSPY/` - Complete reference (7 guides)
- `INSTALL_AND_RUN.md` - Detailed setup

### External
- DSPy: https://dspy.ai
- FastMCP: https://github.com/jlowin/fastmcp
- IOWarp: https://iowarp.ai

---

## Development Workflow

1. **Make changes** to expert or add new component
2. **Test component**: `uv run src/claudio/experts/your_expert.py`
3. **Test orchestrator**: `uv run src/claudio/orchestrator.py`
4. **Test in CLI**: `uv run src/claudio/ui/cli.py`
5. **Verify behavior** with test questions

---

## Next Steps for You

### Week 1: Understand
- Run all component tests
- Study DataExpert (ReAct pattern)
- Review orchestrator routing logic
- Try different questions in CLI

### Week 2: Extend
- Upgrade HPCExpert to ReAct
- Create SLURM MCP server
- Add more tools to DataExpert

### Week 3+: Advanced
- Multi-agent coordination
- RAG integration
- Optimization (optional)
- Production deployment

---

## Questions?

Read the docs:
- `INSTALL_AND_RUN.md` - Setup issues
- `QUICKTEST.md` - Testing help
- `docs/CLAUDIO_ARCHITECTURE.md` - Design questions

---

**ClaudIO v0.1.0** - Built by Gnosis Research Center for the IOWarp Project
**Your mission**: Extend it into the ultimate scientific computing AI assistant!

Good luck! 🚀
