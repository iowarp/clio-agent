# Real Provider Benchmark Prompts

These prompts are the reusable CLIO scientific benchmark family. They are
collaborator-style prompts: the user does not name internal tools, MCP servers,
or hidden implementation details. The benchmark expects CLIO to route through
the agent hierarchy, call real tools, preserve session context, create artifacts
where requested, and surface structured errors instead of inventing results.

The historical ALCF evidence run used `argonne / gpt-oss-120b` and produced
`19/21` clean passes, `2` expected surfaced errors, and `0` failures. That run is
recorded in `docs/ALCF_DEMO_BENCHMARK_REPORT.md`; this file is the canonical
provider-neutral prompt book.

## Path Placeholders

Replace the placeholders with absolute or workspace-relative paths for the
current run:

- `{h5}` - fusion HDF5 file.
- `{parquet}` - clean facility Parquet file.
- `{dirty}` - dirty facility Parquet file.
- `{csv}` - sensor/event CSV file.
- `{adios}` - ADIOS/BP5 Gray-Scott output.
- `{fasta}` - synthetic pathogen FASTA reference.
- `{vcf}` - synthetic pathogen VCF variant calls.
- `{cif}` - synthetic strontium titanate CIF structure.
- `{geojson}` - synthetic field-site GeoJSON features.

## Primary Prompt Set

### 1. Cross-File Triage With Tier-3 Workers

Case id: `cross_file_triage_nanoagents`

Category: `multi-agent`

Expected route: `analysis -> [data_validator, analysis_validator, csv_validator, adios_validator]`

Expected evidence:

- Child sessions or tier-3 worker provenance.
- HDF5, Parquet, CSV, and ADIOS/BP5 tool calls.
- Aggregated readiness and follow-up checks.

Prompt:

```text
I have four related files from the same experiment: {h5}, {parquet}, {csv}, and "{adios}". Give me a cross-file triage summary: what is in each file, whether the measurements look ready for downstream analysis, and what I should check next.
```

Why this matters:

This is the main hierarchy stress prompt. It proves CLIO can decompose a natural
multi-file request into specialist worker tasks and synthesize their tool-backed
findings.

### 2. Dirty Cross-File Quality Gate

Case id: `cross_file_dirty_quality_gate_nanoagents`

Category: `multi-agent`

Expected route: `analysis -> [data_validator, analysis_validator, csv_validator, adios_validator]`

Expected evidence:

- Child sessions or tier-3 worker provenance.
- Dirty Parquet quality findings.
- Cross-file handoff blocker list.

Prompt:

```text
Before I share this run, build a quality gate across {h5}, {dirty}, {csv}, and "{adios}". I need to know what each file proves, where the dirty tabular export is risky, and which checks block collaborator handoff.
```

Why this matters:

This is the harder collaborator-review variant. It tests whether CLIO can move
from generic summary into quality-gate semantics over intentionally dirty data.

### 3. No-Guard Cross-File Triage

Case id: `reasoning_cross_file_triage_nanoagents`

Category: `planner-hardening`

Routing mode: `reasoning_only`

Expected route: `analysis -> [data_validator, analysis_validator, csv_validator, adios_validator]`

Prompt:

```text
I have four related files from the same experiment: {h5}, {parquet}, {csv}, and "{adios}". Give me a cross-file triage summary: what is in each file, whether the measurements look ready for downstream analysis, and what I should check next.
```

Why this matters:

This separates planner ability from production routing guards. The benchmark
should not only pass because suffix guards or hardcoded routing shortcuts force
the answer.

### 4. NDP Seismic Waveform Discovery To Plot

Case id: `ndp_seismic_waveform_to_plot`

Category: `hierarchical-science`

Expected route: `visualization -> ndp_catalog -> analysis -> sac_format -> visualization`

Expected evidence:

- NDP catalog discovery through `ndp_*` tools.
- Bounded resource staging or a structured "too large/unavailable" result.
- SAC archive inspection.
- Representative trace statistics.
- PNG plot artifact when a usable resource is staged.

Prompt:

```text
Find a bounded seismic waveform dataset from a seismological or Earth-science organization in the National Data Platform. Choose a usable resource, stage it if it is small enough, inspect the waveform content, compute representative trace statistics, and produce a plot artifact. If a candidate is too large or unavailable, surface that as the result instead of inventing a plot.
```

Why this matters:

This is the core hierarchical science benchmark. It crosses external catalog
search, data access, format-specific analysis, and visualization without the
user naming internal experts.

### 5. HDF5 Fusion File Overview

Case id: `workflow_hdf5_overview`

Category: `tooling`

Expected route: `data`

Expected evidence:

- HDF5 tool calls.
- Dataset names, shapes, units, and compression details.
- Grounded synthesis over real tool results.

Prompt:

```text
I need to brief collaborators on this fusion output: {h5}. What datasets are inside, what shapes and units matter, and what compression details should I mention?
```

Why this matters:

This is the cleanest single-file grounding test. It catches fake HDF5 summaries,
bad path handling, and weak tool-result synthesis.

### 5b. Genomics Reference And Variant Review

Case id: `genomics_reference_variant_review`

Category: `genomics`

Expected route: `genomics`

Expected evidence:

- FASTA inspection through `genomics_inspect_fasta`.
- VCF variant summary through `genomics_summarize_vcf`.
- Grounded discussion of reference composition and variant effects.

Prompt:

```text
Review this synthetic pathogen reference FASTA and variant call file: {fasta} and {vcf}. Summarize the reference composition, the variant types and effects, and what a collaborator should verify before treating the sample as analysis-ready.
```

Why this matters:

This is the first genomics benchmark case. It adds a non-NDP domain and proves
that CLIO can grow new tool-backed expert boundaries beyond existing
HDF5/Parquet/ADIOS/NDP/SAC paths.

### 5c. Materials CIF Structure Review

Case id: `materials_cif_structure_review`

Category: `materials`

Expected route: `materials`

Expected evidence:

- CIF structure inspection through `materials_inspect_cif`.
- Unit-cell, space-group, and atom-site/species summary.
- Grounded collaborator handoff checks around occupancy, provenance, or density.

Prompt:

```text
Review this crystal structure file for collaborator handoff: {cif}. Summarize the unit cell, symmetry, atom species, and any density or occupancy checks that should be verified before simulation setup.
```

Why this matters:

This adds a non-NDP materials science domain with a distinct scientific file
format and a new tool-backed expert boundary.

### 5d. Geospatial Field-Site Review

Case id: `geospatial_field_site_review`

Category: `geospatial`

Expected route: `geospatial`

Expected evidence:

- GeoJSON inspection through `geospatial_inspect_geojson`.
- Geometry counts, bounding box, and property keys.
- Grounded warnings about CRS assumptions or property completeness.

Prompt:

```text
Review this field-site GeoJSON for spatial analysis readiness: {geojson}. Summarize the feature types, coordinate bounds, key properties, and what a collaborator should verify before using it in a map overlay.
```

Why this matters:

This adds a non-NDP geospatial domain with coordinate and geometry semantics,
not just generic JSON inspection.

### 6. Provider Swap Preserves Session Context

Case id: `provider_swap_memory_followup`

Category: `provider-hardening`

Setup prompt:

```text
Profile {parquet} for a provider-swap test. Record the path, schema, and basic temperature_k and pressure_pa facts so a later model can continue.
```

Action before benchmark prompt:

- Switch to the alternate configured provider/model.

Expected route: `analysis`

Expected evidence:

- Parquet schema/statistics tools after the provider switch.
- Answer acknowledges the prior table context without asking for the path again.
- No stale-provider or empty-turn behavior.

Prompt:

```text
The provider/model has changed. Continue from the facility Parquet table we just profiled, compute any statistics you need for temperature and pressure, and tell me whether the session context survived the swap.
```

Why this matters:

Provider/model swaps have historically broken active sessions. This prompt
forces model replacement, memory continuity, and real tool use in one turn.

### 7. Context Pressure Plus Explicit Compaction

Case id: `context_pressure_compaction_followup`

Category: `memory-hardening`

Setup prompts:

```text
Build a detailed evidence note from {h5}: include every dataset name, shape, units, compression, and at least one risk or follow-up check.
```

```text
Now add a detailed evidence note from {parquet}: include row-group facts, schema, and statistics for temperature_k, pressure_pa, humidity_pct, vibration_mm_s, and anomaly_score.
```

```text
Now add a detailed evidence note from {csv}: include the event columns, status semantics, operator notes, and any timestamp caveats.
```

```text
Now add a detailed evidence note from the BP5 run at "{adios}": include container/profiling information and dependency caveats.
```

Action before benchmark prompt:

- Run explicit session compaction.

Expected route: `analysis`

Expected evidence:

- Response cites retained HDF5, Parquet, CSV, and BP5 evidence.
- Answer preserves scientific identifiers after compaction.
- No request to repeat the source paths.

Prompt:

```text
After the compaction step, use the retained evidence to decide whether the experiment looks ready for collaborator review. Cite the strongest evidence from the HDF5, Parquet, CSV, and BP5 stages, and name what still needs checking.
```

Why this matters:

This makes memory compaction a benchmarked semantic contract rather than an
untested UI operation.

### 8. CSV Status Distribution Chart

Case id: `csv_status_visual_summary`

Category: `visualization`

Setup prompt:

```text
Inspect the CSV event stream at {csv}. Record the columns and which field represents event status.
```

Expected route: `visualization`

Expected evidence:

- Resolves prior CSV context.
- Calls charting tools for the `status` field.
- Produces a PNG artifact path.

Prompt:

```text
Create a PNG bar chart of the event status distribution from the CSV stream we just inspected. Tell me where it was saved and what field was plotted.
```

Why this matters:

This tests analysis-to-visualization handoff over CSV context, not only Parquet
dashboards.

### 9. Dirty Data Dashboard After Quality Review

Case id: `dirty_quality_dashboard_multi_turn`

Category: `visualization`

Setup prompt:

```text
Review the suspicious Parquet export at {dirty}. Record the schema, quality_flag, temperature_k, pressure_pa, and any quality concerns.
```

Expected route: `visualization`

Expected evidence:

- Resolves the dirty Parquet file from session context.
- Calls dashboard/summary plotting.
- Produces a PNG artifact path.

Prompt:

```text
Create a compact dashboard PNG for the dirty Parquet export we just reviewed. Use it to support the quality review, and tell me where the artifact was saved.
```

Why this matters:

This stresses dirty-data review, memory, and visualization in sequence.

### 10. NDP Catalog Discovery

Case id: `ndp_catalog_discovery`

Category: `external-catalog`

Expected route: `data` or `ndp_catalog`

Expected evidence:

- NDP organization or dataset search.
- Dataset summaries with verification/download caveats.
- No invented local file analysis before a resource is staged.

Prompt:

```text
Find a few NOAA or climate-related datasets in the National Data Platform catalog that might complement this facility data. Summarize what you found and what I should verify before download.
```

Why this matters:

This is the external-catalog discovery benchmark before staged data is handed to
analysis or visualization experts.

## Secondary Prompt Set

These cases are part of the executable runner but are not in the primary
collaborator-facing top ten.

### Parquet Facility Profile

Case id: `workflow_parquet_profile`

```text
Profile the facility measurements in {parquet}. I care about schema, row groups, and whether temperature_k, pressure_pa, humidity_pct, and anomaly_score look sane.
```

### Memory Follow-Up Without Repeating Path

Case id: `workflow_memory_followup`

```text
Based on the Parquet file we just profiled, compute whatever schema or column statistics you need for a quick anomaly triage view. Do not ask me for the path again.
```

### CSV Event Stream Schema

Case id: `workflow_csv_event_schema`

```text
This event stream came with the run: {csv}. What columns does it contain, and where are the status and operator_note fields?
```

### Follow-Up Visualization Artifact

Case id: `workflow_visual_dashboard`

```text
Create a compact PNG dashboard from the Parquet file we just profiled. Tell me where it was saved and what the chart is summarizing.
```

### Natural HDF5 Dataset Deep Dive

Case id: `hdf5_dataset_focus`

```text
Focus on plasma/electron_temperature inside {h5}. What shape, chunks, compression, and statistics matter if we mostly read it over time?
```

### ADIOS/BP5 Container Inspection

Case id: `adios_bp5_container`

```text
This ADIOS BP5 output came from a Gray-Scott run: "{adios}". Tell me what the container looks like, whether profiling metadata is present, and what extra runtime is needed if variable-level metadata is unavailable.
```

### No-Guard ADIOS/BP5 Route

Case id: `reasoning_adios_bp5_container`

```text
This ADIOS BP5 output came from a Gray-Scott run: "{adios}". Tell me what the container looks like, whether profiling metadata is present, and what extra runtime is needed if variable-level metadata is unavailable.
```

### Dirty Parquet Quality Review

Case id: `dirty_parquet_quality`

```text
This Parquet export looks suspicious: {dirty}. Review it for data quality problems and tell me what fields need attention before downstream analysis.
```

### Targeted Scatter Plot

Case id: `visual_scatter_artifact`

```text
Create a scatter plot from {parquet} with vibration_mm_s on the x-axis and anomaly_score on the y-axis. Save it as a PNG and explain what relationship the plot is meant to reveal.
```

### Missing HDF5 Error Surfacing

Case id: `missing_hdf5_error`

```text
Inspect this HDF5 file and tell me what datasets are inside: {missing_h5}. If the file is unavailable, surface the real error.
```

### Missing CSV Error Surfacing

Case id: `missing_csv_error`

```text
Read this collaborator CSV and summarize the columns: {missing_csv}. If it is unavailable, surface the real error rather than guessing.
```
