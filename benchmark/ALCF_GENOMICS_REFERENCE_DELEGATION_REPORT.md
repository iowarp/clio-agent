# CLIO Marketplace Agent Benchmark Report

Generated: 2026-06-03 10:55:02 CDT
Evidence JSONL: `/tmp/clio-09-readiness/benchmark/ALCF_GENOMICS_REFERENCE_DELEGATION_EVIDENCE.jsonl`
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

- Max elapsed case: `marketplace_genomics_reference_review` (20.1s)
- Max expert depth: `marketplace_genomics_reference_review` (4)
- Max branch fanout: `marketplace_genomics_reference_review` (2)
- Unique tools used: genomics_inspect_fasta
- Data/input files referenced: 1
- Artifacts verified on disk: 0/0
- Root session logs captured: 1/1
- Child session logs captured: 0
- Semantic trace events captured: 0 events across 0/1 cases (0 live-observed)
- Semantic event types: none
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
- marketplace_genomics_reference_review: depth=4 branches=2 sync_handoffs=2

## All Cases

| Case | Category | Blueprint | Mode | Source | Outcome | Agent | Handoffs | Tools | Children | Elapsed |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| marketplace_genomics_reference_review | marketplace-genomics | genomics-review | auto | live_tool_observer | pass | genomics | reference x3, reference_quality x2, main | genomics_inspect_fasta, genomics_inspect_fasta, genomics_inspect_fasta | 0 | 20.1s |

## Best 10 Demo Prompts

### 1. Marketplace genomics FASTA reference review

Case: `marketplace_genomics_reference_review`
Category: marketplace-genomics
Routing mode: `auto`
Status: pass
Selected agent: `genomics`
Active Agent Blueprint: `genomics-review`
Provider/model: `argonne` / `gpt-oss-120b` via `https://inference-api.alcf.anl.gov/resource_server/metis/api/v1`
Provider settings: temperature=1.0, max_tokens=256, context_length=0, thinking_budget=0
Route graph: orchestrator -> genomics; main -> reference -> main; reference -> reference_quality -> reference
Route metrics: depth=4, branches=2, sync_handoffs=2, child_sessions=0, tools=3
Semantic trace: 0 events, 0 live, types=none
Expert handoffs: reference x3, reference_quality x2, main
Tools: genomics_inspect_fasta, genomics_inspect_fasta, genomics_inspect_fasta
Data/input files: /tmp/clio-benchmark-data/pathogen_reference.fasta
Setup turns: 0
Root session messages: 2
Child session logs: 0
Actions: none
Child sessions: none
Artifacts: none
Artifact evidence: none
Elapsed: 20.1s

Prompt:

```text
Review this reference FASTA for collaborator handoff: /tmp/clio-benchmark-data/pathogen_reference.fasta. Summarize contigs, composition evidence, and what should be verified before variant interpretation.
```

What to see: CLIO runs the genomics-review marketplace Agent Blueprint in this session, routes through the root expert, and uses the reference expert's FASTA tool.

Why this is interesting: Proves a domain agent installed from the marketplace can be activated per session and execute its own hierarchy plus expert/tool surface.

Observed excerpt:

```text
The reference FASTA `/tmp/clio-benchmark-data/pathogen_reference.fasta` contains two clean contigs:

* **chrA** – 4,800 bp, 58.6 % GC, no ambiguous bases.  
* **plasmidB** – 1,250 bp, 35.4 % GC, no ambiguous bases.  

Overall the file has 6,050 bp, ≈ 53.8 % GC, and no truncation or “N” characters.  The contig identifiers and metadata are well‑formed, indicating a complete and uncorrupted reference suitable for downstream analyses.

**Recommended verification before variant interpretation**

1. **Checksum generation** – compute and record an MD5 or SHA‑256 checksum of the FASTA file.  
2. **Metadata manifest** – store a small JSON/TSV file documenting contig IDs, lengths, GC fractions, and base counts.  
3. **Integrity check after transfers** – recompute the checksum and compare to the recorded value to confirm file fidelity.

After completing these steps, the reference can be safely used
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
