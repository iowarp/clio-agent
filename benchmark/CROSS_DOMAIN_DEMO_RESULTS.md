# CLIO Cross-Domain Demo Results

This document is the short human-facing index for the current real
cross-domain CLIO demo evidence. The benchmark itself is not a pytest suite and
is not just a pass-count table. The intended workflow is:

1. Run natural prompts through a normal CLIO session with a real provider.
2. Read the recorded JSONL row for each case.
3. Confirm the route, tool calls, root/child session logs, artifacts, errors,
   and final answer all agree.

Generated reports and raw JSONL evidence are the source of truth and are kept
alongside this summary:

- `benchmark/LIVE_CROSS_DOMAIN_REPORT.md` /
  `benchmark/LIVE_CROSS_DOMAIN_EVIDENCE.jsonl`
- `benchmark/NDP_WAVEFORM_REPORT.md` /
  `benchmark/NDP_WAVEFORM_EVIDENCE.jsonl`
- `benchmark/VISUAL_MULTITURN_REPORT.md` /
  `benchmark/VISUAL_MULTITURN_EVIDENCE.jsonl`
- `benchmark/CROSS_FILE_DIRTY_REPORT.md` /
  `benchmark/CROSS_FILE_DIRTY_EVIDENCE.jsonl`
- `benchmark/CROSS_FILE_TRIAGE_REPORT.md` /
  `benchmark/CROSS_FILE_TRIAGE_EVIDENCE.jsonl`
- `benchmark/FRESH_REAL_ORCHESTRATOR_REPORT.md` /
  `benchmark/FRESH_REAL_ORCHESTRATOR_EVIDENCE.jsonl`

## Evidence Runs

- Date: 2026-05-29
- Backend: fresh isolated live `clio-agent-gact`
- Provider: `codex` / `gpt-5.5`
- Lane: `real_orchestrator`
- Result metadata: the refreshed fresh-lane replay passed 12/12 selected cases,
  captured root session logs for every case, captured eight child-session logs,
  and verified four on-disk artifacts.
- Route source: selected cases used `dspy`, not guard or keyword shortcuts.
- Hierarchy check: no refreshed case selected child experts such as
  `ndp_catalog` or `sac_format` as the public top-level route.

Command:

```bash
uv run python scripts/run_demo_benchmark.py \
  --require-lane-criteria \
  --base-url http://127.0.0.1:17966 \
  --data-dir tmp/fresh-session-log-benchmark-data \
  --output-jsonl benchmark/FRESH_REAL_ORCHESTRATOR_EVIDENCE.jsonl \
  --report benchmark/FRESH_REAL_ORCHESTRATOR_REPORT.md
```

## Review Standard

For each workflow, inspect the matching JSONL row before trusting the prose
summary. A workflow is only a real benchmark pass when the log shows:

- a natural prompt sent through the normal CLIO session path;
- a non-shortcut route source for real-orchestrator cases;
- tool calls with grounded arguments, results, errors, durations, and artifact
  paths when relevant;
- root session logs and child-session or handoff provenance for multi-branch
  cases;
- final assistant text that cites the same evidence present in the log;
- any failure recorded as an explicit failure point, followed by a tracked fix
  or an honest remaining gap.

Pytest output can prove that the runner, fixture generator, and individual
tools are not broken. It does not prove that the benchmark worked.

## Acceptance Matrix

| Requirement | Status | Evidence |
| --- | --- | --- |
| 5-10 completed workflows | Met | `FRESH_REAL_ORCHESTRATOR_EVIDENCE.jsonl` records 12 selected real-orchestrator cases, all passing. The human prompt book still treats ten as the core stretch set; the replay includes two extra lane guards. |
| At least four distinct domains | Met | Genomics, materials, geospatial, imaging, mass spectrometry, seismic/NDP/SAC, CSV/Parquet/HDF5, and ADIOS/BP5 all have recorded evidence rows. |
| Real orchestrator/provider/tools/data | Met for recorded rows | Each fresh JSONL row records `provider=codex`, `model=gpt-5.5`, `route_source=dspy`, selected agent, tool calls, input files, elapsed time, outcome, and full root session log. |
| At least two multi-hop or fanout workflows | Met | `ndp_seismic_waveform_to_plot` records a five-expert path. `cross_file_dirty_quality_gate_nanoagents` and `reasoning_cross_file_triage_nanoagents` each record four child sessions and six tool calls. |
| At least two real visual artifacts | Met | `ndp_seismic_waveform_to_plot`, `csv_status_visual_summary`, and `dirty_quality_dashboard_multi_turn` each record verified PNG artifacts with nonzero byte sizes. |
| At least two workflows needed new or materially improved tool support | Met | The NDP waveform workflow required SAC/EarthScope tooling and handoffs fixed in #439/#443. Cross-domain workflows required new genomics, materials, geospatial, imaging, and mass-spec tool servers (#397, #399, #401, #403, #405) plus mzML/PNG fixes (#410/#412). Visualization artifact placement was corrected in #447. |
| Per-workflow provenance fields | Met for recorded rows | JSONL rows include route graph/metrics, route source, experts/handoffs, branch count, tool calls, elapsed time, files, artifacts, provider/model, pass/fail outcome, root session logs, and child-session logs when spawned. |
| Fresh-machine runnable evidence | Met | `FRESH_REAL_ORCHESTRATOR_EVIDENCE.jsonl` records a clean replay from isolated XDG config, isolated ARC working directory, fresh generated data, and a clean artifact root. The replay passed 12/12 selected real-orchestrator cases, captured 12/12 root logs and eight child-session logs, and verified four artifacts. |
| Honest final report | Met with caveats | This summary lists fixes made and remaining weaknesses. Current remaining weakness: broader non-seismic NDP breadth if that becomes a release gate. |

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

Agent path: `orchestrator -> data -> ndp_catalog -> data -> orchestrator -> analysis -> sac_format -> analysis -> orchestrator -> visualization -> orchestrator`

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

### No-Guard Cross-File Triage

Agent path: `analysis -> [csv_validator, analysis_validator, adios_validator, data_validator]`

Prompt:

```text
I have four related files from the same experiment: tmp/clio-benchmark-data/fusion_run.h5, tmp/clio-benchmark-data/facility_measurements.parquet, tmp/clio-benchmark-data/sensor_events.csv, and "tmp/clio-benchmark-data/gray scott noise 0.01 data.bp5". Give me a cross-file triage summary: what is in each file, whether the measurements look ready for downstream analysis, and what I should check next.
```

Worked because CLIO selected `analysis` through `route_source=dspy` even with
shortcut routing disabled, spawned four tool-backed child sessions, and called
HDF5, ADIOS/BP5, Parquet, and CSV tools. The run recorded `branch_count=4`,
six tool calls, and a final triage answer grounded in the logged file evidence.

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
- #450 recorded real-session evidence for the dirty cross-file nanoagent
  fanout case.
- #452 clarified that the benchmark summary is an index over JSONL session
  evidence, not a pytest-style result.
- #454 recorded real-session evidence for the no-guard cross-file triage
  nanoagent case.
- #459 fixed fresh-install NDP catalog fallback when the published clio-kit
  `uvx` entry point is unavailable.
- #461 separated optional extended stress coverage from the real-orchestrator
  benchmark pass/fail gate.
- #458 recorded a clean fresh-directory replay of the documented
  `real_orchestrator` lane: 12/12 clean passes, `route_source=dspy` for every
  case, four verified artifacts, max expert depth 5, and max branch fanout 4.

## Remaining Gaps

Current committed evidence proves five single-expert cross-domain workflows, one
deep NDP/SAC/visualization workflow, two multi-turn visualization artifact
workflows, and two cross-file nanoagent fanout workflows. It proves at least two
multi-hop or fanout paths through recorded session evidence:

- `ndp_seismic_waveform_to_plot`: multi-expert path through NDP catalog, SAC
  analysis, and visualization.
- `cross_file_dirty_quality_gate_nanoagents`: four child sessions and six
  file/tool inspections.
- `reasoning_cross_file_triage_nanoagents`: four child sessions and six
  file/tool inspections with shortcut routing disabled.

Remaining caveat before treating this as broad NDP coverage:

- more NDP-backed workflows outside the seismic happy path if NDP breadth is a
  release criterion.
