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
- commit: `5aa5d6f566cf542bc32c7bccf963fd765f803caf`
- default activation: `data-semantics`

Fresh installs install that snapshot into the normal Agent Blueprint store.
Missing or mismatched pins are surfaced as disabled Blueprint diagnostics; CLIO
does not fall back to `src/clio_agent/agent_blueprints/builtin`.

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

## Child Experts

For every declared parent-child edge in the active Blueprint, CLIO can expose a
generated internal tool such as `delegate_to_sac_format`. The tool runs the
child synchronously, enforces the declared parent edge, and returns compact
evidence to the parent. Provenance is recorded as semantic delegation events and
assistant `expert_handoff` metadata.

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
