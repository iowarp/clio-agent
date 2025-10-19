# ClaudIO v0.1.0 Quick Test Guide

For students testing the system.

## Prerequisites

1. LM Studio running at `http://100.127.255.164:1234`
2. Model loaded: `gpt-oss-20b` or similar
3. UV installed: `curl -LsSf https://astral.sh/uv/install.sh | sh`

---

## Component Tests (5 minutes)

```bash
# 1. Config (should connect to LM Studio)
uv run src/claudio/config.py
# Expected: "✓ Using LM Studio" + simple prediction "What is 2+2?" → "4"

# 2. Signatures (should show structure)
uv run src/claudio/signatures/orchestrator_sig.py
uv run src/claudio/signatures/expert_sig.py
# Expected: Field descriptions for all signatures

# 3. Orchestrator (should route 5/5 correctly)
uv run src/claudio/orchestrator.py
# Expected:
# - HDF5 question → data expert ✓
# - SLURM question → hpc expert ✓
# - Visualization → analysis expert ✓
# - Papers → research expert ✓
# - Pipeline → workflow expert ✓

# 4. DataExpert (should work in both modes)
uv run src/claudio/experts/data_expert.py
# Expected:
# - ChainOfThought mode: analysis + recommendations
# - ReAct mode: tool calls visible
```

---

## CLI Test (Interactive)

```bash
uv run src/claudio/ui/cli.py

# Commands to try:
/help        # Shows commands
/experts     # Lists 5 experts with capabilities
/verbose     # Toggles routing details
/history     # Shows conversation
/quit        # Exit
```

### Test Questions

**Data I/O**:
```
How do I optimize my 100GB HDF5 file for parallel I/O on 64 cores?
```
Expected: Routes to DATA expert, provides compression/chunking advice

**HPC Performance**:
```
My SLURM job uses 256 nodes but takes 10 hours instead of 2. Help me debug it.
```
Expected: Routes to HPC expert, provides diagnosis + solution

**Analysis**:
```
I have 1 million timesteps of simulation data. How do I visualize trends?
```
Expected: Routes to ANALYSIS expert, provides approach + code example

**Research**:
```
Find recent papers on I/O optimization for exascale computing.
```
Expected: Routes to RESEARCH expert, provides findings + methodology

**Workflow**:
```
Automate my daily data processing: download, simulate, analyze, plot.
```
Expected: Routes to WORKFLOW expert, provides design + implementation

---

## Expected Behavior

### Routing
- Orchestrator should select correct expert for each question
- Routing reasoning should be logical
- No errors in expert selection

### Responses
- Each expert returns its specific output format
- Data: analysis + recommendations
- HPC: diagnosis + solution
- Analysis: approach + code_example
- Research: findings + methodology
- Workflow: design + implementation

### CLI
- ASCII art displays correctly
- Colors: blue (#00B4FF), green (#00FF88), orange (#FF8800)
- Panels with expert name in title
- Markdown rendering works

---

## Troubleshooting

**LM Studio not connecting**:
```bash
# Check LM Studio is running
curl http://100.127.255.164:1234/v1/models

# Verify model loaded in LM Studio UI
```

**Import errors**:
```bash
# UV handles dependencies automatically, but check:
uv --version  # Should be installed

# If issues, try:
uv cache clean
```

**Expert errors**:
```bash
# Check signature matches forward() arguments
# Each expert has specific context field:
# - data: file_context
# - hpc: cluster_context
# - analysis: data_context
# - research: research_context
# - workflow: workflow_context
```

---

## Success Criteria

- [ ] All component tests pass
- [ ] CLI starts without errors
- [ ] 5/5 test questions route correctly
- [ ] Expert responses are structured properly
- [ ] No DSPy mentions in CLI output (internal only)
- [ ] ASCII art displays correctly
- [ ] Students can run and understand code

---

**Version**: 0.1.0
**Ready for**: Student handoff and continued development
