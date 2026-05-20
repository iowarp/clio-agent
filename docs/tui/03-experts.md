# 03 - Experts

> CLIO routes scientific work to specialized expert agents. This doc describes what an expert is, the current roster, and how the TUI should render their lifecycle.

## What Is An Expert?

An expert is a `dspy.Module` with:

- A DSPy Signature defining the input/output contract, such as `question, file_context -> analysis, recommendations`.
- Real MCP-backed tools exposed as `dspy.Tool` objects.
- Optional ARC-backed caching and provenance through the tool execution layer.
- A static `get_capabilities()` method that registers keywords, tool names, and specialization metadata with the Agent Registry.

The top-level `ClioAgent` planner is responsible for deciding whether to call a tool directly, delegate to an expert, answer conversationally, or surface a no-action route.

## Current Roster

| Expert | Purpose | Tools | Signature | Source |
|---|---|---|---|---|
| **DataExpert** | HDF5 optimization and scientific data I/O | `hdf5_list_datasets`, `hdf5_analyze`, `hdf5_optimize`, `hdf5_check_compression`, `hdf5_analyze_file` | `DataExpertSignature` | `experts/data_expert.py` |
| **AnalysisExpert** | Parquet/statistical profiling and column analysis | `parquet_analyze_schema`, `parquet_query_data`, `parquet_compute_statistics` | `AnalysisExpertSignature` | `experts/analysis_expert.py` |
| **VisualizationExpert** | Charts, plots, and visual summaries | `plot_histogram`, `plot_bar_chart`, `plot_scatter`, `plot_summary` | `VisualizationExpertSignature` | `experts/visualization_expert.py` |
| **ChatAgent** | Conversational answers without tool use | none | `ChatAgentSignature` | `agent.py` |

Planned but not live: `HPCExpert`, `ResearchExpert`, and A2A-bridged external agents.

## Registration

The registry lists the native tier-2 experts and their capabilities:

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

The planner reads these capabilities, plus the live MCP tool catalog, when choosing an action.

## How An Expert Runs

When the planner emits `{"action":"expert","expert":"data|analysis|visualization"}`, CLIO:

1. Checks the requested expert exists in the registry.
2. Checks file compatibility when a current file context exists.
3. Runs the selected expert under the configured DSPy/LiteLLM provider context.
4. Merges tool provenance into the turn trace.
5. Returns `analysis`/`recommendations` or visualization metadata to the GACT message renderer.

When the planner emits `{"action":"tool", ...}`, CLIO calls that tool through the same execution/provenance layer and records the observation before continuing the loop.

## What The TUI Should Show

- **Active expert badge**: e.g. `DataExpert`, `AnalysisExpert`, `VisualizationExpert`, or chat.
- **Tool calls**: inline rows with tool name, args summary, result/error, duration, and cached/fresh state.
- **Routing rationale**: the planner route reason and confidence when present.
- **Registry panel**: `/v1/agents` and tool catalog views showing IDs, descriptions, keywords, and tools.

## Expert Signatures

| Signature | Input fields | Output fields | Module |
|---|---|---|---|
| `AgentActionSignature` | `question`, `session_context`, `file_context`, `capabilities`, `observations` | `action_json` | planner loop |
| `AgentAnswerSignature` | `question`, `session_context`, `observations` | `answer` | final synthesis |
| `ChatAgentSignature` | `question`, `session_context` | `answer` | chat |
| `DataExpertSignature` | `question`, `file_context` | `analysis`, `recommendations` | data expert |
| `AnalysisExpertSignature` | `question`, `file_context` | `analysis`, `recommendations` | analysis expert |
| `VisualizationExpertSignature` | `question`, `file_context` | `visualization_description`, `file_path` | visualization expert |

DSPy is an implementation detail. The TUI should surface CLIO concepts and structured `error_info`, not DSPy internals.

## Error Paths Per Expert

| Error | Meaning | TUI rendering |
|---|---|---|
| `routing_error` | Planner could not select or validate a safe action | Show retry/reconfigure/exit actions when present |
| `expert_error` | Expert execution failed | Red toast + failed assistant message with details |
| `tool_error` | MCP/tool call failed | Inline under the tool row |
| `provider_error` | LM unavailable, timed out, or auth failed | Offer retry and provider reconfiguration |
| `config_error` | Provider/configuration invalid | Route user to Settings or doctor output |
