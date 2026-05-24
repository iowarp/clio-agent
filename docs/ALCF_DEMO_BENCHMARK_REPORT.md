# CLIO ALCF Demo Benchmark Report

Generated: 2026-05-23 19:54:42 Central Daylight Time
Evidence JSONL: `D:\Libraries\Documents\projects\clio-agent\tmp\clio-demo-benchmark-alcf-gptoss120b-v3.jsonl`

Result: 14/14 cases passed.

## All Cases

| Case | Category | Mode | Source | Pass | Agent | Tools | Children | Elapsed |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| workflow_hdf5_overview | tooling | auto | dspy | yes | data | hdf5_analyze_file, hdf5_list_datasets | 0 | 14.6s |
| workflow_parquet_profile | analysis | auto | dspy | yes | analysis | parquet_analyze_schema, parquet_compute_statistics, parquet_compute_statistics, parquet_compute_statistics, parquet_compute_statistics | 0 | 16.6s |
| workflow_memory_followup | memory | auto | dspy | yes | analysis | parquet_analyze_schema, parquet_compute_statistics, parquet_compute_statistics, parquet_compute_statistics, parquet_compute_statistics, parquet_compute_statistics | 0 | 22.7s |
| workflow_csv_event_schema | analysis | auto | dspy | yes | analysis | csv_read_table | 0 | 9.1s |
| workflow_visual_dashboard | visualization | auto | dspy | yes | visualization | plot_summary | 0 | 10.1s |
| hdf5_dataset_focus | tooling | auto | dspy | yes | data | hdf5_analyze_dataset | 0 | 4.0s |
| cross_file_triage_nanoagents | multi-agent | auto | guard | yes | analysis | hdf5_analyze_file, hdf5_list_datasets, adios_inspect_file, parquet_analyze_schema, parquet_compute_statistics, csv_read_table | 4 | 0.5s |
| reasoning_cross_file_triage_nanoagents | planner-hardening | reasoning_only | dspy | yes | analysis | hdf5_analyze_file, hdf5_list_datasets, adios_inspect_file, parquet_analyze_schema, parquet_compute_statistics, csv_read_table | 4 | 2.5s |
| adios_bp5_container | tooling | auto | guard | yes | data | adios_inspect_file | 0 | 0.5s |
| reasoning_adios_bp5_container | planner-hardening | reasoning_only | dspy | yes | data | adios_inspect_file | 0 | 6.5s |
| dirty_parquet_quality | analysis | auto | dspy | yes | analysis | parquet_analyze_schema, parquet_compute_statistics, parquet_compute_statistics, parquet_compute_statistics, parquet_compute_statistics | 0 | 2.5s |
| ndp_catalog_discovery | external-catalog | auto | dspy | yes | analysis | ndp_search_datasets, ndp_search_datasets, ndp_search_datasets, ndp_search_datasets, ndp_search_datasets, ndp_search_datasets | 0 | 39.8s |
| visual_scatter_artifact | visualization | auto | dspy | yes | visualization | plot_scatter | 0 | 5.5s |
| missing_hdf5_error | hardening | auto | dspy | yes | data | hdf5_list_datasets | 0 | 5.0s |

## Best 10 Demo Prompts

### 1. Cross-file triage with tier-3 workers

Case: `cross_file_triage_nanoagents`
Category: multi-agent
Routing mode: `auto`
Status: pass
Selected agent: `analysis`
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
Tools: hdf5_analyze_file, hdf5_list_datasets, adios_inspect_file, parquet_analyze_schema, parquet_compute_statistics, csv_read_table
Child sessions: csv_validator subagent, analysis_validator subagent, adios_validator subagent, data_validator subagent
Artifacts: none
Elapsed: 2.5s

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

### 3. Memory follow-up without repeating path

Case: `workflow_memory_followup`
Category: memory
Routing mode: `auto`
Status: pass
Selected agent: `analysis`
Tools: parquet_analyze_schema, parquet_compute_statistics, parquet_compute_statistics, parquet_compute_statistics, parquet_compute_statistics, parquet_compute_statistics
Child sessions: none
Artifacts: none
Elapsed: 22.7s

Prompt:

```text
Based on the Parquet file we just profiled, compute whatever schema or column statistics you need for a quick anomaly triage view. Do not ask me for the path again.
```

What to see: CLIO resolves the previously profiled Parquet file from session context.

Why this is interesting: Demonstrates session memory and current-file resolution instead of copy/paste paths.

Observed excerpt:

```text
**Facility Measurements – Quick Anomaly‑Triaging View**

| Aspect | Details |
|--------|---------|
| **File** | `D:\Libraries\Documents\projects\clio-agent\tmp\clio-benchmark-data\facility_measurements.parquet` |
| **Rows / Row‑Groups** | 3 000 rows, 8 row groups |
| **File size** | 154 818 bytes |
| **Created by** | parquet‑cpp‑arrow v22.0.0 |
| **Key‑value metadata** | `scenario = clio-stress-benchmark`  <br> `source = deterministic` |
| **Schema** | <ul><li>`sample_id` – **int64** (nullable)</li><li>`run_id` – **string** (nullable)</li><li>`site` – **string** (nullable)</li><li>`temperature_k` – **double** (nullable)</li><li>`pressure_pa` – **double** (nullable)</li><li>`humidity_pct` – **double** (nullable)</li><li>`vibration_mm_s` – **double** (nullable)</li><li>`anomaly_score` – **double** (nullable)</li></ul> |

### Column‑wise Statistics (all 3 000 values non‑null)

| Column | M
```

### 4. Follow-up visualization artifact

Case: `workflow_visual_dashboard`
Category: visualization
Routing mode: `auto`
Status: pass
Selected agent: `visualization`
Tools: plot_summary
Child sessions: none
Artifacts: D:\Libraries\Documents\projects\clio-agent\tmp\clio-benchmark-data\facility_measurements_dashboard.png, D:\\Libraries\\Documents\\projects\\clio-agent\\tmp\\clio-benchmark-data\\facility_measurements_dashboard.png
Elapsed: 10.1s

Prompt:

```text
Create a compact PNG dashboard from the Parquet file we just profiled. Tell me where it was saved and what the chart is summarizing.
```

What to see: Visualization expert resolves prior Parquet context and creates a PNG artifact.

Why this is interesting: Shows multi-turn handoff from analysis to visualization with a real saved artifact.

Observed excerpt:

```text
The dashboard was saved to D:\\Libraries\\Documents\\projects\\clio-agent\\tmp\\clio-benchmark-data\\facility_measurements_dashboard.png and provides a compact 2×2 summary of the facility_measurements data, showing histograms of the sensor columns (temperature, pressure, humidity, vibration, anomaly_score) and a scatter plot to aid quick anomaly triage.
```

### 5. Targeted scatter plot

Case: `visual_scatter_artifact`
Category: visualization
Routing mode: `auto`
Status: pass
Selected agent: `visualization`
Tools: plot_scatter
Child sessions: none
Artifacts: D:\Libraries\Documents\projects\clio-agent\tmp\clio-benchmark-data\facility_measurements_scatter.png, D:\\Libraries\\Documents\\projects\\clio-agent\\tmp\\clio-benchmark-data\\facility_measurements_scatter.png
Elapsed: 5.5s

Prompt:

```text
Create a scatter plot from D:\Libraries\Documents\projects\clio-agent\tmp\clio-benchmark-data\facility_measurements.parquet with vibration_mm_s on the x-axis and anomaly_score on the y-axis. Save it as a PNG and explain what relationship the plot is meant to reveal.
```

What to see: Visualization expert chooses plot_scatter and saves a PNG artifact.

Why this is interesting: Checks whether a specific visualization intent maps to the right chart tool.

Observed excerpt:

```text
The scatter plot saved at D:\\Libraries\\Documents\\projects\\clio-agent\\tmp\\clio-benchmark-data\\facility_measurements_scatter.png visualizes vibration_mm_s versus anomaly_score, showing whether higher vibration amplitudes are associated with higher anomaly scores.
```

### 6. Parquet facility profile

Case: `workflow_parquet_profile`
Category: analysis
Routing mode: `auto`
Status: pass
Selected agent: `analysis`
Tools: parquet_analyze_schema, parquet_compute_statistics, parquet_compute_statistics, parquet_compute_statistics, parquet_compute_statistics
Child sessions: none
Artifacts: none
Elapsed: 16.6s

Prompt:

```text
Profile the facility measurements in D:\Libraries\Documents\projects\clio-agent\tmp\clio-benchmark-data\facility_measurements.parquet. I care about schema, row groups, and whether temperature_k, pressure_pa, humidity_pct, and anomaly_score look sane.
```

What to see: Analysis expert reads Parquet schema and computes statistics for named fields.

Why this is interesting: Checks statistical tool calls and model feedback from multiple numeric observations.

Observed excerpt:

```text
The Parquet file has 10 columns (including sample_id int64, run_id string, site string, temperature_k double, pressure_pa double, humidity_pct double, vibration_mm_s double, anomaly_score double), 3000 rows, 8 row groups; temperature_k ranges 267‑321 K (mean ≈ 294 K), pressure_pa 98 k‑104 k Pa (mean ≈ 101 k Pa), humidity_pct 4‑96 % (mean ≈ 45 %), and anomaly_score 0.056‑6.35 (mean ≈ 1.6), all with zero nulls and appear physically plausible.
```

### 7. Dirty Parquet quality review

Case: `dirty_parquet_quality`
Category: analysis
Routing mode: `auto`
Status: pass
Selected agent: `analysis`
Tools: parquet_analyze_schema, parquet_compute_statistics, parquet_compute_statistics, parquet_compute_statistics, parquet_compute_statistics
Child sessions: none
Artifacts: none
Elapsed: 2.5s

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

### 8. NDP catalog discovery

Case: `ndp_catalog_discovery`
Category: external-catalog
Routing mode: `auto`
Status: pass
Selected agent: `analysis`
Tools: ndp_search_datasets, ndp_search_datasets, ndp_search_datasets, ndp_search_datasets, ndp_search_datasets, ndp_search_datasets
Child sessions: none
Artifacts: none
Elapsed: 39.8s

Prompt:

```text
Find a few NOAA or climate-related datasets in the National Data Platform catalog that might complement this facility data. Summarize what you found and what I should verify before download.
```

What to see: Analysis expert calls NDP tools through the CLIO gateway.

Why this is interesting: Exercises external catalog tools and tool-result feedback beyond local files.

Observed excerpt:

```text
All six attempts to search the National Data Platform (NDP) for NOAA or climate‑related datasets returned empty results – the `ndp_search_datasets` tool reported a `count` of 0 and no items were listed. This means that, with the queries used, the NDP did not surface any matching datasets.

**What to verify before trying again**
1. **Search terms** – Make sure you are using keywords that appear in the dataset titles, descriptions, or tags (e.g., "NOAA", "climate", "temperature", "precipitation", "GFS", "NCEI").
2. **Filters** – Check whether any filters (such as `category`, `spatial_coverage`, `temporal_coverage`, or `license`) were unintentionally set to restrictive values that exclude results.
3. **Scope** – Confirm you are querying the correct NDP server (global vs. regional) and that the datasets you need are actually cataloged in the NDP (some NOAA data may be hosted on separate por
```

### 9. HDF5 fusion file overview

Case: `workflow_hdf5_overview`
Category: tooling
Routing mode: `auto`
Status: pass
Selected agent: `data`
Tools: hdf5_analyze_file, hdf5_list_datasets
Child sessions: none
Artifacts: none
Elapsed: 14.6s

Prompt:

```text
I need to brief collaborators on this fusion output: D:\Libraries\Documents\projects\clio-agent\tmp\clio-benchmark-data\fusion_run.h5. What datasets are inside, what shapes and units matter, and what compression details should I mention?
```

What to see: Data expert uses HDF5 tools and summarizes datasets, units, and compression.

Why this is interesting: Proves real HDF5 path handling, tool argument generation, and grounded synthesis.

Observed excerpt:

```text
The HDF5 file contains seven datasets—axes/diagnostic_channel (12 int32), axes/radius_norm (64 float64), axes/time_hours (96 float64), diagnostics/heat_flux (96×12 float32, units MW/m²), plasma/density (96×64 float64, units m⁻³), plasma/electron_temperature (96×64 float32, units eV), quality/flags (96×12 int8)—with five datasets compressed (overall compression ratio 1.12) and two uncompressed.
```

### 10. No-guard ADIOS/BP5 route

Case: `reasoning_adios_bp5_container`
Category: planner-hardening
Routing mode: `reasoning_only`
Status: pass
Selected agent: `data`
Tools: adios_inspect_file
Child sessions: none
Artifacts: none
Elapsed: 6.5s

Prompt:

```text
This ADIOS BP5 output came from a Gray-Scott run: "D:\Libraries\Documents\projects\clio-agent\tmp\clio-benchmark-data\gray scott noise 0.01 data.bp5". Tell me what the container looks like, whether profiling metadata is present, and what extra runtime is needed if variable-level metadata is unavailable.
```

What to see: With routing guards disabled, the planner still selects the data expert.

Why this is interesting: Checks that BP5 routing is not only a hardcoded suffix guard behavior.

Observed excerpt:

```text
The BP5 container is a directory with five members (data.0, md.0, md.idx, mmd.0, profiling.json), includes profiling metadata, and you need to install the ADIOS2 Python bindings to access variable‑level metadata.
```
