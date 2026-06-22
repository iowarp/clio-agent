# GOAL — ARC as the Live Context Plane

> Grounding goal for an ultracode (multi-agent) run. This file states the *goal*,
> the *definition of done*, the *locked decisions*, and the *testing posture*. It
> does NOT sequence the build — decomposition and ordering are the executor's job.
> Full design context: [`docs/design/arc-live-context-plane.md`](docs/design/arc-live-context-plane.md).

## The goal

Implement ARC as clio-agent's **live context plane**: the authoritative, mutable,
ordered, scoped segment store that the DSPy ReAct loop reads from on **every**
iteration — replacing the transient local `trajectory` dict as the source of the
prompt. Deliver the four context operations, per-expert and per-agent scoping, 90%
auto-compaction, and the ARC/Trace separation, as **one working system**, built and
tested together (not as slices/MVPs), validated against **ALCF/Argonne** models as
you go.

This is the foundation for the larger vision (a distributed team of agents that
discover and communicate over shared context) — but THIS goal is the single-node
live plane done correctly. Cross-node CTE backing is explicitly out of scope here
(see Deferred).

## Definition of done

1. **ARC is provably the source.** On every iteration, the message list sent to the
   LM is built from ARC for that scope — verified at the LM boundary:
   - **Byte-equality**: an independent render of ARC@scope == the trajectory content
     in the captured outgoing prompt (modulo static signature/instructions).
   - **Mutation propagation** (the decisive tests): out-of-band mutate ARC mid-loop
     and the *next* prompt reflects it — `append(X)`→present; **`delete(C)`→absent**;
     **`summarize(E→E')`→shows E', not E**; `insert(mid,Y)`→present at position.
   - The local `trajectory` dict is no longer an independent source (true by
     construction: one store, nothing to diverge).
2. **The four ops work** (`append`, `insert`, `delete`, `summarize`) over the segment
   store, at **expert** and **agent** scope. `context-compaction = summarize(all)`.
3. **90% auto-compaction** fires **per-expert**, driven by the provider's exact
   `prompt_tokens / context_window`; threshold **configurable**; dspy's reactive
   `truncate_trajectory` remains only as a never-fired backstop.
4. **ARC/Trace separation holds**: every ARC op is logged to the durable Trace; ARC
   is reconstructable from the Trace; compacting ARC never loses Trace fidelity.
5. **Validated on real ALCF/Argonne runs**, not just synthetic tests — the acceptance
   tests above green against a live model, end-to-end through the gact ReAct path.

## Testing posture

Build and test as a unified whole. Write the acceptance contract (recording-LM
harness at the `dspy.BaseCallback` / `on_lm_start` boundary; byte-equality;
mutation-propagation; prefix cross-check; trace-audit) as the spec, and make the
system satisfy it. Exercise against ALCF/Argonne models throughout — local-model
token-counting quirks (tiktoken mismatch) are part of what must work in reality.

## Locked decisions

**Segment schema** (the load-bearing data model — locked so parallel agents share it):

| Field | Purpose |
|---|---|
| `id` | stable unique id — op target; survives reorder/edit |
| `scope` | tag address `agentX/expertY` — expert/agent addressing + communication |
| `step` | ReAct iteration index — groups thought+tool_call+observation of one iteration |
| `order` | ordering key within scope — render order, monotonic |
| `logical_time` | monotonic clock — as-of-T reads, concurrent-writer ordering |
| `kind` | `system\|user\|tool_def\|thought\|tool_call\|observation\|summary` — render + token attribution |
| `content` | payload — str (thought/observation), `{name,args}` (tool_call), schema/text (tool_def/system) |
| `token_count` | cached per-segment estimate — attribution + compaction targeting |
| `derived_from` | `list[id]` — provenance; for `summary` = ids it replaced (expand + Trace reconstruction) |
| `status` | `live\|tombstoned` — deletion as tombstone; render skips tombstoned |
| `trace_ref` | link to the Trace event/turn — ARC-derived-from-Trace |

- **Granularity:** one piece per segment (thought, tool_call, observation each
  separate), grouped by `step` — enables targeted summarize/delete of one heavy
  segment.
- **dspy round-trip:** *write* — `_RetainingReAct.forward` (`gact/app.py:5407`)
  appends a segment per produced piece; *read* — override `_format_trajectory`
  (dspy `react.py:91`) to rebuild dspy's `thought_/tool_name_/tool_args_/
  observation_{idx}` dict from ARC's live segments (in `order`, skip tombstoned,
  substitute `summary`). The dict dspy formats is built from ARC each call.

**Other locked decisions** (rationale in the design doc):
- Two structural primitives: `insert(position, content)` + `delete(range)`;
  `summarize = delete + insert(LLM_summary)`; `append = insert(end)`.
- Scope = address (expert | agent | cross-agent) → CTE-style tag namespaces; reads
  are `(scope, as-of-T)`.
- Trigger off exact provider `prompt_tokens` (not `token_counter`); `token_counter`
  with the model-DB tokenizer is a pre-send guard only.
- Implement ops as a working v1 behind a stable `apply(op, scope, ...)` interface;
  the naive KV path (re-prompt, recompute from edit point) is correct-and-acceptable
  now; KV-surgery is a later backend swap (see Deferred).

## Constraints (clio core principles — do not violate)

- **No deterministic decision-making in clio core** — the model (parent agent) is the
  router/decider via structured output; clio carries results and re-asks. No keyword/
  prose heuristics, no fabricated decisions.
- Fix root causes in code/data-flow; do not bolt prose constraints onto expert `.md`s.
- DSPy is the internal engine and the reference (source under `docs/ref/dspy/` in the
  main checkout, or the installed package); honor its typed-output/adapter semantics.
- Do not break baseline (the gact ReAct path must keep working).

## Open questions for the executor (resolve in-code)

- Manual **task-change** compaction trigger surface (the non-90% trigger).
- **Scope-aware `ContextCompiler`**: how expert-private + subscribed agent-scope
  segments merge within budget.
- **as-of-T** read semantics for concurrent shared-scope writers (logical clock
  source) — only relevant once agent-scope sharing is exercised.
- Per-model **tokenizer** enrichment in the model DB + post-call self-calibration
  against exact `prompt_tokens`.

## Deferred (out of scope for this goal — hooks present, not built)

- Physical KV surgery (delete/insert/summarize without recompute) behind the
  `apply(op, scope)` interface — clio-core `context-transfer-engine/llm-hooks/kvcache`
  backend; a separate KVCache effort.
- CTE-backed `ARCStore` cross-node shared plane (prove the single-node logical plane
  first).
- Vector/embedding semantic search (neither ARC nor CTE has it).
