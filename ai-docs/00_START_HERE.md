# 🚀 Start Here: ClaudIO Documentation Guide

Welcome to ClaudIO documentation! This guide will get you oriented and productive quickly.

## Quick Navigation

- **New to ClaudIO?** → Read [IDEA.md](../IDEA.md) (5 min) then [CLAUDIO_ARCHITECTURE.md](architecture/CLAUDIO_ARCHITECTURE.md) (15 min)
- **New to DSPy?** → Start with [foundation/01_DSPY_FUNDAMENTALS.md](foundation/01_DSPY_FUNDAMENTALS.md)
- **Want to see it working?** → Check [poc/README.md](poc/README.md) and run the POC
- **Ready to build?** → Read all [foundation/](foundation/) docs, then [architecture/](architecture/)
- **Need quick answers?** → See [reference/](reference/) docs

## Documentation Structure

```
docs/
├── 00_START_HERE.md           ← You are here
├── 00_DOCUMENTATION_INDEX.md  ← Full documentation map
│
├── foundation/                ← Core DSPy concepts (read first!)
│   ├── 01_DSPY_FUNDAMENTALS.md
│   ├── 02_SIGNATURES_GUIDE.md
│   ├── 05_LM_INTEGRATION.md
│   └── ...
│
├── architecture/              ← ClaudIO-specific design
│   ├── CLAUDIO_ARCHITECTURE.md
│   └── ...
│
├── poc/                       ← Working proof-of-concept
│   ├── README.md
│   ├── chat.py, orchestrator.py, experts.py
│   └── LEARNINGS.md
│
├── research/                  ← Advanced topics
│   ├── ADVANCED_PATTERNS.md
│   └── MULTI_AGENT_SYSTEMS.md
│
└── reference/                 ← Quick lookup
    ├── UV_SCRIPTS_GUIDE.md
    └── ...
```

## Recommended Reading Order

### Path 1: Quick Understanding (2-3 hours)
1. [IDEA.md](../IDEA.md) - Vision and goals
2. [architecture/CLAUDIO_ARCHITECTURE.md](architecture/CLAUDIO_ARCHITECTURE.md) - How it works
3. [poc/README.md](poc/README.md) - Working example
4. Run POC: `cd poc && uv run chat.py`

### Path 2: Full Implementation (1-2 days)
1. All [foundation/](foundation/) docs (3-4 hours)
2. All [architecture/](architecture/) docs (2 hours)
3. Study [poc/](poc/) code (1 hour + hands-on)
4. Reference [research/](research/) as needed

### Path 3: Specific Feature
- **Adding expert** → architecture/EXPERT_SYSTEM_DESIGN.md + poc/experts.py
- **Adding tool** → architecture/MCP_TOOL_INTEGRATION.md
- **Optimization** → research/OPTIMIZATION_ALGORITHMS.md
- **Production** → research/ADVANCED_PATTERNS.md

## Key Concepts You'll Learn

1. **DSPy Paradigm**: Programming LMs, not prompting them
2. **Signatures**: Declarative I/O specifications
3. **Modules**: Composable LM components
4. **Optimizers**: Self-improvement from usage data
5. **UV Scripts**: Self-contained Python with inline deps
6. **ReAct Agents**: Tool-calling LM agents
7. **MCP Integration**: Scientific computing tools
8. **Local AI**: Ollama/LM Studio for privacy

## Prerequisites

- **Python 3.11+**: ClaudIO requires modern Python
- **UV installed**: `curl -LsSf https://astral.sh/uv/install.sh | sh`
- **Basic LM knowledge**: Understanding of LLMs/AI helpful
- **Scientific computing**: Familiarity with HPC/data tools beneficial

## Getting Help

- **Concept unclear?** → Check [00_DOCUMENTATION_INDEX.md](00_DOCUMENTATION_INDEX.md) for cross-references
- **Code not working?** → See [reference/TROUBLESHOOTING.md](reference/TROUBLESHOOTING.md)
- **Missing info?** → Check [research/](research/) deep dives

## What's NOT Here

- ❌ Claude Code plugin documentation (see claude-code-4-science repo)
- ❌ Prompt engineering guides (DSPy replaces prompting)
- ❌ General Python tutorials (assumed knowledge)

## Next Steps

1. **Read** [IDEA.md](../IDEA.md) to understand ClaudIO's vision
2. **Browse** [00_DOCUMENTATION_INDEX.md](00_DOCUMENTATION_INDEX.md) to see what's available
3. **Start** with foundation docs if new to DSPy
4. **Run** the POC to see it working
5. **Build** following the architecture docs

Happy building! 🚀

---

*Last Updated: 2025-01-18*
