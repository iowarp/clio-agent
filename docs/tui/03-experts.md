# 03 - Experts

> CLIO routes scientific work to registry-loaded Agent Blueprint experts. This
> doc describes the current expert model and how the TUI should render their
> lifecycle.

## What Is An Expert?

An expert is an Agent Blueprint node loaded from the registry/marketplace store
and compiled at runtime into a DSPy module. CLIO core supplies the runtime
substrate: registry bootstrap, tool adapters, MCP descriptors, workspace policy,
semantic events, permissions, memory, and provenance.

Expert definitions are not privileged Python classes in `clio_agent.experts`.
The production native `DataExpert`, `AnalysisExpert`, `VisualizationExpert`,
`NDPExpert`, and `SACFormatExpert` paths have been removed from the runtime.

## Current Default Registry

The default baseline comes from the pinned marketplace registry:

| Field | Value |
|---|---|
| Registry | `git@github.com:JaimeCernuda/clio-agent-marketplace.git` |
| Ref | `main` |
| Commit | `5aa5d6f566cf542bc32c7bccf963fd765f803caf` |
| Submodule | `external/clio-agent-marketplace` |
| Default blueprint | `data-semantics` |

At runtime the default blueprint installs into the normal blueprint store and
surfaces as registry rows such as `main`, `data`, `analysis`, and
`visualization`, each with `agent_blueprint` and `pack.definition_path`
metadata.

## Blueprint Contract

Blueprint experts declare:

- `module.kind`: `predict`, `chain_of_thought`, or `react`.
- `signature`: ordered input/output fields. Empty declarations default to
  `system_prompt`, `question`, and `answer`.
- `structured_outputs`: normalized `evidence`, `artifacts`, `errors`,
  `delegation`, and `expert_handoffs` fields.
- `tools`: allowed tool names. Tool lists scope access; they do not select the
  module kind.
- `children`: declared child experts available for synchronous delegation.

The compiler maps module kinds to DSPy as follows:

| `module.kind` | Runtime module |
|---|---|
| `predict` | `dspy.Predict(signature)` |
| `chain_of_thought` | `dspy.ChainOfThought(signature)` |
| `react` | `dspy.ReAct(signature, tools=allowed_tools + child_expert_tools)` |

## What The TUI Should Show

- Active expert id and title.
- Blueprint id, version, scope, install commit, and definition path.
- Module kind and structured-output availability.
- Tool calls with tool name, args summary, result/error, duration, and
  cached/fresh state.
- Child delegation lifecycle: started, completed, failed, parent resumed.
- Provider/model provenance.
- Structured `error_info` and stream fallback metadata when present.

## Error Paths

| Error | Meaning | TUI rendering |
|---|---|---|
| `routing_error` | Planner could not select or validate a safe action | Show retry/reconfigure/exit actions when present |
| `agent_error` | Blueprint/user-agent execution failed | Failed assistant message with runtime/provenance details |
| `tool_error` | MCP/tool call failed | Inline under the tool row |
| `provider_error` | LM unavailable, timed out, or auth failed | Offer retry and provider reconfiguration |
| `config_error` | Provider/configuration invalid | Route user to Settings or doctor output |

DSPy remains an implementation detail. The TUI should surface CLIO concepts:
agent ids, blueprint provenance, module kind, tools, structured outputs,
semantic events, and durable evidence.
