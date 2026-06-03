# CLIO Marketplace Agent Benchmark Report

Generated: 2026-06-03 10:29:36 CDT
Evidence JSONL: `/tmp/clio-09-readiness/benchmark/DEMO_READINESS_PROTEOMICS_SKILLS_EVIDENCE.jsonl`
Benchmark lane: `marketplace_agents`

This is a CLIO session-evidence audit. It is produced from real session JSONL rows. Review the embedded `session_log` root and child messages for prompt, route, tool, artifact, error, recovery, and final-answer evidence. Pytest coverage only guards the harness and tools; it is not the benchmark result.

Result: 1/1 clean passes, 0 expected surfaced errors, 0 expected cancellations, 0 partial recoveries, 0 failures.

Extended stress coverage: has optional gaps outside the per-lane pass/fail gate.

## Extended Stress Coverage Audit

| Criterion | Observed | Required | Status |
| --- | ---: | ---: | --- |
| at least ten complex collaborator-grade demos | 1 | 10 | gap |
| at least five long or high-event stress cases | 1 | 5 | gap |
| at least three cases with tier-3 agents or nanoagents | 0 | 3 | gap |
| at least three visualization artifacts from analyzed data | 0 | 3 | gap |
| at least two deliberate surfaced-error cases | 0 | 2 | gap |
| at least one context-pressure or compaction case | 0 | 1 | gap |
| at least one provider/model-swap stress case | 0 | 1 | gap |

High-event or long-running cases:

- marketplace_proteomics_lfq_differential (163.1s, 6 events)

## Evidence Summary

- Max elapsed case: `marketplace_proteomics_lfq_differential` (163.1s)
- Max expert depth: `marketplace_proteomics_lfq_differential` (2)
- Max branch fanout: `marketplace_proteomics_lfq_differential` (1)
- Unique tools used: mass_spec_lfq_differential_abundance
- Data/input files referenced: 1
- Artifacts verified on disk: 0/0
- Root session logs captured: 1/1
- Child session logs captured: 0
- Semantic trace events captured: 19 events across 1/1 cases (19 live-observed)
- Semantic event types: agent.invocation.completed, agent.invocation.started, delegation.completed, delegation.parent_resumed, delegation.started, hook.invocation.completed, hook.invocation.started, llm.request.started, llm.response.completed, tool.call.completed, tool.call.started, turn.completed, turn.started
- Declared semantic proofs: none
- Observed semantic proofs: none
- Active Agent Blueprints: proteomics-mzml-review

## Provider Lane Audit

| Criterion | Observed | Required | Status |
| --- | ---: | ---: | --- |
| all marketplace cases prove the requested active Agent Blueprint | 1 | 1 | pass |
| at least five distinct marketplace Agent Blueprints | 1 | 5 | gap |
| all marketplace cases call at least one blueprint expert tool | 1 | 1 | pass |
| marketplace hierarchy cases prove root sync delegation | 1 | 1 | pass |
| at least three marketplace cases prove complex hierarchy depth | 0 | 3 | gap |
| marketplace shallow cases are reported as smoke coverage | 1 | reported | pass |

Provider evidence details:

- proteomics-mzml-review
- marketplace_proteomics_lfq_differential: depth=2 branches=1 counts as load/tool smoke, not complex hierarchy proof

## All Cases

| Case | Category | Blueprint | Mode | Source | Outcome | Agent | Handoffs | Tools | Children | Elapsed |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| marketplace_proteomics_lfq_differential | marketplace-proteomics | proteomics-mzml-review | auto | user_agent | pass | main | lfq_differential x2, main x2 | mass_spec_lfq_differential_abundance, mass_spec_lfq_differential_abundance | 0 | 163.1s |

## Best 10 Demo Prompts

### 1. Marketplace proteomics LFQ differential abundance

Case: `marketplace_proteomics_lfq_differential`
Category: marketplace-proteomics
Routing mode: `auto`
Status: pass
Selected agent: `main`
Active Agent Blueprint: `proteomics-mzml-review`
Provider/model: `codex` / `gpt-5.5` via `codex://exec`
Provider settings: temperature=1.0, max_tokens=32000, context_length=0, thinking_budget=0
Route graph: orchestrator -> main; main -> lfq_differential -> main
Route metrics: depth=2, branches=1, sync_handoffs=1, child_sessions=0, tools=2
Semantic trace: 19 events, 19 live, types=agent.invocation.completed, agent.invocation.started, delegation.completed, delegation.parent_resumed, delegation.started, hook.invocation.completed, hook.invocation.started, llm.request.started, llm.response.completed, tool.call.completed, tool.call.started, turn.completed, turn.started
Expert handoffs: lfq_differential x2, main x2
Tools: mass_spec_lfq_differential_abundance, mass_spec_lfq_differential_abundance
Data/input files: /tmp/clio-09-readiness/tmp/demo-readiness-data-28/proteinGroups_lfq_benchmark.tsv
Setup turns: 0
Root session messages: 2
Child session logs: 0
Actions: none
Child sessions: none
Artifacts: none
Artifact evidence: none
Elapsed: 163.1s

Prompt:

```text
Review this LFQ proteinGroups matrix for differential abundance: /tmp/clio-09-readiness/tmp/demo-readiness-data-28/proteinGroups_lfq_benchmark.tsv. Compare columns matching Control against Treatment, use spike-in terms SPIKEUP,SPIKEUPB with an expected log2 fold change near 2.0, and tell me which proteins look most changed.
```

What to see: CLIO runs the proteomics marketplace Agent Blueprint through its lfq_differential expert and calls the LFQ differential-abundance tool.

Why this is interesting: Exercises the first-wave proteomics LFQ decision-subtree infrastructure rather than only mzML inspection.

Observed excerpt:

```text
Reviewed `/tmp/clio-09-readiness/tmp/demo-readiness-data-28/proteinGroups_lfq_benchmark.tsv` as an LFQ proteinGroups matrix.

After MaxQuant-style contaminant/reverse filtering, 5 of 7 rows were analyzed; 2 rows were removed as contaminant or reverse hits. Control columns were `LFQ intensity Control_1`, `LFQ intensity Control_2`, `LFQ intensity Control_3`; Treatment columns were `LFQ intensity Treatment_1`, `LFQ intensity Treatment_2`, `LFQ intensity Treatment_3`.

Use the `raw` LFQ scale for interpretation. The spike-ins support this choice: raw spike median log2FC was `1.985499`, very close to the expected log2 fold change near `2.0`, while median normalization compressed the spike effect to `1.229601`.

Most changed proteins:
- `spike_UP_A`, gene `SPIKEUP`: log2FC `1.996352`, strong Treatment increase, matches expected spike-in behavior.
- `spike_UP_B`, gene `SPIKEUPB`: log2FC `1.9746
```

## Failures Fixed During This Campaign

- GACT compaction originally bypassed transient-provider retry and only updated the GACT transcript; compaction now retries provider throttles, updates ARC memory, and fails with structured errors if memory storage fails.
- Compact summaries could lose exact scientific identifiers at the ARC truncation boundary; compact memory now preserves a labeled exact evidence index for paths, variables, columns, artifacts, and caveats.
- Retained multi-file context could make analysis narrow to the first file or let CSV follow-ups be stolen by broad synthesis; explicit file paths now take precedence and retained multi-source synthesis is limited to true synthesis questions.
- Planner-selected tool actions used to make benchmark evidence look flat; reports now preserve parent-owned sync delegation returns such as `data -> ndp_catalog -> data` and audit missing parent-resume evidence.
- Provider throttles during expert dispatch, handoffs, and compaction could surface as brittle partial recoveries; expert paths now use bounded transient-provider retry and still surface structured errors if exhausted.

## Remaining Caveats

- This report is evidence for the recorded provider/session run, not a guarantee that provider availability, model latency, token freshness, or external data services will be identical later.
- Several high-event cases are intentionally fast because child/nanoagent workers use deterministic local tools after routing; elapsed time alone should not be treated as benchmark depth.
- The benchmark now covers the hierarchy and handoff classes listed here, but future providers, file formats, and per-expert model assignments still need their own evidence runs.
