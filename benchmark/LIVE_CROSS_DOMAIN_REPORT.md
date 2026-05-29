# CLIO Real-Orchestrator Benchmark Report

Generated: 2026-05-28 20:21:14 CDT
Evidence JSONL: `/home/jcernuda/clio-agent/benchmark/LIVE_CROSS_DOMAIN_EVIDENCE.jsonl`
Benchmark lane: `real_orchestrator`

Result: 5/5 clean passes, 0 expected surfaced errors, 0 expected cancellations, 0 partial recoveries, 0 failures.

Stress coverage: does not yet meet the documented benchmark standard.

## Stress Coverage Audit

| Criterion | Observed | Required | Status |
| --- | ---: | ---: | --- |
| at least ten complex collaborator-grade demos | 1 | 10 | gap |
| at least five long or high-event stress cases | 0 | 5 | gap |
| at least three cases with tier-3 agents or nanoagents | 0 | 3 | gap |
| at least three visualization artifacts from analyzed data | 1 | 3 | gap |
| at least two deliberate surfaced-error cases | 0 | 2 | gap |
| at least one context-pressure or compaction case | 0 | 1 | gap |
| at least one provider/model-swap stress case | 0 | 1 | gap |

## Evidence Summary

- Max elapsed case: `genomics_reference_variant_review` (8.0s)
- Max expert depth: `genomics_reference_variant_review` (1)
- Max branch fanout: `genomics_reference_variant_review` (0)
- Unique tools used: genomics_inspect_fasta, genomics_summarize_vcf, geospatial_inspect_geojson, imaging_inspect_png, mass_spec_inspect_mzml, materials_inspect_cif
- Data/input files referenced: 6
- Artifacts verified on disk: 1/1

## Provider Lane Audit

| Criterion | Observed | Required | Status |
| --- | ---: | ---: | --- |
| all selected cases avoid shortcut route sources | 5 | 5 | pass |
| passing cases include structured route/tool evidence | 5 | 5 | pass |
| artifact-producing cases verify artifacts on disk | 1 | 1 | pass |
| planner multi-file hierarchy case passes | 0 | 1 | gap |
| dirty cross-file quality gate passes | 0 | 1 | gap |
| NDP-to-SAC-to-plot science chain passes | 0 | 1 | gap |

## All Cases

| Case | Category | Mode | Source | Outcome | Agent | Handoffs | Tools | Children | Elapsed |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| genomics_reference_variant_review | genomics | auto | dspy | pass | genomics | genomics x3 | genomics_inspect_fasta, genomics_summarize_vcf | 0 | 8.0s |
| materials_cif_structure_review | materials | auto | dspy | pass | materials | materials x2 | materials_inspect_cif | 0 | 6.0s |
| geospatial_field_site_review | geospatial | auto | dspy | pass | geospatial | geospatial x2 | geospatial_inspect_geojson | 0 | 8.0s |
| microscopy_png_readiness_review | imaging | auto | dspy | pass | imaging | imaging x2 | imaging_inspect_png | 0 | 7.5s |
| mass_spec_mzml_qc_review | mass_spec | auto | dspy | pass | mass_spec | mass_spec x2 | mass_spec_inspect_mzml | 0 | 6.5s |

## Best 10 Demo Prompts

### 1. Genomics reference and variant review

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
Data/input files: /home/jcernuda/clio-agent/tmp/clio-benchmark-data/pathogen_reference.fasta, /home/jcernuda/clio-agent/tmp/clio-benchmark-data/pathogen_sample_variants.vcf
Setup turns: 0
Actions: none
Child sessions: none
Artifacts: none
Artifact evidence: none
Elapsed: 8.0s

Prompt:

```text
Review this synthetic pathogen reference FASTA and variant call file: /home/jcernuda/clio-agent/tmp/clio-benchmark-data/pathogen_reference.fasta and /home/jcernuda/clio-agent/tmp/clio-benchmark-data/pathogen_sample_variants.vcf. Summarize the reference composition, the variant types and effects, and what a collaborator should verify before treating the sample as analysis-ready.
```

What to see: CLIO uses FASTA and VCF genomics tools, then grounds a review in sequence composition and variant effect evidence.

Why this is interesting: Adds a non-NDP, non-HDF5/Parquet domain that requires new domain tools and a new expert boundary.

Observed excerpt:

```text
genomics | success | direct_tool
genomics | success | direct_tool
genomics | success | planner_dispatch | Genomics review: FASTA: {"filepath": "/home/jcernuda/clio-agent/tmp/clio-benchmark-data/pathogen_reference.fasta", "record_count": 2, "total_bases": 6050, "gc_fraction": 0.537851, "base_counts": {"A": 1406, "C": 1673, "G": 1581, "T": 1390, "N": 0}, "longest_record": {"id": "chrA", "length": 4800, "gc_fraction": 0.585833}, "records": {"count": 2, "items": [{"id": "chrA", "description": "synthetic pathogen benchmark reference", "length": 4800, "gc_fraction": 0.585833, "ambiguous_bas...[truncated]
Genomics review:
FASTA: {"filepath": "/home/jcernuda/clio-agent/tmp/clio-benchmark-data/pathogen_reference.fasta", "record_count": 2, "total_bases": 6050, "gc_fraction": 0.537851, "base_counts": {"A": 1406, "C": 1673, "G": 1581, "T": 1390, "N": 0}, "longest_record": {"id": "chrA", "length":
```

### 2. Microscopy PNG readiness review

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
Data/input files: /home/jcernuda/clio-agent/tmp/clio-benchmark-data/microscopy_cells.png
Setup turns: 0
Actions: none
Child sessions: none
Artifacts: /home/jcernuda/clio-agent/tmp/clio-benchmark-data/microscopy_cells.png
Artifact evidence: /home/jcernuda/clio-agent/tmp/clio-benchmark-data/microscopy_cells.png (ok, 614 B)
Elapsed: 7.5s

Prompt:

```text
Review this microscopy-style PNG for collaborator handoff: /home/jcernuda/clio-agent/tmp/clio-benchmark-data/microscopy_cells.png. Summarize the image dimensions, intensity range, foreground estimate, region evidence, and what acquisition metadata should be verified before quantitative analysis.
```

What to see: CLIO uses PNG imaging tools and grounds the review in dimensions, intensity, foreground, and region evidence.

Why this is interesting: Adds a binary scientific image domain with pixel and region semantics, not generic file text or chart-generation behavior.

Observed excerpt:

```text
imaging | success | direct_tool
imaging | success | planner_dispatch | Scientific image review: PNG: {"filepath": "/home/jcernuda/clio-agent/tmp/clio-benchmark-data/microscopy_cells.png", "format": "PNG", "mode": "L", "width": 128, "height": 96, "channels": 1, "threshold": 32, "intensity": {"min": 4, "max": 245, "mean": 32.568, "std": 62.502}, "foreground_pixels": 1497, "foreground_fraction": 0.121826, "foreground_bbox": {"count": 4, "items": [19, 21, 100, 79]}, "connected_regions": 3, "ok": true} Recommendations: - Verify acquisition scale and chann...[truncated]
Scientific image review:
PNG: {"filepath": "/home/jcernuda/clio-agent/tmp/clio-benchmark-data/microscopy_cells.png", "format": "PNG", "mode": "L", "width": 128, "height": 96, "channels": 1, "threshold": 32, "intensity": {"min": 4, "max": 245, "mean": 32.568, "std": 62.502}, "foreground_pixels": 1497, "foreground_fraction": 0.
```

### 3. Materials CIF structure review

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
Data/input files: /home/jcernuda/clio-agent/tmp/clio-benchmark-data/strontium_titanate.cif
Setup turns: 0
Actions: none
Child sessions: none
Artifacts: none
Artifact evidence: none
Elapsed: 6.0s

Prompt:

```text
Review this crystal structure file for collaborator handoff: /home/jcernuda/clio-agent/tmp/clio-benchmark-data/strontium_titanate.cif. Summarize the unit cell, symmetry, atom species, and any density or occupancy checks that should be verified before simulation setup.
```

What to see: CLIO uses CIF materials tools and grounds the review in unit-cell, space-group, species, and atom-site evidence.

Why this is interesting: Adds a non-NDP materials science domain that requires a new file parser, tool, and expert route instead of generic text inspection.

Observed excerpt:

```text
materials | success | direct_tool
materials | success | planner_dispatch | Materials structure review: CIF: {"filepath": "/home/jcernuda/clio-agent/tmp/clio-benchmark-data/strontium_titanate.cif", "data_block": "data_SrTiO3_benchmark", "formula_sum": "Sr1 Ti1 O3", "formula_structural": "SrTiO3", "space_group": "P m -3 m", "cell": {"a": 3.905, "b": 3.905, "c": 3.905, "alpha": 90.0, "beta": 90.0, "gamma": 90.0}, "cell_volume_angstrom3": 59.547443, "atom_site_count": 5, "species_counts": {"Sr": 1, "Ti": 1, "O": 3}, "occupancy_weighted_species_counts": {"Sr"...[truncated]
Materials structure review:
CIF: {"filepath": "/home/jcernuda/clio-agent/tmp/clio-benchmark-data/strontium_titanate.cif", "data_block": "data_SrTiO3_benchmark", "formula_sum": "Sr1 Ti1 O3", "formula_structural": "SrTiO3", "space_group": "P m -3 m", "cell": {"a": 3.905, "b": 3.905, "c": 3.905, "alpha": 90.0, "beta": 90.0,
```

### 4. Mass spectrometry mzML QC review

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
Data/input files: /home/jcernuda/clio-agent/tmp/clio-benchmark-data/proteomics_qc.mzML
Setup turns: 0
Actions: none
Child sessions: none
Artifacts: none
Artifact evidence: none
Elapsed: 6.5s

Prompt:

```text
Review this proteomics mzML run for collaborator handoff: /home/jcernuda/clio-agent/tmp/clio-benchmark-data/proteomics_qc.mzML. Summarize the spectra, MS-level balance, m/z coverage, intensity/TIC evidence, and what acquisition metadata should be verified before peptide-search analysis.
```

What to see: CLIO uses mzML mass spectrometry tools and grounds the review in spectra, MS levels, m/z range, peak counts, and TIC evidence.

Why this is interesting: Adds a structured XML scientific instrument domain with spectra and ion-current semantics, not generic XML text inspection.

Observed excerpt:

```text
mass_spec | success | direct_tool
mass_spec | success | planner_dispatch | Mass spectrometry data review: mzML: {"filepath": "/home/jcernuda/clio-agent/tmp/clio-benchmark-data/proteomics_qc.mzML", "format": "mzML", "spectrum_count": 4, "ms_levels": {"1": 2, "2": 2}, "total_peak_count": 14, "mz_range": {"count": 2, "items": [399.8, 933.5]}, "tic_total": 25140.0, "tic_max": 9500.0, "total_ion_current_total": 25140.0, "total_ion_current_max": 9500.0, "representative_spectra": {"count": 4, "items": [{"id": "scan=1", "ms_level": "1", "scan_start_time": "0.12"...[truncated]
Mass spectrometry data review:
mzML: {"filepath": "/home/jcernuda/clio-agent/tmp/clio-benchmark-data/proteomics_qc.mzML", "format": "mzML", "spectrum_count": 4, "ms_levels": {"1": 2, "2": 2}, "total_peak_count": 14, "mz_range": {"count": 2, "items": [399.8, 933.5]}, "tic_total": 25140.0, "tic_max": 9500.0, "total_ion_curr
```

### 5. Geospatial field-site review

Case: `geospatial_field_site_review`
Category: geospatial
Routing mode: `auto`
Status: pass
Selected agent: `geospatial`
Provider/model: `codex` / `gpt-5.5` via `codex://exec`
Provider settings: temperature=1.0, max_tokens=32000, context_length=0, thinking_budget=0
Route graph: geospatial
Route metrics: depth=1, branches=0, tools=1
Expert handoffs: geospatial x2
Tools: geospatial_inspect_geojson
Data/input files: /home/jcernuda/clio-agent/tmp/clio-benchmark-data/field_sites.geojson
Setup turns: 0
Actions: none
Child sessions: none
Artifacts: none
Artifact evidence: none
Elapsed: 8.0s

Prompt:

```text
Review this field-site GeoJSON for spatial analysis readiness: /home/jcernuda/clio-agent/tmp/clio-benchmark-data/field_sites.geojson. Summarize the feature types, coordinate bounds, key properties, and what a collaborator should verify before using it in a map overlay.
```

What to see: CLIO uses GeoJSON geospatial tools and grounds the review in feature, geometry, bounds, and property evidence.

Why this is interesting: Adds a non-NDP geospatial domain with coordinate and geometry semantics, not just generic JSON text inspection.

Observed excerpt:

```text
geospatial | success | direct_tool
geospatial | success | planner_dispatch | Geospatial data review: GeoJSON: {"filepath": "/home/jcernuda/clio-agent/tmp/clio-benchmark-data/field_sites.geojson", "geojson_type": "FeatureCollection", "feature_count": 4, "geometry_types": {"Point": 2, "LineString": 1, "Polygon": 1}, "property_keys": {"count": 3, "items": ["kind", "site_id", "status"]}, "bbox": {"count": 4, "items": [-105.292, 39.982, -105.238, 40.026]}, "coordinate_count": 10, "invalid_coordinate_count": 0, "representative_features": {"count": 4, "items": [{...[truncated]
Geospatial data review:
GeoJSON: {"filepath": "/home/jcernuda/clio-agent/tmp/clio-benchmark-data/field_sites.geojson", "geojson_type": "FeatureCollection", "feature_count": 4, "geometry_types": {"Point": 2, "LineString": 1, "Polygon": 1}, "property_keys": {"count": 3, "items": ["kind", "site_id", "status"]}, "bbox": {"c
```

## Failures Fixed During This Campaign

- GACT compaction originally bypassed transient-provider retry and only updated the GACT transcript; compaction now retries provider throttles, updates ARC memory, and fails with structured errors if memory storage fails.
- Compact summaries could lose exact scientific identifiers at the ARC truncation boundary; compact memory now preserves a labeled exact evidence index for paths, variables, columns, artifacts, and caveats.
- Retained multi-file context could make analysis narrow to the first file or let CSV follow-ups be stolen by broad synthesis; explicit file paths now take precedence and retained multi-source synthesis is limited to true synthesis questions.
- Planner-selected tool actions used to make benchmark evidence look flat; reports now preserve parent-owned sync delegation returns such as `data -> ndp_catalog -> data` and audit missing parent-resume evidence.
- Provider throttles during expert dispatch, handoffs, and compaction could surface as brittle partial recoveries; expert paths now use bounded transient-provider retry and still surface structured errors if exhausted.

## Remaining Caveats

- This report is evidence for the recorded ALCF run, not a guarantee that ALCF availability, model latency, or token freshness will be identical later.
- Several high-event cases are intentionally fast because child/nanoagent workers use deterministic local tools after routing; elapsed time alone should not be treated as benchmark depth.
- Two cases are deliberate surfaced-error checks. They are counted as successful hardening cases only because they returned structured errors without normal-looking fake assistant text.
- The benchmark now covers the hierarchy and handoff classes listed here, but future providers, file formats, and per-expert model assignments still need their own evidence runs.
