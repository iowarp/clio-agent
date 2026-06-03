# CLIO Marketplace Agent Benchmark Report

Generated: 2026-06-03 06:06:36 CDT
Evidence JSONL: `benchmark/MARKETPLACE_COMPLEX_HIERARCHY_EVIDENCE.jsonl`
Benchmark lane: `marketplace_agents`

This is a CLIO session-evidence audit. It is produced from real session JSONL rows. Review the embedded `session_log` root and child messages for prompt, route, tool, artifact, error, recovery, and final-answer evidence. Pytest coverage only guards the harness and tools; it is not the benchmark result.

Result: 5/6 clean passes, 0 expected surfaced errors, 0 expected cancellations, 0 partial recoveries, 1 failures.

Extended stress coverage: has optional gaps outside the per-lane pass/fail gate.

## Extended Stress Coverage Audit

| Criterion | Observed | Required | Status |
| --- | ---: | ---: | --- |
| at least ten complex collaborator-grade demos | 5 | 10 | gap |
| at least five long or high-event stress cases | 5 | 5 | pass |
| at least three cases with tier-3 agents or nanoagents | 1 | 3 | gap |
| at least three visualization artifacts from analyzed data | 1 | 3 | gap |
| at least two deliberate surfaced-error cases | 0 | 2 | gap |
| at least one context-pressure or compaction case | 0 | 1 | gap |
| at least one provider/model-swap stress case | 0 | 1 | gap |

High-event or long-running cases:

- marketplace_genomics_reference_review (187.8s, 8 events)
- marketplace_genomics_variant_review (174.7s, 8 events)
- marketplace_materials_crystal_review (144.1s, 8 events)
- marketplace_proteomics_mzml_review (148.1s, 8 events)
- marketplace_seismic_waveform_review (406.7s, 31 events)

## Evidence Summary

- Max elapsed case: `marketplace_seismic_waveform_review` (406.7s)
- Max expert depth: `marketplace_seismic_waveform_review` (6)
- Max branch fanout: `marketplace_seismic_waveform_review` (5)
- Unique tools used: genomics_inspect_fasta, genomics_summarize_vcf, mass_spec_inspect_mzml, materials_inspect_cif, ndp_get_dataset_details, ndp_search_datasets, ndp_stage_resource, sac_compute_trace_statistics, sac_fetch_earthscope_waveform, sac_inspect_archive, sac_plot_traces
- Data/input files referenced: 6
- Artifacts verified on disk: 1/1
- Root session logs captured: 6/6
- Child session logs captured: 0
- Semantic trace events captured: 137 events across 6/6 cases (137 live-observed)
- Semantic event types: agent.invocation.completed, agent.invocation.started, delegation.completed, delegation.parent_resumed, delegation.started, hook.invocation.completed, hook.invocation.started, llm.request.started, llm.response.completed, tool.call.completed, tool.call.started, turn.completed, turn.started
- Declared semantic proofs: failure_recovery, marketplace_pack, nested_tier3, root_delegation, sync_parent_return
- Observed semantic proofs: failure_recovery, marketplace_pack, nested_tier3, root_delegation, sync_parent_return
- Active Agent Blueprints: genomics-review, geospatial-field-review, materials-crystal-review, proteomics-mzml-review, seismic-waveform-review

## Provider Lane Audit

| Criterion | Observed | Required | Status |
| --- | ---: | ---: | --- |
| all marketplace cases prove the requested active Agent Blueprint | 6 | 6 | pass |
| at least five distinct marketplace Agent Blueprints | 5 | 5 | pass |
| all marketplace cases call at least one blueprint expert tool | 5 | 6 | gap |
| marketplace hierarchy cases prove root sync delegation | 5 | 6 | gap |
| at least three marketplace cases prove complex hierarchy depth | 5 | 3 | pass |
| marketplace shallow cases are reported as smoke coverage | 0 | reported | pass |

Provider evidence details:

- genomics-review
- geospatial-field-review
- materials-crystal-review
- proteomics-mzml-review
- seismic-waveform-review
- marketplace_geospatial_field_review: tools=[]
- marketplace_geospatial_field_review: selected=main handoffs=['main'] missing_returns=[]
- marketplace_genomics_reference_review: depth=3 branches=2 sync_handoffs=2
- marketplace_genomics_variant_review: depth=3 branches=2 sync_handoffs=2
- marketplace_materials_crystal_review: depth=3 branches=2 sync_handoffs=2
- marketplace_proteomics_mzml_review: depth=3 branches=2 sync_handoffs=2
- marketplace_seismic_waveform_review: depth=6 branches=5 sync_handoffs=5

## Semantic Proof Declarations

| Case | Declared | Observed |
| --- | --- | --- |
| marketplace_genomics_reference_review | - | - |
| marketplace_genomics_variant_review | - | - |
| marketplace_materials_crystal_review | - | - |
| marketplace_geospatial_field_review | - | - |
| marketplace_proteomics_mzml_review | - | - |
| marketplace_seismic_waveform_review | marketplace_pack, root_delegation, nested_tier3, sync_parent_return, failure_recovery | marketplace_pack, root_delegation, nested_tier3, sync_parent_return, failure_recovery |

## All Cases

| Case | Category | Blueprint | Mode | Source | Outcome | Agent | Handoffs | Tools | Children | Elapsed |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| marketplace_genomics_reference_review | marketplace-genomics | genomics-review | auto | user_agent | pass | main | reference x3, reference_quality x2, main | genomics_inspect_fasta, genomics_inspect_fasta | 0 | 187.8s |
| marketplace_genomics_variant_review | marketplace-genomics | genomics-review | auto | user_agent | pass | main | variants x3, variant_impact x2, main | genomics_summarize_vcf, genomics_summarize_vcf | 0 | 174.7s |
| marketplace_materials_crystal_review | marketplace-materials | materials-crystal-review | auto | user_agent | pass | main | crystal_structure x3, symmetry_quality x2, main | materials_inspect_cif, materials_inspect_cif | 0 | 144.1s |
| marketplace_geospatial_field_review | marketplace-geospatial | geospatial-field-review | auto | user_agent | fail | main | main | - | 0 | 33.2s |
| marketplace_proteomics_mzml_review | marketplace-proteomics | proteomics-mzml-review | auto | user_agent | pass | main | mass_spec x3, spectra_quality x2, main | mass_spec_inspect_mzml, mass_spec_inspect_mzml | 0 | 148.1s |
| marketplace_seismic_waveform_review | marketplace-seismic | seismic-waveform-review | auto | user_agent | pass | main | data x3, ndp_catalog x2, main x4, analysis x6, sac_format x4, visualization | ndp_search_datasets, ndp_get_dataset_details, ndp_stage_resource, ndp_get_dataset_details, ndp_stage_resource, sac_fetch_earthscope_waveform, sac_inspect_archive, sac_compute_trace_statistics, sac_plot_traces, sac_inspect_archive, sac_compute_trace_statistics | 0 | 406.7s |

## Best 10 Demo Prompts

### 1. Marketplace seismic waveform recovery review

Case: `marketplace_seismic_waveform_review`
Category: marketplace-seismic
Routing mode: `auto`
Status: pass
Selected agent: `main`
Active Agent Blueprint: `seismic-waveform-review`
Provider/model: `codex` / `gpt-5.5` via `codex://exec`
Provider settings: temperature=1.0, max_tokens=32000, context_length=0, thinking_budget=0
Route graph: orchestrator -> main; analysis -> sac_format -> analysis; main -> data -> main; main -> analysis -> main; data -> ndp_catalog -> data; main -> visualization -> main
Route metrics: depth=6, branches=5, sync_handoffs=5, child_sessions=0, tools=11
Semantic trace: 52 events, 52 live, types=agent.invocation.completed, agent.invocation.started, delegation.completed, delegation.parent_resumed, delegation.started, hook.invocation.completed, hook.invocation.started, llm.request.started, llm.response.completed, tool.call.completed, tool.call.started, turn.completed, turn.started
Expert handoffs: data x3, ndp_catalog x2, main x4, analysis x6, sac_format x4, visualization
Tools: ndp_search_datasets, ndp_get_dataset_details, ndp_stage_resource, ndp_get_dataset_details, ndp_stage_resource, sac_fetch_earthscope_waveform, sac_inspect_archive, sac_compute_trace_statistics, sac_plot_traces, sac_inspect_archive, sac_compute_trace_statistics
Data/input files: /tmp/clio-09-readiness/tmp/clio-seismic-staging/earthscope_IU_ANMO_00_BHZ_2010-02-27T063000.sac
Setup turns: 0
Root session messages: 2
Child session logs: 0
Actions: none
Child sessions: none
Artifacts: /tmp/clio-09-readiness/.clio-agent-artifacts/charts/sac_traces_earthscope_IU_ANMO_00_BHZ_2010-02-27T063000.png
Artifact evidence: /tmp/clio-09-readiness/.clio-agent-artifacts/charts/sac_traces_earthscope_IU_ANMO_00_BHZ_2010-02-27T063000.png (ok, 96135 B)
Elapsed: 406.7s

Prompt:

```text
Using the active seismic waveform review agent, find bounded seismic waveform evidence through NDP discovery, recover with an alternate SAC source if NDP staging is blocked, inspect the waveform, compute trace statistics, and produce a PNG plot artifact without using stale local files.
```

What to see: CLIO runs the seismic-waveform-review marketplace Agent Blueprint, surfaces NDP staging blockers, recovers with an observed EarthScope SAC path, and creates a verified PNG artifact.

Why this is interesting: Proves the marketplace can carry the strongest hierarchical workflow, not just single-expert file inspection packages.

Observed excerpt:

```text
Waveform inspection and trace statistics are complete from the EarthScope fallback SAC source. A PNG plot artifact is still required, so I am requesting the visualization expert to produce it from the validated SAC path without using stale local files.
/tmp/clio-09-readiness/.clio-agent-artifacts/charts/sac_traces_earthscope_IU_ANMO_00_BHZ_2010-02-27T063000.png
```

### 2. Marketplace genomics FASTA reference review

Case: `marketplace_genomics_reference_review`
Category: marketplace-genomics
Routing mode: `auto`
Status: pass
Selected agent: `main`
Active Agent Blueprint: `genomics-review`
Provider/model: `codex` / `gpt-5.5` via `codex://exec`
Provider settings: temperature=1.0, max_tokens=32000, context_length=0, thinking_budget=0
Route graph: orchestrator -> main; reference -> reference_quality -> reference; main -> reference -> main
Route metrics: depth=3, branches=2, sync_handoffs=2, child_sessions=0, tools=2
Semantic trace: 19 events, 19 live, types=agent.invocation.completed, agent.invocation.started, delegation.completed, delegation.parent_resumed, delegation.started, hook.invocation.completed, hook.invocation.started, llm.request.started, llm.response.completed, tool.call.completed, tool.call.started, turn.completed, turn.started
Expert handoffs: reference x3, reference_quality x2, main
Tools: genomics_inspect_fasta, genomics_inspect_fasta
Data/input files: /tmp/clio-09-readiness/tmp/clio-benchmark-data/pathogen_reference.fasta
Setup turns: 0
Root session messages: 2
Child session logs: 0
Actions: none
Child sessions: none
Artifacts: none
Artifact evidence: none
Elapsed: 187.8s

Prompt:

```text
Review this reference FASTA for collaborator handoff: /tmp/clio-09-readiness/tmp/clio-benchmark-data/pathogen_reference.fasta. Summarize contigs, composition evidence, and what should be verified before variant interpretation.
```

What to see: CLIO runs the genomics-review marketplace Agent Blueprint in this session, routes through the root expert, and uses the reference expert's FASTA tool.

Why this is interesting: Proves a domain agent installed from the marketplace can be activated per session and execute its own hierarchy plus expert/tool surface.

Observed excerpt:

```text
Reference FASTA collaborator handoff summary:

Observed evidence:
- File: `/tmp/clio-09-readiness/tmp/clio-benchmark-data/pathogen_reference.fasta`
- Records: 2
- Total length: 6,050 bp
- Overall GC fraction: 0.537851
- Overall base counts: A=1,406, C=1,673, G=1,581, T=1,390, N=0
- Longest contig: `chrA`, 4,800 bp, GC fraction 0.585833

Per-contig summary:
- `chrA`
  - Description: `synthetic pathogen benchmark reference`
  - Length: 4,800 bp
  - GC fraction: 0.585833
  - Base counts: A=1,005, C=1,446, G=1,366, T=983, N=0
  - Ambiguous bases: 0

- `plasmidB`
  - Description: `synthetic pathogen benchmark reference`
  - Length: 1,250 bp
  - GC fraction: 0.3536
  - Base counts: A=401, C=227, G=215, T=407, N=0
  - Ambiguous bases: 0

Interpretation:
- The reference contains one larger chromosome-like contig, `chrA`, and one smaller plasmid-like contig, `plasmidB`.
- No ambiguous or non-ACGT
```

### 3. Marketplace genomics VCF variant review

Case: `marketplace_genomics_variant_review`
Category: marketplace-genomics
Routing mode: `auto`
Status: pass
Selected agent: `main`
Active Agent Blueprint: `genomics-review`
Provider/model: `codex` / `gpt-5.5` via `codex://exec`
Provider settings: temperature=1.0, max_tokens=32000, context_length=0, thinking_budget=0
Route graph: orchestrator -> main; variants -> variant_impact -> variants; main -> variants -> main
Route metrics: depth=3, branches=2, sync_handoffs=2, child_sessions=0, tools=2
Semantic trace: 19 events, 19 live, types=agent.invocation.completed, agent.invocation.started, delegation.completed, delegation.parent_resumed, delegation.started, hook.invocation.completed, hook.invocation.started, llm.request.started, llm.response.completed, tool.call.completed, tool.call.started, turn.completed, turn.started
Expert handoffs: variants x3, variant_impact x2, main
Tools: genomics_summarize_vcf, genomics_summarize_vcf
Data/input files: /tmp/clio-09-readiness/tmp/clio-benchmark-data/pathogen_sample_variants.vcf
Setup turns: 0
Root session messages: 2
Child session logs: 0
Actions: none
Child sessions: none
Artifacts: none
Artifact evidence: none
Elapsed: 174.7s

Prompt:

```text
Review this VCF for collaborator handoff: /tmp/clio-09-readiness/tmp/clio-benchmark-data/pathogen_sample_variants.vcf. Summarize variant types, likely effects, and what should be verified before analysis.
```

What to see: CLIO runs the genomics-review marketplace Agent Blueprint in this session, routes through the root expert, and uses the variants expert's VCF tool.

Why this is interesting: Exercises a second expert in the same marketplace agent, proving the active blueprint changes the available hierarchy and expert surface.

Observed excerpt:

```text
Review summary for `/tmp/clio-09-readiness/tmp/clio-benchmark-data/pathogen_sample_variants.vcf`:

Observed evidence:
- 1 sample: `sample_A`
- 6 variants total
- Variant types: `snp=3`, `deletion=2`, `insertion=1`
- Contigs: `chrA=4`, `plasmidB=2`
- FILTER counts: `PASS=5`, `LowQual=1`
- Effects: `missense=1`, `frameshift=1`, `insertion=1`, `synonymous=1`, `stop_gained=1`, `deletion=1`

Likely highest-priority effects:
- `var005`: `plasmidB:217`, `REF=C`, `ALT=T`, type `snp`, `QUAL=88.4`, `FILTER=PASS`, `GENE=resistance`, `EFFECT=stop_gained`, `sample_A=0/1:42`
  - Treat as the top collaborator follow-up because it is a `PASS` stop-gained variant in `GENE=resistance` on `plasmidB`.
- `var002`: `chrA:790`, `REF=CT`, `ALT=C`, type `deletion`, `QUAL=54.5`, `FILTER=PASS`, `GENE=membrane`, `EFFECT=frameshift`, `sample_A=0/1:42`
  - Treat as another high-priority disruptive call because it is
```

### 4. Marketplace materials CIF readiness review

Case: `marketplace_materials_crystal_review`
Category: marketplace-materials
Routing mode: `auto`
Status: pass
Selected agent: `main`
Active Agent Blueprint: `materials-crystal-review`
Provider/model: `codex` / `gpt-5.5` via `codex://exec`
Provider settings: temperature=1.0, max_tokens=32000, context_length=0, thinking_budget=0
Route graph: orchestrator -> main; main -> crystal_structure -> main; crystal_structure -> symmetry_quality -> crystal_structure
Route metrics: depth=3, branches=2, sync_handoffs=2, child_sessions=0, tools=2
Semantic trace: 19 events, 19 live, types=agent.invocation.completed, agent.invocation.started, delegation.completed, delegation.parent_resumed, delegation.started, hook.invocation.completed, hook.invocation.started, llm.request.started, llm.response.completed, tool.call.completed, tool.call.started, turn.completed, turn.started
Expert handoffs: crystal_structure x3, symmetry_quality x2, main
Tools: materials_inspect_cif, materials_inspect_cif
Data/input files: /tmp/clio-09-readiness/tmp/clio-benchmark-data/strontium_titanate.cif
Setup turns: 0
Root session messages: 2
Child session logs: 0
Actions: none
Child sessions: none
Artifacts: none
Artifact evidence: none
Elapsed: 144.1s

Prompt:

```text
Review this CIF as a materials simulation handoff: /tmp/clio-09-readiness/tmp/clio-benchmark-data/strontium_titanate.cif. Summarize formula, symmetry, occupancy or atom-site quality, and whether the structure is ready to spend compute time on.
```

What to see: CLIO runs the materials-crystal-review marketplace Agent Blueprint through its root expert, inspects the CIF with crystal_structure, continues through symmetry_quality, and includes simulation_readiness before final synthesis.

Why this is interesting: Proves a separate materials marketplace agent can be loaded per session and can execute a non-seismic multi-expert hierarchy.

Observed excerpt:

```text
The CIF `/tmp/clio-09-readiness/tmp/clio-benchmark-data/strontium_titanate.cif` appears ready for a materials simulation handoff based on the completed crystal-structure inspection.

Observed evidence:
- Data block: `data_SrTiO3_benchmark`
- Formula sum: `Sr1 Ti1 O3`
- Structural formula: `SrTiO3`
- Space group: `P m -3 m`
- Unit cell: `a = 3.905`, `b = 3.905`, `c = 3.905`, `alpha = 90.0`, `beta = 90.0`, `gamma = 90.0`
- Cell volume: `59.547443 Å^3`
- Approximate density: `5.116632 g/cm3`

Atom-site and occupancy quality:
- `Sr1`, `Ti1`, `O1`, `O2`, and `O3` are all present with occupancy `1.0`.
- Species counts are `{"Sr": 1, "Ti": 1, "O": 3}`.
- Occupancy-weighted counts are `{"Sr": 1.0, "Ti": 1.0, "O": 3.0}`.
- Formula, atom sites, and occupancy-weighted composition agree.
- No partial occupancy, atom-site truncation, or formula mismatch was observed.

Interpretation:
- The cubic cell
```

### 5. Marketplace proteomics mzML readiness review

Case: `marketplace_proteomics_mzml_review`
Category: marketplace-proteomics
Routing mode: `auto`
Status: pass
Selected agent: `main`
Active Agent Blueprint: `proteomics-mzml-review`
Provider/model: `codex` / `gpt-5.5` via `codex://exec`
Provider settings: temperature=1.0, max_tokens=32000, context_length=0, thinking_budget=0
Route graph: orchestrator -> main; main -> mass_spec -> main; mass_spec -> spectra_quality -> mass_spec
Route metrics: depth=3, branches=2, sync_handoffs=2, child_sessions=0, tools=2
Semantic trace: 19 events, 19 live, types=agent.invocation.completed, agent.invocation.started, delegation.completed, delegation.parent_resumed, delegation.started, hook.invocation.completed, hook.invocation.started, llm.request.started, llm.response.completed, tool.call.completed, tool.call.started, turn.completed, turn.started
Expert handoffs: mass_spec x3, spectra_quality x2, main
Tools: mass_spec_inspect_mzml, mass_spec_inspect_mzml
Data/input files: /tmp/clio-09-readiness/tmp/clio-benchmark-data/proteomics_qc.mzML
Setup turns: 0
Root session messages: 2
Child session logs: 0
Actions: none
Child sessions: none
Artifacts: none
Artifact evidence: none
Elapsed: 148.1s

Prompt:

```text
Review this mzML run for peptide-search handoff: /tmp/clio-09-readiness/tmp/clio-benchmark-data/proteomics_qc.mzML. Summarize spectra, MS-level balance, m/z coverage, TIC evidence, spectra-quality risks, and whether the run is ready for search.
```

What to see: CLIO runs the proteomics-mzml-review marketplace Agent Blueprint through its root expert, inspects mzML with mass_spec, continues through spectra_quality, and includes search_readiness before final synthesis.

Why this is interesting: Proves a proteomics marketplace agent can be loaded per session and can execute a non-seismic multi-expert hierarchy.

Observed excerpt:

```text
Observed evidence for `/tmp/clio-09-readiness/tmp/clio-benchmark-data/proteomics_qc.mzML`:

- Format: `mzML`
- Inspection status: `ok: true`
- Spectrum count: `4`
- Total peak count: `14`
- MS-level balance: `2` MS1 spectra and `2` MS2 spectra
- m/z coverage: `399.8` to `933.5`
- TIC evidence: total TIC `25140.0`; max TIC `9500.0`

Representative spectra:

- `scan=1`: MS1, scan start time `0.12`, `4` peaks, TIC `9500.0`
- `scan=2`: MS2, scan start time `0.18`, `3` peaks, TIC `3630.0`
- `scan=3`: MS1, scan start time `0.25`, `4` peaks, TIC `8880.0`
- `scan=4`: MS2, scan start time `0.31`, `3` peaks, TIC `3130.0`

Interpretation:

The run has valid mzML structure, detectable TIC, nonzero peaks, and an even MS1/MS2 split. The m/z range is broad enough to show signal across roughly `399.8` to `933.5`.

However, this is not ready for a real peptide-search handoff. The file is extremely small,
```

### 6. Marketplace geospatial GeoJSON review

Case: `marketplace_geospatial_field_review`
Category: marketplace-geospatial
Routing mode: `auto`
Status: fail
Selected agent: `main`
Active Agent Blueprint: `geospatial-field-review`
Provider/model: `codex` / `gpt-5.5` via `codex://exec`
Provider settings: temperature=1.0, max_tokens=32000, context_length=0, thinking_budget=0
Route graph: orchestrator -> main
Route metrics: depth=1, branches=0, sync_handoffs=0, child_sessions=0, tools=0
Semantic trace: 9 events, 9 live, types=agent.invocation.completed, agent.invocation.started, hook.invocation.completed, hook.invocation.started, llm.request.started, llm.response.completed, turn.completed, turn.started
Expert handoffs: main
Tools: none
Data/input files: /tmp/clio-09-readiness/tmp/clio-benchmark-data/field_sites.geojson
Setup turns: 0
Root session messages: 2
Child session logs: 0
Actions: none
Child sessions: none
Artifacts: none
Artifact evidence: none
Elapsed: 33.2s

Prompt:

```text
Review this GeoJSON for field-site map readiness: /tmp/clio-09-readiness/tmp/clio-benchmark-data/field_sites.geojson. Summarize feature types, bounds, properties, and map-overlay risks.
```

What to see: CLIO runs the geospatial-field-review marketplace Agent Blueprint through its root expert and uses the spatial_features expert.

Why this is interesting: Proves a geospatial marketplace agent can be loaded per session and can delegate through its own hierarchy.

Observed excerpt:

```text
Delegating the GeoJSON readiness review to the geospatial root expert so its spatial_features child can inspect /tmp/clio-09-readiness/tmp/clio-benchmark-data/field_sites.geojson with geospatial_inspect_geojson and summarize feature types, bounds, properties, and map-overlay risks.
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

## Failures To Investigate

- `marketplace_geospatial_field_review`: expected CLIO runs the geospatial-field-review marketplace Agent Blueprint through its root expert and uses the spatial_features expert.
  observed agent=main, tools=-, error=None
