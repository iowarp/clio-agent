# ClaudIO Development Rules for AI Agents

Essential rules and guidelines for AI agents implementing ClaudIO.

---

## Core Principles

**ClaudIO IS**: Self-improving autonomous agent for scientific data (not a framework)
**Architecture**: 3-Tier Orchestration + ARC Memory + Optimizer Layer + IOWarp Integration
**Current Version**: v0.1.0 (baseline)
**Target Version**: v0.5.0 (public beta)

---

## Locked-Down Stack

### Required Dependencies

```python
# Core
"dspy-ai>=3.0.3"            # Agent patterns + optimizers (INTERNAL)
"fastmcp>=2.13.0"           # MCP protocol

# UI
"rich>=14.2.0"              # Terminal UI

# Memory Layer (v0.2.0+)
"sortedcontainers>=2.4.0"   # B-tree index
"lru-dict>=1.3.0"           # LRU cache
"msgpack>=1.1.0"            # Serialization

# Optimizer Layer (v0.4.0+)
"scipy>=1.11.0"             # Statistical tests
"numpy>=1.24.0"             # Numerical ops

# Tools (v0.3.0+)
"h5py>=3.10.0"              # HDF5
"pyarrow>=14.0.0"           # Parquet
```

### Forbidden

**DO NOT add** without approval:
- LangChain, CrewAI, AutoGen as core dependencies (external integration only)
- Heavy ML frameworks (TensorFlow, PyTorch)
- Database ORMs (SQLAlchemy)

**Python Version**: >= 3.12 (LOCKED)

---

## Critical Rules (NON-NEGOTIABLE)

### RULE 1: Follow PLAN.md Task Order
- Implement phases sequentially: v0.2.0 → v0.3.0 → v0.4.0 → v0.5.0
- Complete tasks within phase before moving to next

### RULE 2: Never Break v0.1.0 Baseline
- Main agent, DataExpert, CLI must always work
- Test before committing: `uv run src/claudio/ui/cli.py`

### RULE 3: DSPy is Internal Implementation Detail
- Use DSPy internally
- **DO NOT expose** in user-facing docs or public APIs
- **Exception**: CLAUDE.md and code comments only

### RULE 4: All Data Goes Through ARC
- Store conversations, invocations, metrics in ARC
- **DO NOT create** separate storage systems

### RULE 5: Performance Targets
- Cache hit rate > 85%
- ARC retrieval < 10ms (O(log N))
- Tool cache hit rate > 50%

### RULE 6: Test Coverage > 80%
- Unit tests for all new code
- Integration tests for critical paths
- Run `pytest tests/` before committing

### RULE 7: Type Hints + Docstrings
- Type hints on all functions
- Google-style docstrings
- Examples in docstrings

---

## Architecture DOs and DONTs

### 3-Tier Hierarchy

✅ **DO**: Tier 1 (Main) → Tier 2 (Experts) → Tier 3 (Nanoagents)
❌ **DON'T**: Skip tiers or mix responsibilities

### Agent Registry

✅ **DO**: Use registry for capability-based routing
❌ **DON'T**: Hardcode if/else routing logic

### ARC Memory

✅ **DO**: Check cache before expensive operations
❌ **DON'T**: Skip caching or use O(N) algorithms

### Optimizers

✅ **DO**: Validate before deploying optimized variants
❌ **DON'T**: Deploy without statistical significance test

---

## Development Workflow

1. Read PLAN.md for current task
2. Read PROJECT_STRUCTURE.md for file locations
3. Read relevant architecture docs (SYSTEM_IDENTITY, CLAUDIO_ARCHITECTURE, etc.)
4. Implement with type hints + docstrings
5. Write tests (>80% coverage)
6. Run: `pytest tests/ && ruff check src/`
7. Commit with clear message: `feat: <description>`

---

## File Organization

### Current (v0.1.0) ✅
```
src/claudio/
├── config.py
├── claudio.py
├── signatures/
├── experts/data_expert.py
└── ui/cli.py
```

### Target Structure
See `PROJECT_STRUCTURE.md` for full 70+ file layout

---

## Common Patterns

### Pattern 1: ARC Cache-First
```python
cached = arc.get_cached_tool_result(tool, params)
if cached:
    return cached
result = execute_tool(tool, params)
arc.cache_tool_result(tool, params, result)
```

### Pattern 2: Store Metrics
```python
start = time.time()
result = expert.forward(query)
arc.store_invocation({
    "duration_ms": (time.time() - start) * 1000,
    "success": True,
    ...
})
```

### Pattern 3: Registry Routing
```python
routing = registry.route_query(query)
expert = get_agent(routing.selected_agent)
arc.store_routing_decision(session_id, routing)
```

---

## Testing Requirements

- Unit tests: `tests/test_<module>/`
- Integration tests: `tests/test_integration/`
- Coverage: > 80%
- Run before commit: `pytest tests/`

---

## Error Handling

**Graceful Degradation**:
- IOWarp unavailable → file-based storage
- ARC unavailable → continue without memory (warn user)
- MCP server down → pure reasoning mode
- Optimizer fails → keep current variant

---

## Git Workflow

**Commit Format**: `<type>: <subject>`
- `feat`: New feature
- `fix`: Bug fix
- `refactor`: Code refactoring
- `test`: Tests
- `docs`: Documentation

**Before Commit**:
- [ ] Tests passing
- [ ] Lint clean: `ruff check src/`
- [ ] Type check: `mypy src/claudio/`

---

## Quick Reference

**Read First**:
- `PLAN.md` - What to build
- `PROJECT_STRUCTURE.md` - Where files go
- `docs/CLAUDIO_ARCHITECTURE.md` - How it all fits together

**Test**:
```bash
pytest tests/
ruff check src/
mypy src/claudio/
```

**Run**:
```bash
uv run src/claudio/ui/cli.py
```

---

**THIS IS YOUR REFERENCE. KEEP IT SIMPLE. FOLLOW PLAN.MD.**
