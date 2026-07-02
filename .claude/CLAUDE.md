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
   semantics, the source of truth is the **DSPy reference in `ai-docs/DSPY/` plus the
   upstream DSPy source** (not guesswork,
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

## Cleanup program (2026-07)

An active, audited cleanup program governs structural changes:
`docs/design/system-cleanup-2026-07.md`, umbrella issue
[#775](https://github.com/iowarp/clio-agent/issues/775). Two of its ground
rules apply to every change:

- **No silent fallback.** Every degradation, downgrade, or alternate path must
  emit a structured reason that reaches the trace/API — or the path gets
  deleted. The model is the `stream_fallback` catalog in
  `src/clio_agent/gact/streaming.py` (`_stream_fallback_payload` /
  `_stream_fallback_reasons`): a typed reason, recorded per session, queryable
  after the fact. A bare `except: pass` or an unexplained quality downgrade is
  a bug even when the output looks fine.
- **No accretion.** Fixes that add more than a trivial amount of code go in an
  owner module, not appended to a god file — patch-driven development is how
  `turn.py` / `agent.py` / `app.py` grew to 3–4k lines. The warn-only CI guards
  (`scripts/check_file_size.py`, `scripts/check_no_class_in_function.py`) will
  become enforcing; do not add to files they flag.

---

## Core Principles

**CLIO Agent IS**: Self-improving autonomous agent for scientific data (not a framework)
**Architecture**: 3-Tier Orchestration + ARC Memory + Optimizer Layer + IOWarp Integration
**Internal Engine**: DSPy 3.x (signatures, modules, adapters) + FastMCP 3.x (tool servers)
**Product Surface**: the GACT server (`clio_agent.gact.app`) that gact-tui talks to

---

## Actual Stack

The dependency truth is `pyproject.toml`, not this list. As of 0.5.x the core is:

```python
# Engine + tools
"dspy>=3.1.3"                # (package is `dspy`, NOT `dspy-ai`)
"fastmcp>=3.2.4"

# GACT server (shipped product surface, not optional)
"fastapi", "uvicorn", "sse-starlette"

# ARC / memory
"iowarp-core>=2.1.0"         # clio-core CTE — default ARC store backend
"sortedcontainers", "lru-dict", "msgspec", "platformdirs"

# Runtime daemon lifecycle (cross-platform)
"filelock", "psutil"

# UI / misc
"rich", "prompt-toolkit", "requests", "h5py", "pyarrow", "matplotlib"
```

Optional extras: `optimizers` (scipy/numpy), `adios` (non-Windows), `argonne`
(globus-sdk), `claude-code`, `dev`. Install for development with
`uv sync --extra dev --extra optimizers`.

### Forbidden

**DO NOT add** without approval:
- LangChain, CrewAI, AutoGen as core dependencies (external integration only via A2A)
- Heavy ML frameworks (TensorFlow, PyTorch)
- Database ORMs (SQLAlchemy)
- Custom async/sync bridge code (use native DSPy/FastMCP instead)

**Python Version**: >= 3.12 (LOCKED)

---

## Critical Rules (NON-NEGOTIABLE)

### RULE 1: Follow the Live Roadmap
- The roadmap is `docs/design/roadmap.md`; structural work follows the cleanup
  program `docs/design/system-cleanup-2026-07.md` (#775)
- `PLAN.md` at the repo root is a superseded pointer file — do not treat its
  historical phases as tasks

### RULE 2: Never Break Baseline
- The GACT server, main agent, and CLI must always work
- Smoke-check before committing: `uv run src/clio_agent/ui/cli.py`
- If a change breaks baseline, revert first, then fix

### RULE 3: DSPy is Internal Implementation Detail
- Use DSPy internally for signatures, modules, adapters
- **DO NOT expose** DSPy in user-facing docs, APIs, or error messages
- **Exception**: CLAUDE.md and code comments only

### RULE 4: Know Where Data Actually Lives (honest topology)
- **ARC** owns the prompt working set (the live context plane the ReAct loop
  reads each iteration — `arc/live.py`, `arc/prompt_recorder.py`) and the
  semantic event log (`arc/schema.py`, `arc/segments.py`), plus invocations
  and metrics
- **gact** stores its own sessions and messages (`gact/session_store.py`,
  `~/.config/clio-agent/messages/sess_*.json`) and workflow state
  (`run.extra.workflow_state`)
- These are today ~4 parallel materializations of the same history. The agreed
  direction is [#737](https://github.com/iowarp/clio-agent/issues/737):
  collapse them into one normalized log + thin projections (event-sourcing)
- Rule of conduct: do NOT add a fifth store. New persistent state goes in an
  existing store, and new code should not deepen the duplication #737 removes

### RULE 5: Tool Curation (Max 5-7 Per Expert)
- Each expert gets 5-7 high-level curated tools, not every atomic operation
- Hide implementation complexity behind composite tools
- Document each tool with an "agent story" (when/why an agent would use it)
- **DO NOT auto-generate** tools from OpenAPI specs or file system scans

### RULE 6: Context is Compiled, Not Concatenated
- Never dump raw conversation history into prompts
- Use the context compilation pipeline (`arc/context_compiler.py`):
  filter -> compact -> enrich -> assemble
- Budgets are discovered from the model (handshake), not hardcoded (⚑ #6)
- **DO NOT concatenate** all ARC data into a single string

### RULE 7: Test Coverage — CI Floor 70, Ratcheting Up
- CI enforces `--cov-fail-under=70` (lowered when the v0.5 gact merge landed;
  tracked to ratchet back to 80 as gact code gains tests)
- Unit tests for all new code; integration tests for critical paths
- Use `Client(server)` for in-memory MCP server testing
- Run `uv run pytest tests/ -m "not integration"` before committing

### RULE 8: Type Hints + Docstrings
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
# Native bridge: MCP tool -> DSPy tool
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
gateway.mount("/fs", fs_server)
gateway.mount("/shell", shell_server)
# Tools are namespaced per mount
```

### In-Memory Testing
```python
# Test MCP servers without subprocess or network
from fastmcp import Client
async def test_fs_read():
    async with Client(fs_server) as client:
        result = await client.call_tool("read_file", {"filepath": "test.txt"})
        assert result is not None
```

---

## Architecture DOs and DONTs

### 3-Tier Hierarchy
- **DO**: Tier 1 (Main) -> Tier 2 (Experts) -> Tier 3 (Nanoagents)
- **DON'T**: Skip tiers, mix responsibilities, or have experts call other experts directly

### Agent Registry
- **DO**: Use registry for capability-based routing with typed outputs
- **DON'T**: Hardcode if/else routing logic or keyword-match routing (⚑ #1)

### ARC Memory
- **DO**: Compile context before injection; record prompts/events through the live plane
- **DON'T**: Concatenate raw history or invent parallel storage (see RULE 4)

### MCP Tools
- **DO**: Use FastMCP mount() gateway pattern; test with Client(server) in-memory
- **DON'T**: Write custom async/sync bridges, spawn subprocess per tool call
- **DO**: Curate 5-7 tools per expert with clear agent stories
- **DON'T**: Auto-generate tools, expose 10+ tools, or duplicate tool functionality

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

1. Read `docs/design/roadmap.md` (and the cleanup program for structural work)
2. Read relevant architecture docs (docs/CLIO_AGENT_ARCHITECTURE.md, etc.)
3. Implement with type hints + docstrings, in the owner module (no accretion)
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
├── agent.py                  # Main agent — planner loop (Tier 1)
├── conf.py / config.py       # Runtime config + multi-provider LM configuration
├── paths.py                  # Canonical config/data/cache locations
├── prompts.py                # External editable prompt system
├── harness.py                # RunTrace, RouteDecision, tool-result normalization
├── conversation_manager.py   # Session conversation state
├── errors.py                 # Structured error types
├── signatures/               # DSPy signatures (main agent)
├── experts/                  # native_tools.py — native expert tool surface
├── prompt_packs/             # Built-in prompt packs
├── agent_blueprints/         # Built-in agent blueprints
├── registry/                 # Capability-based agent registry + matching
├── arc/                      # ARC memory: live context plane, prompt recorder,
│                             #   context compiler, cache, index, LSM, storage,
│                             #   retrieval, semantic schema/segments, replay
├── optimizer/                # Instrumentation, trainer, variants, runner
├── runtime/                  # Doctor/status, hooks, LM activity + stream audit,
│                             #   nanoagent spawn primitive (Tier 3)
├── providers/                # Provider auth + LiteLLM bridges (Argonne/ALCF,
│                             #   claude_code, codex) and handshake/ (model
│                             #   limits discovery)
├── tools/                    # FastMCP gateway, catalog, file policy, fs_write,
│   │                         #   execution boundary, mcp_config
│   └── servers/              # FS + shell MCP servers
├── gact/                     # GACT server — THE shipped API surface gact-tui
│   │                         #   talks to (FastAPI + SSE)
│   ├── app.py                # FastAPI app assembly
│   ├── turn.py               # Turn orchestration
│   ├── streaming.py          # SSE streaming + stream_fallback reason catalog
│   ├── sessions.py / session_store.py / messages.py / messaging.py
│   ├── events.py / semantic_events.py / evidence.py / usage.py
│   ├── routes/               # HTTP routes: sessions, messages, agents, memory,
│   │                         #   permissions, providers, workspaces, ...
│   ├── agents/               # Agent composition/resolution/builders/runtime
│   ├── runtime/              # Capabilities, commands, permission policies,
│   │                         #   context tokens, globals
│   ├── workflow_state/       # Workflow-state merge
│   ├── providers/            # Provider config/auth (LM Studio, ...)
│   ├── agent_blueprints.py / expert_packs.py / user_agents.py
│   ├── catalog.py / context.py / delegation.py / diagnostics.py
│   ├── enrichment.py / permission_gate.py / scheduler.py / tool_observer.py
│   └── workspaces.py / workspace_scope.py / types.py
└── ui/                       # LEGACY surfaces (gact is the product)
    ├── cli.py                # Interactive CLI + doctor (still the smoke test)
    └── api.py                # REST API
```

---

## Common Patterns

### Pattern 1: Store Metrics
```python
start = time.time()
result = expert.forward(query)
arc.store_invocation({
    "duration_ms": (time.time() - start) * 1000,
    "success": True,
    ...
})
```

### Pattern 2: Registry Routing (Typed)
```python
class RoutingSignature(dspy.Signature):
    question: str = dspy.InputField()
    selected_expert: Literal["data", "hpc", "none"] = dspy.OutputField()

router = dspy.ChainOfThought(RoutingSignature)
routing = router(question=query)
expert = registry.get_agent(routing.selected_expert)
```

### Pattern 3: Context Compilation
```python
# DON'T: raw_context = "\n".join(all_messages)
# DO:
compiled = context_compiler.compile(
    query=question,
    session_id=session_id,
    include_procedural=True  # what worked/failed before
)
```

---

## Testing Requirements

- Unit tests: `tests/test_<module>/`
- Integration tests: `tests/test_integration/`
- MCP server tests: use `Client(server)` in-memory (no subprocess)
- LM tests: mock dspy.LM responses
- Coverage gate: CI floor is 70 (`--cov-fail-under=70`), ratcheting back to 80
- Run before commit: `uv run pytest tests/ -m "not integration"`

---

## Error Handling

**Graceful Degradation Chain** (every step must emit a structured reason — see
the no-silent-fallback ground rule above):
- clio-core CTE unavailable -> file-based ARC storage
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
- [ ] Tests passing: `uv run pytest tests/ -m "not integration"`
- [ ] Lint clean: `ruff check src/`
- [ ] Baseline works: `uv run src/clio_agent/ui/cli.py`

---

## Quick Reference

**Read First**:
- `docs/design/roadmap.md` - What to build next
- `docs/design/system-cleanup-2026-07.md` + #775 - Active cleanup program
- `docs/CLIO_AGENT_ARCHITECTURE.md` - How it all fits together

**Test**:
```bash
uv run pytest tests/ -m "not integration"
ruff check src/
```

**Run**:
```bash
uv run src/clio_agent/ui/cli.py
```

---

**THIS IS YOUR REFERENCE. FOLLOW THE LIVE ROADMAP. USE NATIVE DSPy/FastMCP PATTERNS.**
