# DSPy Blueprint Expert Runtime

## Rule

CLIO core provides runtime substrate only. Domain experts are Agent Blueprint
programs loaded from an installed registry snapshot. CLIO must not route through
privileged Python-native domain experts or bundled domain-agent folders inside
`clio-agent`.

The default Data Exploration/Search agent is bootstrapped from the pinned
default registry:

- registry: `git@github.com:JaimeCernuda/clio-agent-marketplace.git`
- ref: `main`
- commit: `908e013d68a80b1e13d5e7d633309d1f6813d970`
- default activation: `data-semantics`

Fresh installs install that snapshot into the normal Agent Blueprint store.
Missing or mismatched pins are surfaced as disabled Blueprint diagnostics; CLIO
has no in-repo builtin blueprint fallback.

## Expert Module Contract

Each expert declares its DSPy program through frontmatter:

```yaml
module:
  kind: predict | chain_of_thought | react
signature:
  inputs:
    question: User request
  outputs:
    answer: Final answer
structured_outputs:
  evidence: true
  artifacts: true
  errors: true
```

`module.kind` selects the DSPy module:

- `predict` compiles to `dspy.Predict`.
- `chain_of_thought` compiles to `dspy.ChainOfThought`.
- `react` compiles to `dspy.ReAct`.

`answer` is a REQUIRED output on every expert. A leaf expert's answer is its
whole return contract, and an orchestrator's answer IS the user deliverable —
there is no post-forward synthesis pass that could supply it later. An omitted or
empty answer is a typed failure, never a legitimate state.

An expert that declares children (an orchestrator) MUST declare `module.kind:
react`: it routes by CALLING the spawn-runtime tools over real child turns (see
[Child Experts](#child-experts)), not by emitting a typed routing field for a
settle loop to consume. `predict` / `chain_of_thought` are valid for leaf experts
only; declaring children on a non-react expert fails Blueprint validation with a
`declare module.kind: react` error.

`tools` scopes tool access only. It does not select the module kind. A react
orchestrator receives its declared tools plus the spawn-runtime tools for its
declared children.

## Structured Workflow State

DSPy provides the module, signature, and ReAct/Predict/ChainOfThought substrate.
CLIO's runtime orchestration is separate. Routing is the MODEL's decision: a react
orchestrator inspects its children's returned evidence and DECIDES which child to
spawn next by CALLING a spawn tool. CLIO does not route on model prose, and there
is no deterministic settle loop matching keywords or typed routing fields — the
settle/synthesis layer and its `next_expert` / continuation-contract vocabulary
were deleted (#948 S4). A baseline-0 guard keeps that vocabulary out of the tree.

Blueprint children return their prose deliverable as `answer` and carry compact
structured evidence on the first-class typed `workflow_state` output, for example:

```json
{
  "workflow_state": {
    "geospatial": {
      "status": "resolved",
      "region_name": "Los Angeles, California",
      "bbox": [-119.0, 33.2, -117.5, 34.4]
    },
    "resource": {
      "status": "selected",
      "dataset_id": "selected-by-catalog",
      "station_id": "selected-by-ranking"
    }
  }
}
```

The typed `workflow_state` field is the STRUCTURED carrier: it rides the
`AgentTask` record and the `blueprint.delegation.*` / `agent.task.*` events back
to the parent, never serialized into the user-facing `answer` text. The parent
react loop reads a completed child's `output` (the child's verbatim `answer`) and
`workflow_state` from the tool result of `wait_agent_tasks`, and decides its next
spawn from that typed evidence plus the child's prose.

Typed evidence is for the model to REASON over, not for CLIO to branch on
mechanically. Encode semantic prerequisites and evidence-integrity gates —
"profile only after a concrete CSV is staged", "plot only after a profile exists"
— in the orchestrator's prompt so the model spawns the next child when its own
judgment says the evidence is ready. Reserve deterministic checks for surfacing
reality (a file/path exists, an auth/HITL gate, a schema-validate), never for
deciding the route (see the superseding principles in `.claude/CLAUDE.md`).

## Child Experts

An orchestrator routes to its declared children by CALLING the spawn-runtime
tools, attached automatically to a react expert with declared children:

- `spawn_agent_task(agent, task)` starts a declared child as a REAL child turn in
  a REAL child session (projected as an `AgentTask`, `session_type=agent_task`)
  and returns a `task_id`.
- `wait_agent_tasks([task_id], timeout_s=...)` blocks until the named child turns
  reach a terminal state and returns each child's `output` (its verbatim `answer`)
  and typed `workflow_state`.
- `spawn_agents_parallel([...])` fans a batch of declared children out at once.
- `check_agent_tasks()` lists the tasks this session has spawned and their status.

Each spawn/return is recorded as `blueprint.delegation.{started,completed,failed,
parent_resumed}` semantic events and `expert_handoff` transcript Parts (a
`delegate.started` header and a terminal return row), so the canonical transcript
renders the delegation header, nesting, and return row. The declared parent→child
edge is enforced (a spawn target must be a declared child of the spawning expert)
and the 3-tier hierarchy is bounded structurally. There are no generated in-thread
`delegate_to_<child>` tools; children run as real, independently-persisted turns.

## Workspace Artifacts

Blueprint tool execution is scoped to the active session workspace. Tools with
optional staging or artifact directories should default to workspace-owned
paths, currently:

- NDP resource staging: `<workspace>/.clio/artifacts/ndp-staging`
- SAC waveform staging: `<workspace>/.clio/artifacts/sac-staging`

Model-selected disposable paths such as `/tmp/...` are rewritten to the active
workspace artifact root for these staging tools. Explicit user- or tool-supplied
paths outside the workspace are still allowed only when they pass file policy
and should be reported with absolute provenance. Benchmark evidence must count
only artifacts that exist on disk; stale requested paths and URL fragments are
input references, not verified artifacts.

## Fanout

Concurrent children are spawned with `spawn_agents_parallel`, which admits a batch
of declared children onto a dedicated child-turn executor pool (never the default)
under a global concurrency cap — queueing at the cap and admitting queued tasks as
running ones reach a terminal state. Each child runs as a real child turn and
emits live plus durable `agent.task.*` / `blueprint.delegation.*` semantic events.

## Native Expert Removal

`DataExpert`, `AnalysisExpert`, `VisualizationExpert`, `NDPExpert`, and
`SACFormatExpert` are not default registry routes. Reusable Python code may
remain only as generic tools, validators, adapters, MCP descriptors, or
pack-local marketplace code.

## Optimize

`/optimize` remains design-only in this pass. When implemented it will optimize
Blueprint artifacts and evals, not privileged native expert classes.
