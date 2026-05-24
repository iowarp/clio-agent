# CLIO ALCF Demo Benchmark Report

Generated: 2026-05-24 03:22:15 Central Daylight Time
Evidence JSONL: `D:\Libraries\Documents\projects\clio-agent\tmp\clio-demo-benchmark-alcf-metis-20260524-stress-final4.jsonl`

Result: 19/21 clean passes, 2 expected surfaced errors, 0 partial recoveries, 0 failures.

Stress coverage: meets the documented benchmark standard.

## Stress Coverage Audit

| Criterion | Observed | Required | Status |
| --- | ---: | ---: | --- |
| at least ten complex collaborator-grade demos | 10 | 10 | pass |
| at least five long or high-event stress cases | 6 | 5 | pass |
| at least three cases with tier-3 agents or nanoagents | 5 | 3 | pass |
| at least three visualization artifacts from analyzed data | 5 | 3 | pass |
| at least two deliberate surfaced-error cases | 2 | 2 | pass |
| at least one context-pressure or compaction case | 1 | 1 | pass |
| at least one provider/model-swap stress case | 1 | 1 | pass |

High-event or long-running cases:

- workflow_hdf5_overview (21.6s, 16 events)
- cross_file_triage_nanoagents (0.5s, 11 events)
- cross_file_dirty_quality_gate_nanoagents (0.5s, 11 events)
- reasoning_cross_file_triage_nanoagents (1.1s, 11 events)
- ndp_seismic_waveform_to_plot (24.2s, 13 events)
- provider_swap_memory_followup (8.1s, 10 events)

## All Cases

| Case | Category | Mode | Source | Outcome | Agent | Handoffs | Tools | Children | Elapsed |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| workflow_hdf5_overview | tooling | auto | dspy | pass | data | data x8 | hdf5_analyze_file, hdf5_analyze_dataset, hdf5_analyze_dataset, hdf5_analyze_dataset, hdf5_analyze_dataset, hdf5_analyze_dataset, hdf5_analyze_dataset, hdf5_analyze_dataset | 0 | 21.6s |
| workflow_parquet_profile | analysis | auto | dspy | pass | analysis | analysis | parquet_analyze_schema, parquet_compute_statistics, parquet_compute_statistics, parquet_compute_statistics, parquet_compute_statistics | 0 | 1.0s |
| workflow_memory_followup | memory | auto | dspy | pass | analysis | analysis | parquet_analyze_schema, parquet_compute_statistics, parquet_compute_statistics, parquet_compute_statistics, parquet_compute_statistics | 0 | 1.5s |
| context_pressure_compaction_followup | memory-hardening | auto | dspy | pass | analysis | analysis | - | 0 | 5.1s |
| workflow_csv_event_schema | analysis | auto | dspy | pass | analysis | analysis | csv_read_table | 0 | 3.6s |
| workflow_visual_dashboard | visualization | auto | dspy | pass | visualization | visualization | plot_summary | 0 | 3.6s |
| csv_status_visual_summary | visualization | auto | dspy | pass | visualization | visualization | plot_bar_chart | 0 | 4.6s |
| hdf5_dataset_focus | tooling | auto | dspy | pass | data | data | hdf5_analyze_dataset | 0 | 2.7s |
| cross_file_triage_nanoagents | multi-agent | auto | guard | pass | analysis | analysis | hdf5_analyze_file, hdf5_list_datasets, adios_inspect_file, parquet_analyze_schema, parquet_compute_statistics, csv_read_table | 4 | 0.5s |
| cross_file_dirty_quality_gate_nanoagents | multi-agent | auto | guard | pass | analysis | analysis | hdf5_analyze_file, hdf5_list_datasets, adios_inspect_file, parquet_analyze_schema, parquet_compute_statistics, csv_read_table | 4 | 0.5s |
| reasoning_cross_file_triage_nanoagents | planner-hardening | reasoning_only | dspy | pass | analysis | analysis | hdf5_analyze_file, hdf5_list_datasets, adios_inspect_file, parquet_analyze_schema, parquet_compute_statistics, csv_read_table | 4 | 1.1s |
| adios_bp5_container | tooling | auto | guard | pass | data | data | adios_inspect_file | 0 | 0.5s |
| reasoning_adios_bp5_container | planner-hardening | reasoning_only | dspy | pass | data | data | adios_inspect_file | 0 | 2.0s |
| dirty_parquet_quality | analysis | auto | dspy | pass | analysis | analysis | parquet_analyze_schema, parquet_compute_statistics, parquet_compute_statistics | 0 | 1.0s |
| dirty_quality_dashboard_multi_turn | visualization | auto | dspy | pass | visualization | visualization | plot_summary | 0 | 2.5s |
| ndp_catalog_discovery | external-catalog | auto | dspy | pass | ndp_catalog | ndp_catalog | ndp_list_organizations, ndp_search_datasets, ndp_get_dataset_details, ndp_stage_resource | 0 | 9.1s |
| ndp_seismic_waveform_to_plot | hierarchical-science | auto | dspy | pass | visualization | ndp_catalog, analysis, sac_format, visualization | ndp_list_organizations, ndp_search_datasets, ndp_search_datasets, ndp_search_datasets, ndp_get_dataset_details, ndp_stage_resource, sac_inspect_archive, sac_compute_trace_statistics, sac_plot_traces | 0 | 24.2s |
| visual_scatter_artifact | visualization | auto | dspy | pass | visualization | visualization | plot_scatter | 0 | 2.0s |
| missing_hdf5_error | hardening | auto | dspy | expected_error | data | data | hdf5_list_datasets | 0 | 2.0s |
| missing_csv_error | hardening | auto | dspy | expected_error | analysis | analysis | csv_read_table | 0 | 2.0s |
| provider_swap_memory_followup | provider-hardening | auto | dspy | pass | analysis | analysis x3 | parquet_compute_statistics, parquet_compute_statistics, parquet_analyze_schema, parquet_compute_statistics, parquet_compute_statistics, parquet_compute_statistics, parquet_compute_statistics | 0 | 8.1s |

## Best 10 Demo Prompts

### 1. Cross-file triage with tier-3 workers

Case: `cross_file_triage_nanoagents`
Category: multi-agent
Routing mode: `auto`
Status: pass
Selected agent: `analysis`
Provider/model: `argonne` / `gpt-oss-120b` via `https://inference-api.alcf.anl.gov/resource_server/metis/api/v1`
Provider settings: temperature=0.0, max_tokens=4096, context_length=0, thinking_budget=0
Route graph: analysis -> [csv_validator subagent, analysis_validator subagent, adios_validator subagent, data_validator subagent]
Expert handoffs: analysis
Tools: hdf5_analyze_file, hdf5_list_datasets, adios_inspect_file, parquet_analyze_schema, parquet_compute_statistics, csv_read_table
Setup turns: 0
Actions: none
Child sessions: csv_validator subagent, analysis_validator subagent, adios_validator subagent, data_validator subagent
Artifacts: none
Elapsed: 0.5s

Prompt:

```text
I have four related files from the same experiment: D:\Libraries\Documents\projects\clio-agent\tmp\clio-benchmark-data\fusion_run.h5, D:\Libraries\Documents\projects\clio-agent\tmp\clio-benchmark-data\facility_measurements.parquet, D:\Libraries\Documents\projects\clio-agent\tmp\clio-benchmark-data\sensor_events.csv, and "D:\Libraries\Documents\projects\clio-agent\tmp\clio-benchmark-data\gray scott noise 0.01 data.bp5". Give me a cross-file triage summary: what is in each file, whether the measurements look ready for downstream analysis, and what I should check next.
```

What to see: Analysis coordinates tool-backed child workers and aggregates their findings.

Why this is interesting: Best stress case for hierarchical routing and child-session evidence.

Observed excerpt:

```text
Parallel validation completed with tool-backed nanoagents.

data_validator:
Inspected HDF5 file D:\Libraries\Documents\projects\clio-agent\tmp\clio-benchmark-data\fusion_run.h5. It contains 7 datasets and 4 groups.
- axes/diagnostic_channel: shape=[12], dtype=int32, size=48 B
- axes/radius_norm: shape=[64], dtype=float64, size=512 B
- axes/time_hours: shape=[96], dtype=float64, size=768 B
- diagnostics/heat_flux: shape=[96, 12], dtype=float32, size=4.5 KiB, units=MW/m^2
- plasma/density: shape=[96, 64], dtype=float64, size=48.0 KiB, units=m^-3
- plasma/electron_temperature: shape=[96, 64], dtype=float32, size=24.0 KiB, units=eV
- quality/flags: shape=[96, 12], dtype=int8, size=1.1 KiB

Compression summary: 5 compressed, 2 uncompressed. Overall raw-to-stored ratio is about 1.12x.

Compression is partially configured. Review uncompressed numeric datasets and consider chunked gzip or lzf
```

### 2. Dirty cross-file quality gate

Case: `cross_file_dirty_quality_gate_nanoagents`
Category: multi-agent
Routing mode: `auto`
Status: pass
Selected agent: `analysis`
Provider/model: `argonne` / `gpt-oss-120b` via `https://inference-api.alcf.anl.gov/resource_server/metis/api/v1`
Provider settings: temperature=0.0, max_tokens=4096, context_length=0, thinking_budget=0
Route graph: analysis -> [csv_validator subagent, analysis_validator subagent, adios_validator subagent, data_validator subagent]
Expert handoffs: analysis
Tools: hdf5_analyze_file, hdf5_list_datasets, adios_inspect_file, parquet_analyze_schema, parquet_compute_statistics, csv_read_table
Setup turns: 0
Actions: none
Child sessions: csv_validator subagent, analysis_validator subagent, adios_validator subagent, data_validator subagent
Artifacts: none
Elapsed: 0.5s

Prompt:

```text
Before I share this run, build a quality gate across D:\Libraries\Documents\projects\clio-agent\tmp\clio-benchmark-data\fusion_run.h5, D:\Libraries\Documents\projects\clio-agent\tmp\clio-benchmark-data\facility_measurements_dirty.parquet, D:\Libraries\Documents\projects\clio-agent\tmp\clio-benchmark-data\sensor_events.csv, and "D:\Libraries\Documents\projects\clio-agent\tmp\clio-benchmark-data\gray scott noise 0.01 data.bp5". I need to know what each file proves, where the dirty tabular export is risky, and which checks block collaborator handoff.
```

What to see: Analysis coordinates tool-backed child workers over HDF5, dirty Parquet, CSV, and BP5 evidence.

Why this is interesting: Adds a harder cross-file case where one source is intentionally dirty and the user asks for a review gate rather than a generic summary.

Observed excerpt:

```text
Parallel validation completed with tool-backed nanoagents.

data_validator:
Inspected HDF5 file D:\Libraries\Documents\projects\clio-agent\tmp\clio-benchmark-data\fusion_run.h5. It contains 7 datasets and 4 groups.
- axes/diagnostic_channel: shape=[12], dtype=int32, size=48 B
- axes/radius_norm: shape=[64], dtype=float64, size=512 B
- axes/time_hours: shape=[96], dtype=float64, size=768 B
- diagnostics/heat_flux: shape=[96, 12], dtype=float32, size=4.5 KiB, units=MW/m^2
- plasma/density: shape=[96, 64], dtype=float64, size=48.0 KiB, units=m^-3
- plasma/electron_temperature: shape=[96, 64], dtype=float32, size=24.0 KiB, units=eV
- quality/flags: shape=[96, 12], dtype=int8, size=1.1 KiB

Compression summary: 5 compressed, 2 uncompressed. Overall raw-to-stored ratio is about 1.12x.

Compression is partially configured. Review uncompressed numeric datasets and consider chunked gzip or lzf
```

### 3. No-guard cross-file triage

Case: `reasoning_cross_file_triage_nanoagents`
Category: planner-hardening
Routing mode: `reasoning_only`
Status: pass
Selected agent: `analysis`
Provider/model: `argonne` / `gpt-oss-120b` via `https://inference-api.alcf.anl.gov/resource_server/metis/api/v1`
Provider settings: temperature=0.0, max_tokens=4096, context_length=0, thinking_budget=0
Route graph: analysis -> [csv_validator subagent, analysis_validator subagent, adios_validator subagent, data_validator subagent]
Expert handoffs: analysis
Tools: hdf5_analyze_file, hdf5_list_datasets, adios_inspect_file, parquet_analyze_schema, parquet_compute_statistics, csv_read_table
Setup turns: 0
Actions: none
Child sessions: csv_validator subagent, analysis_validator subagent, adios_validator subagent, data_validator subagent
Artifacts: none
Elapsed: 1.1s

Prompt:

```text
I have four related files from the same experiment: D:\Libraries\Documents\projects\clio-agent\tmp\clio-benchmark-data\fusion_run.h5, D:\Libraries\Documents\projects\clio-agent\tmp\clio-benchmark-data\facility_measurements.parquet, D:\Libraries\Documents\projects\clio-agent\tmp\clio-benchmark-data\sensor_events.csv, and "D:\Libraries\Documents\projects\clio-agent\tmp\clio-benchmark-data\gray scott noise 0.01 data.bp5". Give me a cross-file triage summary: what is in each file, whether the measurements look ready for downstream analysis, and what I should check next.
```

What to see: With routing guards disabled for the session, the planner still reaches analysis and tool-backed child workers.

Why this is interesting: Separates planner capability from the production registry guard, which matters as CLIO grows beyond a few built-in experts.

Observed excerpt:

```text
Parallel validation completed with tool-backed nanoagents.

data_validator:
Inspected HDF5 file D:\Libraries\Documents\projects\clio-agent\tmp\clio-benchmark-data\fusion_run.h5. It contains 7 datasets and 4 groups.
- axes/diagnostic_channel: shape=[12], dtype=int32, size=48 B
- axes/radius_norm: shape=[64], dtype=float64, size=512 B
- axes/time_hours: shape=[96], dtype=float64, size=768 B
- diagnostics/heat_flux: shape=[96, 12], dtype=float32, size=4.5 KiB, units=MW/m^2
- plasma/density: shape=[96, 64], dtype=float64, size=48.0 KiB, units=m^-3
- plasma/electron_temperature: shape=[96, 64], dtype=float32, size=24.0 KiB, units=eV
- quality/flags: shape=[96, 12], dtype=int8, size=1.1 KiB

Compression summary: 5 compressed, 2 uncompressed. Overall raw-to-stored ratio is about 1.12x.

Compression is partially configured. Review uncompressed numeric datasets and consider chunked gzip or lzf
```

### 4. NDP seismic waveform discovery to plot

Case: `ndp_seismic_waveform_to_plot`
Category: hierarchical-science
Routing mode: `auto`
Status: pass
Selected agent: `visualization`
Provider/model: `argonne` / `gpt-oss-120b` via `https://inference-api.alcf.anl.gov/resource_server/metis/api/v1`
Provider settings: temperature=0.0, max_tokens=4096, context_length=0, thinking_budget=0
Route graph: visualization -> ndp_catalog -> analysis -> sac_format -> visualization
Expert handoffs: ndp_catalog, analysis, sac_format, visualization
Tools: ndp_list_organizations, ndp_search_datasets, ndp_search_datasets, ndp_search_datasets, ndp_get_dataset_details, ndp_stage_resource, sac_inspect_archive, sac_compute_trace_statistics, sac_plot_traces
Setup turns: 0
Actions: none
Child sessions: none
Artifacts: D:\Libraries\Documents\projects\clio-agent\.clio-agent-artifacts\charts\sac_traces_Pachhai_etal_2023_ScP_data.png
Elapsed: 24.2s

Prompt:

```text
Find a bounded seismic waveform dataset from a seismological or Earth-science organization in the National Data Platform. Choose a usable resource, stage it if it is small enough, inspect the waveform content, compute representative trace statistics, and produce a plot artifact. If a candidate is too large or unavailable, surface that as the result instead of inventing a plot.
```

What to see: CLIO delegates NDP discovery to ndp_catalog, stages a bounded waveform resource, analyzes SAC traces through sac_format, and creates a PNG plot.

Why this is interesting: This is the core hierarchical science demo: provider discovery, data access, format-specific analysis, and visualization without the user naming internal agents.

Observed excerpt:

```text
Data stage:
Queried the National Data Platform catalog through clio-kit MCP.

Organizations matched: 0

Datasets matched: 9
- Data for: Improved Characterization of Ultralow-velocity Zones through Advances in Bayesian Inversion of ScP Waveforms (marriott_library_hive; PDF, TAR, TXT; resources: Pachhai_etal_2023_ScP_data.tar)
- Data for: A Compositional Component to the Samoa Ultralow-velocity Zone Revealed through 2- and 3-D  Waveform Modeling of SKS and SKKS Differential Travel-times and Amplitudes (marriott_library_hive; GZ, TXT; resources: 2021_Samoa_Data.tar.gz)
- Salton Sea Seismic Data (ucr-earth-and-planetary-sciences; formats not listed; resources: Salton Sea Seismic Waveforms)
- USGS_LPC_VA_Central_Seismic_2013_LAS_2015 (usgs-3dep; GEOJSON; resources: spatial_extents_USGS_LPC_VA_Central_Seismic_2013_LAS_2015.json)
- Central U.S. ARRA Lidar, New Madrid Seismic Zone (opentopogra
```

### 5. HDF5 fusion file overview

Case: `workflow_hdf5_overview`
Category: tooling
Routing mode: `auto`
Status: pass
Selected agent: `data`
Provider/model: `argonne` / `gpt-oss-120b` via `https://inference-api.alcf.anl.gov/resource_server/metis/api/v1`
Provider settings: temperature=0.0, max_tokens=4096, context_length=0, thinking_budget=0
Route graph: data
Expert handoffs: data x8
Tools: hdf5_analyze_file, hdf5_analyze_dataset, hdf5_analyze_dataset, hdf5_analyze_dataset, hdf5_analyze_dataset, hdf5_analyze_dataset, hdf5_analyze_dataset, hdf5_analyze_dataset
Setup turns: 0
Actions: none
Child sessions: none
Artifacts: none
Elapsed: 21.6s

Prompt:

```text
I need to brief collaborators on this fusion output: D:\Libraries\Documents\projects\clio-agent\tmp\clio-benchmark-data\fusion_run.h5. What datasets are inside, what shapes and units matter, and what compression details should I mention?
```

What to see: Data expert uses HDF5 tools and summarizes datasets, units, and compression.

Why this is interesting: Proves real HDF5 path handling, tool argument generation, and grounded synthesis.

Observed excerpt:

```text
The HDF5 file contains seven datasets: axes/diagnostic_channel (12‑element int32, no compression), axes/radius_norm (64‑element float64, no compression), axes/time_hours (96‑element float64, gzip‑4, chunked 24), diagnostics/heat_flux (96×12 float32, gzip‑5, units MW/m²), plasma/density (96×64 float64, gzip‑4, units m⁻³), plasma/electron_temperature (96×64 float32, gzip‑6, units eV), and quality/flags (96×12 int8, gzip‑4), with an overall compression ratio of about 1.12.
```

### 6. Provider swap preserves session context

Case: `provider_swap_memory_followup`
Category: provider-hardening
Routing mode: `auto`
Status: pass
Selected agent: `analysis`
Provider/model: `argonne` / `meta-llama/Meta-Llama-3.1-8B-Instruct` via `https://inference-api.alcf.anl.gov/resource_server/sophia/vllm/v1`
Provider settings: temperature=0.0, max_tokens=4096, context_length=0, thinking_budget=0
Route graph: analysis
Expert handoffs: analysis x3
Tools: parquet_compute_statistics, parquet_compute_statistics, parquet_analyze_schema, parquet_compute_statistics, parquet_compute_statistics, parquet_compute_statistics, parquet_compute_statistics
Setup turns: 1
Actions: provider_swap=ok
Child sessions: none
Artifacts: none
Elapsed: 8.1s

Prompt:

```text
The provider/model has changed. Continue from the facility Parquet table we just profiled, compute any statistics you need for temperature and pressure, and tell me whether the session context survived the swap.
```

What to see: After a live ALCF provider/model swap, CLIO keeps the session coherent and uses the remembered Parquet context with visible tool evidence.

Why this is interesting: Provider/model swaps have historically destabilized active sessions, so this turn should catch stale model refs, lost context, and hidden `(no parts)` errors.

Observed excerpt:

```text
Inspected Parquet file D:\Libraries\Documents\projects\clio-agent\tmp\clio-benchmark-data\facility_measurements.parquet. It has 3000 rows, 10 columns, and 8 row groups.
- sample_id: int64, nullable=True
- run_id: string, nullable=True
- site: string, nullable=True
- temperature_k: double, nullable=True
- pressure_pa: double, nullable=True
- humidity_pct: double, nullable=True
- vibration_mm_s: double, nullable=True
- anomaly_score: double, nullable=True
- quality_flag: string, nullable=True
- valid: bool, nullable=True

Column statistics:
temperature_k: min=267.0871843867271, max=320.74640790864, mean=293.98561529394186, median=294.0945170982585, std=7.524389539680949, null_count=0, unique_count=3000
pressure_pa: min=98435.39430602924, max=103998.62554169081, mean=101231.17575211122, median=101229.29273464816, std=766.5117267674602, null_count=0, unique_count=3000
humidity_pct: min=4.0
```

### 7. Context pressure plus explicit compaction

Case: `context_pressure_compaction_followup`
Category: memory-hardening
Routing mode: `auto`
Status: pass
Selected agent: `analysis`
Provider/model: `argonne` / `gpt-oss-120b` via `https://inference-api.alcf.anl.gov/resource_server/metis/api/v1`
Provider settings: temperature=0.0, max_tokens=4096, context_length=0, thinking_budget=0
Route graph: analysis
Expert handoffs: analysis
Tools: none
Setup turns: 4
Actions: compact=ok
Child sessions: none
Artifacts: none
Elapsed: 5.1s

Prompt:

```text
After the compaction step, use the retained evidence to decide whether the experiment looks ready for collaborator review. Cite the strongest evidence from the HDF5, Parquet, CSV, and BP5 stages, and name what still needs checking.
```

What to see: A long multi-turn session is compacted, then CLIO answers from retained evidence instead of losing prior HDF5/Parquet/CSV/BP5 conclusions.

Why this is interesting: This stresses context retention and makes compaction a first-class benchmark event rather than an untested UI command.

Observed excerpt:

```text
**Evidence from the HDF5 stage**  
- The HDF5 file `fusion_run.h5` contains **7 datasets** organized in **4 groups**.  
- All scientific datasets have the expected dimensionality for a 96‑step time series with 12 diagnostic channels or 64 radial points (e.g., `diagnostics/heat_flux` = [96, 12], `plasma/density` = [96, 64]).  
- Units are explicitly stored in the dataset attributes (e.g., heat flux = MW · m⁻², density = m⁻³, electron temperature = eV), confirming that the physical meaning of the data is documented.  
- No null‑value information is present in the retained summary, but the presence of full‑size arrays (no “0‑length” dimensions) suggests that the HDF5 stage produced a complete, dense matrix.

**Evidence from the Parquet stage**  
- The Parquet file `facility_measurements.parquet` is listed among the retained paths.  
- Identifier list extracted from the file includes the e
```

### 8. CSV status distribution chart

Case: `csv_status_visual_summary`
Category: visualization
Routing mode: `auto`
Status: pass
Selected agent: `visualization`
Provider/model: `argonne` / `gpt-oss-120b` via `https://inference-api.alcf.anl.gov/resource_server/metis/api/v1`
Provider settings: temperature=0.0, max_tokens=4096, context_length=0, thinking_budget=0
Route graph: visualization
Expert handoffs: visualization
Tools: plot_bar_chart
Setup turns: 1
Actions: none
Child sessions: none
Artifacts: D:\Libraries\Documents\projects\clio-agent\bar_chart_status.png
Elapsed: 4.6s

Prompt:

```text
Create a PNG bar chart of the event status distribution from the CSV stream we just inspected. Tell me where it was saved and what field was plotted.
```

What to see: Visualization resolves the prior CSV context and plots the status field.

Why this is interesting: Exercises a CSV analysis-to-visualization handoff and verifies that charting is not limited to Parquet dashboards.

Observed excerpt:

```text
Visualization: **Chart type:** Horizontal bar chart  
**Plotted field:** `status` (event status)  
**Key observations:** The chart displays the frequency of each event status, highlighting which statuses are most prevalent in the dataset. The bars are sorted in descending order, making the ranking of status categories immediately apparent. Any rare or infrequent statuses appear as shorter bars at the bottom of the chart.

File: D:\Libraries\Documents\projects\clio-agent\bar_chart_status.png
```

### 9. Dirty data dashboard after quality review

Case: `dirty_quality_dashboard_multi_turn`
Category: visualization
Routing mode: `auto`
Status: pass
Selected agent: `visualization`
Provider/model: `argonne` / `gpt-oss-120b` via `https://inference-api.alcf.anl.gov/resource_server/metis/api/v1`
Provider settings: temperature=0.0, max_tokens=4096, context_length=0, thinking_budget=0
Route graph: visualization
Expert handoffs: visualization
Tools: plot_summary
Setup turns: 1
Actions: none
Child sessions: none
Artifacts: D:\Libraries\Documents\projects\clio-agent\tmp\clio-benchmark-data\facility_measurements_dirty_dashboard.png
Elapsed: 2.5s

Prompt:

```text
Create a compact dashboard PNG for the dirty Parquet export we just reviewed. Use it to support the quality review, and tell me where the artifact was saved.
```

What to see: Visualization resolves the reviewed dirty Parquet file from memory and creates a real dashboard artifact.

Why this is interesting: Stresses multi-turn analysis-to-visualization over intentionally dirty data, not only clean demo fixtures.

Observed excerpt:

```text
The dashboard PNG was saved to D:\Libraries\Documents\projects\clio-agent\tmp\clio-benchmark-data\facility_measurements_dirty_dashboard.png
```

### 10. NDP catalog discovery

Case: `ndp_catalog_discovery`
Category: external-catalog
Routing mode: `auto`
Status: pass
Selected agent: `ndp_catalog`
Provider/model: `argonne` / `gpt-oss-120b` via `https://inference-api.alcf.anl.gov/resource_server/metis/api/v1`
Provider settings: temperature=0.0, max_tokens=4096, context_length=0, thinking_budget=0
Route graph: ndp_catalog
Expert handoffs: ndp_catalog
Tools: ndp_list_organizations, ndp_search_datasets, ndp_get_dataset_details, ndp_stage_resource
Setup turns: 0
Actions: none
Child sessions: none
Artifacts: none
Elapsed: 9.1s

Prompt:

```text
Find a few NOAA or climate-related datasets in the National Data Platform catalog that might complement this facility data. Summarize what you found and what I should verify before download.
```

What to see: Data expert delegates discovery to NDP tools through the CLIO gateway.

Why this is interesting: Exercises external catalog discovery as a data-stage capability, before analysis consumes staged data.

Observed excerpt:

```text
Queried the National Data Platform catalog through clio-kit MCP.

Organizations matched: 3
- noaa-global-systems-laboratory
- noaa-national-centers-for-environmental-information-ncei
- noaa-demo

Datasets matched: 4
- Full Climate Connectivity Network (california-landscape-metrics; HTML, SHP, WFS, WMS; resources: [WMS] Full Climate Connectivity Network)
- Climate Refugia - Baseline (Historical) 1981 - 2010 (california-landscape-metrics; GEOTIFF, HTML, WCS, WMS; resources: [WMS] Climate Refugia - Baseline (Historical) 1981 - 2010)
- Change in Average Climatic Water Deficit (california-landscape-metrics; GEOTIFF, HTML, WCS, WMS; resources: [WMS] Change in Average Climatic Water Deficit)
- Environment Canada Climate Data (ndp-admin-ingest; GEOJSON, CSV; resources: Environment Canada Climate Data)

Staging note: CLIO staged the selected NDP resource at D:\Libraries\Documents\projects\clio-
```

## Failures Fixed During This Campaign

- GACT compaction originally bypassed transient-provider retry and only updated the GACT transcript; compaction now retries provider throttles, updates ARC memory, and fails with structured errors if memory storage fails.
- Compact summaries could lose exact scientific identifiers at the ARC truncation boundary; compact memory now preserves a labeled exact evidence index for paths, variables, columns, artifacts, and caveats.
- Retained multi-file context could make analysis narrow to the first file or let CSV follow-ups be stolen by broad synthesis; explicit file paths now take precedence and retained multi-source synthesis is limited to true synthesis questions.
- Visualization-intent follow-ups could route to analysis or a data tool even when the user asked for a chart/dashboard; file-grounded visual artifact requests are promoted to the visualization expert.
- Direct planner-selected NDP and Parquet/statistical tool actions could flatten expert ownership; NDP catalog work is promoted to the nested `ndp_catalog` expert, and statistical Parquet triage is promoted to `analysis`.
- Provider throttles during expert dispatch, handoffs, and compaction could surface as brittle partial recoveries; expert paths now use bounded transient-provider retry and still surface structured errors if exhausted.

## Remaining Caveats

- This report is evidence for the recorded ALCF run, not a guarantee that ALCF availability, model latency, or token freshness will be identical later.
- Several high-event cases are intentionally fast because child/nanoagent workers use deterministic local tools after routing; elapsed time alone should not be treated as benchmark depth.
- Two cases are deliberate surfaced-error checks. They are counted as successful hardening cases only because they returned structured errors without normal-looking fake assistant text.
- The benchmark now covers the hierarchy and handoff classes listed here, but future providers, file formats, and per-expert model assignments still need their own evidence runs.
