# CLIO Real-Orchestrator Benchmark Report

Generated: 2026-05-29 05:28:24 CDT
Evidence JSONL: `/home/jcernuda/clio-agent/benchmark/FRESH_REAL_ORCHESTRATOR_EVIDENCE.jsonl`
Benchmark lane: `real_orchestrator`

This is a CLIO session-evidence audit. It is produced from real session JSONL rows. Review the embedded `session_log` root and child messages for prompt, route, tool, artifact, error, recovery, and final-answer evidence. Pytest coverage only guards the harness and tools; it is not the benchmark result.

Result: 10/12 clean passes, 0 expected surfaced errors, 0 expected cancellations, 0 partial recoveries, 2 failures.

Extended stress coverage: has optional gaps outside the per-lane pass/fail gate.

## Extended Stress Coverage Audit

| Criterion | Observed | Required | Status |
| --- | ---: | ---: | --- |
| at least ten complex collaborator-grade demos | 7 | 10 | gap |
| at least five long or high-event stress cases | 3 | 5 | gap |
| at least three cases with tier-3 agents or nanoagents | 4 | 3 | pass |
| at least three visualization artifacts from analyzed data | 3 | 3 | pass |
| at least two deliberate surfaced-error cases | 0 | 2 | gap |
| at least one context-pressure or compaction case | 0 | 1 | gap |
| at least one provider/model-swap stress case | 0 | 1 | gap |

High-event or long-running cases:

- cross_file_dirty_quality_gate_nanoagents (36.6s, 15 events)
- reasoning_cross_file_triage_nanoagents (16.1s, 11 events)
- ndp_seismic_waveform_to_plot (210.8s, 31 events)

## Evidence Summary

- Max elapsed case: `ndp_seismic_waveform_to_plot` (210.8s)
- Max expert depth: `ndp_catalog_discovery` (2)
- Max branch fanout: `cross_file_dirty_quality_gate_nanoagents` (4)
- Unique tools used: adios_inspect_file, csv_read_table, genomics_inspect_fasta, genomics_summarize_vcf, geospatial_inspect_geojson, hdf5_analyze_file, hdf5_list_datasets, imaging_inspect_png, mass_spec_inspect_mzml, materials_inspect_cif, ndp_get_dataset_details, ndp_list_organizations, ndp_search_datasets, ndp_stage_resource, parquet_analyze_schema, parquet_compute_statistics, plot_bar_chart, plot_summary
- Data/input files referenced: 11
- Artifacts verified on disk: 3/3
- Root session logs captured: 12/12
- Child session logs captured: 8

## Provider Lane Audit

| Criterion | Observed | Required | Status |
| --- | ---: | ---: | --- |
| all selected cases avoid shortcut route sources | 12 | 12 | pass |
| passing cases include structured route/tool evidence | 10 | 10 | pass |
| artifact-producing cases verify artifacts on disk | 3 | 4 | gap |
| nested expert handoffs include sync return/resume provenance | 10 | 10 | pass |
| planner multi-file hierarchy case passes | 1 | 1 | pass |
| dirty cross-file quality gate passes | 1 | 1 | pass |
| NDP waveform benchmark reaches verified SAC/PNG artifact | 0 | 1 | gap |
| NDP full SAC/PNG chain verified | 0 | 1 | gap |

Provider evidence details:

- ndp_seismic_waveform_to_plot: artifact_evidence=[]
- full SAC/PNG path not reached in this run

## All Cases

| Case | Category | Blueprint | Mode | Source | Outcome | Agent | Handoffs | Tools | Children | Elapsed |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| csv_status_visual_summary | visualization | - | auto | dspy | pass | visualization | visualization | plot_bar_chart | 0 | 17.6s |
| cross_file_dirty_quality_gate_nanoagents | multi-agent | - | auto | dspy | pass | analysis | analysis x3 | hdf5_analyze_file, hdf5_list_datasets, adios_inspect_file, parquet_analyze_schema, parquet_compute_statistics, csv_read_table, parquet_compute_statistics, parquet_compute_statistics | 4 | 36.6s |
| reasoning_cross_file_triage_nanoagents | planner-hardening | - | reasoning_only | dspy | pass | analysis | analysis | hdf5_analyze_file, hdf5_list_datasets, adios_inspect_file, parquet_analyze_schema, parquet_compute_statistics, csv_read_table | 4 | 16.1s |
| reasoning_adios_bp5_container | planner-hardening | - | reasoning_only | dspy | pass | data | data | adios_inspect_file | 0 | 16.6s |
| dirty_quality_dashboard_multi_turn | visualization | - | auto | dspy | pass | visualization | visualization | plot_summary | 0 | 32.6s |
| ndp_catalog_discovery | external-catalog | - | auto | dspy | pass | data | data x2, ndp_catalog x2 | ndp_list_organizations, ndp_search_datasets, ndp_get_dataset_details, ndp_stage_resource | 0 | 27.1s |
| ndp_seismic_waveform_to_plot | hierarchical-science | - | auto | dspy | fail | data | data x3, ndp_catalog x2 | ndp_list_organizations, ndp_search_datasets, ndp_search_datasets, ndp_search_datasets, ndp_get_dataset_details, ndp_stage_resource, ndp_stage_resource, ndp_stage_resource, ndp_get_dataset_details, ndp_stage_resource, ndp_stage_resource, ndp_get_dataset_details, ndp_stage_resource, ndp_list_organizations, ndp_search_datasets, ndp_search_datasets, ndp_search_datasets, ndp_get_dataset_details, ndp_stage_resource, ndp_stage_resource, ndp_stage_resource, ndp_get_dataset_details, ndp_stage_resource, ndp_stage_resource, ndp_get_dataset_details, ndp_stage_resource | 0 | 210.8s |
| genomics_reference_variant_review | genomics | - | auto | dspy | fail | genomics | genomics x3 | genomics_inspect_fasta, genomics_summarize_vcf | 0 | 13.1s |
| materials_cif_structure_review | materials | - | auto | dspy | pass | materials | materials x2 | materials_inspect_cif | 0 | 13.1s |
| geospatial_field_site_review | geospatial | - | auto | dspy | pass | geospatial | geospatial x2 | geospatial_inspect_geojson | 0 | 12.1s |
| microscopy_png_readiness_review | imaging | - | auto | dspy | pass | imaging | imaging x2 | imaging_inspect_png | 0 | 14.1s |
| mass_spec_mzml_qc_review | mass_spec | - | auto | dspy | pass | mass_spec | mass_spec x2 | mass_spec_inspect_mzml | 0 | 14.6s |

## Best 10 Demo Prompts

### 1. Dirty cross-file quality gate

Case: `cross_file_dirty_quality_gate_nanoagents`
Category: multi-agent
Routing mode: `auto`
Status: pass
Selected agent: `analysis`
Active Agent Blueprint: `-`
Provider/model: `codex` / `gpt-5.5` via `codex://exec`
Provider settings: temperature=1.0, max_tokens=32000, context_length=0, thinking_budget=0
Route graph: orchestrator -> analysis -> [csv_validator subagent, analysis_validator subagent, adios_validator subagent, data_validator subagent]
Route metrics: depth=1, branches=4, tools=8
Expert handoffs: analysis x3
Tools: hdf5_analyze_file, hdf5_list_datasets, adios_inspect_file, parquet_analyze_schema, parquet_compute_statistics, csv_read_table, parquet_compute_statistics, parquet_compute_statistics
Data/input files: /home/jcernuda/clio-agent/tmp/clio-benchmark-data/fusion_run.h5, /home/jcernuda/clio-agent/tmp/clio-benchmark-data/facility_measurements_dirty.parquet, /home/jcernuda/clio-agent/tmp/clio-benchmark-data/sensor_events.csv
Setup turns: 0
Root session messages: 2
Child session logs: 4
Actions: none
Child sessions: csv_validator subagent, analysis_validator subagent, adios_validator subagent, data_validator subagent
Artifacts: none
Artifact evidence: none
Elapsed: 36.6s

Prompt:

```text
Before I share this run, build a quality gate across /home/jcernuda/clio-agent/tmp/clio-benchmark-data/fusion_run.h5, /home/jcernuda/clio-agent/tmp/clio-benchmark-data/facility_measurements_dirty.parquet, /home/jcernuda/clio-agent/tmp/clio-benchmark-data/sensor_events.csv, and "/home/jcernuda/clio-agent/tmp/clio-benchmark-data/gray scott noise 0.01 data.bp5". I need to know what each file proves, where the dirty tabular export is risky, and which checks block collaborator handoff.
```

What to see: Analysis coordinates tool-backed child workers over HDF5, dirty Parquet, CSV, and BP5 evidence.

Why this is interesting: Adds a harder cross-file case where one source is intentionally dirty and the user asks for a review gate rather than a generic summary.

Observed excerpt:

```text
Quality gate: fusion_run.h5 proves structured fusion axes/plasma/diagnostic/quality datasets with a compression caveat, sensor_events.csv proves a complete 420-row event table, the BP5 proves container/profiling health but not variable semantics because ADIOS2 is missing, and handoff should block on the dirty Parquet nulls/2 invalid rows plus missing BP5 variable-level inspection.
```

### 2. NDP seismic waveform discovery to plot

Case: `ndp_seismic_waveform_to_plot`
Category: hierarchical-science
Routing mode: `auto`
Status: fail
Selected agent: `data`
Active Agent Blueprint: `-`
Provider/model: `codex` / `gpt-5.5` via `codex://exec`
Provider settings: temperature=1.0, max_tokens=32000, context_length=0, thinking_budget=0
Route graph: orchestrator -> data; data -> ndp_catalog -> data
Route metrics: depth=2, branches=0, tools=26
Expert handoffs: data x3, ndp_catalog x2
Tools: ndp_list_organizations, ndp_search_datasets, ndp_search_datasets, ndp_search_datasets, ndp_get_dataset_details, ndp_stage_resource, ndp_stage_resource, ndp_stage_resource, ndp_get_dataset_details, ndp_stage_resource, ndp_stage_resource, ndp_get_dataset_details, ndp_stage_resource, ndp_list_organizations, ndp_search_datasets, ndp_search_datasets, ndp_search_datasets, ndp_get_dataset_details, ndp_stage_resource, ndp_stage_resource, ndp_stage_resource, ndp_get_dataset_details, ndp_stage_resource, ndp_stage_resource, ndp_get_dataset_details, ndp_stage_resource
Data/input files: none
Setup turns: 0
Root session messages: 2
Child session logs: 0
Actions: none
Child sessions: none
Artifacts: none
Artifact evidence: none
Elapsed: 210.8s

Prompt:

```text
Find a bounded seismic waveform dataset from a seismological or Earth-science organization in the National Data Platform. Choose a usable resource, stage it if it is small enough, inspect the waveform content, compute representative trace statistics, and produce a plot artifact. If a candidate is too large or unavailable, surface that as the result instead of inventing a plot.
```

What to see: CLIO delegates NDP discovery to ndp_catalog, stages a bounded waveform resource, analyzes SAC traces through sac_format, and creates a PNG plot.

Why this is interesting: This is the core hierarchical science demo: provider discovery, data access, format-specific analysis, and visualization without the user naming internal agents.

Observed excerpt:

```text
No bounded NDP seismic waveform resource could be staged: HIVE waveform resources timed out via curl and the Salton Sea MiniSEED resource is 1503238553 bytes, above the 52428800-byte limit; next action is to retry later, raise the staging bound intentionally, or provide a smaller concrete resource.
```

### 3. No-guard cross-file triage

Case: `reasoning_cross_file_triage_nanoagents`
Category: planner-hardening
Routing mode: `reasoning_only`
Status: pass
Selected agent: `analysis`
Active Agent Blueprint: `-`
Provider/model: `codex` / `gpt-5.5` via `codex://exec`
Provider settings: temperature=1.0, max_tokens=32000, context_length=0, thinking_budget=0
Route graph: orchestrator -> analysis -> [csv_validator subagent, analysis_validator subagent, adios_validator subagent, data_validator subagent]
Route metrics: depth=1, branches=4, tools=6
Expert handoffs: analysis
Tools: hdf5_analyze_file, hdf5_list_datasets, adios_inspect_file, parquet_analyze_schema, parquet_compute_statistics, csv_read_table
Data/input files: /home/jcernuda/clio-agent/tmp/clio-benchmark-data/fusion_run.h5, /home/jcernuda/clio-agent/tmp/clio-benchmark-data/facility_measurements.parquet, /home/jcernuda/clio-agent/tmp/clio-benchmark-data/sensor_events.csv
Setup turns: 0
Root session messages: 2
Child session logs: 4
Actions: none
Child sessions: csv_validator subagent, analysis_validator subagent, adios_validator subagent, data_validator subagent
Artifacts: none
Artifact evidence: none
Elapsed: 16.1s

Prompt:

```text
I have four related files from the same experiment: /home/jcernuda/clio-agent/tmp/clio-benchmark-data/fusion_run.h5, /home/jcernuda/clio-agent/tmp/clio-benchmark-data/facility_measurements.parquet, /home/jcernuda/clio-agent/tmp/clio-benchmark-data/sensor_events.csv, and "/home/jcernuda/clio-agent/tmp/clio-benchmark-data/gray scott noise 0.01 data.bp5". Give me a cross-file triage summary: what is in each file, whether the measurements look ready for downstream analysis, and what I should check next.
```

What to see: With routing guards disabled for the session, the planner still reaches analysis and tool-backed child workers.

Why this is interesting: Separates planner capability from the production registry guard, which matters as CLIO grows beyond a few built-in experts.

Observed excerpt:

```text
The files contain HDF5 fusion grids/diagnostics, Parquet facility measurements with 2998/3000 valid rows, CSV sensor events with complete basic fields, and a BP5 Gray-Scott container whose profiling is readable but variable metadata needs ADIOS2; next check invalid Parquet rows, cross-file time/site alignment, HDF5 quality flags, and BP5 variables after installing ADIOS2.
```

### 4. NDP catalog discovery

Case: `ndp_catalog_discovery`
Category: external-catalog
Routing mode: `auto`
Status: pass
Selected agent: `data`
Active Agent Blueprint: `-`
Provider/model: `codex` / `gpt-5.5` via `codex://exec`
Provider settings: temperature=1.0, max_tokens=32000, context_length=0, thinking_budget=0
Route graph: orchestrator -> data; data -> ndp_catalog -> data
Route metrics: depth=2, branches=0, tools=4
Expert handoffs: data x2, ndp_catalog x2
Tools: ndp_list_organizations, ndp_search_datasets, ndp_get_dataset_details, ndp_stage_resource
Data/input files: none
Setup turns: 0
Root session messages: 2
Child session logs: 0
Actions: none
Child sessions: none
Artifacts: none
Artifact evidence: none
Elapsed: 27.1s

Prompt:

```text
Find a few NOAA or climate-related datasets in the National Data Platform catalog that might complement this facility data. Summarize what you found and what I should verify before download.
```

What to see: Data expert delegates discovery to NDP tools through the CLIO gateway.

Why this is interesting: Exercises external catalog discovery as a data-stage capability, before analysis consumes staged data.

Observed excerpt:

```text
Found NDP climate candidates including Full Climate Connectivity Network, Climate Refugia baseline, Change in Average Climatic Water Deficit, and Environment Canada Climate Data; before downloading, verify spatial/temporal coverage against the facility locations, usable resource format versus WMS/WCS service endpoints, licensing/provenance, file size, and whether the catalog details identify the concrete ZIP/GeoTIFF/CSV resource rather than only a map service.
```

### 5. Genomics reference and variant review

Case: `genomics_reference_variant_review`
Category: genomics
Routing mode: `auto`
Status: fail
Selected agent: `genomics`
Active Agent Blueprint: `-`
Provider/model: `codex` / `gpt-5.5` via `codex://exec`
Provider settings: temperature=1.0, max_tokens=32000, context_length=0, thinking_budget=0
Route graph: orchestrator -> genomics
Route metrics: depth=1, branches=0, tools=2
Expert handoffs: genomics x3
Tools: genomics_inspect_fasta, genomics_summarize_vcf
Data/input files: /home/jcernuda/clio-agent/tmp/clio-benchmark-data/pathogen_reference.fasta, /home/jcernuda/clio-agent/tmp/clio-benchmark-data/pathogen_sample_variants.vcf
Setup turns: 0
Root session messages: 2
Child session logs: 0
Actions: none
Child sessions: none
Artifacts: none
Artifact evidence: none
Elapsed: 13.1s

Prompt:

```text
Review this synthetic pathogen reference FASTA and variant call file: /home/jcernuda/clio-agent/tmp/clio-benchmark-data/pathogen_reference.fasta and /home/jcernuda/clio-agent/tmp/clio-benchmark-data/pathogen_sample_variants.vcf. Summarize the reference composition, the variant types and effects, and what a collaborator should verify before treating the sample as analysis-ready.
```

What to see: CLIO uses FASTA and VCF genomics tools, then grounds a review in sequence composition and variant effect evidence.

Why this is interesting: Adds a non-NDP, non-HDF5/Parquet domain that requires new domain tools and a new expert boundary.

Observed excerpt:

```text
Reference has two unambiguous contigs totaling 6,050 bp with overall GC 53.8%; the VCF has one sample with six variants spanning SNPs, deletions, and an insertion, including missense, synonymous, frameshift, stop-gained, insertion, and deletion effects, and the collaborator should verify reference/sample provenance, coordinate compatibility, genotype/depth support, the LowQual call, and functional annotations before treating it as analysis-ready.
```

### 6. CSV status distribution chart

Case: `csv_status_visual_summary`
Category: visualization
Routing mode: `auto`
Status: pass
Selected agent: `visualization`
Active Agent Blueprint: `-`
Provider/model: `codex` / `gpt-5.5` via `codex://exec`
Provider settings: temperature=1.0, max_tokens=32000, context_length=0, thinking_budget=0
Route graph: orchestrator -> visualization
Route metrics: depth=1, branches=0, tools=1
Expert handoffs: visualization
Tools: plot_bar_chart
Data/input files: /home/jcernuda/clio-agent/tmp/clio-benchmark-data/sensor_events.csv, /home/jcernuda/clio-agent/tmp/clio-benchmark-data/sensor_events_status_distribution.png
Setup turns: 1
Root session messages: 4
Child session logs: 0
Actions: none
Child sessions: none
Artifacts: /home/jcernuda/clio-agent/tmp/clio-benchmark-data/sensor_events_status_distribution.png
Artifact evidence: /home/jcernuda/clio-agent/tmp/clio-benchmark-data/sensor_events_status_distribution.png (ok, 20259 B)
Elapsed: 17.6s

Prompt:

```text
Create a PNG bar chart of the event status distribution from the CSV stream we just inspected. Tell me where it was saved and what field was plotted.
```

What to see: Visualization resolves the prior CSV context and plots the status field.

Why this is interesting: Exercises a CSV analysis-to-visualization handoff and verifies that charting is not limited to Parquet dashboards.

Observed excerpt:

```text
Saved to /home/jcernuda/clio-agent/tmp/clio-benchmark-data/sensor_events_status_distribution.png; the plotted field was status.
```

### 7. Dirty data dashboard after quality review

Case: `dirty_quality_dashboard_multi_turn`
Category: visualization
Routing mode: `auto`
Status: pass
Selected agent: `visualization`
Active Agent Blueprint: `-`
Provider/model: `codex` / `gpt-5.5` via `codex://exec`
Provider settings: temperature=1.0, max_tokens=32000, context_length=0, thinking_budget=0
Route graph: orchestrator -> visualization
Route metrics: depth=1, branches=0, tools=1
Expert handoffs: visualization
Tools: plot_summary
Data/input files: /home/jcernuda/clio-agent/tmp/clio-benchmark-data/facility_measurements_dirty.parquet
Setup turns: 1
Root session messages: 4
Child session logs: 0
Actions: none
Child sessions: none
Artifacts: /tmp/clio-agent-artifacts/charts/summary_facility_measurements_dirty.png
Artifact evidence: /tmp/clio-agent-artifacts/charts/summary_facility_measurements_dirty.png (ok, 125470 B)
Elapsed: 32.6s

Prompt:

```text
Create a compact dashboard PNG for the dirty Parquet export we just reviewed. Use it to support the quality review, and tell me where the artifact was saved.
```

What to see: Visualization resolves the reviewed dirty Parquet file from memory and creates a real dashboard artifact.

Why this is interesting: Stresses multi-turn analysis-to-visualization over intentionally dirty data, not only clean demo fixtures.

Observed excerpt:

```text
Created the compact quality-review dashboard PNG at /tmp/clio-agent-artifacts/charts/summary_facility_measurements_dirty.png.
```

### 8. Microscopy PNG readiness review

Case: `microscopy_png_readiness_review`
Category: imaging
Routing mode: `auto`
Status: pass
Selected agent: `imaging`
Active Agent Blueprint: `-`
Provider/model: `codex` / `gpt-5.5` via `codex://exec`
Provider settings: temperature=1.0, max_tokens=32000, context_length=0, thinking_budget=0
Route graph: orchestrator -> imaging
Route metrics: depth=1, branches=0, tools=1
Expert handoffs: imaging x2
Tools: imaging_inspect_png
Data/input files: /home/jcernuda/clio-agent/tmp/clio-benchmark-data/microscopy_cells.png
Setup turns: 0
Root session messages: 2
Child session logs: 0
Actions: none
Child sessions: none
Artifacts: /home/jcernuda/clio-agent/tmp/clio-benchmark-data/microscopy_cells.png
Artifact evidence: /home/jcernuda/clio-agent/tmp/clio-benchmark-data/microscopy_cells.png (ok, 614 B)
Elapsed: 14.1s

Prompt:

```text
Review this microscopy-style PNG for collaborator handoff: /home/jcernuda/clio-agent/tmp/clio-benchmark-data/microscopy_cells.png. Summarize the image dimensions, intensity range, foreground estimate, region evidence, and what acquisition metadata should be verified before quantitative analysis.
```

What to see: CLIO uses PNG imaging tools and grounds the review in dimensions, intensity, foreground, and region evidence.

Why this is interesting: Adds a binary scientific image domain with pixel and region semantics, not generic file text or chart-generation behavior.

Observed excerpt:

```text
The PNG is 128x96 grayscale with intensity 4-245, foreground estimate 1497 pixels (12.18%) at threshold 32, bbox [19,21,100,79], 3 connected regions; verify pixel scale, channel/stain meaning, exposure/gain/bit depth, preprocessing, and segmentation threshold before quantitative analysis.
```

### 9. Materials CIF structure review

Case: `materials_cif_structure_review`
Category: materials
Routing mode: `auto`
Status: pass
Selected agent: `materials`
Active Agent Blueprint: `-`
Provider/model: `codex` / `gpt-5.5` via `codex://exec`
Provider settings: temperature=1.0, max_tokens=32000, context_length=0, thinking_budget=0
Route graph: orchestrator -> materials
Route metrics: depth=1, branches=0, tools=1
Expert handoffs: materials x2
Tools: materials_inspect_cif
Data/input files: /home/jcernuda/clio-agent/tmp/clio-benchmark-data/strontium_titanate.cif
Setup turns: 0
Root session messages: 2
Child session logs: 0
Actions: none
Child sessions: none
Artifacts: none
Artifact evidence: none
Elapsed: 13.1s

Prompt:

```text
Review this crystal structure file for collaborator handoff: /home/jcernuda/clio-agent/tmp/clio-benchmark-data/strontium_titanate.cif. Summarize the unit cell, symmetry, atom species, and any density or occupancy checks that should be verified before simulation setup.
```

What to see: CLIO uses CIF materials tools and grounds the review in unit-cell, space-group, species, and atom-site evidence.

Why this is interesting: Adds a non-NDP materials science domain that requires a new file parser, tool, and expert route instead of generic text inspection.

Observed excerpt:

```text
SrTiO3 is cubic P m -3 m with a=b=c=3.905 Å, 90° angles, volume 59.547443 Å^3, full Sr/Ti/O occupancies matching Sr1 Ti1 O3, and approximate density 5.116632 g/cm^3; verify provenance for cell/symmetry, full occupancies, oxidation/charge assumptions, and density convention before simulation setup.
```

### 10. Mass spectrometry mzML QC review

Case: `mass_spec_mzml_qc_review`
Category: mass_spec
Routing mode: `auto`
Status: pass
Selected agent: `mass_spec`
Active Agent Blueprint: `-`
Provider/model: `codex` / `gpt-5.5` via `codex://exec`
Provider settings: temperature=1.0, max_tokens=32000, context_length=0, thinking_budget=0
Route graph: orchestrator -> mass_spec
Route metrics: depth=1, branches=0, tools=1
Expert handoffs: mass_spec x2
Tools: mass_spec_inspect_mzml
Data/input files: /home/jcernuda/clio-agent/tmp/clio-benchmark-data/proteomics_qc.mzML
Setup turns: 0
Root session messages: 2
Child session logs: 0
Actions: none
Child sessions: none
Artifacts: none
Artifact evidence: none
Elapsed: 14.6s

Prompt:

```text
Review this proteomics mzML run for collaborator handoff: /home/jcernuda/clio-agent/tmp/clio-benchmark-data/proteomics_qc.mzML. Summarize the spectra, MS-level balance, m/z coverage, intensity/TIC evidence, and what acquisition metadata should be verified before peptide-search analysis.
```

What to see: CLIO uses mzML mass spectrometry tools and grounds the review in spectra, MS levels, m/z range, peak counts, and TIC evidence.

Why this is interesting: Adds a structured XML scientific instrument domain with spectra and ion-current semantics, not generic XML text inspection.

Observed excerpt:

```text
The mzML run has 4 spectra split evenly between MS1 and MS2, 14 total peaks, observed m/z endpoints from 399.8 to 933.5, TIC total 25140.0 with max 9500.0, and acquisition metadata such as instrument, scan settings, precursor/isolation, fragmentation, calibration, polarity, centroiding, and retention-time units should be verified before peptide search.
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

- `ndp_seismic_waveform_to_plot`: expected CLIO delegates NDP discovery to ndp_catalog, stages a bounded waveform resource, analyzes SAC traces through sac_format, and creates a PNG plot.
  observed agent=data, tools=ndp_list_organizations, ndp_search_datasets, ndp_search_datasets, ndp_search_datasets, ndp_get_dataset_details, ndp_stage_resource, ndp_stage_resource, ndp_stage_resource, ndp_get_dataset_details, ndp_stage_resource, ndp_stage_resource, ndp_get_dataset_details, ndp_stage_resource, ndp_list_organizations, ndp_search_datasets, ndp_search_datasets, ndp_search_datasets, ndp_get_dataset_details, ndp_stage_resource, ndp_stage_resource, ndp_stage_resource, ndp_get_dataset_details, ndp_stage_resource, ndp_stage_resource, ndp_get_dataset_details, ndp_stage_resource, error=None
- `genomics_reference_variant_review`: expected CLIO uses FASTA and VCF genomics tools, then grounds a review in sequence composition and variant effect evidence.
  observed agent=genomics, tools=genomics_inspect_fasta, genomics_summarize_vcf, error=None
