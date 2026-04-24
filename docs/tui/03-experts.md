# 03 — Experts

> CLIO routes work to specialised Expert agents. This doc describes what an Expert IS, the current roster, and how the TUI should render their lifecycle.

## What is an Expert?

An Expert is a `dspy.Module` subclass (`src/clio_agent/agent.py:37`) with:

- A **DSPy Signature** defining input/output contract (e.g. `DataExpertSignature` — `question, file_context → analysis, recommendations`).
- A **ReAct pattern** loop (reason → act via tools → observe → iterate, max 5 iterations — `data_expert.py:85`).
- **Real MCP tools** wired in via the `MCPToolBridge` (`data_expert.py:72-86`) which converts async FastMCP tools into sync `dspy.Tool` objects consumable by the ReAct agent.
- Optional **ARC memory** for caching tool results (hidden from the LLM schema via FastMCP `Depends()`).
- A static `get_capabilities()` method that registers keywords + tool names + specialisation with the Agent Registry.

## Current roster (v0.2)

| Expert | Purpose | Tools | Signature | Source |
|---|---|---|---|---|
| **DataExpert** | HDF5 + Parquet optimisation, I/O analysis | `hdf5_list_datasets`, `hdf5_analyze`, `hdf5_optimize`, `hdf5_check_compression`, `hdf5_analyze_file` | `DataExpertSignature` | `experts/data_expert.py:37-131` |
| **AnalysisExpert** | Statistical profiling, column-level analysis | `parquet_analyze_schema`, `parquet_query_data`, `parquet_compute_statistics` | `AnalysisExpertSignature` | `experts/analysis_expert.py:37-137` |
| **VisualizationExpert** | Charts / plots / visual summaries | `plot_histogram`, `plot_bar_chart`, `plot_scatter`, `plot_summary` | `VisualizationExpertSignature` | `experts/__init__.py` |
| **ChatAgent** | Conversational fallback (no tools) | — | `ChatAgentSignature` | `agent.py:~144` |

Planned (not live): `HPCExpert` (SLURM/MPI/Darshan), `ResearchExpert` (papers/citations), plus A2A-bridged external agents (LangChain / CrewAI / AutoGen). (`CLIO_AGENT_ARCHITECTURE.md:110-157`, `PLAN.md:113-134`)

## Registration

All three experts register in `ClioAgent.__init__` (`agent.py:160-215`):

```python
registry.register_agent(
    agent_id="data",
    agent=self.data_expert,
    capabilities=AgentCapability(
        keywords=["hdf5", "compression", "chunking", "data", "io"],
        description="Data I/O optimization expert with HDF5 tools",
        tools=["hdf5_list_datasets", "hdf5_analyze_dataset", ...],
        specialization="data_io",
    ),
)
```

Keywords are what the heuristic router (`agent.py:386-402`) reads to skip the LM entirely.

## How an Expert runs

Given `selected == "data"` (`agent.py:307-329`):

1. **Fast path**: `_direct_tool_answer(selected, question, file_context)`. If the query is deterministic (e.g. "list datasets in X.h5"), the tool is called directly with no LM involvement.
2. **ReAct fallback**: `self.data_expert(question=question, file_context=file_context)` runs the full DSPy ReAct loop.
3. Returns `expert_result` with `.analysis` + `.recommendations` fields.
4. `answer = f"{expert_result.analysis}\n\nRecommendations:\n{expert_result.recommendations}"`

Experts are **not chained by default** — multi-expert collaboration is a Phase-2 item (`CLIO_AGENT_ARCHITECTURE.md:759`).

## What the TUI should show

- **Active-expert badge** — e.g. `[DataExpert]` chip near the assistant message, coloured per specialisation (`data_io`, `data_analysis`, `data_visualization`, `chat`).
- **Tool calls** — inline rows under the expert activity: tool name, args summary, result length, and a cached/fresh glyph (read `arc_stats` to decide; `⚡` fresh, `✓` cache hit).
- **Routing confidence** — the routing decision carries a confidence score (0–1.0) from the LM router; display when present.
- **Registry panel** — `/experts` view: tiles showing `id`, `description`, `keywords`, `tools` count for each registered expert.

## Expert signatures (internal, don't surface)

| Signature | Input fields | Output fields | Module | Compile time |
|---|---|---|---|---|
| `RouterSignature` | `question` | `selected_expert: Literal[...]` | `ChainOfThought` | runtime |
| `ChatAgentSignature` | `question, session_context` | `answer` | `Predict` | runtime |
| `DataExpertSignature` | `question, file_context` | `analysis, recommendations` | `ReAct` | Phase 3: SIMBA compile |
| `AnalysisExpertSignature` | `question, file_context` | `analysis, recommendations` | `ReAct` | Phase 3: SIMBA compile |
| `VisualizationExpertSignature` | `question, file_context` | `visualization_description, file_path` | `ReAct` | Phase 3: SIMBA compile |

DSPy is an **implementation detail**. The TUI must not display DSPy-specific errors or types (`CLAUDE.md` Rule 3).

## SIMBA compile-time variants (future)

`optimizer/runner.py` runs DSPy SIMBA to tune expert prompts offline; A/B'd variants are versioned in ARC (`optimizer/variants.py`). Once this lands, the TUI can display "variant v3" badges or a Settings affordance to trigger `--tune` — but it's gated on Phase 3+. (`CLIO_VISION.md:51`, `PLAN.md:290-335`)

## Error paths per expert

From `errors.py` (see `08-semantics-and-lifecycle.md` for full details):

| Error | Meaning | TUI rendering |
|---|---|---|
| `RoutingError` | Router failed to classify — falls back to chat | Transient warning, not a fatal |
| `ExpertError` | Expert's ReAct loop blew up | Red toast + muted "degraded" badge, keep session open |
| `ToolError` | MCP tool call failed | Inline under the tool row, don't kill the turn |
| `ProviderError` | LM unavailable / timeout | Offer retry; surface provider name |
| `ConfigError` | `CLIO_LM_*` missing / invalid | Route user to Settings / `/doctor` |
