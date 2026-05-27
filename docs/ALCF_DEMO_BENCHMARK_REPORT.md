# CLIO ALCF Demo Benchmark Report

Generated: 2026-05-25 03:57:33 CDT
Evidence JSONL: `/home/jcernuda/clio-agent/tmp/clio-demo-benchmark-alcf-metis-20260525-visual-loop2.jsonl`

Result: 16/21 clean passes, 2 expected surfaced errors, 1 partial recoveries, 2 failures.

Stress coverage: meets the documented benchmark standard.

## Stress Coverage Audit

| Criterion | Observed | Required | Status |
| --- | ---: | ---: | --- |
| at least ten complex collaborator-grade demos | 11 | 10 | pass |
| at least five long or high-event stress cases | 7 | 5 | pass |
| at least three cases with tier-3 agents or nanoagents | 5 | 3 | pass |
| at least three visualization artifacts from analyzed data | 5 | 3 | pass |
| at least two deliberate surfaced-error cases | 2 | 2 | pass |
| at least one context-pressure or compaction case | 1 | 1 | pass |
| at least one provider/model-swap stress case | 1 | 1 | pass |

High-event or long-running cases:

- workflow_memory_followup (9.6s, 16 events)
- cross_file_triage_nanoagents (0.5s, 11 events)
- cross_file_dirty_quality_gate_nanoagents (0.5s, 11 events)
- reasoning_cross_file_triage_nanoagents (1.5s, 11 events)
- ndp_seismic_waveform_to_plot (17.6s, 13 events)
- visual_scatter_artifact (20.6s, 24 events)
- provider_swap_memory_followup (28.1s, 14 events)

## All Cases

| Case | Category | Mode | Source | Outcome | Agent | Handoffs | Tools | Children | Elapsed |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| workflow_hdf5_overview | tooling | auto | dspy | pass | data | data | hdf5_analyze_file, hdf5_list_datasets | 0 | 4.0s |
| workflow_parquet_profile | analysis | auto | dspy | pass | analysis | analysis | parquet_analyze_schema, parquet_compute_statistics, parquet_compute_statistics, parquet_compute_statistics, parquet_compute_statistics | 0 | 2.0s |
| workflow_memory_followup | memory | auto | dspy | fail | analysis | analysis x6 | parquet_compute_statistics, parquet_compute_statistics, parquet_compute_statistics, parquet_analyze_schema, parquet_query_data, parquet_analyze_schema, parquet_compute_statistics, parquet_compute_statistics, parquet_compute_statistics, parquet_compute_statistics | 0 | 9.6s |
| context_pressure_compaction_followup | memory-hardening | auto | dspy | pass | analysis | analysis | - | 0 | 4.5s |
| workflow_csv_event_schema | analysis | auto | dspy | pass | analysis | analysis | csv_read_table | 0 | 1.5s |
| workflow_visual_dashboard | visualization | auto | dspy | pass | visualization | visualization | plot_summary | 0 | 14.6s |
| csv_status_visual_summary | visualization | auto | dspy | fail | visualization | visualization x2 | plot_bar_chart, plot_bar_chart, plot_bar_chart, plot_histogram | 0 | 11.0s |
| hdf5_dataset_focus | tooling | auto | dspy | pass | data | data | hdf5_list_datasets, hdf5_analyze_dataset | 0 | 2.5s |
| cross_file_triage_nanoagents | multi-agent | auto | guard | pass | analysis | analysis | hdf5_analyze_file, hdf5_list_datasets, adios_inspect_file, parquet_analyze_schema, parquet_compute_statistics, csv_read_table | 4 | 0.5s |
| cross_file_dirty_quality_gate_nanoagents | multi-agent | auto | guard | pass | analysis | analysis | hdf5_analyze_file, hdf5_list_datasets, adios_inspect_file, parquet_analyze_schema, parquet_compute_statistics, csv_read_table | 4 | 0.5s |
| reasoning_cross_file_triage_nanoagents | planner-hardening | reasoning_only | dspy | pass | analysis | analysis | hdf5_analyze_file, hdf5_list_datasets, adios_inspect_file, parquet_analyze_schema, parquet_compute_statistics, csv_read_table | 4 | 1.5s |
| adios_bp5_container | tooling | auto | guard | pass | data | data | adios_inspect_file | 0 | 0.5s |
| reasoning_adios_bp5_container | planner-hardening | reasoning_only | dspy | pass | data | data | adios_inspect_file | 0 | 2.5s |
| dirty_parquet_quality | analysis | auto | dspy | pass | analysis | analysis | parquet_analyze_schema, parquet_compute_statistics, parquet_compute_statistics | 0 | 1.5s |
| dirty_quality_dashboard_multi_turn | visualization | auto | dspy | pass | visualization | visualization | plot_summary | 0 | 7.1s |
| ndp_catalog_discovery | external-catalog | auto | dspy | pass | ndp_catalog | ndp_catalog | ndp_list_organizations, ndp_search_datasets, ndp_get_dataset_details, ndp_stage_resource | 0 | 6.5s |
| ndp_seismic_waveform_to_plot | hierarchical-science | auto | dspy | pass | visualization | ndp_catalog, analysis, sac_format, visualization | ndp_list_organizations, ndp_search_datasets, ndp_search_datasets, ndp_search_datasets, ndp_get_dataset_details, ndp_stage_resource, sac_inspect_archive, sac_compute_trace_statistics, sac_plot_traces | 0 | 17.6s |
| visual_scatter_artifact | visualization | auto | dspy | partial | visualization | visualization x12 | plot_scatter, plot_scatter, plot_scatter, plot_scatter, plot_scatter, plot_scatter, plot_scatter, plot_scatter, plot_scatter, plot_scatter, plot_scatter, plot_scatter | 0 | 20.6s |
| missing_hdf5_error | hardening | auto | dspy | expected_error | data | data x2 | hdf5_list_datasets, hdf5_analyze_file | 0 | 2.5s |
| missing_csv_error | hardening | auto | dspy | expected_error | analysis | analysis | csv_read_table | 0 | 3.0s |
| provider_swap_memory_followup | provider-hardening | auto | dspy | pass | analysis | analysis x5 | parquet_compute_statistics, parquet_compute_statistics, parquet_compute_statistics, parquet_compute_statistics, parquet_analyze_schema, parquet_compute_statistics, parquet_compute_statistics, parquet_compute_statistics, parquet_compute_statistics | 0 | 28.1s |

## Best 10 Demo Prompts

### 1. Targeted scatter plot

Case: `visual_scatter_artifact`
Category: visualization
Routing mode: `auto`
Status: partial
Selected agent: `visualization`
Provider/model: `argonne` / `meta-llama/Meta-Llama-3.1-8B-Instruct` via `https://inference-api.alcf.anl.gov/resource_server/sophia/vllm/v1`
Provider settings: temperature=0.0, max_tokens=4096, context_length=0, thinking_budget=0
Route graph: visualization
Expert handoffs: visualization x12
Tools: plot_scatter, plot_scatter, plot_scatter, plot_scatter, plot_scatter, plot_scatter, plot_scatter, plot_scatter, plot_scatter, plot_scatter, plot_scatter, plot_scatter
Setup turns: 0
Actions: none
Child sessions: none
Artifacts: /home/jcernuda/clio-agent/tmp/scatter_plot.png
Elapsed: 20.6s

Prompt:

```text
Create a scatter plot from /home/jcernuda/clio-agent/tmp/clio-benchmark-data/facility_measurements.parquet with vibration_mm_s on the x-axis and anomaly_score on the y-axis. Save it as a PNG and explain what relationship the plot is meant to reveal.
```

What to see: Visualization expert chooses plot_scatter and saves a PNG artifact.

Why this is interesting: Checks whether a specific visualization intent maps to the right chart tool.

Observed excerpt:

```text
A scatter plot has been created with vibration_mm_s on the x-axis and anomaly_score on the y-axis. The plot is saved as a PNG file at /home/jcernuda/clio-agent/tmp/scatter_plot.png. This plot is meant to reveal the relationship between the vibration measurements in millimeters per second and the anomaly scores. A positive correlation between the two variables would indicate that higher vibration measurements are associated with higher anomaly scores, suggesting potential issues with the facility's equipment.
```

### 2. Cross-file triage with tier-3 workers

Case: `cross_file_triage_nanoagents`
Category: multi-agent
Routing mode: `auto`
Status: pass
Selected agent: `analysis`
Provider/model: `argonne` / `meta-llama/Meta-Llama-3.1-8B-Instruct` via `https://inference-api.alcf.anl.gov/resource_server/sophia/vllm/v1`
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
I have four related files from the same experiment: /home/jcernuda/clio-agent/tmp/clio-benchmark-data/fusion_run.h5, /home/jcernuda/clio-agent/tmp/clio-benchmark-data/facility_measurements.parquet, /home/jcernuda/clio-agent/tmp/clio-benchmark-data/sensor_events.csv, and "/home/jcernuda/clio-agent/tmp/clio-benchmark-data/gray scott noise 0.01 data.bp5". Give me a cross-file triage summary: what is in each file, whether the measurements look ready for downstream analysis, and what I should check next.
```

What to see: Analysis coordinates tool-backed child workers and aggregates their findings.

Why this is interesting: Best stress case for hierarchical routing and child-session evidence.

Observed excerpt:

```text
Parallel validation completed with tool-backed nanoagents.

data_validator:
Inspected HDF5 file /home/jcernuda/clio-agent/tmp/clio-benchmark-data/fusion_run.h5. It contains 7 datasets and 4 groups.
- axes/diagnostic_channel: shape=[12], dtype=int32, size=48 B
- axes/radius_norm: shape=[64], dtype=float64, size=512 B
- axes/time_hours: shape=[96], dtype=float64, size=768 B
- diagnostics/heat_flux: shape=[96, 12], dtype=float32, size=4.5 KiB, units=MW/m^2
- plasma/density: shape=[96, 64], dtype=float64, size=48.0 KiB, units=m^-3
- plasma/electron_temperature: shape=[96, 64], dtype=float32, size=24.0 KiB, units=eV
- quality/flags: shape=[96, 12], dtype=int8, size=1.1 KiB

Compression summary: 5 compressed, 2 uncompressed. Overall raw-to-stored ratio is about 1.12x.

Compression is partially configured. Review uncompressed numeric datasets and consider chunked gzip or lzf compression when re
```

### 3. Dirty cross-file quality gate

Case: `cross_file_dirty_quality_gate_nanoagents`
Category: multi-agent
Routing mode: `auto`
Status: pass
Selected agent: `analysis`
Provider/model: `argonne` / `meta-llama/Meta-Llama-3.1-8B-Instruct` via `https://inference-api.alcf.anl.gov/resource_server/sophia/vllm/v1`
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
Before I share this run, build a quality gate across /home/jcernuda/clio-agent/tmp/clio-benchmark-data/fusion_run.h5, /home/jcernuda/clio-agent/tmp/clio-benchmark-data/facility_measurements_dirty.parquet, /home/jcernuda/clio-agent/tmp/clio-benchmark-data/sensor_events.csv, and "/home/jcernuda/clio-agent/tmp/clio-benchmark-data/gray scott noise 0.01 data.bp5". I need to know what each file proves, where the dirty tabular export is risky, and which checks block collaborator handoff.
```

What to see: Analysis coordinates tool-backed child workers over HDF5, dirty Parquet, CSV, and BP5 evidence.

Why this is interesting: Adds a harder cross-file case where one source is intentionally dirty and the user asks for a review gate rather than a generic summary.

Observed excerpt:

```text
Parallel validation completed with tool-backed nanoagents.

data_validator:
Inspected HDF5 file /home/jcernuda/clio-agent/tmp/clio-benchmark-data/fusion_run.h5. It contains 7 datasets and 4 groups.
- axes/diagnostic_channel: shape=[12], dtype=int32, size=48 B
- axes/radius_norm: shape=[64], dtype=float64, size=512 B
- axes/time_hours: shape=[96], dtype=float64, size=768 B
- diagnostics/heat_flux: shape=[96, 12], dtype=float32, size=4.5 KiB, units=MW/m^2
- plasma/density: shape=[96, 64], dtype=float64, size=48.0 KiB, units=m^-3
- plasma/electron_temperature: shape=[96, 64], dtype=float32, size=24.0 KiB, units=eV
- quality/flags: shape=[96, 12], dtype=int8, size=1.1 KiB

Compression summary: 5 compressed, 2 uncompressed. Overall raw-to-stored ratio is about 1.12x.

Compression is partially configured. Review uncompressed numeric datasets and consider chunked gzip or lzf compression when re
```

### 4. No-guard cross-file triage

Case: `reasoning_cross_file_triage_nanoagents`
Category: planner-hardening
Routing mode: `reasoning_only`
Status: pass
Selected agent: `analysis`
Provider/model: `argonne` / `meta-llama/Meta-Llama-3.1-8B-Instruct` via `https://inference-api.alcf.anl.gov/resource_server/sophia/vllm/v1`
Provider settings: temperature=0.0, max_tokens=4096, context_length=0, thinking_budget=0
Route graph: analysis -> [csv_validator subagent, analysis_validator subagent, adios_validator subagent, data_validator subagent]
Expert handoffs: analysis
Tools: hdf5_analyze_file, hdf5_list_datasets, adios_inspect_file, parquet_analyze_schema, parquet_compute_statistics, csv_read_table
Setup turns: 0
Actions: none
Child sessions: csv_validator subagent, analysis_validator subagent, adios_validator subagent, data_validator subagent
Artifacts: none
Elapsed: 1.5s

Prompt:

```text
I have four related files from the same experiment: /home/jcernuda/clio-agent/tmp/clio-benchmark-data/fusion_run.h5, /home/jcernuda/clio-agent/tmp/clio-benchmark-data/facility_measurements.parquet, /home/jcernuda/clio-agent/tmp/clio-benchmark-data/sensor_events.csv, and "/home/jcernuda/clio-agent/tmp/clio-benchmark-data/gray scott noise 0.01 data.bp5". Give me a cross-file triage summary: what is in each file, whether the measurements look ready for downstream analysis, and what I should check next.
```

What to see: With routing guards disabled for the session, the planner still reaches analysis and tool-backed child workers.

Why this is interesting: Separates planner capability from the production registry guard, which matters as CLIO grows beyond a few built-in experts.

Observed excerpt:

```text
Parallel validation completed with tool-backed nanoagents.

data_validator:
Inspected HDF5 file /home/jcernuda/clio-agent/tmp/clio-benchmark-data/fusion_run.h5. It contains 7 datasets and 4 groups.
- axes/diagnostic_channel: shape=[12], dtype=int32, size=48 B
- axes/radius_norm: shape=[64], dtype=float64, size=512 B
- axes/time_hours: shape=[96], dtype=float64, size=768 B
- diagnostics/heat_flux: shape=[96, 12], dtype=float32, size=4.5 KiB, units=MW/m^2
- plasma/density: shape=[96, 64], dtype=float64, size=48.0 KiB, units=m^-3
- plasma/electron_temperature: shape=[96, 64], dtype=float32, size=24.0 KiB, units=eV
- quality/flags: shape=[96, 12], dtype=int8, size=1.1 KiB

Compression summary: 5 compressed, 2 uncompressed. Overall raw-to-stored ratio is about 1.12x.

Compression is partially configured. Review uncompressed numeric datasets and consider chunked gzip or lzf compression when re
```

### 5. NDP seismic waveform discovery to plot

Case: `ndp_seismic_waveform_to_plot`
Category: hierarchical-science
Routing mode: `auto`
Status: pass
Selected agent: `visualization`
Provider/model: `argonne` / `meta-llama/Meta-Llama-3.1-8B-Instruct` via `https://inference-api.alcf.anl.gov/resource_server/sophia/vllm/v1`
Provider settings: temperature=0.0, max_tokens=4096, context_length=0, thinking_budget=0
Route graph: visualization -> ndp_catalog -> analysis -> sac_format -> visualization
Expert handoffs: ndp_catalog, analysis, sac_format, visualization
Tools: ndp_list_organizations, ndp_search_datasets, ndp_search_datasets, ndp_search_datasets, ndp_get_dataset_details, ndp_stage_resource, sac_inspect_archive, sac_compute_trace_statistics, sac_plot_traces
Setup turns: 0
Actions: none
Child sessions: none
Artifacts: /home/jcernuda/clio-agent/.clio-agent-artifacts/charts/sac_traces_Pachhai_etal_2023_ScP_data.png
Elapsed: 17.6s

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
- Central U.S. ARRA Lidar, New Madrid Seismic Zone (opentopograph
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
Expert handoffs: analysis x5
Tools: parquet_compute_statistics, parquet_compute_statistics, parquet_compute_statistics, parquet_compute_statistics, parquet_analyze_schema, parquet_compute_statistics, parquet_compute_statistics, parquet_compute_statistics, parquet_compute_statistics
Setup turns: 1
Actions: provider_swap=ok
Child sessions: none
Artifacts: none
Elapsed: 28.1s

Prompt:

```text
The provider/model has changed. Continue from the facility Parquet table we just profiled, compute any statistics you need for temperature and pressure, and tell me whether the session context survived the swap.
```

What to see: After a live ALCF provider/model swap, CLIO keeps the session coherent and uses the remembered Parquet context with visible tool evidence.

Why this is interesting: Provider/model swaps have historically destabilized active sessions, so this turn should catch stale model refs, lost context, and hidden `(no parts)` errors.

Observed excerpt:

```text
Inspected Parquet file /home/jcernuda/clio-agent/tmp/clio-benchmark-data/facility_measurements.parquet. It has 3000 rows, 10 columns, and 8 row groups.
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
humidity_pct: min=4.0, max=96.0, mean=44
```

### 7. Memory follow-up without repeating path

Case: `workflow_memory_followup`
Category: memory
Routing mode: `auto`
Status: fail
Selected agent: `analysis`
Provider/model: `argonne` / `meta-llama/Meta-Llama-3.1-8B-Instruct` via `https://inference-api.alcf.anl.gov/resource_server/sophia/vllm/v1`
Provider settings: temperature=0.0, max_tokens=4096, context_length=0, thinking_budget=0
Route graph: analysis
Expert handoffs: analysis x6
Tools: parquet_compute_statistics, parquet_compute_statistics, parquet_compute_statistics, parquet_analyze_schema, parquet_query_data, parquet_analyze_schema, parquet_compute_statistics, parquet_compute_statistics, parquet_compute_statistics, parquet_compute_statistics
Setup turns: 0
Actions: none
Child sessions: none
Artifacts: none
Elapsed: 9.6s

Prompt:

```text
Based on the Parquet file we just profiled, compute whatever schema or column statistics you need for a quick anomaly triage view. Do not ask me for the path again.
```

What to see: CLIO resolves the previously profiled Parquet file from session context.

Why this is interesting: Demonstrates session memory and current-file resolution instead of copy/paste paths.

Observed excerpt:

```text
Inspected Parquet file /home/jcernuda/clio-agent/tmp/clio-benchmark-data/facility_measurements.parquet. It has 3000 rows, 10 columns, and 8 row groups.
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
anomaly_score: min=0.05644623437754352, max=6.352218321140548, mean=1.5960493743015989, median=1.4736386911045298, std=0.8429400102739837, null_count=0, unique_count=3000
temperature_k: min=267.0871843867271, max=320.74640790864, mean=293.98561529394186, median=294.0945170982585, std=7.524389539680949, null_count=0, unique_count=3000
pressure_pa: min=98435.39430602924,
```

### 8. CSV status distribution chart

Case: `csv_status_visual_summary`
Category: visualization
Routing mode: `auto`
Status: fail
Selected agent: `visualization`
Provider/model: `argonne` / `meta-llama/Meta-Llama-3.1-8B-Instruct` via `https://inference-api.alcf.anl.gov/resource_server/sophia/vllm/v1`
Provider settings: temperature=0.0, max_tokens=4096, context_length=0, thinking_budget=0
Route graph: visualization
Expert handoffs: visualization x2
Tools: plot_bar_chart, plot_bar_chart, plot_bar_chart, plot_histogram
Setup turns: 1
Actions: none
Child sessions: none
Artifacts: /home/jcernuda/clio-agent/bar_chart_status.png, /home/jcernuda/clio-agent/histogram_status.png
Elapsed: 11.0s

Prompt:

```text
Create a PNG bar chart of the event status distribution from the CSV stream we just inspected. Tell me where it was saved and what field was plotted.
```

What to see: Visualization resolves the prior CSV context and plots the status field.

Why this is interesting: Exercises a CSV analysis-to-visualization handoff and verifies that charting is not limited to Parquet dashboards.

Observed excerpt:

```text
Visualization: The generated chart is a horizontal bar chart showing the distribution of event status. The chart type was chosen because it effectively communicates the relative frequency of each event status category. The key observations visible in the chart are the top 10 event status categories, which can be identified by the height of each bar. The chart provides a clear overview of the event status distribution, allowing for easy identification of the most common event status categories.

File: /home/jcernuda/clio-agent/bar_chart_status.png
```

### 9. Context pressure plus explicit compaction

Case: `context_pressure_compaction_followup`
Category: memory-hardening
Routing mode: `auto`
Status: pass
Selected agent: `analysis`
Provider/model: `argonne` / `meta-llama/Meta-Llama-3.1-8B-Instruct` via `https://inference-api.alcf.anl.gov/resource_server/sophia/vllm/v1`
Provider settings: temperature=0.0, max_tokens=4096, context_length=0, thinking_budget=0
Route graph: analysis
Expert handoffs: analysis
Tools: none
Setup turns: 4
Actions: compact=ok
Child sessions: none
Artifacts: none
Elapsed: 4.5s

Prompt:

```text
After the compaction step, use the retained evidence to decide whether the experiment looks ready for collaborator review. Cite the strongest evidence from the HDF5, Parquet, CSV, and BP5 stages, and name what still needs checking.
```

What to see: A long multi-turn session is compacted, then CLIO answers from retained evidence instead of losing prior HDF5/Parquet/CSV/BP5 conclusions.

Why this is interesting: This stresses context retention and makes compaction a first-class benchmark event rather than an untested UI command.

Observed excerpt:

```text
Based on the retained evidence from the HDF5, Parquet, CSV, and BP5 stages, here are the strongest evidence points:

1. **Data completeness**: All files inspected have no null values, indicating excellent data completeness.
2. **Data quality**: The HDF5 file has a raw-to-stored ratio of 1.12x, indicating efficient compression. The Parquet file has a row-group size of 10,000, which is a reasonable size for this dataset.
3. **Data distribution**: The CSV file has a unique count of 5 for the column "city", suggesting that this column may be suitable for stratification in analysis.
4. **Data consistency**: The BP5 run at "/home/jcernuda/clio-agent/tmp/clio-benchmark-data" has a consistent timestamp format, indicating that the data is well-structured.

However, there are still some areas that need checking:

1. **Data consistency**: The CSV file has a unique count of 5 for the column "city",
```

### 10. Dirty data dashboard after quality review

Case: `dirty_quality_dashboard_multi_turn`
Category: visualization
Routing mode: `auto`
Status: pass
Selected agent: `visualization`
Provider/model: `argonne` / `meta-llama/Meta-Llama-3.1-8B-Instruct` via `https://inference-api.alcf.anl.gov/resource_server/sophia/vllm/v1`
Provider settings: temperature=0.0, max_tokens=4096, context_length=0, thinking_budget=0
Route graph: visualization
Expert handoffs: visualization
Tools: plot_summary
Setup turns: 1
Actions: none
Child sessions: none
Artifacts: /home/jcernuda/clio-agent/summary_facility_measurements_dirty.png
Elapsed: 7.1s

Prompt:

```text
Create a compact dashboard PNG for the dirty Parquet export we just reviewed. Use it to support the quality review, and tell me where the artifact was saved.
```

What to see: Visualization resolves the reviewed dirty Parquet file from memory and creates a real dashboard artifact.

Why this is interesting: Stresses multi-turn analysis-to-visualization over intentionally dirty data, not only clean demo fixtures.

Observed excerpt:

```text
Visualization: The generated chart is a 2x2 summary dashboard PNG that shows the data type composition (top-left), null counts (top-right), numeric column histograms (bottom-left), and correlation heatmap (bottom-right). This chart provides a clear and concise overview of the dataset, highlighting any potential issues or anomalies.

File: /home/jcernuda/clio-agent/summary_facility_measurements_dirty.png
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

## Partial Recovery Caveats

- `visual_scatter_artifact`: Agent planner reached the step limit after partial observations.
  stage=step_limit_after_observations, tools=plot_scatter, plot_scatter, plot_scatter, plot_scatter, plot_scatter, plot_scatter, plot_scatter, plot_scatter, plot_scatter, plot_scatter, plot_scatter, plot_scatter
## Failures To Investigate

- `workflow_memory_followup`: expected CLIO resolves the previously profiled Parquet file from session context.
  observed agent=analysis, tools=parquet_compute_statistics, parquet_compute_statistics, parquet_compute_statistics, parquet_analyze_schema, parquet_query_data, parquet_analyze_schema, parquet_compute_statistics, parquet_compute_statistics, parquet_compute_statistics, parquet_compute_statistics, error={'error': 'tool_error', 'message': "Column 'humidity_%' not found in file", 'details': {'expert': 'analysis', 'tool': 'parquet_compute_statistics', 'tool_error': {'type': 'tool_error', 'code': 'tool_failed', 'message': "Column 'humidity_%' not found in file", 'next_action': 'Check the tool arguments and gateway health, then retry.', 'tool': 'parquet_compute_statistics'}, 'partial': True, 'recovery_actions': ['retry', 'reconfigure_provider', 'exit'], 'successful_tools': ['parquet_compute_statistics', 'parquet_compute_statistics', 'parquet_analyze_schema', 'parquet_query_data', 'parquet_analyze_schema', 'parquet_compute_statistics', 'parquet_compute_statistics', 'parquet_compute_statistics', 'parquet_compute_statistics']}, 'recoverable': True}
- `csv_status_visual_summary`: expected Visualization resolves the prior CSV context and plots the status field.
  observed agent=visualization, tools=plot_bar_chart, plot_bar_chart, plot_bar_chart, plot_histogram, error={'error': 'tool_error', 'message': "Column 'event_status' not found. Available: ['event_id', 'timestamp', 'site', 'temperature_k', 'pressure_pa', 'status', 'operator_note']", 'details': {'expert': 'visualization', 'tool': 'plot_bar_chart', 'tool_error': {'type': 'tool_error', 'code': 'tool_failed', 'message': "Column 'event_status' not found. Available: ['event_id', 'timestamp', 'site', 'temperature_k', 'pressure_pa', 'status', 'operator_note']", 'next_action': 'Check the tool arguments and gateway health, then retry.', 'tool': 'plot_bar_chart'}, 'partial': True, 'recovery_actions': ['retry', 'reconfigure_provider', 'exit'], 'successful_tools': ['plot_bar_chart', 'plot_histogram']}, 'recoverable': True}
