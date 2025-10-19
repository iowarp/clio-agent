# ClaudIO: The Self-Evolving Scientific Computing Agent

**Cognitive Layer for Adaptive Universal Data & Intelligent Operations**

> A DSPy-powered, UV-native AI agent that grows through experience, optimizes itself from usage logs, and orchestrates scientific computing workflows with learned expertise.

---

## 🎯 Vision Statement

ClaudIO is **not** a plugin, configuration layer, or wrapper around existing AI systems. ClaudIO is a **standalone, self-improving AI agent** built from the ground up using DSPy's programming paradigm and UV's self-contained execution model. It embodies three breakthrough concepts:

### 1. **Programming Over Prompting**
Traditional AI agents use hand-crafted prompts. ClaudIO uses **compiled programs** that automatically optimize themselves through DSPy's declarative signatures and optimization algorithms.

### 2. **UV-Native Self-Contained Scripts**
Every ClaudIO module is a UV script with inline dependencies (`# /// script`). No virtual environments, no dependency hell, no installation complexity. Just run `uv run module.py`.

### 3. **Experience-Driven Evolution**
ClaudIO learns from every interaction. Usage logs become training data. MIPROv2 optimization continuously improves expert routing, tool selection, and response quality—achieving 20-100% accuracy gains over static prompts.

---

## 🔬 What ClaudIO Is (And Isn't)

### ✅ ClaudIO IS:
- A **standalone AI agent** built with DSPy + UV
- A **self-optimizing orchestrator** for scientific computing workflows
- A **learning system** that improves from collected usage data
- A **reproducible research tool** with deterministic compilation
- A **local-first agent** supporting Ollama, LM Studio, and privacy-preserving HPC
- A **multi-expert system** with learned routing and tool orchestration

### ❌ ClaudIO IS NOT:
- A Claude Code plugin or extension
- An MCP server or configuration layer
- A wrapper around existing AI assistants
- A prompt engineering template collection
- Dependent on proprietary cloud services

---

## 🏗️ Architecture Foundation

Based on proven patterns from `warpio_dspy_poc/` and research in `ai-docs/DSPY_FOR_WARPIO.md`:

```
┌─────────────────────────────────────────────────────────┐
│              ClaudIO Core Architecture                  │
├─────────────────────────────────────────────────────────┤
│                                                         │
│  1. DSPy Modules (Composable Components)                │
│     ├─ Claudio-Orchestrator (main router)               │
│     ├─ DataExpert (HDF5, ADIOS, Parquet + more)         │
│     ├─ HPCExpert (SLURM, MPI, logging + tools)          │
│     ├─ AnalysisExpert (viz, stats, ML + codegen)        │
│     ├─ ResearchExpert (papers, citations + search)      │
│     └─ WorkflowExpert (automation, pipelines, tracing)  │
│                                                         │
│  2. Declarative Signatures (Input → Output Specs)       │
│     ├─ OrchestratorSignature: task → expert, strategy   │
│     ├─ DataExpertSignature: file → analysis, actions    │
│     └─ [...custom signatures for each domain...]        │
│                                                         │
│  3. Self-Optimization Engine                            │
│     ├─ Usage log collection (every interaction)         │
│     └─ Continuous improvement cycle                     │
│                                                         │
│  4. MCP Tool Integration                                │
│     ├─ Scientific tools as DSPy tools                   │
│     ├─ ReAct agents with tool calling                   │
│     ├─ Automatic tool selection via optimization        │
│     └─ Graceful fallbacks when MCPs unavailable         │
│                                                          │
└─────────────────────────────────────────────────────────┘
```

---

## 📦 Repository Structure Vision

New repository at `github.com/iowarp/claudio` currently:

```
.
├── ai-docs
│   ├── 00_DOCUMENTATION_INDEX.md
│   ├── 00_START_HERE.md
│   ├── architecture
│   │   ├── CLAUDIO_ARCHITECTURE.md
│   │   ├── EXPERT_SYSTEM_DESIGN.md
│   │   ├── MCP_TOOL_INTEGRATION.md
│   │   └── OPTIMIZATION_STRATEGY.md
│   ├── foundation
│   │   ├── 01_DSPY_FUNDAMENTALS.md
│   │   ├── 02_SIGNATURES_GUIDE.md
│   │   ├── 03_MODULES_GUIDE.md
│   │   ├── 04_OPTIMIZATION_GUIDE.md
│   │   └── 05_LM_INTEGRATION.md
│   ├── poc
│   │   ├── chat.py
│   │   ├── config.py
│   │   ├── experts.py
│   │   ├── LEARNINGS.md
│   │   ├── orchestrator.py
│   │   └── README.md
│   ├── reference
│   │   ├── DSPY_API_REFERENCE.md
│   │   ├── MCP_TOOLS_CATALOG.md
│   │   ├── TROUBLESHOOTING.md
│   │   └── UV_SCRIPTS_GUIDE.md
│   └── research
│       ├── ADVANCED_PATTERNS.md
│       └── MULTI_AGENT_SYSTEMS.md
└── IDEA.md (this file)
```


```
claudio/
├── README.md                     # Comprehensive vision + quick start
├── LICENSE                       # Apache 2.0
├── pyproject.toml               # UV-compatible project definition
│
├── claudio/                     # Core agent implementation
│   ├── __init__.py
│   ├── orchestrator.py          # Main ClaudIOOrchestrator (UV script)
│   ├── experts/                 # Expert modules
│   │   ├── __init__.py
│   │   ├── data_expert.py       # HDF5, ADIOS, Parquet expert (UV)
│   │   ├── hpc_expert.py        # SLURM, MPI, performance (UV)
│   │   ├── analysis_expert.py   # Visualization, stats, ML (UV)
│   │   ├── research_expert.py   # Papers, citations, context7 (UV)
│   │   └── workflow_expert.py   # Automation, Jarvis (UV)
│   ├── signatures/              # DSPy signature definitions
│   │   ├── __init__.py
│   │   ├── orchestrator_sig.py
│   │   ├── data_sig.py
│   │   └── [...]
│   ├── tools/                   # MCP tool wrappers
│   │   ├── __init__.py
│   │   ├── mcp_wrapper.py       # Base MCP calling logic
│   │   ├── data_tools.py        # hdf5, adios, parquet wrappers
│   │   ├── hpc_tools.py         # slurm, darshan wrappers
│   │   └── [...]
│   ├── optimizers/              # Custom optimization logic
│   │   ├── __init__.py
│   │   ├── usage_logger.py      # Collect training examples
│   │   ├── metric.py            # Evaluation metrics
│   │   └── auto_optimize.py     # Scheduled optimization runs
│   ├── ui/                      # User interfaces
│   │   ├── __init__.py
│   │   ├── cli.py              # Command-line interface with Rich-based TUI (like warpio_dspy_poc)
│   │   └── api.py              # FastAPI server (fully supporting SSE from MCP and serving at a port)
│   └── config.py               # LM configuration (Ollama/OpenAI/local)
│
├── examples/                    # Working examples
│   ├── basic_usage.py          # Simple Q&A interaction
│   ├── hdf5_optimization.py    # Optimize HDF5 file workflow
│   ├── multi_expert_collab.py # Complex multi-domain task
│   └── local_ai_setup.py      # Using Ollama/LM Studio
│
├── data/                       # Training and optimization data
│   ├── usage_logs/            # Collected interaction logs
│   ├── training_sets/         # Curated training examples
│   ├── compiled/              # Optimized module artifacts
│   └── metrics/               # Performance tracking
│
├── tests/                      # Comprehensive testing
│   ├── test_orchestrator.py
│   ├── test_experts/
│   ├── test_optimization.py
│   └── test_tools.py
│
├── claudio-docs/                       # Documentation
│   ├── architecture.md         # Detailed architecture
│   ├── dspy_patterns.md       # DSPy usage patterns
│   ├── optimization_guide.md  # How to optimize ClaudIO
│   ├── tool_integration.md    # Adding new MCP tools
│   └── deployment.md          # Production deployment
│
├── scripts/                    # Utility scripts
│   ├── setup.sh               # One-command setup
│   ├── optimize.py            # Run optimization cycle
│   ├── benchmark.py           # Performance benchmarking
│   └── collect_logs.py        # Aggregate usage logs
│
└── research/                   # Research artifacts
    ├── DSPY_FOUNDATION.md     # Core DSPy concepts for ClaudIO
    ├── OPTIMIZATION_RESULTS.md # Tracking optimization gains
    └── EVOLUTION_LOG.md       # How ClaudIO improved over time
```

---

## 🚀 Core Design Principles

### Principle 1: **Declarative Over Imperative**

```python
# ❌ OLD WAY (Manual prompting - like old Warpio)
prompt = """
You are a data expert. When analyzing HDF5:
- Check compression (gzip-6 recommended)
- Validate chunking (auto-detect or 100,100,100)
- [... 50 more lines of manual instructions ...]
"""

# ✅ NEW WAY (DSPy signatures - ClaudIO)
class DataExpertSignature(dspy.Signature):
    """Analyze scientific data files and provide optimization recommendations."""
    
    filepath: str = dspy.InputField(desc="Path to data file")
    file_format: str = dspy.InputField(desc="Format: hdf5, adios, parquet")
    
    analysis: str = dspy.OutputField(desc="Detailed analysis of file structure")
    recommendations: list[str] = dspy.OutputField(desc="Actionable optimization steps")
    mcp_commands: list[dict] = dspy.OutputField(desc="MCP tool commands to execute")

# DSPy automatically generates and optimizes the prompt!
```

### Principle 2: **Self-Contained UV Execution**

```python
#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "dspy-ai>=2.6.0",
#   "rich>=13.0.0",
# ]
# ///

"""
ClaudIO Data Expert - UV Self-Contained Script
Run directly: uv run data_expert.py
"""

import dspy
from rich import print

# Everything needed is declared inline!
# No pip install, no venv, just run!
```

### Principle 3: **Optimization-First Development**

```python
# Every ClaudIO module follows this pattern:

# 1. Define signature (what we want)
class ExpertSignature(dspy.Signature):
    """Expert behavior specification."""
    task: str = dspy.InputField()
    result: str = dspy.OutputField()

# 2. Create module (how to do it)
class Expert(dspy.Module):
    def __init__(self):
        self.generate = dspy.ChainOfThought(ExpertSignature)
    
    def forward(self, task):
        return self.generate(task=task)

# 3. Collect usage examples (real interactions)
trainset = load_usage_logs("data/usage_logs/")

# 4. Optimize (automatic improvement!)
optimizer = dspy.MIPROv2(metric=quality_metric)
optimized_expert = optimizer.compile(Expert(), trainset=trainset)

# 5. Deploy optimized version
optimized_expert.save("data/compiled/expert_v2.json")

# Result: 30%+ improvement without manual prompt engineering!
```

### Principle 4: **Tool Integration via ReAct**

```python
# ClaudIO experts use DSPy's ReAct for tool calling

def hdf5_analyze(filepath: str) -> dict:
    """Analyze HDF5 file structure and performance.
    
    Use when: User asks about HDF5 optimization, compression, or chunking.
    """
    return call_mcp("hdf5", "analyze", {"filepath": filepath})

class DataExpert(dspy.Module):
    def __init__(self):
        self.agent = dspy.ReAct(
            DataExpertSignature,
            tools=[hdf5_analyze, hdf5_optimize, adios_convert, ...]
        )
    
    def forward(self, task, filepath):
        # ReAct automatically decides when/how to call tools!
        return self.agent(task=task, filepath=filepath)
```

---

## 🎓 Relationship to Research & POC

ClaudIO builds on extensive DSPy research documented in `ai-docs/`:

| Research Document | How It Informs ClaudIO |
|-------------------|------------------------|
| `DSPY_FOR_WARPIO.md` | Core architecture patterns, optimization strategies |
| `WARPIO_DSPY_ARCHITECTURE_MAPPING.md` | Mapping old Warpio concepts to DSPy modules |
| `warpio_dspy_poc/` | Working proof-of-concept validating the approach |

**POC Evolution:**
```
warpio_dspy_poc/           →    claudio/
├── chat.py (basic TUI)    →    claudio/ui/tui.py (enhanced)
├── orchestrator.py        →    claudio/orchestrator.py (production)
├── experts.py            →    claudio/experts/*.py (domain-specific)
└── config.py             →    claudio/config.py (extended)
```

**Key Learnings from POC:**
1. ✅ DSPy ChainOfThought routing works excellently
2. ✅ UV inline scripts provide perfect self-containment
3. ✅ Rich TUI is intuitive for scientific users
4. ✅ LM Studio integration enables local-first AI
5. ⏭️ Need to add: MCP tool integration, optimization cycles, production deployment

---

## 🔄 Evolution Through Usage

ClaudIO's breakthrough feature: **it gets better over time**.

```
Week 1: Baseline Performance
├─ Manual signatures, no optimization
├─ Generic expert routing
└─ Basic tool usage

↓ [Collect 50 usage examples]

Week 2: First Optimization (BootstrapFewShot)
├─ 15-25% accuracy improvement
├─ Better expert selection
└─ More relevant tool usage

↓ [Collect 200 more examples]

Week 4: Advanced Optimization (MIPROv2)
├─ 30-50% accuracy improvement
├─ Optimized instructions + demonstrations
└─ Learned tool orchestration patterns

↓ [Collect 1000+ examples]

Month 3: Domain-Adapted Expert
├─ 50-100% accuracy improvement
├─ Institution-specific knowledge
└─ HPC-optimized patterns
```

**Continuous Optimization Loop:**
```python
# Scheduled optimization (e.g., weekly)
def optimize_claudio():
    # 1. Load new usage logs
    new_examples = load_logs_since_last_optimization()
    
    # 2. Augment training set
    trainset = existing_trainset + new_examples
    
    # 3. Run optimization
    optimizer = dspy.MIPROv2(metric=scientific_quality_metric)
    improved = optimizer.compile(current_expert, trainset=trainset)
    
    # 4. A/B test: old vs new
    if improved.score > current_expert.score * 1.1:  # 10% improvement
        deploy(improved, version="v2.5.3")
    
    # ClaudIO just got smarter!
```

---

## 🛠️ Initial Implementation Roadmap

### Phase 1: Core Foundation (Weeks 1-2)
- [x] Research DSPy patterns (DONE - see `ai-docs/`)
- [x] Proof of concept (DONE - see `warpio_dspy_poc/`)
- [ ] Repository setup and structure
- [ ] Basic orchestrator with 3 experts (data, HPC, analysis)
- [ ] UV script infrastructure
- [ ] Simple TUI for testing
- [ ] MCP tool wrapper foundation

### Phase 2: Tool Integration (Weeks 3-4)
- [ ] Wrap 5+ MCP tools as DSPy tools
- [ ] Implement ReAct agents for each expert
- [ ] Add tool error handling and fallbacks
- [ ] Usage logging system
- [ ] Basic optimization with 20-30 examples

### Phase 3: Optimization Engine (Weeks 5-8)
- [ ] Automated usage log collection
- [ ] BootstrapFewShot optimization pipeline
- [ ] MIPROv2 instruction optimization
- [ ] Metric system for scientific quality
- [ ] A/B testing framework
- [ ] Continuous optimization scheduler

### Phase 4: Production Deployment (Weeks 9-12)
- [ ] FastAPI server option
- [ ] MLflow integration for tracking
- [ ] Documentation and examples
- [ ] Benchmark suite
- [ ] Multi-user support
- [ ] HPC cluster deployment guide

---

## 💡 Key Differentiators

| Feature | Traditional AI Agents | ClaudIO |
|---------|----------------------|---------|
| **Prompting** | Manual prompt engineering | Declarative signatures, auto-optimized |
| **Improvement** | Requires expert tuning | Learns from usage automatically |
| **Dependencies** | Complex installation | UV self-contained scripts |
| **Tools** | Hard-coded integrations | Dynamic tool learning via ReAct |
| **Reproducibility** | Prompt drift over time | Compiled, versioned artifacts |
| **Local AI** | Cloud-dependent | First-class Ollama/LM Studio support |
| **Scientific Focus** | General purpose | HPC-optimized, research-aware |

---

## 🎯 Success Metrics

ClaudIO will be considered successful when:

1. **Performance**: 30%+ accuracy improvement over manual prompts (measured on held-out test set)
2. **Adoption**: 10+ research groups using ClaudIO for HPC workflows
3. **Evolution**: Demonstrable improvement from Week 1 to Month 3 usage
4. **Tool Efficiency**: 50%+ reduction in unnecessary MCP tool calls via learned optimization
5. **Reproducibility**: Same compiled ClaudIO produces identical results across runs
6. **Local Viability**: Ollama-based ClaudIO achieves 80%+ of GPT-4 quality on domain tasks

---

## 📚 Documentation Structure for New Repo

ClaudIO documentation will include:

1. **README.md**: Vision, quick start, compelling examples
2. **ARCHITECTURE.md**: Deep dive into DSPy modules, signatures, optimization
3. **QUICKSTART.md**: 5-minute setup to first interaction
4. **OPTIMIZATION_GUIDE.md**: How to collect data and run optimization cycles
5. **TOOL_INTEGRATION.md**: Adding new MCP tools as DSPy tools
6. **DEPLOYMENT.md**: Production deployment on HPC clusters
7. **RESEARCH.md**: Scientific computing patterns and best practices
8. **API_REFERENCE.md**: Complete API documentation
9. **CONTRIBUTING.md**: How to extend ClaudIO
10. **CHANGELOG.md**: Evolution log showing improvements

---


**This is ClaudIO**: A self-improving scientific computing agent that grows through experience. 

## Repository Structure

Create a proper project structure, like the example below:

```
claudio/
├── README.md (comprehensive, professional)
├── LICENSE (Apache 2.0)
├── CONTRIBUTING.md
├── CODE_OF_CONDUCT.md
├── SECURITY.md
├── .github/
│   ├── ISSUE_TEMPLATE/
│   │   ├── bug_report.md
│   │   ├── feature_request.md
│   │   └── memory_provider.md
│   ├── workflows/
│   │   ├── ci.yml
│   │   ├── release.yml
│   │   └── docs.yml
│   └── FUNDING.yml
├── claudio/
│   ├── __init__.py
│   ├── core/
│   │   ├── __init__.py
│   │   ├── memory.py (base memory classes)
│   │   ├── context.py (context management)
│   │   ├── persistence.py (storage backends)
│   │   └── orchestrator.py (main engine)
│   ├── providers/
│   │   ├── __init__.py
│   │   ├── base.py (abstract provider)
│   │   ├── anthropic_provider.py
│   │   ├── openai_provider.py
│   │   ├── google_provider.py
│   │   └── local_provider.py
│   ├── storage/
│   │   ├── __init__.py
│   │   ├── filesystem.py
│   │   ├── redis.py
│   │   ├── postgres.py
│   │   └── distributed.py (for HPC)
│   ├── utils/
│   │   ├── __init__.py
│   │   ├── serialization.py
│   │   ├── compression.py
│   │   └── monitoring.py
│   └── cli.py
├── examples/
│   ├── basic_usage.py
│   ├── claude_integration.py
│   ├── gpt4_integration.py
│   ├── scientific_workflow.py
│   └── multi_agent_coordination.py
├── tests/
│   ├── __init__.py
│   ├── test_core/
│   ├── test_providers/
│   └── test_integration/
├── benchmarks/
│   ├── memory_performance.py
│   ├── provider_comparison.py
│   └── scaling_tests.py
├── scripts/
│   ├── setup.sh
│   ├── install_hpc.sh
│   └── benchmark.sh
├── pyproject.toml
├── setup.py
├── requirements.txt
├── requirements-dev.txt
└── .gitignore
```

## Key Positioning Points:

- CLAUDIO is NOT "Claude" - it's universal agent for science tasks.
- Works great with Claude, but equally well with ALL major LLMs
- Focus on scientific computing and HPC
- Part of the IOWarp ecosystem

---

This positions CLAUDIO perfectly:
1. **Universal tool** that works with any LLM
2. **Scientific computing focus** differentiates it
3. **Provider-agnostic** avoids trademark issues
4. **"Happens to work great with Claude"** - wink wink
5. **IOWarp branding** gives it legitimacy

