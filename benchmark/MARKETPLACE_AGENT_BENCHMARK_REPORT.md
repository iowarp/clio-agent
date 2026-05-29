# CLIO Marketplace Agent Benchmark Report

Generated: 2026-05-29 03:38:44 CDT
Evidence JSONL: `/home/jcernuda/clio-agent/tmp/clio-marketplace-benchmark.jsonl`
Benchmark lane: `marketplace_agents`

This is a CLIO session-evidence audit. It is produced from real session JSONL rows. Review the embedded `session_log` root and child messages for prompt, route, tool, artifact, error, recovery, and final-answer evidence. Pytest coverage only guards the harness and tools; it is not the benchmark result.

Result: 5/5 clean passes, 0 expected surfaced errors, 0 expected cancellations, 0 partial recoveries, 0 failures.

Extended stress coverage: has optional gaps outside the per-lane pass/fail gate.

## Extended Stress Coverage Audit

| Criterion | Observed | Required | Status |
| --- | ---: | ---: | --- |
| at least ten complex collaborator-grade demos | 0 | 10 | gap |
| at least five long or high-event stress cases | 0 | 5 | gap |
| at least three cases with tier-3 agents or nanoagents | 0 | 3 | gap |
| at least three visualization artifacts from analyzed data | 0 | 3 | gap |
| at least two deliberate surfaced-error cases | 0 | 2 | gap |
| at least one context-pressure or compaction case | 0 | 1 | gap |
| at least one provider/model-swap stress case | 0 | 1 | gap |

## Evidence Summary

- Max elapsed case: `marketplace_geospatial_field_review` (38.7s)
- Max expert depth: `marketplace_genomics_reference_review` (1)
- Max branch fanout: `marketplace_genomics_reference_review` (0)
- Unique tools used: genomics_inspect_fasta, genomics_summarize_vcf, geospatial_inspect_geojson, mass_spec_inspect_mzml, materials_inspect_cif
- Data/input files referenced: 5
- Artifacts verified on disk: 0/0
- Root session logs captured: 5/5
- Child session logs captured: 0
- Active Agent Blueprints: genomics-review, geospatial-field-review, materials-crystal-review, proteomics-mzml-review

## Provider Lane Audit

| Criterion | Observed | Required | Status |
| --- | ---: | ---: | --- |
| all marketplace cases prove the requested active Agent Blueprint | 5 | 5 | pass |
| at least four distinct marketplace Agent Blueprints | 4 | 4 | pass |
| all marketplace cases call at least one blueprint expert tool | 5 | 5 | pass |

Provider evidence details:

- genomics-review
- geospatial-field-review
- materials-crystal-review
- proteomics-mzml-review

## All Cases

| Case | Category | Blueprint | Mode | Source | Outcome | Agent | Handoffs | Tools | Children | Elapsed |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| marketplace_genomics_reference_review | marketplace-genomics | genomics-review | auto | user_agent | pass | reference | - | genomics_inspect_fasta | 0 | 34.2s |
| marketplace_genomics_variant_review | marketplace-genomics | genomics-review | auto | user_agent | pass | variants | - | genomics_summarize_vcf | 0 | 30.7s |
| marketplace_materials_crystal_review | marketplace-materials | materials-crystal-review | auto | user_agent | pass | crystal_structure | - | materials_inspect_cif | 0 | 37.7s |
| marketplace_geospatial_field_review | marketplace-geospatial | geospatial-field-review | auto | user_agent | pass | spatial_features | - | geospatial_inspect_geojson | 0 | 38.7s |
| marketplace_proteomics_mzml_review | marketplace-proteomics | proteomics-mzml-review | auto | user_agent | pass | mass_spec | - | mass_spec_inspect_mzml | 0 | 33.1s |

## Best 10 Demo Prompts

### 1. Marketplace genomics FASTA reference review

Case: `marketplace_genomics_reference_review`
Category: marketplace-genomics
Routing mode: `auto`
Status: pass
Selected agent: `reference`
Active Agent Blueprint: `genomics-review`
Provider/model: `codex` / `gpt-5.5` via `codex://exec`
Provider settings: temperature=1.0, max_tokens=32000, context_length=0, thinking_budget=0
Route graph: reference
Route metrics: depth=1, branches=0, tools=1
Expert handoffs: none
Tools: genomics_inspect_fasta
Data/input files: /tmp/clio-benchmark-data/pathogen_reference.fasta
Setup turns: 0
Root session messages: 2
Child session logs: 0
Actions: none
Child sessions: none
Artifacts: none
Artifact evidence: none
Elapsed: 34.2s

Prompt:

```text
Review this reference FASTA for collaborator handoff: /tmp/clio-benchmark-data/pathogen_reference.fasta. Summarize contigs, composition evidence, and what should be verified before variant interpretation.
```

What to see: CLIO runs the genomics-review marketplace Agent Blueprint in this session and uses the reference expert's FASTA tool.

Why this is interesting: Proves a domain agent installed from the marketplace can be activated per session and execute its own expert/tool surface.

Observed excerpt:

```text
Reference FASTA reviewed: `/tmp/clio-benchmark-data/pathogen_reference.fasta`

Observed contigs:
- `chrA`: 4,800 bp, GC fraction 0.585833, 0 ambiguous bases
- `plasmidB`: 1,250 bp, GC fraction 0.3536, 0 ambiguous bases

Overall composition:
- Total records: 2
- Total bases: 6,050
- Overall GC fraction: 0.537851
- Base counts: A=1,406, C=1,673, G=1,581, T=1,390, N=0
- Longest record: `chrA` at 4,800 bp
- Records were not truncated in inspection output.

Interpretation:
- The reference appears clean at the FASTA sequence level: no `N` bases or ambiguous bases were observed.
- `chrA` and `plasmidB` have substantially different GC fractions, which is consistent with distinct reference elements, but this does not by itself validate biological identity or plasmid status.
- The description for both records is `synthetic pathogen benchmark reference`, so this should be treated as benchmark/refer
```

### 2. Marketplace genomics VCF variant review

Case: `marketplace_genomics_variant_review`
Category: marketplace-genomics
Routing mode: `auto`
Status: pass
Selected agent: `variants`
Active Agent Blueprint: `genomics-review`
Provider/model: `codex` / `gpt-5.5` via `codex://exec`
Provider settings: temperature=1.0, max_tokens=32000, context_length=0, thinking_budget=0
Route graph: variants
Route metrics: depth=1, branches=0, tools=1
Expert handoffs: none
Tools: genomics_summarize_vcf
Data/input files: /tmp/clio-benchmark-data/pathogen_sample_variants.vcf
Setup turns: 0
Root session messages: 2
Child session logs: 0
Actions: none
Child sessions: none
Artifacts: none
Artifact evidence: none
Elapsed: 30.7s

Prompt:

```text
Review this VCF for collaborator handoff: /tmp/clio-benchmark-data/pathogen_sample_variants.vcf. Summarize variant types, likely effects, and what should be verified before analysis.
```

What to see: CLIO runs the genomics-review marketplace Agent Blueprint in this session and uses the variants expert's VCF tool.

Why this is interesting: Exercises a second expert in the same marketplace agent, proving the active blueprint changes the available expert surface.

Observed excerpt:

```text
For `/tmp/clio-benchmark-data/pathogen_sample_variants.vcf`, the tool reported 1 sample (`sample_A`) and 6 total variants.

Variant summary:
- Variant types: 3 SNPs, 2 deletions, 1 insertion.
- Contigs: 4 variants on `chrA`, 2 on `plasmidB`.
- Filters: 5 `PASS`, 1 `LowQual`.

Likely annotated effects:
- `var001`: `chrA:128`, SNP `A>G`, `PASS`, gene `repA`, effect `missense`.
- `var002`: `chrA:790`, deletion `CT>C`, `PASS`, gene `membrane`, effect `frameshift`.
- `var003`: `chrA:1432`, insertion `G>GA`, `PASS`, gene `polymerase`, effect `insertion`.
- `var004`: `chrA:3104`, SNP `T>C`, `LowQual`, gene `hypothetical`, effect `synonymous`.
- `var005`: `plasmidB:217`, SNP `C>T`, `PASS`, gene `resistance`, effect `stop_gained`.
- `var006`: `plasmidB:904`, deletion `GTA>G`, `PASS`, gene `mobility`, effect `deletion`.

Before analysis or collaborator handoff, verify:
- The `LowQual` variant `var
```

### 3. Marketplace materials CIF review

Case: `marketplace_materials_crystal_review`
Category: marketplace-materials
Routing mode: `auto`
Status: pass
Selected agent: `crystal_structure`
Active Agent Blueprint: `materials-crystal-review`
Provider/model: `codex` / `gpt-5.5` via `codex://exec`
Provider settings: temperature=1.0, max_tokens=32000, context_length=0, thinking_budget=0
Route graph: crystal_structure
Route metrics: depth=1, branches=0, tools=1
Expert handoffs: none
Tools: materials_inspect_cif
Data/input files: /tmp/clio-benchmark-data/strontium_titanate.cif
Setup turns: 0
Root session messages: 2
Child session logs: 0
Actions: none
Child sessions: none
Artifacts: none
Artifact evidence: none
Elapsed: 37.7s

Prompt:

```text
Review this CIF as a materials handoff: /tmp/clio-benchmark-data/strontium_titanate.cif. Summarize formula, symmetry, species, and what simulation metadata should be verified.
```

What to see: CLIO runs the materials-crystal-review marketplace Agent Blueprint and uses the crystal_structure expert.

Why this is interesting: Proves a separate materials marketplace agent can be loaded per session.

Observed excerpt:

```text
Observed CIF handoff summary for `/tmp/clio-benchmark-data/strontium_titanate.cif`:

- Data block: `data_SrTiO3_benchmark`
- Formula sum: `Sr1 Ti1 O3`
- Structural formula: `SrTiO3`
- Space group: `P m -3 m`
- Cell: `a = b = c = 3.905 Å`, `alpha = beta = gamma = 90.0°`
- Cell volume: `59.547443 Å^3`
- Approximate density: `5.116632 g/cm^3`
- Atom site count: `5`
- Species counts: `Sr: 1`, `Ti: 1`, `O: 3`
- Occupancy-weighted species counts: `Sr: 1.0`, `Ti: 1.0`, `O: 3.0`
- Atom sites were not truncated.

Atomic sites:

- `Sr1`: `Sr`, fractional coordinates `(0.0, 0.0, 0.0)`, occupancy `1.0`
- `Ti1`: `Ti`, fractional coordinates `(0.5, 0.5, 0.5)`, occupancy `1.0`
- `O1`: `O`, fractional coordinates `(0.5, 0.5, 0.0)`, occupancy `1.0`
- `O2`: `O`, fractional coordinates `(0.5, 0.0, 0.5)`, occupancy `1.0`
- `O3`: `O`, fractional coordinates `(0.0, 0.5, 0.5)`, occupancy `1.0`

Interpretation:
```

### 4. Marketplace geospatial GeoJSON review

Case: `marketplace_geospatial_field_review`
Category: marketplace-geospatial
Routing mode: `auto`
Status: pass
Selected agent: `spatial_features`
Active Agent Blueprint: `geospatial-field-review`
Provider/model: `codex` / `gpt-5.5` via `codex://exec`
Provider settings: temperature=1.0, max_tokens=32000, context_length=0, thinking_budget=0
Route graph: spatial_features
Route metrics: depth=1, branches=0, tools=1
Expert handoffs: none
Tools: geospatial_inspect_geojson
Data/input files: /tmp/clio-benchmark-data/field_sites.geojson
Setup turns: 0
Root session messages: 2
Child session logs: 0
Actions: none
Child sessions: none
Artifacts: none
Artifact evidence: none
Elapsed: 38.7s

Prompt:

```text
Review this GeoJSON for field-site map readiness: /tmp/clio-benchmark-data/field_sites.geojson. Summarize feature types, bounds, properties, and map-overlay risks.
```

What to see: CLIO runs the geospatial-field-review marketplace Agent Blueprint and uses the spatial_features expert.

Why this is interesting: Proves a geospatial marketplace agent can be loaded per session.

Observed excerpt:

```text
GeoJSON reviewed: `/tmp/clio-benchmark-data/field_sites.geojson`

Observed structure:
- GeoJSON type: `FeatureCollection`
- Feature count: 4
- Geometry types: 2 `Point`, 1 `LineString`, 1 `Polygon`
- Coordinate count: 10
- Invalid coordinate count: 0
- Features truncated: false

Bounds:
- Bounding box: `[-105.292, 39.982, -105.238, 40.026]`
- Interpreted as longitude/latitude, this covers a compact area near latitude `39.982` to `40.026` and longitude `-105.292` to `-105.238`.

Properties:
- Property keys present: `kind`, `site_id`, `status`
- Representative features:
  - `Point`: `site_id=north_ridge`, `kind=sensor`, `status=active`
  - `Point`: `site_id=south_valley`, `kind=sensor`, `status=maintenance`
  - `LineString`: `site_id=access_transect`, `kind=transect`, `status=active`
  - `Polygon`: `site_id=study_boundary`, `kind=boundary`, `status=active`

Map-readiness assessment:
- The
```

### 5. Marketplace proteomics mzML review

Case: `marketplace_proteomics_mzml_review`
Category: marketplace-proteomics
Routing mode: `auto`
Status: pass
Selected agent: `mass_spec`
Active Agent Blueprint: `proteomics-mzml-review`
Provider/model: `codex` / `gpt-5.5` via `codex://exec`
Provider settings: temperature=1.0, max_tokens=32000, context_length=0, thinking_budget=0
Route graph: mass_spec
Route metrics: depth=1, branches=0, tools=1
Expert handoffs: none
Tools: mass_spec_inspect_mzml
Data/input files: /tmp/clio-benchmark-data/proteomics_qc.mzML
Setup turns: 0
Root session messages: 2
Child session logs: 0
Actions: none
Child sessions: none
Artifacts: none
Artifact evidence: none
Elapsed: 33.1s

Prompt:

```text
Review this mzML run for proteomics handoff: /tmp/clio-benchmark-data/proteomics_qc.mzML. Summarize spectra, MS-level balance, m/z coverage, TIC evidence, and metadata risks.
```

What to see: CLIO runs the proteomics-mzml-review marketplace Agent Blueprint and uses the mass_spec expert.

Why this is interesting: Proves a proteomics marketplace agent can be loaded per session.

Observed excerpt:

```text
Observed evidence for `/tmp/clio-benchmark-data/proteomics_qc.mzML`:

- Format: `mzML`
- Spectrum count: `4`
- MS-level balance: `2` MS1 spectra and `2` MS2 spectra
- Total peak count: `14`
- m/z coverage: `399.8` to `933.5`
- TIC total: `25140.0`
- TIC max: `9500.0`
- Spectra truncated: `false`
- Tool warnings: none

Representative spectra:

- `scan=1`: MS1, scan start time `0.12`, `4` peaks, TIC `9500.0`
- `scan=2`: MS2, scan start time `0.18`, `3` peaks, TIC `3630.0`
- `scan=3`: MS1, scan start time `0.25`, `4` peaks, TIC `8880.0`
- `scan=4`: MS2, scan start time `0.31`, `3` peaks, TIC `3130.0`

Interpretation: the file is readable and contains both MS1 and MS2 spectra, evenly balanced in this small run. The TIC evidence shows signal in all representative spectra, with higher TIC in the MS1 scans than the MS2 scans. The m/z range spans `399.8` to `933.5`, which is plausible for a comp
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
