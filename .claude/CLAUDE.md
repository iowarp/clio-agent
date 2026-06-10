# CLIO Agent Development Rules for AI Agents

Essential rules and guidelines for AI agents implementing CLIO Agent.

---

## ⚑ SUPERSEDING PRINCIPLES (read first — these override anything below)

These were learned the hard way grinding the model-agnostic marketplace across model
families (qwopus/LM Studio, gpt-oss/gemma/nemotron/ALCF). When older rules below
conflict with these, **these win.** Much of the "Locked-Down Stack" / numbers below
is stale or aspirational — do not treat it as ground truth.

1. **NO deterministic decision-making in clio core.** clio MUST NOT decide routing,
   completion, "pending work," or re-work via keyword/phrase/heuristic matching on a
   model's prose, nor fabricate a decision the model didn't make. The **parent agent
   (the model) is the router/decider** — it expresses next steps via its *structured
   output* (`expert_handoffs`: run them; their absence: it decided it's done). clio's
   job is to **carry results back, execute the stated handoffs, and — when one seems
   missing — RE-ASK the parent** (bounded repair) and let *it* decide. Deterministic
   barriers enable the easy case and break every new model/use-case; they do not scale.
   *(Anti-patterns removed this session: a prose-keyword "pending work" detector, a
   "self-contract" path that scraped prose and fabricated a synthetic prediction, a
   completed-child guard. If you find more, kill them — see issues #38/#39.)*

2. **Allowed vs forbidden in core.** clio MAY *surface/gate on reality* — file/path
   exists, HITL block, auth gate, **schema-validate**, and **format-only error
   correction** with no semantic change (the lenient constructor-repr→JSON adapter;
   a declared Pydantic field default). clio must NOT *silently fix / reroute / scrub /
   decide* via case-logic or keyword heuristics.

3. **Fix the root in code/data-flow, don't bolt constraints onto prompts.** Do not add
   `EXACTLY ONCE` / `no &&` / "use column X" prose to expert `.md`s to suppress an
   observed failure — that overfits to tested cases, bloats context (which *causes*
   small-model failures), and forecloses edge cases. Fix the cause: lean tool outputs,
   non-hanging shell, forward the discovered columns, pin staging dirs as infra. Prompt
   editing IS a valid lever for *understanding/grounding* (telling the model what state
   means), just not for hard behavioral handcuffs.

4. **Be the trace-driven driver (the loop).** (1) run the target test, (2) read the
   FULL trace — `~/.config/clio-agent/messages/sess_*.json` + `run.extra.workflow_state`,
   not the summary; add instrumentation where understanding is thin, (3) hypothesize ONE
   cause, (4) verify cheaply (~30s probe) before any multi-minute rerun, (5) fix the root,
   (6) one change per rerun, regression-check, advance. Don't run blindly; don't declare
   blocked; dig. Deterministic code is only ever the *last* error-correction barrier.

5. **DSPy is the internal engine AND the reference.** For typed-output/adapter/signature
   semantics, the source of truth is the **DSPy source in `docs/ref/dspy/`** (not guesswork,
   not this file). Key facts: a Signature *is* a `pydantic.BaseModel`; each field is a
   `FieldInfo`; `dspy.OutputField(**pydantic_kwargs)` forwards every Pydantic constraint/
   default; `adapters/utils.py::parse_value` does `json_repair`→`ast.literal_eval`→
   `TypeAdapter(annotation).validate_python` — so **Pydantic field defaults are honored**
   when a model omits a key (the recommended way to make a field droppable-with-a-value),
   required fields raise `ValidationError`, and `use_json_adapter_fallback=False` is the
   sanctioned way to get a hard error instead of the lenient JSON fallback.

6. **Stale numbers below are NOT ground truth.** `max_tokens=4096` and the "T1: 2K /
   T2: 4K token budgets" were disproven — models expose 128K–262K context and clio should
   discover+use it (handshake plan). Treat such figures as historical, not targets.

---

## Core Principles

**CLIO Agent IS**: Self-improving autonomous agent for scientific data (not a framework)
**Architecture**: 3-Tier Orchestration + ARC Memory + Optimizer Layer + IOWarp Integration
**Internal Engine**: DSPy 3.x (signatures, modules, optimizers) + FastMCP 3.x (tool servers)

---

## Locked-Down Stack

### Required Dependencies

```python
# Core
"dspy-ai>=3.1.0"             # Agent patterns + optimizers (INTERNAL)
"fastmcp>=3.0.0"             # MCP protocol + gateway composition

# UI
"rich>=14.2.0"               # Terminal UI

# Memory Layer
"sortedcontainers>=2.4.0"    # B-tree index
"lru-dict>=1.3.0"            # LRU cache
"msgspec>=0.18.0"            # Serialization

# Optimizer Layer (Phase 3+)
"scipy>=1.11.0"              # Statistical tests
"numpy>=1.24.0"              # Numerical ops

# Tools (Phase 1+)
"h5py>=3.10.0"               # HDF5 MCP server
"pyarrow>=14.0.0"            # Parquet MCP server (Phase 2+)
```

### Forbidden

**DO NOT add** without approval:
- LangChain, CrewAI, AutoGen as core dependencies (external integration only via A2A)
- Heavy ML frameworks (TensorFlow, PyTorch)
- Database ORMs (SQLAlchemy)
- Custom async/sync bridge code (use native DSPy/FastMCP instead)

**Python Version**: >= 3.12 (LOCKED)

---

## Critical Rules (NON-NEGOTIABLE)

### RULE 1: Follow PLAN.md Phase Order
- Implement phases sequentially: Phase 1 -> Phase 2 -> ... -> Phase 6
- Complete tasks within a phase before moving to next
- Never skip phases or cherry-pick tasks from later phases

### RULE 2: Never Break Baseline
- Main agent, DataExpert, CLI must always work
- Test before committing: `uv run src/clio_agent/ui/cli.py`
- If a change breaks baseline, revert first, then fix

### RULE 3: DSPy is Internal Implementation Detail
- Use DSPy internally for signatures, modules, optimizers
- **DO NOT expose** DSPy in user-facing docs, APIs, or error messages
- **Exception**: CLAUDE.md and code comments only

### RULE 4: All Data Goes Through ARC
- Store conversations, invocations, metrics in ARC
- **DO NOT create** separate storage systems, databases, or caches

### RULE 5: Tool Curation (Max 5-7 Per Expert)
- Each expert gets 5-7 high-level curated tools, not every atomic operation
- Hide implementation complexity behind composite tools
- Document each tool with an "agent story" (when/why an agent would use it)
- **DO NOT auto-generate** tools from OpenAPI specs or file system scans

### RULE 6: Context is Compiled, Not Concatenated
- Never dump raw conversation history into prompts
- Use context compilation pipeline: filter -> compact -> enrich -> assemble
- Set context budgets per tier (T1: 2K tokens, T2: 4K tokens)
- **DO NOT concatenate** all ARC data into a single string

### RULE 7: Performance Targets
- Cache hit rate > 85%
- ARC retrieval < 10ms (O(log N))
- Tool cache hit rate > 50%

### RULE 8: Test Coverage > 80% (Phase 4+)
- Unit tests for all new code
- Integration tests for critical paths
- Use `Client(server)` for in-memory MCP server testing
- Run `pytest tests/` before committing

### RULE 9: Type Hints + Docstrings
- Type hints on all functions
- Google-style docstrings
- Use `Literal` types for routing decisions

---

## DSPy 3.x Patterns (Use These)

### ChatAdapter for Local Models
```python
# Makes ReAct work with LM Studio, Ollama, any chat model
import dspy
lm = dspy.LM("openai/model-name", api_base="http://127.0.0.1:1234/v1")
dspy.configure(lm=lm, adapter=dspy.ChatAdapter())
```

### Tool.from_mcp_tool() for MCP Bridge
```python
# Native bridge: MCP tool -> DSPy tool (replaces mcp_connector.py)
from fastmcp import Client
client = Client(server)
async with client:
    mcp_tools = await client.list_tools()
    dspy_tools = [dspy.Tool.from_mcp_tool(t) for t in mcp_tools]
    agent = dspy.ReAct(signature, tools=dspy_tools)
```

### Per-Request Model Selection
```python
# Use dspy.context() instead of global dspy.configure()
with dspy.context(lm=expert_model):
    result = expert(question=query)
```

### SIMBA for Agentic Optimization
```python
# Designed for multi-step agent tasks (not just single predictions)
optimizer = dspy.SIMBA(metric=success_metric, max_steps=50)
optimized_agent = optimizer.compile(agent, trainset=examples)
```

### Typed Outputs
```python
# Use Literal for routing decisions (optimizable by DSPy)
class RoutingSignature(dspy.Signature):
    question: str = dspy.InputField()
    selected_expert: Literal["data", "hpc", "none"] = dspy.OutputField()
```

---

## FastMCP 3.x Patterns (Use These)

### Gateway with mount()
```python
from fastmcp import FastMCP
gateway = FastMCP("clio-gateway")
gateway.mount("/hdf5", hdf5_server)
gateway.mount("/parquet", parquet_server)
# Tools namespaced: hdf5_list_datasets, parquet_analyze_schema
```

### In-Memory Testing
```python
# Test MCP servers without subprocess or network
from fastmcp import Client
async def test_hdf5_analyze():
    async with Client(hdf5_server) as client:
        result = await client.call_tool("analyze_dataset", {"filepath": "test.h5"})
        assert result is not None
```

### Dependency Injection
```python
from fastmcp import FastMCP, Depends

def get_arc_memory():
    return ARCMemory()

@mcp.tool()
def analyze(filepath: str, arc: ARCMemory = Depends(get_arc_memory)) -> dict:
    # arc is injected, hidden from LLM tool schema
    cached = arc.get_cached_tool_result("hdf5", "analyze", {"filepath": filepath})
    if cached:
        return cached
    # ... actual analysis
```

### Transforms for Access Control
```python
from fastmcp.transforms import Enabled
# Only expose tools matching a condition
gateway.mount("/admin", admin_server, transforms=[Enabled(lambda t: user.is_admin)])
```

---

## Architecture DOs and DONTs

### 3-Tier Hierarchy
- **DO**: Tier 1 (Main) -> Tier 2 (Experts) -> Tier 3 (Nanoagents via dspy.Parallel)
- **DON'T**: Skip tiers, mix responsibilities, or have experts call other experts directly

### Agent Registry
- **DO**: Use registry for capability-based routing with typed outputs
- **DON'T**: Hardcode if/else routing logic or keyword-match routing

### ARC Memory
- **DO**: Check cache before expensive operations; compile context before injection
- **DON'T**: Skip caching, use O(N) algorithms, or concatenate raw history

### MCP Tools
- **DO**: Use FastMCP mount() gateway pattern; test with Client(server) in-memory
- **DON'T**: Write custom async/sync bridges, spawn subprocess per tool call
- **DO**: Curate 5-7 tools per expert with clear agent stories
- **DON'T**: Auto-generate tools, expose 10+ tools, or duplicate tool functionality

### Optimizers
- **DO**: Validate with statistical significance before deploying optimized variants
- **DON'T**: Deploy without testing; optimize without sufficient training data (min 50 examples)

### System Prompts
- **DO**: Write 500+ word domain-specific prompts for each expert signature
- **DON'T**: Use generic "helpful assistant" prompts or share prompts across experts

### Model Selection
- **DO**: Use `dspy.context(lm=...)` for per-request model selection
- **DON'T**: Mutate global `dspy.configure()` from within expert code

---

## Development Workflow

### Environment setup

- Python >= 3.12; [`uv`](https://astral.sh/uv) drives everything (deps + execution).
- Install deps: `uv sync --extra dev --extra optimizers`
- `CLIO_ALLOWED_ROOTS` gates tool file access. The tool-server tests
  write fixtures into the pytest temp dir, so that dir must be on the
  allow list or those tests fail with `outside_allowed_roots`. Unset,
  the file policy defaults to the current working directory, which
  excludes temp paths. Before running the tool tests:
  `CLIO_ALLOWED_ROOTS="$TMPDIR:$PWD" uv run pytest tests/`

### Workflow

1. Read PLAN.md for current phase and task
2. Read relevant architecture docs (CLIO_AGENT_ARCHITECTURE, etc.)
3. Implement with type hints + docstrings
4. Write tests (using `Client(server)` for MCP, mocks for LM calls)
5. Test + lint: `uv run pytest tests/ -m "not integration"` then `ruff check src/`
6. Verify baseline still works: `uv run src/clio_agent/ui/cli.py`
7. Commit with clear message: `<type>: <description>`

### Running CLIO

- **Dev (from source):** `uv run src/clio_agent/ui/cli.py`
- **Deployed:** the `install/` system installs a `clio` launcher CLI
  (`clio start|stop|restart|status|logs|doctor`) alongside the gact
  TUI. See `install/README.md`.

---

## File Organization

### Current Working Files
```
src/clio_agent/
├── config.py                 # Multi-provider LM configuration
├── agent.py                  # Main agent — planner loop (Tier 1)
├── harness.py                # RunTrace, RouteDecision, tool-result normalization
├── conversation_manager.py   # Session conversation state
├── errors.py                 # Structured error types
├── signatures/               # DSPy signatures (planner, expert, chat)
├── experts/                  # Domain experts (Tier 2): data, analysis, visualization
├── registry/                 # Capability-based agent registry + matching
├── arc/                      # ARC memory: cache, B-tree index, LSM, storage, retrieval
├── optimizer/                # Instrumentation, training, variants, runner
├── runtime/                  # Doctor / runtime status + nanoagent spawn primitive (Tier 3)
├── providers/                # Provider-specific auth (e.g. Argonne / ALCF)
├── tools/                    # FastMCP gateway, file policy, execution boundary
│   └── servers/              # HDF5 + Parquet MCP servers
├── gact/                     # GACT v0.2 server — the API surface gact-tui talks to
└── ui/
    ├── cli.py                # Interactive CLI + doctor
    └── api.py                # REST API
```

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

### Pattern 3: Registry Routing (Typed)
```python
class RoutingSignature(dspy.Signature):
    question: str = dspy.InputField()
    selected_expert: Literal["data", "hpc", "none"] = dspy.OutputField()

router = dspy.ChainOfThought(RoutingSignature)
routing = router(question=query)
expert = registry.get_agent(routing.selected_expert)
```

### Pattern 4: Context Compilation
```python
# DON'T: raw_context = "\n".join(all_messages)
# DO:
compiled = context_compiler.compile(
    query=question,
    session_id=session_id,
    budget_tokens=2000,
    include_procedural=True  # what worked/failed before
)
```

---

## Testing Requirements

- Unit tests: `tests/test_<module>/`
- Integration tests: `tests/test_integration/`
- MCP server tests: use `Client(server)` in-memory (no subprocess)
- LM tests: mock dspy.LM responses
- Coverage gate: Phase 1 (50%), Phase 2 (60%), Phase 3 (70%), Phase 4+ (80%)
- Run before commit: `pytest tests/`

---

## Error Handling

**Graceful Degradation Chain**:
- IOWarp unavailable -> file-based ARC storage
- ARC unavailable -> continue without memory (warn user)
- MCP server down -> pure reasoning mode (no tool calls)
- Optimizer fails -> keep current variant (no rollback)
- LM timeout -> retry once, then return partial answer

---

## Git Workflow

**Commit Format**: `<type>: <subject>`
- `feat`: New feature
- `fix`: Bug fix
- `refactor`: Code refactoring
- `test`: Tests
- `docs`: Documentation

**Before Commit**:
- [ ] Tests passing: `pytest tests/`
- [ ] Lint clean: `ruff check src/`
- [ ] Baseline works: `uv run src/clio_agent/ui/cli.py`

---

## Quick Reference

**Read First**:
- `PLAN.md` - What to build (current phase)
- `docs/CLIO_AGENT_ARCHITECTURE.md` - How it all fits together

**Test**:
```bash
pytest tests/
ruff check src/
```

**Run**:
```bash
uv run src/clio_agent/ui/cli.py
```

---

**THIS IS YOUR REFERENCE. FOLLOW PLAN.MD. USE NATIVE DSPy/FastMCP PATTERNS.**
