# CLIO Marketplace Agent Benchmark Report

Generated: 2026-06-03 11:35:35 CDT
Evidence JSONL: `/tmp/clio-09-readiness/benchmark/ALCF_GENOMICS_COHORT_QC_REWRITE_EVIDENCE.jsonl`
Benchmark lane: `marketplace_agents`

This is a CLIO session-evidence audit. It is produced from real session JSONL rows. Review the embedded `session_log` root and child messages for prompt, route, tool, artifact, error, recovery, and final-answer evidence. Pytest coverage only guards the harness and tools; it is not the benchmark result.

Result: 1/1 clean passes, 0 expected surfaced errors, 0 expected cancellations, 0 partial recoveries, 0 failures.

Extended stress coverage: has optional gaps outside the per-lane pass/fail gate.

## Extended Stress Coverage Audit

| Criterion | Observed | Required | Status |
| --- | ---: | ---: | --- |
| at least ten complex collaborator-grade demos | 1 | 10 | gap |
| at least five long or high-event stress cases | 0 | 5 | gap |
| at least three cases with tier-3 agents or nanoagents | 0 | 3 | gap |
| at least three visualization artifacts from analyzed data | 0 | 3 | gap |
| at least two deliberate surfaced-error cases | 0 | 2 | gap |
| at least one context-pressure or compaction case | 0 | 1 | gap |
| at least one provider/model-swap stress case | 0 | 1 | gap |

## Evidence Summary

- Max elapsed case: `marketplace_genomics_cohort_qc` (11.7s)
- Max expert depth: `marketplace_genomics_cohort_qc` (5)
- Max branch fanout: `marketplace_genomics_cohort_qc` (4)
- Unique tools used: genomics_vcf_cohort_qc
- Data/input files referenced: 1
- Artifacts verified on disk: 0/0
- Root session logs captured: 1/1
- Child session logs captured: 0
- Semantic trace events captured: 23 events across 1/1 cases (23 live-observed)
- Semantic event types: agent.invocation.completed, agent.invocation.started, delegation.completed, delegation.parent_resumed, delegation.started, hook.invocation.completed, hook.invocation.started, llm.request.started, llm.response.completed, tool.call.completed, tool.call.started, turn.completed, turn.started
- Declared semantic proofs: none
- Observed semantic proofs: none
- Active Agent Blueprints: genomics-review

## Provider Lane Audit

| Criterion | Observed | Required | Status |
| --- | ---: | ---: | --- |
| all marketplace cases prove the requested active Agent Blueprint | 1 | 1 | pass |
| at least five distinct marketplace Agent Blueprints | 1 | 5 | gap |
| all marketplace cases call at least one blueprint expert tool | 1 | 1 | pass |
| marketplace hierarchy cases prove root sync delegation | 1 | 1 | pass |
| at least three marketplace cases prove complex hierarchy depth | 1 | 3 | gap |
| marketplace shallow cases are reported as smoke coverage | 0 | reported | pass |

Provider evidence details:

- genomics-review
- marketplace_genomics_cohort_qc: depth=5 branches=4 sync_handoffs=4

## All Cases

| Case | Category | Blueprint | Mode | Source | Outcome | Agent | Handoffs | Tools | Children | Elapsed |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| marketplace_genomics_cohort_qc | marketplace-genomics | genomics-review | auto | user_agent | pass | main | cohort_qc x4, per_sample_metrics, cohort_outliers, manifest_reconciliation, main | genomics_vcf_cohort_qc | 0 | 11.7s |

## Best 10 Demo Prompts

### 1. Marketplace genomics cohort QC

Case: `marketplace_genomics_cohort_qc`
Category: marketplace-genomics
Routing mode: `auto`
Status: pass
Selected agent: `main`
Active Agent Blueprint: `genomics-review`
Provider/model: `argonne` / `gpt-oss-120b` via `https://inference-api.alcf.anl.gov/resource_server/metis/api/v1`
Provider settings: temperature=1.0, max_tokens=4096, context_length=0, thinking_budget=0
Route graph: orchestrator -> main; cohort_qc -> manifest_reconciliation -> cohort_qc; cohort_qc -> cohort_outliers -> cohort_qc; cohort_qc -> per_sample_metrics -> cohort_qc; main -> cohort_qc -> main
Route metrics: depth=5, branches=4, sync_handoffs=4, child_sessions=0, tools=1
Semantic trace: 23 events, 23 live, types=agent.invocation.completed, agent.invocation.started, delegation.completed, delegation.parent_resumed, delegation.started, hook.invocation.completed, hook.invocation.started, llm.request.started, llm.response.completed, tool.call.completed, tool.call.started, turn.completed, turn.started
Expert handoffs: cohort_qc x4, per_sample_metrics, cohort_outliers, manifest_reconciliation, main
Tools: genomics_vcf_cohort_qc
Data/input files: /tmp/clio-benchmark-data/pathogen_sample_variants.vcf
Setup turns: 0
Root session messages: 2
Child session logs: 0
Actions: none
Child sessions: none
Artifacts: none
Artifact evidence: none
Elapsed: 11.7s

Prompt:

```text
Review this VCF cohort for downstream QC readiness: /tmp/clio-benchmark-data/pathogen_sample_variants.vcf. Check per-sample call rate, missingness, heterozygosity, and whether any samples should be dropped before analysis.
```

What to see: CLIO runs the genomics-review marketplace Agent Blueprint through its cohort_qc expert, executes the per_sample_metrics -> cohort_outliers -> manifest_reconciliation child chain, and calls genomics_vcf_cohort_qc from the metric child.

Why this is interesting: Brings the first-wave cohort QC benchmark infrastructure into the marketplace runner instead of leaving it as only a tool-level proof.

Observed excerpt:

```text
The cohort QC review is complete.

**Results**
- **Sample_A**: Passes all genotype‑metric thresholds  
  - Call rate: 1.0  
  - Heterozygosity: 1.0  
  → **Keep** for downstream analysis.

**Caveats**
- No cohort manifest was supplied, so we could not verify sample identifiers, expected sex/relatedness labels, duplicate IDs, or other metadata.  
- While VCF‑derived metrics confirm call‑rate and heterozygosity quality, they cannot address possible sample swaps, sex mismatches, pedigree errors, or identity disagreements. Providing the manifest would be required for a full reconciliation.

**Conclusion**
Based on the available VCF data, the cohort meets the QC thresholds for call rate and heterozygosity, and the sample(s) evaluated can be retained for downstream analysis. However, the lack of a manifest limits the completeness of the QC assessment.
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
