# CLIO Real-Orchestrator Benchmark Report

Generated: 2026-05-28 23:01:00 CDT
Evidence JSONL: `/home/jcernuda/clio-agent/benchmark/FRESH_REAL_ORCHESTRATOR_EVIDENCE.jsonl`
Benchmark lane: `real_orchestrator`

This is a CLIO session-evidence audit. It is produced from real session JSONL rows and should be reviewed as prompt, route, tool, artifact, error, and final-answer evidence. Pytest coverage only guards the harness and tools; it is not the benchmark result.

Result: 12/12 clean passes, 0 expected surfaced errors, 0 expected cancellations, 0 partial recoveries, 0 failures.

Extended stress coverage: has optional gaps outside the per-lane pass/fail gate.

## Extended Stress Coverage Audit

| Criterion | Observed | Required | Status |
| --- | ---: | ---: | --- |
| at least ten complex collaborator-grade demos | 7 | 10 | gap |
| at least five long or high-event stress cases | 3 | 5 | gap |
| at least three cases with tier-3 agents or nanoagents | 4 | 3 | pass |
| at least three visualization artifacts from analyzed data | 4 | 3 | pass |
| at least two deliberate surfaced-error cases | 0 | 2 | gap |
| at least one context-pressure or compaction case | 0 | 1 | gap |
| at least one provider/model-swap stress case | 0 | 1 | gap |

High-event or long-running cases:

- cross_file_dirty_quality_gate_nanoagents (6.0s, 11 events)
- reasoning_cross_file_triage_nanoagents (6.5s, 11 events)
- ndp_seismic_waveform_to_plot (92.8s, 22 events)

## Evidence Summary

- Max elapsed case: `ndp_seismic_waveform_to_plot` (92.8s)
- Max expert depth: `ndp_seismic_waveform_to_plot` (5)
- Max branch fanout: `cross_file_dirty_quality_gate_nanoagents` (4)
- Unique tools used: adios_inspect_file, csv_read_table, genomics_inspect_fasta, genomics_summarize_vcf, geospatial_inspect_geojson, hdf5_analyze_file, hdf5_list_datasets, imaging_inspect_png, mass_spec_inspect_mzml, materials_inspect_cif, ndp_get_dataset_details, ndp_list_organizations, ndp_search_datasets, ndp_stage_resource, parquet_analyze_schema, parquet_compute_statistics, plot_bar_chart, plot_summary, sac_compute_trace_statistics, sac_fetch_earthscope_waveform, sac_inspect_archive, sac_plot_traces
- Data/input files referenced: 12
- Artifacts verified on disk: 4/4

## Provider Lane Audit

| Criterion | Observed | Required | Status |
| --- | ---: | ---: | --- |
| all selected cases avoid shortcut route sources | 12 | 12 | pass |
| passing cases include structured route/tool evidence | 12 | 12 | pass |
| artifact-producing cases verify artifacts on disk | 4 | 4 | pass |
| planner multi-file hierarchy case passes | 1 | 1 | pass |
| dirty cross-file quality gate passes | 1 | 1 | pass |
| NDP waveform benchmark reaches verified SAC/PNG artifact | 1 | 1 | pass |
| NDP full SAC/PNG chain verified | 1 | 1 | pass |

## All Cases

| Case | Category | Mode | Source | Outcome | Agent | Handoffs | Tools | Children | Elapsed |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| csv_status_visual_summary | visualization | auto | dspy | pass | visualization | visualization | plot_bar_chart | 0 | 27.6s |
| cross_file_dirty_quality_gate_nanoagents | multi-agent | auto | dspy | pass | analysis | analysis | hdf5_analyze_file, hdf5_list_datasets, adios_inspect_file, parquet_analyze_schema, parquet_compute_statistics, csv_read_table | 4 | 6.0s |
| reasoning_cross_file_triage_nanoagents | planner-hardening | reasoning_only | dspy | pass | analysis | analysis | hdf5_analyze_file, hdf5_list_datasets, adios_inspect_file, parquet_analyze_schema, parquet_compute_statistics, csv_read_table | 4 | 6.5s |
| reasoning_adios_bp5_container | planner-hardening | reasoning_only | dspy | pass | data | data | adios_inspect_file | 0 | 6.0s |
| dirty_quality_dashboard_multi_turn | visualization | auto | dspy | pass | visualization | visualization | plot_summary | 0 | 28.1s |
| ndp_catalog_discovery | external-catalog | auto | dspy | pass | data | data, ndp_catalog | ndp_list_organizations, ndp_search_datasets, ndp_get_dataset_details, ndp_stage_resource | 0 | 10.0s |
| ndp_seismic_waveform_to_plot | hierarchical-science | auto | dspy | pass | visualization | data, ndp_catalog, analysis, sac_format, visualization | ndp_list_organizations, ndp_search_datasets, ndp_search_datasets, ndp_search_datasets, ndp_get_dataset_details, ndp_stage_resource, ndp_stage_resource, ndp_stage_resource, ndp_get_dataset_details, ndp_stage_resource, ndp_stage_resource, ndp_get_dataset_details, ndp_stage_resource, sac_fetch_earthscope_waveform, sac_inspect_archive, sac_compute_trace_statistics, sac_plot_traces | 0 | 92.8s |
| genomics_reference_variant_review | genomics | auto | dspy | pass | genomics | genomics x3 | genomics_inspect_fasta, genomics_summarize_vcf | 0 | 7.0s |
| materials_cif_structure_review | materials | auto | dspy | pass | materials | materials x2 | materials_inspect_cif | 0 | 7.0s |
| geospatial_field_site_review | geospatial | auto | dspy | pass | geospatial | geospatial x2 | geospatial_inspect_geojson | 0 | 7.5s |
| microscopy_png_readiness_review | imaging | auto | dspy | pass | imaging | imaging x2 | imaging_inspect_png | 0 | 6.0s |
| mass_spec_mzml_qc_review | mass_spec | auto | dspy | pass | mass_spec | mass_spec x2 | mass_spec_inspect_mzml | 0 | 8.0s |

## Best 10 Demo Prompts

### 1. NDP seismic waveform discovery to plot

Case: `ndp_seismic_waveform_to_plot`
Category: hierarchical-science
Routing mode: `auto`
Status: pass
Selected agent: `visualization`
Provider/model: `codex` / `gpt-5.5` via `codex://exec`
Provider settings: temperature=1.0, max_tokens=32000, context_length=0, thinking_budget=0
Route graph: visualization -> data -> ndp_catalog -> analysis -> sac_format -> visualization
Route metrics: depth=5, branches=0, tools=17
Expert handoffs: data, ndp_catalog, analysis, sac_format, visualization
Tools: ndp_list_organizations, ndp_search_datasets, ndp_search_datasets, ndp_search_datasets, ndp_get_dataset_details, ndp_stage_resource, ndp_stage_resource, ndp_stage_resource, ndp_get_dataset_details, ndp_stage_resource, ndp_stage_resource, ndp_get_dataset_details, ndp_stage_resource, sac_fetch_earthscope_waveform, sac_inspect_archive, sac_compute_trace_statistics, sac_plot_traces
Data/input files: /home/jcernuda/clio-agent/tmp/fresh-server-workdir-pass/tmp/clio-seismic-staging/earthscope_IU_ANMO_00_BHZ_2010-02-27T063000.sac
Setup turns: 0
Actions: none
Child sessions: none
Artifacts: /home/jcernuda/clio-agent/tmp/fresh-server-workdir-pass/.clio-agent-artifacts/charts/sac_traces_earthscope_IU_ANMO_00_BHZ_2010-02-27T063000.png
Artifact evidence: /home/jcernuda/clio-agent/tmp/fresh-server-workdir-pass/.clio-agent-artifacts/charts/sac_traces_earthscope_IU_ANMO_00_BHZ_2010-02-27T063000.png (ok, 96135 B)
Elapsed: 92.8s

Prompt:

```text
Find a bounded seismic waveform dataset from a seismological or Earth-science organization in the National Data Platform. Choose a usable resource, stage it if it is small enough, inspect the waveform content, compute representative trace statistics, and produce a plot artifact. If a candidate is too large or unavailable, surface that as the result instead of inventing a plot.
```

What to see: CLIO delegates NDP discovery to ndp_catalog, stages a bounded waveform resource, analyzes SAC traces through sac_format, and creates a PNG plot.

Why this is interesting: This is the core hierarchical science demo: provider discovery, data access, format-specific analysis, and visualization without the user naming internal agents.

Observed excerpt:

```text
data | success | planner_dispatch | Queried the National Data Platform catalog through clio-kit MCP. Organizations matched: 0 Datasets matched: 11 - Data for: Improved Characterization of Ultralow-velocity Zones through Advances in Bayesian Inversion of ScP Waveforms (d8534abe-3c46-440a-ab1e-d594c7b6189f; PDF, TAR, TXT; resources: Pachhai_etal_2023_ScP_data.tar) - Data for: A Compositional Component to the Samoa Ultralow-velocity Zone Revealed through 2- and 3-D Waveform Modeling of SKS and SKKS Differential Travel-...[truncated]
data -> ndp_catalog | success | planner_dispatch_child | Queried the National Data Platform catalog through clio-kit MCP. Organizations matched: 0 Datasets matched: 11 - Data for: Improved Characterization of Ultralow-velocity Zones through Advances in Bayesian Inversion of ScP Waveforms (d8534abe-3c46-440a-ab1e-d594c7b6189f; PDF, TAR, TXT; resources: Pachhai_
```

### 2. Dirty cross-file quality gate

Case: `cross_file_dirty_quality_gate_nanoagents`
Category: multi-agent
Routing mode: `auto`
Status: pass
Selected agent: `analysis`
Provider/model: `codex` / `gpt-5.5` via `codex://exec`
Provider settings: temperature=1.0, max_tokens=32000, context_length=0, thinking_budget=0
Route graph: analysis -> [csv_validator subagent, analysis_validator subagent, adios_validator subagent, data_validator subagent]
Route metrics: depth=1, branches=4, tools=6
Expert handoffs: analysis
Tools: hdf5_analyze_file, hdf5_list_datasets, adios_inspect_file, parquet_analyze_schema, parquet_compute_statistics, csv_read_table
Data/input files: /home/jcernuda/clio-agent/tmp/fresh-benchmark-replay-pass/data/fusion_run.h5, /home/jcernuda/clio-agent/tmp/fresh-benchmark-replay-pass/data/facility_measurements_dirty.parquet, /home/jcernuda/clio-agent/tmp/fresh-benchmark-replay-pass/data/sensor_events.csv
Setup turns: 0
Actions: none
Child sessions: csv_validator subagent, analysis_validator subagent, adios_validator subagent, data_validator subagent
Artifacts: none
Artifact evidence: none
Elapsed: 6.0s

Prompt:

```text
Before I share this run, build a quality gate across /home/jcernuda/clio-agent/tmp/fresh-benchmark-replay-pass/data/fusion_run.h5, /home/jcernuda/clio-agent/tmp/fresh-benchmark-replay-pass/data/facility_measurements_dirty.parquet, /home/jcernuda/clio-agent/tmp/fresh-benchmark-replay-pass/data/sensor_events.csv, and "/home/jcernuda/clio-agent/tmp/fresh-benchmark-replay-pass/data/gray scott noise 0.01 data.bp5". I need to know what each file proves, where the dirty tabular export is risky, and which checks block collaborator handoff.
```

What to see: Analysis coordinates tool-backed child workers over HDF5, dirty Parquet, CSV, and BP5 evidence.

Why this is interesting: Adds a harder cross-file case where one source is intentionally dirty and the user asks for a review gate rather than a generic summary.

Observed excerpt:

```text
analysis | success | planner_dispatch | Parallel validation completed with tool-backed nanoagents. data_validator: Inspected HDF5 file /home/jcernuda/clio-agent/tmp/fresh-benchmark-replay-pass/data/fusion_run.h5. It contains 7 datasets and 4 groups. - axes/diagnostic_channel: shape=[12], dtype=int32, size=48 B - axes/radius_norm: shape=[64], dtype=float64, size=512 B - axes/time_hours: shape=[96], dtype=float64, size=768 B - diagnostics/heat_flux: shape=[96, 12], dtype=float32, size=4.5 KiB, units=MW/m^2 - plasma/densit...[truncated]
Parallel validation completed with tool-backed nanoagents.

data_validator:
Inspected HDF5 file /home/jcernuda/clio-agent/tmp/fresh-benchmark-replay-pass/data/fusion_run.h5. It contains 7 datasets and 4 groups.
- axes/diagnostic_channel: shape=[12], dtype=int32, size=48 B
- axes/radius_norm: shape=[64], dtype=float64, size=512 B
- axes/time_hours: shape=[9
```

### 3. No-guard cross-file triage

Case: `reasoning_cross_file_triage_nanoagents`
Category: planner-hardening
Routing mode: `reasoning_only`
Status: pass
Selected agent: `analysis`
Provider/model: `codex` / `gpt-5.5` via `codex://exec`
Provider settings: temperature=1.0, max_tokens=32000, context_length=0, thinking_budget=0
Route graph: analysis -> [csv_validator subagent, analysis_validator subagent, adios_validator subagent, data_validator subagent]
Route metrics: depth=1, branches=4, tools=6
Expert handoffs: analysis
Tools: hdf5_analyze_file, hdf5_list_datasets, adios_inspect_file, parquet_analyze_schema, parquet_compute_statistics, csv_read_table
Data/input files: /home/jcernuda/clio-agent/tmp/fresh-benchmark-replay-pass/data/fusion_run.h5, /home/jcernuda/clio-agent/tmp/fresh-benchmark-replay-pass/data/facility_measurements.parquet, /home/jcernuda/clio-agent/tmp/fresh-benchmark-replay-pass/data/sensor_events.csv
Setup turns: 0
Actions: none
Child sessions: csv_validator subagent, analysis_validator subagent, adios_validator subagent, data_validator subagent
Artifacts: none
Artifact evidence: none
Elapsed: 6.5s

Prompt:

```text
I have four related files from the same experiment: /home/jcernuda/clio-agent/tmp/fresh-benchmark-replay-pass/data/fusion_run.h5, /home/jcernuda/clio-agent/tmp/fresh-benchmark-replay-pass/data/facility_measurements.parquet, /home/jcernuda/clio-agent/tmp/fresh-benchmark-replay-pass/data/sensor_events.csv, and "/home/jcernuda/clio-agent/tmp/fresh-benchmark-replay-pass/data/gray scott noise 0.01 data.bp5". Give me a cross-file triage summary: what is in each file, whether the measurements look ready for downstream analysis, and what I should check next.
```

What to see: With routing guards disabled for the session, the planner still reaches analysis and tool-backed child workers.

Why this is interesting: Separates planner capability from the production registry guard, which matters as CLIO grows beyond a few built-in experts.

Observed excerpt:

```text
analysis | success | planner_dispatch | Parallel validation completed with tool-backed nanoagents. data_validator: Inspected HDF5 file /home/jcernuda/clio-agent/tmp/fresh-benchmark-replay-pass/data/fusion_run.h5. It contains 7 datasets and 4 groups. - axes/diagnostic_channel: shape=[12], dtype=int32, size=48 B - axes/radius_norm: shape=[64], dtype=float64, size=512 B - axes/time_hours: shape=[96], dtype=float64, size=768 B - diagnostics/heat_flux: shape=[96, 12], dtype=float32, size=4.5 KiB, units=MW/m^2 - plasma/densit...[truncated]
Parallel validation completed with tool-backed nanoagents.

data_validator:
Inspected HDF5 file /home/jcernuda/clio-agent/tmp/fresh-benchmark-replay-pass/data/fusion_run.h5. It contains 7 datasets and 4 groups.
- axes/diagnostic_channel: shape=[12], dtype=int32, size=48 B
- axes/radius_norm: shape=[64], dtype=float64, size=512 B
- axes/time_hours: shape=[9
```

### 4. NDP catalog discovery

Case: `ndp_catalog_discovery`
Category: external-catalog
Routing mode: `auto`
Status: pass
Selected agent: `data`
Provider/model: `codex` / `gpt-5.5` via `codex://exec`
Provider settings: temperature=1.0, max_tokens=32000, context_length=0, thinking_budget=0
Route graph: data -> ndp_catalog
Route metrics: depth=2, branches=0, tools=4
Expert handoffs: data, ndp_catalog
Tools: ndp_list_organizations, ndp_search_datasets, ndp_get_dataset_details, ndp_stage_resource
Data/input files: none
Setup turns: 0
Actions: none
Child sessions: none
Artifacts: none
Artifact evidence: none
Elapsed: 10.0s

Prompt:

```text
Find a few NOAA or climate-related datasets in the National Data Platform catalog that might complement this facility data. Summarize what you found and what I should verify before download.
```

What to see: Data expert delegates discovery to NDP tools through the CLIO gateway.

Why this is interesting: Exercises external catalog discovery as a data-stage capability, before analysis consumes staged data.

Observed excerpt:

```text
data | success | planner_dispatch | Queried the National Data Platform catalog through clio-kit MCP. Organizations matched: 3 - {'id': 'fa8088e2-bbc2-4501-8979-27c65052bd9b', 'name': 'noaa-global-systems-laboratory', 'title': 'NOAA Global Systems Laboratory', 'package_count': 1} - {'id': 'fd6c2c9f-94cf-4337-b647-5a26d07ea5d9', 'name': 'noaa-national-centers-for-environmental-information-ncei', 'title': 'NOAA National Centers for Environmental Information (NCEI)', 'package_count': 1} - {'id': '5e32bd49-b527-446f-b9b2...[truncated]
data -> ndp_catalog | success | planner_dispatch_child | Queried the National Data Platform catalog through clio-kit MCP. Organizations matched: 3 - {'id': 'fa8088e2-bbc2-4501-8979-27c65052bd9b', 'name': 'noaa-global-systems-laboratory', 'title': 'NOAA Global Systems Laboratory', 'package_count': 1} - {'id': 'fd6c2c9f-94cf-4337-b647-5a26d07ea5d9', 'name': 'noa
```

### 5. Genomics reference and variant review

Case: `genomics_reference_variant_review`
Category: genomics
Routing mode: `auto`
Status: pass
Selected agent: `genomics`
Provider/model: `codex` / `gpt-5.5` via `codex://exec`
Provider settings: temperature=1.0, max_tokens=32000, context_length=0, thinking_budget=0
Route graph: genomics
Route metrics: depth=1, branches=0, tools=2
Expert handoffs: genomics x3
Tools: genomics_inspect_fasta, genomics_summarize_vcf
Data/input files: /home/jcernuda/clio-agent/tmp/fresh-benchmark-replay-pass/data/pathogen_reference.fasta, /home/jcernuda/clio-agent/tmp/fresh-benchmark-replay-pass/data/pathogen_sample_variants.vcf
Setup turns: 0
Actions: none
Child sessions: none
Artifacts: none
Artifact evidence: none
Elapsed: 7.0s

Prompt:

```text
Review this synthetic pathogen reference FASTA and variant call file: /home/jcernuda/clio-agent/tmp/fresh-benchmark-replay-pass/data/pathogen_reference.fasta and /home/jcernuda/clio-agent/tmp/fresh-benchmark-replay-pass/data/pathogen_sample_variants.vcf. Summarize the reference composition, the variant types and effects, and what a collaborator should verify before treating the sample as analysis-ready.
```

What to see: CLIO uses FASTA and VCF genomics tools, then grounds a review in sequence composition and variant effect evidence.

Why this is interesting: Adds a non-NDP, non-HDF5/Parquet domain that requires new domain tools and a new expert boundary.

Observed excerpt:

```text
genomics | success | direct_tool
genomics | success | direct_tool
genomics | success | planner_dispatch | Genomics review: FASTA: {"filepath": "/home/jcernuda/clio-agent/tmp/fresh-benchmark-replay-pass/data/pathogen_reference.fasta", "record_count": 2, "total_bases": 6050, "gc_fraction": 0.537851, "base_counts": {"A": 1406, "C": 1673, "G": 1581, "T": 1390, "N": 0}, "longest_record": {"id": "chrA", "length": 4800, "gc_fraction": 0.585833}, "records": {"count": 2, "items": [{"id": "chrA", "description": "synthetic pathogen benchmark reference", "length": 4800, "gc_fraction": 0.585833, "...[truncated]
Genomics review:
FASTA: {"filepath": "/home/jcernuda/clio-agent/tmp/fresh-benchmark-replay-pass/data/pathogen_reference.fasta", "record_count": 2, "total_bases": 6050, "gc_fraction": 0.537851, "base_counts": {"A": 1406, "C": 1673, "G": 1581, "T": 1390, "N": 0}, "longest_record": {"id": "chrA
```

### 6. CSV status distribution chart

Case: `csv_status_visual_summary`
Category: visualization
Routing mode: `auto`
Status: pass
Selected agent: `visualization`
Provider/model: `codex` / `gpt-5.5` via `codex://exec`
Provider settings: temperature=1.0, max_tokens=32000, context_length=0, thinking_budget=0
Route graph: visualization
Route metrics: depth=1, branches=0, tools=1
Expert handoffs: visualization
Tools: plot_bar_chart
Data/input files: /home/jcernuda/clio-agent/tmp/fresh-benchmark-replay-pass/data/sensor_events.csv, /home/jcernuda/clio-agent/tmp/fresh-benchmark-replay-pass/data/event_status_distribution.png
Setup turns: 1
Actions: none
Child sessions: none
Artifacts: /home/jcernuda/clio-agent/tmp/fresh-benchmark-replay-pass/data/event_status_distribution.png
Artifact evidence: /home/jcernuda/clio-agent/tmp/fresh-benchmark-replay-pass/data/event_status_distribution.png (ok, 20259 B)
Elapsed: 27.6s

Prompt:

```text
Create a PNG bar chart of the event status distribution from the CSV stream we just inspected. Tell me where it was saved and what field was plotted.
```

What to see: Visualization resolves the prior CSV context and plots the status field.

Why this is interesting: Exercises a CSV analysis-to-visualization handoff and verifies that charting is not limited to Parquet dashboards.

Observed excerpt:

```text
visualization | success | direct_tool
Saved the PNG bar chart to /home/jcernuda/clio-agent/tmp/fresh-benchmark-replay-pass/data/event_status_distribution.png, plotting value counts for the status field.
/home/jcernuda/clio-agent/tmp/fresh-benchmark-replay-pass/data/event_status_distribution.png
```

### 7. Dirty data dashboard after quality review

Case: `dirty_quality_dashboard_multi_turn`
Category: visualization
Routing mode: `auto`
Status: pass
Selected agent: `visualization`
Provider/model: `codex` / `gpt-5.5` via `codex://exec`
Provider settings: temperature=1.0, max_tokens=32000, context_length=0, thinking_budget=0
Route graph: visualization
Route metrics: depth=1, branches=0, tools=1
Expert handoffs: visualization
Tools: plot_summary
Data/input files: /home/jcernuda/clio-agent/tmp/fresh-benchmark-replay-pass/data/facility_measurements_dirty.parquet
Setup turns: 1
Actions: none
Child sessions: none
Artifacts: /home/jcernuda/clio-agent/tmp/fresh-artifacts-pass/charts/summary_facility_measurements_dirty.png
Artifact evidence: /home/jcernuda/clio-agent/tmp/fresh-artifacts-pass/charts/summary_facility_measurements_dirty.png (ok, 125470 B)
Elapsed: 28.1s

Prompt:

```text
Create a compact dashboard PNG for the dirty Parquet export we just reviewed. Use it to support the quality review, and tell me where the artifact was saved.
```

What to see: Visualization resolves the reviewed dirty Parquet file from memory and creates a real dashboard artifact.

Why this is interesting: Stresses multi-turn analysis-to-visualization over intentionally dirty data, not only clean demo fixtures.

Observed excerpt:

```text
visualization | success | planner_dispatch | Generated a compact dataset summary dashboard for `facility_measurements_dirty.parquet`. The dashboard supports quality review by showing column type composition, missing-value counts, numeric distributions for measurement fields, and correlations among numeric columns in a single overview.
Visualization: Generated a compact dataset summary dashboard for `facility_measurements_dirty.parquet`. The dashboard supports quality review by showing column type composition, missing-value counts, numeric distributions for measurement fields, and correlations among numeric columns in a single overview.

File: /home/jcernuda/clio-agent/tmp/fresh-artifacts-pass/charts/summary_facility_measurements_dirty.png
/home/jcernuda/clio-agent/tmp/fresh-artifacts-pass/charts/summary_facility_measurements_dirty.png
```

### 8. Microscopy PNG readiness review

Case: `microscopy_png_readiness_review`
Category: imaging
Routing mode: `auto`
Status: pass
Selected agent: `imaging`
Provider/model: `codex` / `gpt-5.5` via `codex://exec`
Provider settings: temperature=1.0, max_tokens=32000, context_length=0, thinking_budget=0
Route graph: imaging
Route metrics: depth=1, branches=0, tools=1
Expert handoffs: imaging x2
Tools: imaging_inspect_png
Data/input files: /home/jcernuda/clio-agent/tmp/fresh-benchmark-replay-pass/data/microscopy_cells.png
Setup turns: 0
Actions: none
Child sessions: none
Artifacts: /home/jcernuda/clio-agent/tmp/fresh-benchmark-replay-pass/data/microscopy_cells.png
Artifact evidence: /home/jcernuda/clio-agent/tmp/fresh-benchmark-replay-pass/data/microscopy_cells.png (ok, 614 B)
Elapsed: 6.0s

Prompt:

```text
Review this microscopy-style PNG for collaborator handoff: /home/jcernuda/clio-agent/tmp/fresh-benchmark-replay-pass/data/microscopy_cells.png. Summarize the image dimensions, intensity range, foreground estimate, region evidence, and what acquisition metadata should be verified before quantitative analysis.
```

What to see: CLIO uses PNG imaging tools and grounds the review in dimensions, intensity, foreground, and region evidence.

Why this is interesting: Adds a binary scientific image domain with pixel and region semantics, not generic file text or chart-generation behavior.

Observed excerpt:

```text
imaging | success | direct_tool
imaging | success | planner_dispatch | Scientific image review: PNG: {"filepath": "/home/jcernuda/clio-agent/tmp/fresh-benchmark-replay-pass/data/microscopy_cells.png", "format": "PNG", "mode": "L", "width": 128, "height": 96, "channels": 1, "threshold": 32, "intensity": {"min": 4, "max": 245, "mean": 32.568, "std": 62.502}, "foreground_pixels": 1497, "foreground_fraction": 0.121826, "foreground_bbox": {"count": 4, "items": [19, 21, 100, 79]}, "connected_regions": 3, "ok": true} Recommendations: - Verify acquisition sc...[truncated]
Scientific image review:
PNG: {"filepath": "/home/jcernuda/clio-agent/tmp/fresh-benchmark-replay-pass/data/microscopy_cells.png", "format": "PNG", "mode": "L", "width": 128, "height": 96, "channels": 1, "threshold": 32, "intensity": {"min": 4, "max": 245, "mean": 32.568, "std": 62.502}, "foreground_pixels": 1497, "foreground_
```

### 9. Materials CIF structure review

Case: `materials_cif_structure_review`
Category: materials
Routing mode: `auto`
Status: pass
Selected agent: `materials`
Provider/model: `codex` / `gpt-5.5` via `codex://exec`
Provider settings: temperature=1.0, max_tokens=32000, context_length=0, thinking_budget=0
Route graph: materials
Route metrics: depth=1, branches=0, tools=1
Expert handoffs: materials x2
Tools: materials_inspect_cif
Data/input files: /home/jcernuda/clio-agent/tmp/fresh-benchmark-replay-pass/data/strontium_titanate.cif
Setup turns: 0
Actions: none
Child sessions: none
Artifacts: none
Artifact evidence: none
Elapsed: 7.0s

Prompt:

```text
Review this crystal structure file for collaborator handoff: /home/jcernuda/clio-agent/tmp/fresh-benchmark-replay-pass/data/strontium_titanate.cif. Summarize the unit cell, symmetry, atom species, and any density or occupancy checks that should be verified before simulation setup.
```

What to see: CLIO uses CIF materials tools and grounds the review in unit-cell, space-group, species, and atom-site evidence.

Why this is interesting: Adds a non-NDP materials science domain that requires a new file parser, tool, and expert route instead of generic text inspection.

Observed excerpt:

```text
materials | success | direct_tool
materials | success | planner_dispatch | Materials structure review: CIF: {"filepath": "/home/jcernuda/clio-agent/tmp/fresh-benchmark-replay-pass/data/strontium_titanate.cif", "data_block": "data_SrTiO3_benchmark", "formula_sum": "Sr1 Ti1 O3", "formula_structural": "SrTiO3", "space_group": "P m -3 m", "cell": {"a": 3.905, "b": 3.905, "c": 3.905, "alpha": 90.0, "beta": 90.0, "gamma": 90.0}, "cell_volume_angstrom3": 59.547443, "atom_site_count": 5, "species_counts": {"Sr": 1, "Ti": 1, "O": 3}, "occupancy_weighted_species_c...[truncated]
Materials structure review:
CIF: {"filepath": "/home/jcernuda/clio-agent/tmp/fresh-benchmark-replay-pass/data/strontium_titanate.cif", "data_block": "data_SrTiO3_benchmark", "formula_sum": "Sr1 Ti1 O3", "formula_structural": "SrTiO3", "space_group": "P m -3 m", "cell": {"a": 3.905, "b": 3.905, "c": 3.905, "alpha": 90.0, "
```

### 10. Mass spectrometry mzML QC review

Case: `mass_spec_mzml_qc_review`
Category: mass_spec
Routing mode: `auto`
Status: pass
Selected agent: `mass_spec`
Provider/model: `codex` / `gpt-5.5` via `codex://exec`
Provider settings: temperature=1.0, max_tokens=32000, context_length=0, thinking_budget=0
Route graph: mass_spec
Route metrics: depth=1, branches=0, tools=1
Expert handoffs: mass_spec x2
Tools: mass_spec_inspect_mzml
Data/input files: /home/jcernuda/clio-agent/tmp/fresh-benchmark-replay-pass/data/proteomics_qc.mzML
Setup turns: 0
Actions: none
Child sessions: none
Artifacts: none
Artifact evidence: none
Elapsed: 8.0s

Prompt:

```text
Review this proteomics mzML run for collaborator handoff: /home/jcernuda/clio-agent/tmp/fresh-benchmark-replay-pass/data/proteomics_qc.mzML. Summarize the spectra, MS-level balance, m/z coverage, intensity/TIC evidence, and what acquisition metadata should be verified before peptide-search analysis.
```

What to see: CLIO uses mzML mass spectrometry tools and grounds the review in spectra, MS levels, m/z range, peak counts, and TIC evidence.

Why this is interesting: Adds a structured XML scientific instrument domain with spectra and ion-current semantics, not generic XML text inspection.

Observed excerpt:

```text
mass_spec | success | direct_tool
mass_spec | success | planner_dispatch | Mass spectrometry data review: mzML: {"filepath": "/home/jcernuda/clio-agent/tmp/fresh-benchmark-replay-pass/data/proteomics_qc.mzML", "format": "mzML", "spectrum_count": 4, "ms_levels": {"1": 2, "2": 2}, "total_peak_count": 14, "mz_range": {"count": 2, "items": [399.8, 933.5]}, "tic_total": 25140.0, "tic_max": 9500.0, "total_ion_current_total": 25140.0, "total_ion_current_max": 9500.0, "representative_spectra": {"count": 4, "items": [{"id": "scan=1", "ms_level": "1", "scan_start_...[truncated]
Mass spectrometry data review:
mzML: {"filepath": "/home/jcernuda/clio-agent/tmp/fresh-benchmark-replay-pass/data/proteomics_qc.mzML", "format": "mzML", "spectrum_count": 4, "ms_levels": {"1": 2, "2": 2}, "total_peak_count": 14, "mz_range": {"count": 2, "items": [399.8, 933.5]}, "tic_total": 25140.0, "tic_max": 9500.0, "t
```

## Failures Fixed During This Campaign

- GACT compaction originally bypassed transient-provider retry and only updated the GACT transcript; compaction now retries provider throttles, updates ARC memory, and fails with structured errors if memory storage fails.
- Compact summaries could lose exact scientific identifiers at the ARC truncation boundary; compact memory now preserves a labeled exact evidence index for paths, variables, columns, artifacts, and caveats.
- Retained multi-file context could make analysis narrow to the first file or let CSV follow-ups be stolen by broad synthesis; explicit file paths now take precedence and retained multi-source synthesis is limited to true synthesis questions.
- Visualization-intent follow-ups could route to analysis or a data tool even when the user asked for a chart/dashboard; file-grounded visual artifact requests are promoted to the visualization expert.
- Direct planner-selected NDP and Parquet/statistical tool actions could flatten expert ownership; NDP catalog work is promoted to the nested `ndp_catalog` expert, and statistical Parquet triage is promoted to `analysis`.
- Provider throttles during expert dispatch, handoffs, and compaction could surface as brittle partial recoveries; expert paths now use bounded transient-provider retry and still surface structured errors if exhausted.

## Remaining Caveats

- This report is evidence for the recorded provider/session run, not a guarantee that provider availability, model latency, token freshness, or external data services will be identical later.
- Several high-event cases are intentionally fast because child/nanoagent workers use deterministic local tools after routing; elapsed time alone should not be treated as benchmark depth.
- The benchmark now covers the hierarchy and handoff classes listed here, but future providers, file formats, and per-expert model assignments still need their own evidence runs.
