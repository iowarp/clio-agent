# CLIO ALCF Demo Benchmark Report

Generated: 2026-05-24 00:45:06 Central Daylight Time
Evidence JSONL: `D:\Libraries\Documents\projects\clio-agent\tmp\clio-demo-benchmark-alcf-metis-20260524-after-retry.jsonl`

Result: 14/15 clean passes, 1 expected surfaced errors, 0 partial recoveries, 0 failures.

## All Cases

| Case | Category | Mode | Source | Outcome | Agent | Handoffs | Tools | Children | Elapsed |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| workflow_hdf5_overview | tooling | auto | dspy | pass | data | data x8 | hdf5_analyze_file, hdf5_analyze_dataset, hdf5_analyze_dataset, hdf5_analyze_dataset, hdf5_analyze_dataset, hdf5_analyze_dataset, hdf5_analyze_dataset, hdf5_analyze_dataset | 0 | 14.1s |
| workflow_parquet_profile | analysis | auto | dspy | pass | analysis | analysis x5 | parquet_analyze_schema, parquet_compute_statistics, parquet_compute_statistics, parquet_compute_statistics, parquet_compute_statistics | 0 | 5.1s |
| workflow_memory_followup | memory | auto | dspy | pass | analysis | analysis x2 | parquet_analyze_schema, parquet_analyze_schema, parquet_compute_statistics, parquet_compute_statistics, parquet_compute_statistics, parquet_compute_statistics | 0 | 49.9s |
| workflow_csv_event_schema | analysis | auto | dspy | pass | analysis | analysis | csv_read_table | 0 | 3.5s |
| workflow_visual_dashboard | visualization | auto | dspy | pass | visualization | visualization | plot_summary | 0 | 4.6s |
| hdf5_dataset_focus | tooling | auto | dspy | pass | data | data | hdf5_analyze_dataset | 0 | 2.5s |
| cross_file_triage_nanoagents | multi-agent | auto | guard | pass | analysis | analysis | hdf5_analyze_file, hdf5_list_datasets, adios_inspect_file, parquet_analyze_schema, parquet_compute_statistics, csv_read_table | 4 | 0.5s |
| reasoning_cross_file_triage_nanoagents | planner-hardening | reasoning_only | dspy | pass | analysis | analysis | hdf5_analyze_file, hdf5_list_datasets, adios_inspect_file, parquet_analyze_schema, parquet_compute_statistics, csv_read_table | 4 | 1.0s |
| adios_bp5_container | tooling | auto | guard | pass | data | data | adios_inspect_file | 0 | 0.5s |
| reasoning_adios_bp5_container | planner-hardening | reasoning_only | dspy | pass | data | data | adios_inspect_file | 0 | 2.5s |
| dirty_parquet_quality | analysis | auto | dspy | pass | analysis | analysis | parquet_analyze_schema, parquet_compute_statistics, parquet_compute_statistics, parquet_compute_statistics, parquet_compute_statistics | 0 | 1.0s |
| ndp_catalog_discovery | external-catalog | auto | dspy | pass | ndp_catalog | ndp_catalog | ndp_list_organizations, ndp_search_datasets, ndp_get_dataset_details, ndp_stage_resource | 0 | 9.1s |
| ndp_seismic_waveform_to_plot | hierarchical-science | auto | dspy | pass | visualization | ndp_catalog, analysis, sac_format, visualization | ndp_list_organizations, ndp_search_datasets, ndp_search_datasets, ndp_search_datasets, ndp_get_dataset_details, ndp_stage_resource, sac_inspect_archive, sac_compute_trace_statistics, sac_plot_traces | 0 | 20.6s |
| visual_scatter_artifact | visualization | auto | dspy | pass | visualization | visualization | plot_scatter | 0 | 2.0s |
| missing_hdf5_error | hardening | auto | dspy | expected_error | data | data | hdf5_list_datasets | 0 | 2.0s |

## Best 10 Demo Prompts

### 1. Cross-file triage with tier-3 workers

Case: `cross_file_triage_nanoagents`
Category: multi-agent
Routing mode: `auto`
Status: pass
Selected agent: `analysis`
Expert handoffs: analysis
Tools: hdf5_analyze_file, hdf5_list_datasets, adios_inspect_file, parquet_analyze_schema, parquet_compute_statistics, csv_read_table
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

Compression is partially configured. Review uncompressed numeric datasets and consider chunked gzip or lzf c
```

### 2. No-guard cross-file triage

Case: `reasoning_cross_file_triage_nanoagents`
Category: planner-hardening
Routing mode: `reasoning_only`
Status: pass
Selected agent: `analysis`
Expert handoffs: analysis
Tools: hdf5_analyze_file, hdf5_list_datasets, adios_inspect_file, parquet_analyze_schema, parquet_compute_statistics, csv_read_table
Child sessions: csv_validator subagent, analysis_validator subagent, adios_validator subagent, data_validator subagent
Artifacts: none
Elapsed: 1.0s

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

Compression is partially configured. Review uncompressed numeric datasets and consider chunked gzip or lzf c
```

### 3. NDP seismic waveform discovery to plot

Case: `ndp_seismic_waveform_to_plot`
Category: hierarchical-science
Routing mode: `auto`
Status: pass
Selected agent: `visualization`
Expert handoffs: ndp_catalog, analysis, sac_format, visualization
Tools: ndp_list_organizations, ndp_search_datasets, ndp_search_datasets, ndp_search_datasets, ndp_get_dataset_details, ndp_stage_resource, sac_inspect_archive, sac_compute_trace_statistics, sac_plot_traces
Child sessions: none
Artifacts: D:\Libraries\Documents\projects\clio-agent\.clio-agent-artifacts\charts\sac_traces_Pachhai_etal_2023_ScP_data.png
Elapsed: 20.6s

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
- Central U.S. ARRA Lidar, New Madrid Seismic Zone (opentopograp
```

### 4. HDF5 fusion file overview

Case: `workflow_hdf5_overview`
Category: tooling
Routing mode: `auto`
Status: pass
Selected agent: `data`
Expert handoffs: data x8
Tools: hdf5_analyze_file, hdf5_analyze_dataset, hdf5_analyze_dataset, hdf5_analyze_dataset, hdf5_analyze_dataset, hdf5_analyze_dataset, hdf5_analyze_dataset, hdf5_analyze_dataset
Child sessions: none
Artifacts: none
Elapsed: 14.1s

Prompt:

```text
I need to brief collaborators on this fusion output: D:\Libraries\Documents\projects\clio-agent\tmp\clio-benchmark-data\fusion_run.h5. What datasets are inside, what shapes and units matter, and what compression details should I mention?
```

What to see: Data expert uses HDF5 tools and summarizes datasets, units, and compression.

Why this is interesting: Proves real HDF5 path handling, tool argument generation, and grounded synthesis.

Observed excerpt:

```text
The file contains 7 datasets: axes/diagnostic_channel (12‑int32, no compression), axes/radius_norm (64‑float64, no compression), axes/time_hours (96‑float64, gzip‑4, chunk 24), diagnostics/heat_flux (96×12‑float32, gzip‑5, units MW/m²), plasma/density (96×64‑float64, gzip‑4, units m⁻³), plasma/electron_temperature (96×64‑float32, gzip‑6, units eV), quality/flags (96×12‑int8, gzip‑4); overall 5 of 7 datasets are gzip‑compressed with overall compression ratio ~1.12.
```

### 5. Parquet facility profile

Case: `workflow_parquet_profile`
Category: analysis
Routing mode: `auto`
Status: pass
Selected agent: `analysis`
Expert handoffs: analysis x5
Tools: parquet_analyze_schema, parquet_compute_statistics, parquet_compute_statistics, parquet_compute_statistics, parquet_compute_statistics
Child sessions: none
Artifacts: none
Elapsed: 5.1s

Prompt:

```text
Profile the facility measurements in D:\Libraries\Documents\projects\clio-agent\tmp\clio-benchmark-data\facility_measurements.parquet. I care about schema, row groups, and whether temperature_k, pressure_pa, humidity_pct, and anomaly_score look sane.
```

What to see: Analysis expert reads Parquet schema and computes statistics for named fields.

Why this is interesting: Checks statistical tool calls and model feedback from multiple numeric observations.

Observed excerpt:

```text
The Parquet file has 10 columns (sample_id int64, run_id string, site string, temperature_k double, pressure_pa double, humidity_pct double, vibration_mm_s double, anomaly_score double, plus two others), 3000 rows across 8 row groups, and the temperature, pressure, humidity, and anomaly_score statistics fall within expected physical ranges, so they appear sane.
```

### 6. Memory follow-up without repeating path

Case: `workflow_memory_followup`
Category: memory
Routing mode: `auto`
Status: pass
Selected agent: `analysis`
Expert handoffs: analysis x2
Tools: parquet_analyze_schema, parquet_analyze_schema, parquet_compute_statistics, parquet_compute_statistics, parquet_compute_statistics, parquet_compute_statistics
Child sessions: none
Artifacts: none
Elapsed: 49.9s

Prompt:

```text
Based on the Parquet file we just profiled, compute whatever schema or column statistics you need for a quick anomaly triage view. Do not ask me for the path again.
```

What to see: CLIO resolves the previously profiled Parquet file from session context.

Why this is interesting: Demonstrates session memory and current-file resolution instead of copy/paste paths.

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
sample_id: min=0.0, max=2999.0, mean=1499.5, median=1499.5, std=866.0253556719148, null_count=0, unique_count=3000
run_id: null_count=0, unique_count=3
site: null_count=0, unique_count=5
temperature_k: min=267.0871843867271, max=320.74640790864, mean=293.98561529394186, median=294.0945170982585, std=7.524389539680949, null_count=0, unique_count=3000

R
```

### 7. NDP catalog discovery

Case: `ndp_catalog_discovery`
Category: external-catalog
Routing mode: `auto`
Status: pass
Selected agent: `ndp_catalog`
Expert handoffs: ndp_catalog
Tools: ndp_list_organizations, ndp_search_datasets, ndp_get_dataset_details, ndp_stage_resource
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

Staging note: CLIO staged the selected NDP resource at D:\Libraries\Documents\projects\clio-a
```

### 8. Dirty Parquet quality review

Case: `dirty_parquet_quality`
Category: analysis
Routing mode: `auto`
Status: pass
Selected agent: `analysis`
Expert handoffs: analysis
Tools: parquet_analyze_schema, parquet_compute_statistics, parquet_compute_statistics, parquet_compute_statistics, parquet_compute_statistics
Child sessions: none
Artifacts: none
Elapsed: 1.0s

Prompt:

```text
This Parquet export looks suspicious: D:\Libraries\Documents\projects\clio-agent\tmp\clio-benchmark-data\facility_measurements_dirty.parquet. Review it for data quality problems and tell me what fields need attention before downstream analysis.
```

What to see: Analysis expert uses Parquet tools and grounds quality claims in columns/nulls.

Why this is interesting: Separates concrete data-quality findings from generic cleaning advice.

Observed excerpt:

```text
Inspected Parquet file D:\Libraries\Documents\projects\clio-agent\tmp\clio-benchmark-data\facility_measurements_dirty.parquet. It has 3000 rows, 6 columns, and 10 row groups.
- sample_id: int64, nullable=True
- site: string, nullable=True
- temperature_k: double, nullable=True
- pressure_pa: double, nullable=True
- quality_flag: string, nullable=True
- valid: bool, nullable=True

Column statistics:
sample_id: min=0.0, max=2999.0, mean=1499.5, median=1499.5, std=866.0253556719148, null_count=0, unique_count=3000
site: null_count=0, unique_count=6
temperature_k: min=267.0871843867271, max=320.74640790864, mean=293.98862688749205, median=294.1027798559147, std=7.517766411153003, null_count=18, unique_count=2982
pressure_pa: min=98435.39430602924, max=103998.62554169081, mean=101230.9576727976, median=101229.54620674164, std=767.0242855536812, null_count=14, unique_count=2986

Recommendatio
```

### 9. Follow-up visualization artifact

Case: `workflow_visual_dashboard`
Category: visualization
Routing mode: `auto`
Status: pass
Selected agent: `visualization`
Expert handoffs: visualization
Tools: plot_summary
Child sessions: none
Artifacts: D:\Libraries\Documents\projects\clio-agent\summary_facility_measurements.png
Elapsed: 4.6s

Prompt:

```text
Create a compact PNG dashboard from the Parquet file we just profiled. Tell me where it was saved and what the chart is summarizing.
```

What to see: Visualization expert resolves prior Parquet context and creates a PNG artifact.

Why this is interesting: Shows multi-turn handoff from analysis to visualization with a real saved artifact.

Observed excerpt:

```text
Visualization: A compact 2 × 2 PNG dashboard named **summary_facility_measurements.png** was generated.  

* **Data‑type composition bar chart** (top‑left) shows the count of columns by type (e.g., numeric, categorical, datetime).  
* **Null‑value counts bar chart** (top‑right) ranks columns by the number of missing entries, making it easy to spot columns that may need cleaning.  
* **Numeric‑column histograms** (bottom‑left) present the distribution of each numeric variable, revealing skewness, multimodality, or outliers.  
* **Correlation heatmap** (bottom‑right) visualizes Pearson correlation coefficients among numeric columns with a cool‑warm diverging colormap, highlighting strong positive or negative relationships.

Together these four panels give a concise overview of the dataset’s structure, completeness, variable distributions, and inter‑variable relationships.

File: D:\Librar
```

### 10. Targeted scatter plot

Case: `visual_scatter_artifact`
Category: visualization
Routing mode: `auto`
Status: pass
Selected agent: `visualization`
Expert handoffs: visualization
Tools: plot_scatter
Child sessions: none
Artifacts: D:\Libraries\Documents\projects\clio-agent\tmp\clio-benchmark-data\facility_measurements_scatter.png
Elapsed: 2.0s

Prompt:

```text
Create a scatter plot from D:\Libraries\Documents\projects\clio-agent\tmp\clio-benchmark-data\facility_measurements.parquet with vibration_mm_s on the x-axis and anomaly_score on the y-axis. Save it as a PNG and explain what relationship the plot is meant to reveal.
```

What to see: Visualization expert chooses plot_scatter and saves a PNG artifact.

Why this is interesting: Checks whether a specific visualization intent maps to the right chart tool.

Observed excerpt:

```text
The scatter plot (facility_measurements_scatter.png) visualizes vibration_mm_s versus anomaly_score, revealing whether higher vibration levels correspond to higher anomaly scores—typically a positive trend indicates that increased vibration is associated with greater anomaly likelihood.
```
