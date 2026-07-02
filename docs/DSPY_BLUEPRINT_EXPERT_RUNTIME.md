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
  delegation: true
fanout:
  max_workers: 4
```

`module.kind` selects the DSPy module:

- `predict` compiles to `dspy.Predict`.
- `chain_of_thought` compiles to `dspy.ChainOfThought`.
- `react` compiles to `dspy.ReAct`.

`tools` scopes tool access only. It does not select the module kind. A ReAct
expert receives only its declared tools plus generated internal child-expert
tools from the active Blueprint graph.

## Structured Workflow State

DSPy provides the module, signature, and ReAct/Predict/ChainOfThought substrate.
CLIO's runtime orchestration is separate. Deterministic routing is allowed only
when it operates over declared semantic state produced by Blueprint outputs or
tool results, not arbitrary model prose.

Blueprint children should return compact structured evidence that can include
state such as:

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

Continuation predicates may inspect state fields, for example:

```yaml
parameters:
  continuation_contracts:
    - id: resource_to_analysis
      when_state:
        geospatial.status: resolved
        resource.status: selected
      match: all
      next_expert: analysis
```

Continuation predicates may also inspect workflow progress using completed
declared child ids. Use this for required branch order when the next step should
run after a child has produced evidence, even if the child's domain state is
partial:

```yaml
parameters:
  enforce_child_contract_order: true
  continuation_contracts:
    - id: start_with_discovery
      next_expert: ndp_dataset_discovery
      next_action: discover candidate resources
    - id: discovery_to_station_catalog
      when_child_completed: ndp_dataset_discovery
      next_expert: earthscope_station_catalog
      next_action: rank candidate stations
```

`when_child_completed` is a CLIO workflow-state predicate over declared child
completion metadata. It is not a DSPy primitive and it is not free-text matching.
It can be combined with `when_state` when both execution progress and typed
domain state are required.

Typed continuation contracts are not a substitute for agent judgment. They
should encode semantic prerequisites and evidence-integrity gates, such as
"profile only after a concrete CSV is staged" or "plot only after a profile
exists." They should not force optional scientific branches merely because a
field is absent. For example, an event-catalog expert may be available to a
seismic workflow, but the normal NDP/EarthScope GNSS CSV path should not route
through it unless the parent expert asks for event-catalog evidence or prior
typed tool results make that layer necessary.

The acceptable deterministic branch is over typed evidence produced by a module
or tool:

```yaml
when_state:
  acquisition.status: staged
  acquisition.analysis_ready: true
next_expert: gnss_timeseries_analysis
```

The unacceptable pattern is a hidden benchmark script:

```yaml
when_state:
  event_context.status:
    exists: false
next_expert: seismic_event_catalog
```

That second pattern makes the workflow brittle even though it uses typed state:
it converts an optional capability into mandatory execution outside the model's
semantic decision. If the desired behavior is "call the child when event
context is needed," express that in the parent expert prompt/signature and let
the parent request the child; reserve contracts for verifying that the requested
handoff is legal and evidence-grounded.

Legacy `when_request_contains`, `when_output_contains`, and `NEXT_EXPERT`
markers are compatibility scaffolding for old packs and tests. They must not be
the reliability mechanism for new benchmark or marketplace agents. New packs
should route from typed state such as `status`, `bbox`, `dataset_id`,
`local_path`, `profile.status`, and `artifact.status`, plus structured workflow
progress such as `when_child_completed`.

Agent Blueprint validation rejects continuation contracts that use
`when_request_contains` or `when_output_contains` unless the individual contract
sets `allow_text_routing: true`. That opt-in is for temporary migration or
debug scaffolding only; production benchmark packs should validate without it.
When the opt-in is present, validation emits a warning so release and benchmark
review can distinguish a clean typed-state pack from quarantined legacy routing.

## Child Experts

For every declared parent-child edge in the active Blueprint, CLIO can expose a
generated internal tool such as `delegate_to_sac_format`. The tool runs the
child synchronously, enforces the declared parent edge, and returns compact
evidence to the parent. Provenance is recorded as semantic delegation events and
assistant `expert_handoff` metadata.

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

`fanout` is a bounded runtime primitive for future model-callable worker
creation. It is declarative metadata in this pass; worker execution must enforce
the declared limits and emit live plus durable semantic events when enabled.

## Native Expert Removal

`DataExpert`, `AnalysisExpert`, `VisualizationExpert`, `NDPExpert`, and
`SACFormatExpert` are not default registry routes. Reusable Python code may
remain only as generic tools, validators, adapters, MCP descriptors, or
pack-local marketplace code.

## Optimize

`/optimize` remains design-only in this pass. When implemented it will optimize
Blueprint artifacts and evals, not privileged native expert classes.
