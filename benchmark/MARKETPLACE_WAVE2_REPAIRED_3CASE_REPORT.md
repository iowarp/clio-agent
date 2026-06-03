# CLIO Marketplace Agent Benchmark Report

Generated: 2026-06-03 13:15:30 CDT
Evidence JSONL: `/tmp/clio-09-readiness/benchmark/MARKETPLACE_WAVE2_REPAIRED_3CASE_EVIDENCE.jsonl`
Benchmark lane: `marketplace_agents`

This is a CLIO session-evidence audit. It is produced from real session JSONL rows. Review the embedded `session_log` root and child messages for prompt, route, tool, artifact, error, recovery, and final-answer evidence. Pytest coverage only guards the harness and tools; it is not the benchmark result.

Result: 3/3 clean passes, 0 expected surfaced errors, 0 expected cancellations, 0 partial recoveries, 0 failures.

Extended stress coverage: has optional gaps outside the per-lane pass/fail gate.

## Extended Stress Coverage Audit

| Criterion | Observed | Required | Status |
| --- | ---: | ---: | --- |
| at least ten complex collaborator-grade demos | 3 | 10 | gap |
| at least five long or high-event stress cases | 1 | 5 | gap |
| at least three cases with tier-3 agents or nanoagents | 0 | 3 | gap |
| at least three visualization artifacts from analyzed data | 0 | 3 | gap |
| at least two deliberate surfaced-error cases | 0 | 2 | gap |
| at least one context-pressure or compaction case | 0 | 1 | gap |
| at least one provider/model-swap stress case | 0 | 1 | gap |

High-event or long-running cases:

- marketplace_materials_crystal_review (15.1s, 10 events)

## Evidence Summary

- Max elapsed case: `marketplace_proteomics_mzml_review` (16.1s)
- Max expert depth: `marketplace_materials_crystal_review` (4)
- Max branch fanout: `marketplace_materials_crystal_review` (4)
- Unique tools used: genomics_summarize_vcf, mass_spec_inspect_mzml, materials_inspect_cif
- Data/input files referenced: 3
- Artifacts verified on disk: 0/0
- Root session logs captured: 3/3
- Child session logs captured: 0
- Semantic trace events captured: 63 events across 3/3 cases (63 live-observed)
- Semantic event types: agent.invocation.completed, agent.invocation.started, delegation.completed, delegation.parent_resumed, delegation.started, hook.invocation.completed, hook.invocation.started, llm.request.started, llm.response.completed, tool.call.completed, tool.call.started, turn.completed, turn.started
- Declared semantic proofs: none
- Observed semantic proofs: none
- Active Agent Blueprints: genomics-review, materials-crystal-review, proteomics-mzml-review

## Provider Lane Audit

| Criterion | Observed | Required | Status |
| --- | ---: | ---: | --- |
| all marketplace cases prove the requested active Agent Blueprint | 3 | 3 | pass |
| at least five distinct marketplace Agent Blueprints | 3 | 5 | gap |
| all marketplace cases call at least one blueprint expert tool | 3 | 3 | pass |
| marketplace hierarchy cases prove root sync delegation | 3 | 3 | pass |
| at least three marketplace cases prove complex hierarchy depth | 3 | 3 | pass |
| marketplace shallow cases are reported as smoke coverage | 0 | reported | pass |

Provider evidence details:

- genomics-review
- materials-crystal-review
- proteomics-mzml-review
- marketplace_genomics_variant_review: depth=3 branches=2 sync_handoffs=2
- marketplace_materials_crystal_review: depth=4 branches=4 sync_handoffs=4
- marketplace_proteomics_mzml_review: depth=4 branches=3 sync_handoffs=3

## All Cases

| Case | Category | Blueprint | Mode | Source | Outcome | Agent | Handoffs | Tools | Children | Elapsed |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| marketplace_genomics_variant_review | marketplace-genomics | genomics-review | auto | user_agent | pass | main | variants x3, variant_impact, main | genomics_summarize_vcf, genomics_summarize_vcf | 0 | 13.6s |
| marketplace_materials_crystal_review | marketplace-materials | materials-crystal-review | auto | user_agent | pass | main | crystal_structure x2, symmetry_quality x2, main x2, simulation_readiness | materials_inspect_cif, materials_inspect_cif | 0 | 15.1s |
| marketplace_proteomics_mzml_review | marketplace-proteomics | proteomics-mzml-review | auto | user_agent | pass | main | mass_spec x3, spectra_quality, main x2, search_readiness | mass_spec_inspect_mzml, mass_spec_inspect_mzml | 0 | 16.1s |

## Best 10 Demo Prompts

### 1. Marketplace materials CIF readiness review

Case: `marketplace_materials_crystal_review`
Category: marketplace-materials
Routing mode: `auto`
Status: pass
Selected agent: `main`
Active Agent Blueprint: `materials-crystal-review`
Provider/model: `argonne` / `gpt-oss-120b` via `https://inference-api.alcf.anl.gov/resource_server/metis/api/v1`
Provider settings: temperature=1.0, max_tokens=4096, context_length=0, thinking_budget=0
Route graph: orchestrator -> main; main -> simulation_readiness -> main; main -> crystal_structure -> main; crystal_structure -> symmetry_quality -> crystal_structure; main -> symmetry_quality
Route metrics: depth=4, branches=4, sync_handoffs=4, child_sessions=0, tools=2
Semantic trace: 22 events, 22 live, types=agent.invocation.completed, agent.invocation.started, delegation.completed, delegation.parent_resumed, delegation.started, hook.invocation.completed, hook.invocation.started, llm.request.started, llm.response.completed, tool.call.completed, tool.call.started, turn.completed, turn.started
Expert handoffs: crystal_structure x2, symmetry_quality x2, main x2, simulation_readiness
Tools: materials_inspect_cif, materials_inspect_cif
Data/input files: /tmp/clio-benchmark-data/strontium_titanate.cif
Setup turns: 0
Root session messages: 2
Child session logs: 0
Actions: none
Child sessions: none
Artifacts: none
Artifact evidence: none
Elapsed: 15.1s

Prompt:

```text
Review this CIF as a materials simulation handoff: /tmp/clio-benchmark-data/strontium_titanate.cif. Summarize formula, symmetry, occupancy or atom-site quality, and whether the structure is ready to spend compute time on.
```

What to see: CLIO runs the materials-crystal-review marketplace Agent Blueprint through its root expert, inspects the CIF with crystal_structure, and continues through symmetry_quality before final synthesis.

Why this is interesting: Proves a separate materials marketplace agent can be loaded per session and can execute a non-seismic multi-expert hierarchy.

Observed excerpt:

```text
Awaiting detailed symmetry and atom‑site quality analysis to complete the review.
```

### 2. Marketplace proteomics mzML readiness review

Case: `marketplace_proteomics_mzml_review`
Category: marketplace-proteomics
Routing mode: `auto`
Status: pass
Selected agent: `main`
Active Agent Blueprint: `proteomics-mzml-review`
Provider/model: `argonne` / `gpt-oss-120b` via `https://inference-api.alcf.anl.gov/resource_server/metis/api/v1`
Provider settings: temperature=1.0, max_tokens=4096, context_length=0, thinking_budget=0
Route graph: orchestrator -> main; main -> mass_spec -> main; mass_spec -> spectra_quality -> mass_spec; main -> search_readiness -> main
Route metrics: depth=4, branches=3, sync_handoffs=3, child_sessions=0, tools=2
Semantic trace: 22 events, 22 live, types=agent.invocation.completed, agent.invocation.started, delegation.completed, delegation.parent_resumed, delegation.started, hook.invocation.completed, hook.invocation.started, llm.request.started, llm.response.completed, tool.call.completed, tool.call.started, turn.completed, turn.started
Expert handoffs: mass_spec x3, spectra_quality, main x2, search_readiness
Tools: mass_spec_inspect_mzml, mass_spec_inspect_mzml
Data/input files: /tmp/clio-benchmark-data/proteomics_qc.mzML
Setup turns: 0
Root session messages: 2
Child session logs: 0
Actions: none
Child sessions: none
Artifacts: none
Artifact evidence: none
Elapsed: 16.1s

Prompt:

```text
Review this mzML run for peptide-search handoff: /tmp/clio-benchmark-data/proteomics_qc.mzML. Summarize spectra, MS-level balance, m/z coverage, TIC evidence, spectra-quality risks, and whether the run is ready for search.
```

What to see: CLIO runs the proteomics-mzml-review marketplace Agent Blueprint through its root expert, inspects mzML with mass_spec, continues through spectra_quality, and then synthesizes peptide-search readiness from the returned evidence.

Why this is interesting: Proves a proteomics marketplace agent can be loaded per session and can execute a non-seismic multi-expert hierarchy.

Observed excerpt:

```text
Proceeding with mass‑spec inspection to gather the required evidence.
```

### 3. Marketplace genomics VCF variant review

Case: `marketplace_genomics_variant_review`
Category: marketplace-genomics
Routing mode: `auto`
Status: pass
Selected agent: `main`
Active Agent Blueprint: `genomics-review`
Provider/model: `argonne` / `gpt-oss-120b` via `https://inference-api.alcf.anl.gov/resource_server/metis/api/v1`
Provider settings: temperature=1.0, max_tokens=4096, context_length=0, thinking_budget=0
Route graph: orchestrator -> main; variants -> variant_impact -> variants; main -> variants -> main
Route metrics: depth=3, branches=2, sync_handoffs=2, child_sessions=0, tools=2
Semantic trace: 19 events, 19 live, types=agent.invocation.completed, agent.invocation.started, delegation.completed, delegation.parent_resumed, delegation.started, hook.invocation.completed, hook.invocation.started, llm.request.started, llm.response.completed, tool.call.completed, tool.call.started, turn.completed, turn.started
Expert handoffs: variants x3, variant_impact, main
Tools: genomics_summarize_vcf, genomics_summarize_vcf
Data/input files: /tmp/clio-benchmark-data/pathogen_sample_variants.vcf
Setup turns: 0
Root session messages: 2
Child session logs: 0
Actions: none
Child sessions: none
Artifacts: none
Artifact evidence: none
Elapsed: 13.6s

Prompt:

```text
Review this VCF for collaborator handoff: /tmp/clio-benchmark-data/pathogen_sample_variants.vcf. Summarize variant types, likely effects, and what should be verified before analysis.
```

What to see: CLIO runs the genomics-review marketplace Agent Blueprint in this session, routes through the root expert, and uses the variants expert's VCF tool.

Why this is interesting: Exercises a second expert in the same marketplace agent, proving the active blueprint changes the available hierarchy and expert surface.

Observed excerpt:

```text
Awaiting detailed variant summary from the Variant Review Expert.
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
