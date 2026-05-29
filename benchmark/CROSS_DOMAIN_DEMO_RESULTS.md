# CLIO Cross-Domain Demo Results

This document is the short human-facing record for the current real
cross-domain CLIO demo evidence. Generated reports and raw JSONL evidence are
kept alongside this summary:

- `benchmark/LIVE_CROSS_DOMAIN_REPORT.md` /
  `benchmark/LIVE_CROSS_DOMAIN_EVIDENCE.jsonl`
- `benchmark/NDP_WAVEFORM_REPORT.md` /
  `benchmark/NDP_WAVEFORM_EVIDENCE.jsonl`
- `benchmark/VISUAL_MULTITURN_REPORT.md` /
  `benchmark/VISUAL_MULTITURN_EVIDENCE.jsonl`
- `benchmark/CROSS_FILE_DIRTY_REPORT.md` /
  `benchmark/CROSS_FILE_DIRTY_EVIDENCE.jsonl`

## Run

- Date: 2026-05-28
- Backend: live `clio-agent-gact`
- Provider: `codex` / `gpt-5.5`
- Lane: `real_orchestrator`
- Result: 5/5 selected workflows passed
- Route source: all selected cases used `dspy`, not guard or keyword shortcuts

Command:

```bash
uv run python scripts/run_demo_benchmark.py \
  --base-url http://127.0.0.1:17960 \
  --case genomics_reference_variant_review \
  --case materials_cif_structure_review \
  --case geospatial_field_site_review \
  --case microscopy_png_readiness_review \
  --case mass_spec_mzml_qc_review \
  --output-jsonl benchmark/LIVE_CROSS_DOMAIN_EVIDENCE.jsonl \
  --report benchmark/LIVE_CROSS_DOMAIN_REPORT.md
```

## Passing Workflows

### Genomics Reference And Variant Review

Agent: `genomics`

Prompt:

```text
Review this synthetic pathogen reference FASTA and variant call file: tmp/clio-benchmark-data/pathogen_reference.fasta and tmp/clio-benchmark-data/pathogen_sample_variants.vcf. Summarize the reference composition, the variant types and effects, and what a collaborator should verify before treating the sample as analysis-ready.
```

Worked because CLIO selected the genomics expert and called both
`genomics_inspect_fasta` and `genomics_summarize_vcf`. Evidence included
contigs `chrA` and `plasmidB`, base composition, variant effects, and
collaborator verification guidance.

### Materials CIF Structure Review

Agent: `materials`

Prompt:

```text
Review this crystal structure file for collaborator handoff: tmp/clio-benchmark-data/strontium_titanate.cif. Summarize the unit cell, symmetry, atom species, and any density or occupancy checks that should be verified before simulation setup.
```

Worked because CLIO selected the materials expert and called
`materials_inspect_cif`. Evidence included formula `SrTiO3`, space group
`P m -3 m`, unit-cell parameters, species counts, and occupancy/density
checks.

### Geospatial Field-Site Review

Agent: `geospatial`

Prompt:

```text
Review this field-site GeoJSON for spatial analysis readiness: tmp/clio-benchmark-data/field_sites.geojson. Summarize the feature types, coordinate bounds, key properties, and what a collaborator should verify before using it in a map overlay.
```

Worked because CLIO selected the geospatial expert and called
`geospatial_inspect_geojson`. Evidence included Point, LineString, and
Polygon features, coordinate bounds, property keys, and map-overlay caveats.

### Microscopy PNG Readiness Review

Agent: `imaging`

Prompt:

```text
Review this microscopy-style PNG for collaborator handoff: tmp/clio-benchmark-data/microscopy_cells.png. Summarize the image dimensions, intensity range, foreground estimate, region evidence, and what acquisition metadata should be verified before quantitative analysis.
```

Worked because CLIO selected the imaging expert and called
`imaging_inspect_png`. Evidence included PNG dimensions, intensity range,
foreground estimate, connected regions, and acquisition metadata checks. The
artifact path was verified on disk.

### Mass Spectrometry mzML QC Review

Agent: `mass_spec`

Prompt:

```text
Review this proteomics mzML run for collaborator handoff: tmp/clio-benchmark-data/proteomics_qc.mzML. Summarize the spectra, MS-level balance, m/z coverage, intensity/TIC evidence, and what acquisition metadata should be verified before peptide-search analysis.
```

Worked because CLIO selected the mass-spec expert and called
`mass_spec_inspect_mzml`. Evidence included spectrum count, MS-level
distribution, m/z range, peak count, scan IDs, and total ion current summary.

### NDP Seismic Waveform To Plot

Agent path: `visualization -> data -> ndp_catalog -> analysis -> sac_format -> visualization`

Prompt:

```text
Find a bounded seismic waveform dataset from a seismological or Earth-science organization in the National Data Platform. Choose a usable resource, stage it if it is small enough, inspect the waveform content, compute representative trace statistics, and produce a plot artifact. If a candidate is too large or unavailable, surface that as the result instead of inventing a plot.
```

Worked because CLIO used `ndp_*` catalog/staging tools, recovered from
unavailable Hive resources through a bounded EarthScope waveform fetch,
inspected SAC content with `sac_inspect_archive`, computed trace statistics,
and produced a verified PNG artifact.

### CSV Status Distribution Chart

Agent: `visualization`

Prompt:

```text
Create a PNG bar chart of the event status distribution from the CSV stream we just inspected. Tell me where it was saved and what field was plotted.
```

Worked because the setup turn inspected `sensor_events.csv`, the follow-up turn
stayed on the root CLIO orchestrator, CLIO selected visualization with
`route_source=dspy`, called `plot_bar_chart`, and verified
`event_status_distribution.png` on disk.

### Dirty Data Dashboard After Quality Review

Agent: `visualization`

Prompt:

```text
Create a compact dashboard PNG for the dirty Parquet export we just reviewed. Use it to support the quality review, and tell me where the artifact was saved.
```

Worked because the setup turn reviewed the dirty Parquet export, the follow-up
turn recovered the reviewed file context, called `plot_summary`, and verified a
real dashboard PNG on disk.

### Dirty Cross-File Quality Gate

Agent path: `analysis -> [csv_validator, analysis_validator, adios_validator, data_validator]`

Prompt:

```text
Before I share this run, build a quality gate across tmp/clio-benchmark-data/fusion_run.h5, tmp/clio-benchmark-data/facility_measurements_dirty.parquet, tmp/clio-benchmark-data/sensor_events.csv, and "tmp/clio-benchmark-data/gray scott noise 0.01 data.bp5". I need to know what each file proves, where the dirty tabular export is risky, and which checks block collaborator handoff.
```

Worked because CLIO selected `analysis` through `route_source=dspy`, spawned
four tool-backed child sessions, and called HDF5, ADIOS/BP5, dirty Parquet, and
CSV tools. The run recorded `branch_count=4`, six tool calls, and the final
answer synthesized each file's evidence into a collaborator handoff gate.

## Fixes Made During This Pass

- #408 fixed scientific path extraction for new domain suffixes.
- #410 allowed PNG inspection to accept planner-generated `threshold: null`.
- #412 exposed explicit total-ion-current fields in mzML tool output.
- #414 added readable mass-spec QC wording for MS-level and total ion current
  evidence.
- #416 counted PNG scientific inputs as data files.
- #418 filtered benchmark path evidence so scientific slash terms like `m/z`
  are not recorded as fake files.
- #439 made the NDP waveform benchmark reach real SAC inspection/statistics and
  a verified PNG artifact.
- #446 fixed the benchmark runner so all benchmark lanes pin turns to the root
  CLIO orchestrator unless a case explicitly overrides the agent. This avoided
  accidental execution through a non-executable dynamic session agent.
- #447 fixed visualization default output paths so tool-loop charts land under
  the CLIO artifact root instead of the repo root.

## Remaining Gaps

Current committed evidence proves five single-expert cross-domain workflows, one
deep NDP/SAC/visualization workflow, two multi-turn visualization artifact
workflows, and one cross-file nanoagent fanout workflow. It does not yet
complete the full umbrella benchmark target. Still needed:

- at least one more multi-branch workflow with child sessions or tier-3 expert
  handoffs;
- more NDP-backed workflows outside the seismic happy path;
- more visualization artifacts from analyzed data if the final target is ten
  workflows rather than the minimum five;
- a full fresh-machine run using the documented commands.
