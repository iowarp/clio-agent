# Project State

## Project Reference

See: .planning/PROJECT.md (updated 2026-02-10)

**Core value:** Three specialized small-model experts complete the storage-to-insight cycle cheaper and better than one large generalist LLM -- and the system gets measurably better with use.
**Current focus:** Phase 3 complete, ready for Phase 4 - Production Hardening

## Current Position

Phase: 3 of 4 (Self-Improvement) -- COMPLETE
Plan: 3 of 3 in current phase (complete)
Status: Phase 03 complete, ready for Phase 04
Last activity: 2026-02-11 -- Plan 03-03 executed (CLI integration + 70% coverage)

Progress: [#########░] 90%

## Performance Metrics

**Velocity:**
- Total plans completed: 9
- Average duration: 9min
- Total execution time: 1.3 hours

**By Phase:**

| Phase | Plans | Total | Avg/Plan |
|-------|-------|-------|----------|
| 01-foundation-reset | 3/3 | 24min | 8min |
| 02-multi-expert-pipeline | 3/3 | 28min | 9min |
| 03-self-improvement | 3/3 | 25min | 8min |

**Recent Trend:**
- Last 5 plans: 7min, 12min, 6min, 6min, 13min
- Trend: stable

## Accumulated Context

### Decisions

Decisions are logged in PROJECT.md Key Decisions table.
Recent decisions affecting current work:

- Phases 1-4 scope (not 1-6): IOWarp CTE not available, Phase 6 speculative
- CLI can break during Phase 1 rewrites, must work by phase end
- Local LM Studio only for dev, multi-provider in Phase 4
- FastMCP pinned to >=2.14.0 (3.0.0 does not exist on PyPI; 2.14.5 has mount())
- E402 lint rule ignored in ruff (sys.path manipulation is intentional for UV scripts)
- MCPToolBridge with background thread over dspy.Tool.from_mcp_tool() (event loop nesting)
- FastMCP mount prefix without leading slash (avoids invalid / in tool names)
- 529-word domain prompt for DataExpertSignature
- RouterSignature uses Literal['chat','data','analysis','visualization','none'] with ChainOfThought
- Router LM uses temperature 0.3 for deterministic routing
- CLI banner shows all 3 experts (data, analysis, visualization)
- pyarrow moved from optional [tools] to core dependencies for Parquet server
- AnalysisExpert filters gateway tools by parquet_ prefix (not all 8 tools)
- MCPToolBridge reused via import from data_expert.py (no duplication)
- 894-word domain prompt for AnalysisExpertSignature (data profiling, statistics, quality)
- VisualizationExpert uses direct Python functions as dspy.Tool (no MCP server needed)
- matplotlib Agg backend for headless chart rendering
- DatasetProfile keyed by session_id + filepath for cross-expert retrieval
- ProceduralMemory sorted by learned_at descending (most recent first)
- Conversation stored before routing decision in forward() for correct accumulation
- ContextCompiler uses proportional budget: conversation 40%, profiles 30%, procedural 20%, routing 10%
- Token-to-word ratio 0.75 for budget estimation (no tokenizer dependency)
- Mock router replacement pattern for dispatch testing (direct attribute replacement)
- Inline instrumentation in agent.py dispatch instead of decorator on expert.forward() (MCPToolBridge side effects)
- Output fields truncated to 500 chars in instrumented invocations to prevent ARC bloat
- VisualizationExpert metric mapping: visualization_description -> analysis weight, file_path -> recommendations weight
- Error keywords as frozenset for O(1) membership testing in clio_expert_metric
- Cast scipy numpy types to native Python types to prevent msgspec serialization errors
- load_variant reuses existing module instance via module.load(path) to avoid MCPToolBridge constructor side effects
- num_threads=1 for dspy.evaluate.Evaluate to prevent MCPToolBridge threading deadlocks
- Sequential variant IDs ({agent_id}_v{N}) with N from ARC record count
- CLI tests use patched __init__ + MockAgent to avoid LM Studio dependency
- Variant loading wrapped in outer try/except so VariantManager import failure never breaks init
- ConversationManager and CapabilityMatcher tested for coverage despite being unused code

### Pending Todos

None yet.

### Blockers/Concerns

- ~~pyproject.toml lists fastmcp>=2.13.0 but Phase 1 requires >=3.0.0 (upgrade needed)~~ RESOLVED: pinned >=2.14.0
- ~~mcp_connector.py spawns event loop thread on import (must delete early)~~ RESOLVED: deleted
- ~~MainAgentSignature has duplicate field definitions~~ RESOLVED: replaced with RouterSignature + ChatAgentSignature

## Session Continuity

Last session: 2026-02-11
Stopped at: Completed 03-03-PLAN.md (CLI integration + 70% coverage -- --tune, /metrics, /compare, /rollback, 347 tests)
Resume file: None
