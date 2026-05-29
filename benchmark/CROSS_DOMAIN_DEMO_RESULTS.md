# CLIO Cross-Domain Demo Results

This document is the short human-facing record for the first real
cross-domain CLIO demo pass. The full generated report is
`benchmark/LIVE_CROSS_DOMAIN_REPORT.md`; the raw JSONL evidence is
`benchmark/LIVE_CROSS_DOMAIN_EVIDENCE.jsonl`.

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

## Fixes Made During This Pass

- #408 fixed scientific path extraction for new domain suffixes.
- #410 allowed PNG inspection to accept planner-generated `threshold: null`.
- #412 exposed explicit total-ion-current fields in mzML tool output.
- #414 added readable mass-spec QC wording for MS-level and total ion current
  evidence.
- #416 counted PNG scientific inputs as data files.
- #418 filtered benchmark path evidence so scientific slash terms like `m/z`
  are not recorded as fake files.

## Remaining Gaps

This pass proves five single-expert cross-domain workflows. It does not yet
complete the full umbrella benchmark target. Still needed:

- multi-branch workflows with child sessions or tier-3 expert handoffs;
- NDP-backed workflows with bounded catalog staging/download;
- more visualization artifacts from analyzed data;
- a full fresh-machine run using the documented commands.
