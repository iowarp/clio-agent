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

An already-installed default registry that predates the main-as-react pivot is a
special case: a `chain_of_thought` / `predict` root that declares children is
disabled by hierarchy validation (only a `react` root can reach children). Rather
than fail every default session forever with a typed `blueprint_root_disabled`, an
upgraded box **self-heals** (`gact/agent_blueprint_refresh.py`): the stale-invalid
install is detected and refreshed to the shipped pin in a typed, swap-safe
transition (the running registry is replaced atomically; no manual surgery). A
default session that was answering through the old root answers through the react
main after the refresh.

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
tools, attached automatically to a react expert with declared children. Every spawn
is **fire-and-forget async**: the handle returns IMMEDIATELY (`status`
`queued|running`, with a typed `queued_reason` at the concurrency cap) and the child
turn is UNTIED to the spawning turn's lifetime — a parent turn ending never cancels
its children (only cancelling the parent SESSION cascades). The model decides
spawn-vs-wait-vs-observe; CLIO carries the decision, it does not make it.

- `spawn_agent_task(agent, task)` starts a declared child as a REAL child turn in
  a REAL child session (projected as an `AgentTask`, `session_type=agent_task`)
  and returns a `task_id` immediately.
- `spawn_agents_parallel([...])` fans a batch of declared children out at once
  (bounded by the parent's declared `fanout.max_workers`; see [Fanout](#fanout)).
- `wait_agent_tasks([task_id, ...])` performs a committed wait and returns after all
  named child turns reach a terminal state, avoiding model-visible polling loops.
  Pass `timeout_s=...` only when a finite progress checkpoint is intentional; that
  form returns current statuses at the deadline. Both forms return each child's
  `output` (its verbatim `answer`) and typed `workflow_state`.
- `check_agent_tasks([task_id]?)` polls NON-blocking: the tasks this session spawned
  and their status, plus a bounded result excerpt + `message_ref` for finished ones.

Whichever collection path reaches a finished task first CONSUMES it exactly once
(durable `consumed_at` + an `agent.task.consumed` event). A completed-but-unconsumed
child spawned in a PRIOR turn is injected as a bounded, clio-marked grounding block
into the parent's NEXT turn input (task id, child expert, status, result excerpt,
child session id) — the model reads it and decides; CLIO never auto-acts on the
content, and a FAILED child is observed-later IDENTICALLY to a completed one (no
branch anywhere on child output content).

Each spawn/return is recorded as `blueprint.delegation.{started,completed,failed,
parent_resumed}` semantic events and `expert_handoff` transcript Parts (a
`delegate.started` header and a terminal return row), so the canonical transcript
renders the delegation header, nesting, and return row. The declared parent→child
edge is enforced (a spawn target must be a declared child of the spawning expert).
Spawn depth is COMPUTED (each child is one deeper than its parent) and bounded only by
a runaway backstop — `depth > MAX_SPAWN_DEPTH` (8) is refused (typed
`spawn_depth_exceeded`). This is NOT a 3-tier rule: `tier` is semantic weight, not
depth, so a legitimate chain (tier-1 → tier-2 → tier-2 → tier-2 → tier-3s) can run
several levels deep; the backstop only stops unbounded self-spawning. There are no
generated in-thread `delegate_to_<child>` tools; children run as real,
independently-persisted turns on a dedicated executor pool (never the default), so a
waiting parent can never starve its own children.

## Declared Workflows

Deterministic `a → b → c` child pathways are an explicitly DECLARED blueprint
`workflow:` block, not something CLIO infers from model prose. A tier-1 orchestrator
declares a `steps` list, each step naming a declared child and a typed gate over the
accumulated `workflow_state`:

```yaml
workflow:
  steps:
    - id: locate
      agent: geo_resolver
    - id: select
      agent: catalog_selector
      when_state:
        geospatial.status:
          equals: resolved
    - id: profile
      agent: data_profiler
      when_child_completed: select
```

The runner (`gact/workflows.py`) executes the steps in declaration order — each step
is a real `spawn_child_turn` + wait with its own `AgentTask` record, evaluating its
gate (`when_state.<field>.exists` / `.equals` / `when_child_completed`) over the
ACCUMULATED typed `workflow_state`. The DECLARATION is the decision; the model is not
in the loop for the declared steps (declared infra determinism is allowed;
prose-scraping is not). A gate that cannot be satisfied, a child that FAILS, or a
step that exceeds its budget is a TYPED STALL — the run stops and returns
`stalled{reason, step, predicate, observed}` (`workflow_predicate_unsatisfied` /
`workflow_child_failed` / `workflow_step_timeout`, the last cancelling the orphaned
child), never a guess or a silent continuation.

A react main enters the workflow via one `run_workflow` tool, present ONLY when a
workflow is declared (mirroring the children-gated toolset). It returns the full run
record (per-step task ids/results, accumulated `workflow_state`, terminal
`completed | stalled`) and the model decides how to proceed from a stall. Invalid
declarations — unknown child, dependency cycle, malformed predicate, or a
`when_child_completed` naming a step that never runs or runs LATER (an acyclic but
misordered workflow that would stall forever) — are typed validation errors on the
expert row that compose with the react-children hierarchy rules.

## Module Variants (BestOfN / Refine)

An expert may widen its `module` from `{kind}` to `{kind, variant, n, threshold,
reward}` to run its inner DSPy program under a real `dspy.BestOfN` or `dspy.Refine`:

```yaml
module:
  kind: react
  variant: best_of_n        # or: refine
  n: 3
  threshold: 0.8
  reward:
    instructions: Score how completely the answer resolves the user's request.
    inputs: [question, answer]
    target: answer
```

`wrap_module_variant` (`gact/agents/module_variants.py`) wraps the built inner module
in the REAL engine (not a re-implementation); the declared `reward` compiles to a
source-backed generated `def` (`dspy.Refine` requires a real function via
`inspect.getsource`) that scores each attempt with an LM-as-judge `dspy.Predict`,
early-breaking at `reward >= threshold`. An out-of-range/unparseable judge score
clamps or degrades to `0.0` with a typed `variant.reward.parse_failed` log, never a
crash; when EVERY try fails the wrapper raises ONE typed error carrying the last
try's real error (identical for any `n`). The selected try's `winning_index` /
`winning_score` (and every try's score) are stamped additively on the prediction as
`variant_selection` and carried onto the assistant message metadata so the winner is
observable in the durable trace. Invalid declarations (unknown variant, `n < 1`,
missing/malformed reward or threshold) are typed validation errors on the expert row.
N in-process tries of one module in one session are partitioned per try on the ARC
live plane + transcript-tap via a `react_run` keying discriminator (folded only into
keying, never attribution), so try N's model input never accumulates try N-1's
trajectory.

## Workspace Artifacts

Blueprint tool execution is scoped to the active session workspace. Tools with
optional staging or artifact directories should default to workspace-owned
paths, currently:

- NDP resource staging: `<workspace>/.clio/artifacts/ndp-staging`
- SAC waveform staging: `<workspace>/.clio/artifacts/sac-staging`

Model-selected disposable paths such as `/tmp/...` are rewritten to the active
workspace artifact root for these staging tools. Explicit user- or tool-supplied
paths outside the workspace are still allowed only when they pass file policy
and should be reported with absolute provenance.

A produced file becomes a first-class **artifact** by **designation**, never by a
filesystem scan (campaign #966). A tool that declares an output-path argument mints
its artifact automatically at the observer seam; an expert designates a deliverable
it authored (e.g. a report) with the `create_artifact` tool; a user pins one via
`POST /v1/sessions/{sid}/artifacts/pin`. Each artifact carries a hash-pinned,
versioned record (`b = transform(a)` lineage) and gains outbound wire identity as a
`resource_link` part. Consumers — benchmark graders, answer grounding, export —
therefore **query the artifact registry** (`GET /v1/sessions/{sid}/artifacts`,
`.../lineage`, `.../export`), not hand-composed `/artifacts/...` path strings scraped
from tool prose or `workflow_state`. Do NOT instruct a model to compose or cite a
deliverable path string; the registry is the source of truth for which artifacts
exist and where their verified bytes live.

## Fanout

Concurrent children are spawned with `spawn_agents_parallel`, which admits a batch
of declared children onto a dedicated child-turn executor pool (never the default)
under a global concurrency cap — queueing at the cap and admitting queued tasks as
running ones reach a terminal state. Each child runs as a real child turn and
emits live plus durable `agent.task.*` / `blueprint.delegation.*` semantic events.

A parent expert may declare `fanout: {enabled, max_workers}` to bound its own batch:
at most `max_workers` of that parent's concurrent children RUN before the next spawn
queues with the typed `concurrency_cap` reason (queue admission honors the bound
too). The global per-depth cap remains the overall ceiling; an absent/disabled
declaration leaves the batch unbounded up to that cap. The SAME declared child may be
spawned N times concurrently in one parent turn (an ensemble) — each run mints its
own child session + `AgentTask` and carries a durable `run_index` (0, 1, 2… in spawn
order per parent-turn + child) that disambiguates the otherwise-identical rows;
`wait_agent_tasks` merges the runs' typed `workflow_state` in REQUEST order and
surfaces every collision as a typed `workflow_state_merge_conflict` row (no silent
last-writer).

## Native Expert Removal

`DataExpert`, `AnalysisExpert`, `VisualizationExpert`, `NDPExpert`, and
`SACFormatExpert` are not default registry routes. Reusable Python code may
remain only as generic tools, validators, adapters, MCP descriptors, or
pack-local marketplace code.

## Optimize

`/optimize` remains design-only in this pass. When implemented it will optimize
Blueprint artifacts and evals, not privileged native expert classes.
