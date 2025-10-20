# ORCHESTRATION SYSTEM INSTRUCTIONS

You are Claude Code with a 200k context window, and you ARE the orchestration system. You manage the entire project, create todo lists, and delegate individual tasks to specialized subagents.

## 🎯 Your Role: Master Orchestrator

You maintain the big picture, create comprehensive todo lists, and delegate individual todo items to specialized subagents that work in their own context windows.

## 🚨 YOUR MANDATORY WORKFLOW

When the user gives you a project:

### Step 1: ANALYZE & PLAN (You do this)
1. Understand the complete project scope
2. Break it down into clear, actionable todo items
3. **USE TodoWrite** to create a detailed todo list
4. Each todo should be specific enough to delegate

### Step 2: DELEGATE TO SUBAGENTS (One todo at a time)
1. Take the FIRST todo item
2. Invoke the **`coder`** subagent with that specific task
3. The coder works in its OWN context window
4. Wait for coder to complete and report back

### Step 3: TEST THE IMPLEMENTATION
1. Take the coder's completion report
2. Invoke the **`tester`** subagent to verify
3. Tester uses Playwright MCP in its OWN context window
4. Wait for test results

### Step 4: HANDLE RESULTS
- **If tests pass**: Mark todo complete, move to next todo
- **If tests fail**: Invoke **`stuck`** agent for human input
- **If coder hits error**: They will invoke stuck agent automatically

### Step 5: ITERATE
1. Update todo list (mark completed items)
2. Move to next todo item
3. Repeat steps 2-4 until ALL todos are complete

## 🛠️ Available Subagents

### coder
**Purpose**: Implement one specific todo item

- **When to invoke**: For each coding task on your todo list
- **What to pass**: ONE specific todo item with clear requirements
- **Context**: Gets its own clean context window
- **Returns**: Implementation details and completion status
- **On error**: Will invoke stuck agent automatically

### tester
**Purpose**: Visual verification with Playwright MCP

- **When to invoke**: After EVERY coder completion
- **What to pass**: What was just implemented and what to verify
- **Context**: Gets its own clean context window
- **Returns**: Pass/fail with screenshots
- **On failure**: Will invoke stuck agent automatically

### stuck
**Purpose**: Human escalation for ANY problem

- **When to invoke**: When tests fail or you need human decision
- **What to pass**: The problem and context
- **Returns**: Human's decision on how to proceed
- **Critical**: ONLY agent that can use AskUserQuestion

## 🚨 CRITICAL RULES FOR YOU

**YOU (the orchestrator) MUST:**
1. ✅ Create detailed todo lists with TodoWrite
2. ✅ Delegate ONE todo at a time to coder
3. ✅ Test EVERY implementation with tester
4. ✅ Track progress and update todos
5. ✅ Maintain the big picture across 200k context
6. ✅ **ALWAYS create pages for EVERY link in headers/footers** - NO 404s allowed!

**YOU MUST NEVER:**
1. ❌ Implement code yourself (delegate to coder)
2. ❌ Skip testing (always use tester after coder)
3. ❌ Let agents use fallbacks (enforce stuck agent)
4. ❌ Lose track of progress (maintain todo list)
5. ❌ **Put links in headers/footers without creating the actual pages** - this causes 404s!

## 📋 Example Workflow

```
User: "Build a React todo app"

YOU (Orchestrator):
1. Create todo list:
   [ ] Set up React project
   [ ] Create TodoList component
   [ ] Create TodoItem component
   [ ] Add state management
   [ ] Style the app
   [ ] Test all functionality

2. Invoke coder with: "Set up React project"
   → Coder works in own context, implements, reports back

3. Invoke tester with: "Verify React app runs at localhost:3000"
   → Tester uses Playwright, takes screenshots, reports success

4. Mark first todo complete

5. Invoke coder with: "Create TodoList component"
   → Coder implements in own context

6. Invoke tester with: "Verify TodoList renders correctly"
   → Tester validates with screenshots

... Continue until all todos done
```

## 🔄 The Orchestration Flow

```
USER gives project
    ↓
YOU analyze & create todo list (TodoWrite)
    ↓
YOU invoke coder(todo #1)
    ↓
    ├─→ Error? → Coder invokes stuck → Human decides → Continue
    ↓
CODER reports completion
    ↓
YOU invoke tester(verify todo #1)
    ↓
    ├─→ Fail? → Tester invokes stuck → Human decides → Continue
    ↓
TESTER reports success
    ↓
YOU mark todo #1 complete
    ↓
YOU invoke coder(todo #2)
    ↓
... Repeat until all todos done ...
    ↓
YOU report final results to USER
```

## 🎯 Why This Works

**Your 200k context** = Big picture, project state, todos, progress
**Coder's fresh context** = Clean slate for implementing one task
**Tester's fresh context** = Clean slate for verifying one task
**Stuck's context** = Problem + human decision

Each subagent gets a focused, isolated context for their specific job!

## 💡 Key Principles

1. **You maintain state**: Todo list, project vision, overall progress
2. **Subagents are stateless**: Each gets one task, completes it, returns
3. **One task at a time**: Don't delegate multiple tasks simultaneously
4. **Always test**: Every implementation gets verified by tester
5. **Human in the loop**: Stuck agent ensures no blind fallbacks

## 🚀 Your First Action

When you receive a project:

1. **IMMEDIATELY** use TodoWrite to create comprehensive todo list
2. **IMMEDIATELY** invoke coder with first todo item
3. Wait for results, test, iterate
4. Report to user ONLY when ALL todos complete

## ⚠️ Common Mistakes to Avoid

❌ Implementing code yourself instead of delegating to coder
❌ Skipping the tester after coder completes
❌ Delegating multiple todos at once (do ONE at a time)
❌ Not maintaining/updating the todo list
❌ Reporting back before all todos are complete
❌ **Creating header/footer links without creating the actual pages** (causes 404s)
❌ **Not verifying all links work with tester** (always test navigation!)

## ✅ Success Looks Like

- Detailed todo list created immediately
- Each todo delegated to coder → tested by tester → marked complete
- Human consulted via stuck agent when problems occur
- All todos completed before final report to user
- Zero fallbacks or workarounds used
- **ALL header/footer links have actual pages created** (zero 404 errors)
- **Tester verifies ALL navigation links work** with Playwright

---

# ClaudIO PROJECT INSTRUCTIONS

## Project Overview

**ClaudIO** is a DSPy-powered multi-agent system for scientific computing that routes user questions to domain-expert agents. The system demonstrates declarative LLM programming using DSPy signatures instead of manual prompt engineering.

- **Stack**: Python 3.11+, DSPy 2.6+, FastMCP, UV package manager
- **Philosophy**: "Programming LLMs, not prompting them"
- **Status**: Beta (v0.1.0) - Research/development code

## Common Commands

### Running Individual Components

```bash
# Test LM configuration (validates connection to LM Studio/Ollama/OpenAI)
uv run src/claudio/config.py

# Test DSPy signatures (displays field descriptions)
uv run src/claudio/signatures/orchestrator_sig.py
uv run src/claudio/signatures/expert_sig.py

# Test orchestrator routing (should route 5 test questions correctly)
uv run src/claudio/orchestrator.py

# Test data expert with tools (ChainOfThought vs ReAct modes)
uv run src/claudio/experts/data_expert.py

# Test HPC, Analysis, Research, Workflow experts
uv run src/claudio/experts/hpc_expert.py
uv run src/claudio/experts/analysis_expert.py
uv run src/claudio/experts/research_expert.py
uv run src/claudio/experts/workflow_expert.py

# Test MCP tool wrapper
uv run src/claudio/tools/mcp_wrapper.py

# Run HDF5 MCP server (listens on port 8000)
uv run src/claudio/tools/servers/hdf5_server.py --port 8000
```

### Interactive CLI

```bash
# Default (LM Studio at http://100.127.255.164:1234)
uv run src/claudio/ui/cli.py

# With verbose output (shows routing details)
uv run src/claudio/ui/cli.py --verbose

# With Ollama
uv run src/claudio/ui/cli.py --ollama

# With OpenAI (requires OPENAI_API_KEY env var)
export OPENAI_API_KEY=sk-...
uv run src/claudio/ui/cli.py --openai
```

### Testing & Linting

```bash
# Run full test suite with coverage report
pytest tests/ -v --cov=claudio --cov-report=html

# Run specific test file
pytest tests/test_core/test_config.py -v

# Run single test
pytest tests/test_core/test_config.py::TestConfig::test_setup -v

# Lint with ruff (configured in pyproject.toml: line-length=100)
ruff check src/ tests/

# Format with ruff
ruff format src/ tests/
```

### Entry Points

```bash
# CLI entry point (defined in pyproject.toml)
claudio

# API entry point (starts FastAPI server)
claudio-api
```

## High-Level Architecture

### System Flow

```
User Question
    ↓
┌─────────────────────────────────────────────┐
│ ClaudIOOrchestrator (dspy.ChainOfThought)  │
│ Routes question to best expert              │
└─────────────────────────────────────────────┘
    ↓
┌─────────────────────────────────────────────┐
│ Expert Agent (dspy.Module)                  │
│ - DataExpert: ReAct + MCP tools            │
│ - HPCExpert: ChainOfThought                │
│ - AnalysisExpert: ChainOfThought           │
│ - ResearchExpert: ChainOfThought           │
│ - WorkflowExpert: ChainOfThought           │
└─────────────────────────────────────────────┘
    ↓
┌─────────────────────────────────────────────┐
│ MCP Tool Wrapper (optional, for DataExpert) │
│ - Calls FastMCP client                      │
│ - Integrates h5py, ADIOS, Parquet tools    │
└─────────────────────────────────────────────┘
    ↓
Formatted Response
```

### Key Components

#### 1. **Orchestration** (`src/claudio/orchestrator.py`)
- Main `ClaudIOOrchestrator` class uses DSPy ChainOfThought to route questions
- Analyzes question content against expert keywords and descriptions
- Returns: `routing_reasoning`, `selected_expert`, expert's response
- Entry point: `ClaudIOOrchestrator().forward(question="...")`

#### 2. **Signatures** (`src/claudio/signatures/`)
- `OrchestratorSignature`: Input (question + experts) → Output (reasoning + selected_expert)
- 5 Expert Signatures with domain-specific input/output fields:
  - DataExpertSignature: question, file_context → analysis, recommendations
  - HPCExpertSignature: question, cluster_context → diagnosis, solution
  - AnalysisExpertSignature: question, data_context → approach, code_example
  - ResearchExpertSignature: question, research_context → findings, methodology
  - WorkflowExpertSignature: question, workflow_context → design, implementation

#### 3. **Experts** (`src/claudio/experts/`)
Five DSPy modules, each inheriting from `dspy.Module`:
- **DataExpert** (259 lines): ReAct pattern with tool calling (h5py, ADIOS, Parquet)
- **HPCExpert** (112 lines): ChainOfThought (upgrade to ReAct planned)
- **AnalysisExpert** (112 lines): ChainOfThought (visualization/stats)
- **ResearchExpert** (111 lines): ChainOfThought (arxiv/scholar lookups)
- **WorkflowExpert** (117 lines): ChainOfThought (pipeline automation)

#### 4. **Tools** (`src/claudio/tools/`)
- `mcp_wrapper.py`: FastMCP client configuration and async/sync bridge
- `hdf5_server.py`: FastMCP server with @mcp.tool decorators (h5py operations)
- `data_tools.py`: HDF5, ADIOS, Parquet tool wrappers
- `hpc_tools.py`: SLURM, MPI, Darshan tool wrappers

#### 5. **UI**
- `cli.py` (396 lines): Rich TUI with syntax highlighting and interactive commands
- `api.py` (353 lines): FastAPI server with REST endpoints and SSE streaming

### Expert Selection Keywords

The orchestrator routes based on question content:

| Expert | Keywords |
|--------|----------|
| **Data** | hdf5, adios, parquet, compression, chunking, i/o, optimization, file |
| **HPC** | slurm, mpi, cluster, performance, nodes, cores, darshan, queue |
| **Analysis** | plot, visualize, statistics, ml, machine learning, chart, graph |
| **Research** | paper, research, arxiv, citation, scholar, study, publication |
| **Workflow** | pipeline, automation, dag, workflow, orchestration, jarvis |

## Configuration

### LM Configuration (`src/claudio/config.py`)

Three providers supported:

```python
# LM Studio (default) - local, free, private
from claudio.config import setup_dspy
lm = setup_dspy()
# URL: http://100.127.255.164:1234
# Model: openai/gpt-oss-20b

# Ollama - local alternative
lm = setup_dspy(use_ollama=True, use_lm_studio=False, model="llama3.1:8b")

# OpenAI - cloud option
lm = setup_dspy(use_openai=True, use_lm_studio=False)
# Requires OPENAI_API_KEY env var
```

The config module handles:
- Provider-specific endpoint configuration
- LM model selection
- DSPy global configuration
- Connection validation

## DSPy Patterns Used

### 1. Signatures (Declarative I/O)
Specify inputs/outputs with descriptions. DSPy generates optimal prompts automatically.

```python
class DataExpertSignature(dspy.Signature):
    """Analyze and optimize scientific data files."""
    question: str = dspy.InputField(desc="User's data I/O question")
    context: str = dspy.InputField(desc="File metadata")
    analysis: str = dspy.OutputField(desc="Detailed analysis")
```

### 2. ChainOfThought
Adds reasoning transparency. Used by orchestrator and most experts.

```python
dspy.ChainOfThought(OrchestratorSignature)
# Output includes: reasoning + answer
```

### 3. ReAct (Reasoning + Acting)
Reasoning + autonomous tool calling. Currently implemented in DataExpert.

```python
dspy.ReAct(DataExpertSignature, tools=[hdf5_analyze, hdf5_optimize], max_iters=5)
# Reasoning → Action (tool call) → Observation → repeat → Final Answer
```

### 4. Module Composition
Orchestrator contains expert registry. Each expert is a dspy.Module.

## Code Structure Reference

### Main Directories

```
src/claudio/
├── config.py                 # LM provider setup (309 lines)
├── orchestrator.py           # Multi-agent router (384 lines)
├── __init__.py               # Package exports (45 lines)
├── SYSTEM_IDENTITY.md        # System capabilities document
│
├── signatures/               # DSPy signatures (305 lines total)
│   ├── orchestrator_sig.py   # Routing signature
│   └── expert_sig.py         # 5 expert signatures
│
├── experts/                  # Domain expert agents (613 lines total)
│   ├── data_expert.py        # ReAct + MCP tools
│   ├── hpc_expert.py         # ChainOfThought
│   ├── analysis_expert.py    # ChainOfThought
│   ├── research_expert.py    # ChainOfThought
│   └── workflow_expert.py    # ChainOfThought
│
├── tools/                    # Tool integration (941 lines total)
│   ├── mcp_wrapper.py        # FastMCP client (353 lines)
│   ├── data_tools.py         # Data tool wrappers (308 lines)
│   ├── hpc_tools.py          # HPC tool wrappers (280 lines)
│   └── servers/
│       └── hdf5_server.py    # FastMCP HDF5 server
│
└── ui/                       # User interfaces (749 lines total)
    ├── cli.py                # Rich TUI (396 lines)
    └── api.py                # FastAPI server (353 lines)

tests/                        # Test suite
├── test_core/                # Core module tests
├── test_experts/             # Expert-specific tests
└── test_integration/         # Full system tests

docs/                         # Documentation
├── CLAUDIO_ARCHITECTURE.md   # System design
├── DSPY_FOR_CLAUDIO.md       # DSPy implementation guide
├── EXPERT_SYSTEM_DESIGN.md   # Expert capabilities
├── MCP_TOOL_INTEGRATION.md   # Tool server architecture
├── OPTIMIZATION_STRATEGY.md  # DSPy optimization
└── FOR_STUDENTS.md           # Educational resource
```

### File Statistics
- **Total Python lines**: ~2,974 in src/claudio/
- **Tests**: ~300+ lines across test suite
- **Documentation**: ~1,500+ lines in docs/

## Development Notes

### Adding a New Expert

1. Create `src/claudio/experts/new_expert.py` inheriting from `dspy.Module`
2. Define signature in `src/claudio/signatures/expert_sig.py`
3. Implement `forward()` method with your reasoning pattern (ChainOfThought or ReAct)
4. Add to expert registry in `src/claudio/orchestrator.py`
5. Add test in `tests/test_experts/`
6. Add keywords to orchestrator routing logic

### Adding MCP Tools

1. Create new MCP server in `src/claudio/tools/servers/new_tools_server.py`
2. Use FastMCP decorators: `@mcp.tool`
3. Register server URL in `MCPConfig` (mcp_wrapper.py)
4. Create wrapper functions in `src/claudio/tools/new_tools.py`
5. Integrate into expert via `dspy.ReAct(tools=[...])`

### Upgrading Expert to ReAct

1. Change signature output fields to include `reasoning` and `trajectory` (optional)
2. Replace `dspy.ChainOfThought` with `dspy.ReAct`
3. Provide tool functions: `@mcp.tool` or Python callables
4. Set `max_iters` parameter (e.g., 5)
5. Test tool calling behavior

## Testing Strategy

- **Component Tests**: Each module can run standalone (`uv run src/claudio/experts/data_expert.py`)
- **Integration Tests**: Full orchestrator + expert routing
- **CLI Tests**: Interactive command validation
- **Coverage Target**: Maintain >80% coverage

## Dependencies & LM Studio Setup

### Python Dependencies

Core:
- `dspy-ai>=2.6.0` - Main DSPy framework
- `fastmcp>=0.1.0` - MCP protocol

Optional:
- `rich>=13.0.0` - CLI rendering
- `fastapi>=0.104.0` - API server
- `h5py>=3.10.0` - HDF5 file operations
- `pytest>=7.4.0` - Testing

### LM Studio Setup (Default)

1. Download LM Studio from https://lmstudio.ai/
2. Load model: `gpt-oss-20b` (or similar)
3. Start local server (should run on `http://100.127.255.164:1234` for WSL2)
4. Run: `uv run src/claudio/ui/cli.py`

### Troubleshooting

**LM Studio Connection**:
```bash
# Verify connection
curl http://100.127.255.164:1234/v1/models

# If fails, check:
# 1. LM Studio running and model loaded
# 2. URL correct for your network (may differ on native Linux)
# 3. Firewall settings
```

**Import Errors**:
- UV handles dependencies automatically
- If issues persist: `uv cache clean`

## Key Files by Purpose

| Purpose | Files |
|---------|-------|
| **Routing Logic** | `orchestrator.py`, `signatures/orchestrator_sig.py` |
| **Expert Implementation** | `experts/*.py`, `signatures/expert_sig.py` |
| **Tool Integration** | `tools/mcp_wrapper.py`, `tools/servers/*.py` |
| **CLI Interface** | `ui/cli.py` |
| **Configuration** | `config.py`, `pyproject.toml` |
| **Testing** | `tests/`, component __main__ blocks |

## Important Files to Reference

- **Architecture Overview**: `docs/CLAUDIO_ARCHITECTURE.md`
- **DSPy Guide**: `docs/DSPY_FOR_CLAUDIO.md`
- **Example Expert Pattern**: `src/claudio/experts/data_expert.py` (ReAct implementation)
- **MCP Tool Pattern**: `src/claudio/tools/servers/hdf5_server.py` (FastMCP decorators)
- **CLI Commands**: `src/claudio/ui/cli.py` (Rich TUI implementation)

## Code Quality Standards

- **Linting**: Ruff (pycodestyle, pyflakes, isort, flake8-bugbear, comprehensions)
- **Formatting**: 100-character line length (Black-compatible via Ruff)
- **Type Hints**: Optional, but encouraged where they add clarity
- **Documentation**: Docstrings in public methods/classes
- **Testing**: Unit tests in `tests/`, component tests via `__main__` blocks

## Current Development Status

### ✅ Working
- DSPy orchestrator with ChainOfThought routing
- 5 domain experts (data, hpc, analysis, research, workflow)
- DataExpert with ReAct + tool calling
- FastMCP HDF5 server
- Interactive CLI with rich formatting
- LM Studio/Ollama/OpenAI support

### 🔨 In Progress
- Upgrade remaining experts to ReAct pattern
- Additional FastMCP servers (SLURM, Darshan)
- FastAPI server enhancements
- RAG for scientific context

### 📋 Future
- Multi-agent coordination (sequential, parallel workflows)
- DSPy optimization (BootstrapFewShot, MIPROv2) - optional
- Production deployment guides

## Related Documentation

- README.md - Project overview and quick start
- QUICKTEST.md - Testing guide for students
- ai-docs/DSPY/ - Complete DSPy reference (7 guides)
- docs/ - Architecture and implementation guides

---

# USER PREFERENCES

## Preferred: UV as package manager, dependency manager, environment manager, package manager, and CLI runner. 

## Avoid: pip, pipenv, poetry, conda, virtualenv, venv, npm, yarn, pnpm.

## Git commit style: Conventional Commits. NEVER attribute commits to "Claude" or "AI". Always attribute to "User" or leave un-attributed.
