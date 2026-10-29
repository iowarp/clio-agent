# 03 - Agents and subagents (formerly experts)

> The legacy TUI is frozen and unsupported as of 0.9.5.1. Do not install or run
> it for current CLIO. This reference is retained for a future upgrade; use
> Desktop or Web today. See [TUI status](README.md).

> The blueprint "expert" model is gone. CLIO runs agents composed from
> plugins; what used to be an expert is now a defined subagent of a plugin or a
> general subagent. The full reference is
> [`docs/PLUGINS_AND_FLOWS.md`](../PLUGINS_AND_FLOWS.md); this page only lists
> what the TUI can observe.

## What runs a session

Every session runs one agent resolved from plugins and pinned at session
start: a flow (`react` by default), an identity and a set of plugins. The
plugins bring the tools, MCP servers, skills, defined subagents, flows and
prompt fragments. Agents are Python classes in a marketplace's `agents/*.py`;
the default marketplace (`external/clio-agent-marketplace`, installed as
`clio`) ships `clio/standard` (the default agent) and domain agents such as
`clio/clio-data` (HDF5, ADIOS2 and Parquet files) and `clio/factorio`, whose
defined subagents (for example `abaqus_engineer`) replace the old blueprint
experts. Users save their own agents as a
parent plus a delta (`user/<id>`).

CLIO core ships no privileged native domain classes; the native `DataExpert`,
`AnalysisExpert`, `VisualizationExpert`, `NDPExpert` and `SACFormatExpert` paths
are removed.

## Subagents

- **Defined subagents** ship in the plugin they belong to, with their own
  prompt, plugin set and optional tool allowlist. A child session pins its own
  resolution.
- **General subagents** are installed agents started with only a task prompt,
  through the `delegation` plugin's `spawn_agent_task` / `spawn_agents_parallel`.
- **Flows** (`run_flow`, `run_plan`, `council`, `arena`) start children as
  ordinary tasks too.

Every child is a task with a durable handle; the model waits, observes or
cancels it with the shared task controls (`wait_tasks`, `observe_tasks`,
`cancel_tasks`).

## What the TUI can show

- The session's agent ref, its resolved plugins with their provenance, and the
  composed prompt with each fragment attributed (`GET /v1/sessions/{sid}/agent`).
- Tool calls with tool name, args summary, result/error, duration, and
  cached/fresh state.
- Child task lifecycle (accepted, running, settled) by handle, and a flow run's
  live plan (`GET /v1/sessions/{sid}/tasks/{handle}/flow`).
- Provider/model provenance.
- Structured `error_info` and stream fallback metadata when present.

## Error paths

| Error | Meaning | TUI rendering |
|---|---|---|
| `agent_unavailable` | The session's agent does not resolve (missing, untrusted or colliding plugin) | Failed assistant message with every resolution error and the recovery actions |
| `agent_error` | Agent execution failed | Failed assistant message with runtime/provenance details |
| `tool_error` | MCP/tool call failed | Inline under the tool row |
| `provider_error` | LM unavailable, timed out, or auth failed | Offer retry and provider reconfiguration |
| `config_error` | Provider/configuration invalid | Route user to Settings or doctor output |

DSPy remains an implementation detail. The TUI should surface CLIO concepts:
agent refs, plugin provenance, flows, tools, task handles, semantic events and
durable evidence.
