# Pack-Defined DSPy Expert Semantics And Capability Parity

Related issue: `iowarp/clio-agent#629`.

Companion inventory: `NATIVE_EXPERT_MIGRATION_INVENTORY.md`.

## Architecture Target

CLIO should not have two classes of experts. Marketplace/Agent Blueprint
experts must have the same expressive power as the old native Python experts.
Python remains the runtime substrate: provider calls, tool/MCP execution,
storage, memory, artifact handling, semantic logging/streaming, validation,
trust, and sandbox plumbing.

The target state is zero domain/expert behavior left as native Python agents.
Existing native experts should be migrated into blueprint packs, generic runtime
primitives, or built-in tools that are exposed declaratively to packs.

The historical Python/DSPy experts were the motivating prototype: they showed
that specialized DSPy loops, structured signatures, tool use, delegation,
fan-out, and typed evidence returns work. Agent Blueprints are the
generalization of that prototype. They should make those DSPy-style semantics
configurable, portable, editable, and shareable through marketplace packs.

An expert should not be modeled as "just a prompt." Prompt text is one part of
an expert, but the stronger abstraction is a DSPy semantic contract: signature,
module/loop type, allowed tools/MCPs, structured outputs, delegation/fan-out
contracts, memory/artifact access, and provider/model defaults. A prompt-only
expert is a valid lightweight case, not the full expert model.

## Current State

CLIO currently supports several DSPy-style expert behaviors, but not all of
them are exposed through Agent Blueprints:

1. **Sync delegated experts from Agent Blueprints.** A pack can define experts
   with `parent_id`, `tier`, and `children`. The runtime can execute a child
   synchronously and return compact evidence to the parent.
2. **Runtime-spawned nanoagents.** The runtime can materialize
   `nanoagents_spawned` rows as child subagent sessions and emit
   `subagent.started` / `subagent.completed` semantic events.
3. **Native DSPy expert modules.** Hardcoded Python classes can define
   signatures, choose module patterns, run ReAct-style tool loops, attach
   structured provenance, and return custom fields.

Today, several of these behaviors are primarily reachable from hardcoded Python
expert implementations. That is the architectural problem.

Hierarchy depth and nanoagent execution are separate axes. A pack can have
static experts at any depth, including tier-4 or tier-5, and those experts are
still declared hierarchy nodes. A nanoagent is an ephemeral bounded worker
spawned across items such as files, samples, runs, records, or catalog
candidates. It is not synonymous with any fixed tier number.

## Gap

Agent Blueprints do not yet provide a complete file-backed way to declare the
DSPy semantics currently available to hardcoded Python experts. Marketplace
packs can define prompts, tools, model defaults, and static hierarchy, but they
cannot yet fully declare:

- the expert signature: input fields, output fields, and structured evidence
  schema,
- whether prompt-only execution is sufficient or a DSPy signature-backed module
  is required,
- the module/loop pattern: Predict, ChainOfThought, ReAct/tool loop,
  retry/refine, router, reducer, or bounded worker,
- how tool results become typed evidence and final outputs,
- how delegation requests are structured and returned,
- detect these items,
- spawn this bounded worker template once per item,
- cap concurrency and total items,
- tolerate partial failure,
- return this compact evidence shape to the parent,
- record spawned child sessions and semantic events.

That means benchmark cases that require per-sample genomics QC, per-run DFT
classification, per-station climate checks, or per-file validation still depend
on native-only expert behavior instead of reusable marketplace infrastructure.
The long-term direction is to remove that privilege: hardcoded Python experts
should either become generic runtime/tool substrate or be migrated into
blueprint-defined packs.

## Migration Rule

Native Python expert code should be treated as legacy unless it is pure runtime
substrate. For every native expert behavior, classify it as:

- Runtime substrate to keep in Python.
- Built-in tool/MCP adapter to keep in Python but expose declaratively.
- Domain expert behavior to migrate into blueprint/marketplace packs.
- Legacy compatibility shim to remove after migration.

Benchmarks should not be accepted as final public-demo passes if their expert
semantics depend on native-only domain expert behavior.

The current native expert migration inventory is maintained in
`NATIVE_EXPERT_MIGRATION_INVENTORY.md`.

## Desired Blueprint Shape

The exact schema should be validated before implementation, but the capability
should be declarative and explicit. A blueprint expert should be able to
declare the DSPy semantics it needs, for example:

```yaml
module:
  kind: react
  signature:
    id: cohort_qc_review
    inputs:
      question: str
      parent_evidence: str
    outputs:
      evidence: list[object]
      expert_handoffs: list[object]
      final_answer: str
  structured_outputs:
    evidence_fields: [item_id, finding, confidence, provenance]
    delegation_fields: [delegate_to, question, status]
  prompt:
    system: prompts/cohort_qc.system.md
    profile: heavy
```

A parent expert could also declare a fan-out block such as:

```yaml
fanout:
  id: per_sample_qc
  item_source: files
  item_filter:
    suffixes: [".vcf", ".vcf.gz"]
  worker:
    agent_id: sample_qc
    prompt_template: "Compute bounded QC evidence for {item_path}."
  limits:
    max_items: 50
    concurrency: 4
  merge:
    partial_failure: report_and_continue
    evidence_fields: [sample_id, call_rate, heterozygosity, inferred_sex, status]
```

This should stay generic. The runtime should not know that genomics, DFT, or
climate are special; those meanings belong in pack prompts, skills, tools, and
case data.

## Runtime Semantics

- The parent expert owns the fan-out decision and receives all child evidence.
- Each spawned worker becomes a child subagent session with parent/session
  provenance.
- Spawned workers must obey the active workspace, pack, tool, MCP, and provider
  scope.
- Blueprint-declared signatures and structured outputs must be validated before
  runtime, then normalized into the same internal shapes native DSPy experts
  currently return.
- The runtime must emit live session events and durable semantic events for
  LLM calls, module starts/completions, spawn start, completion, tool/MCP calls,
  errors, delegation, and merge results.
- Partial failures should be represented as data, not hidden. The merge policy
  decides whether to continue or fail.
- The parent's final answer must cite returned child evidence, not raw child
  context wholesale.

## Benchmark Implications

Until this exists, a benchmark can prove static nested hierarchy at arbitrary
depth, but cannot honestly claim full marketplace-defined DSPy expert
semantics. Cases that should wait for or exercise this feature include:

- `benchmark/case05-genomics-cohort-qc/`
- DFT or per-run convergence audit if added to the final 12.
- Any regional/station or catalog-candidate fan-out case.
- Multi-file validation cases that should be marketplace-pack driven rather
  than hardcoded `AnalysisExpert` behavior.

The pass evidence should show `subagent.started` and `subagent.completed`
events, child sessions, item-specific tool evidence, and a parent merge result.
