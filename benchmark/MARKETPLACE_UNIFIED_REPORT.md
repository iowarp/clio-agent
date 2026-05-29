# CLIO Marketplace Agent Benchmark Report

Generated: 2026-05-29 08:41:00 CDT
Evidence JSONL: `/home/jcernuda/clio-agent/benchmark/MARKETPLACE_UNIFIED_EVIDENCE.jsonl`
Benchmark lane: `marketplace_agents`

This is a CLIO session-evidence audit. It is produced from real session JSONL rows. Review the embedded `session_log` root and child messages for prompt, route, tool, artifact, error, recovery, and final-answer evidence. Pytest coverage only guards the harness and tools; it is not the benchmark result.

Result: 6/6 clean passes, 0 expected surfaced errors, 0 expected cancellations, 0 partial recoveries, 0 failures.

Extended stress coverage: has optional gaps outside the per-lane pass/fail gate.

## Extended Stress Coverage Audit

| Criterion | Observed | Required | Status |
| --- | ---: | ---: | --- |
| at least ten complex collaborator-grade demos | 1 | 10 | gap |
| at least five long or high-event stress cases | 1 | 5 | gap |
| at least three cases with tier-3 agents or nanoagents | 1 | 3 | gap |
| at least three visualization artifacts from analyzed data | 1 | 3 | gap |
| at least two deliberate surfaced-error cases | 0 | 2 | gap |
| at least one context-pressure or compaction case | 0 | 1 | gap |
| at least one provider/model-swap stress case | 0 | 1 | gap |

High-event or long-running cases:

- marketplace_seismic_waveform_review (742.6s, 30 events)

## Evidence Summary

- Max elapsed case: `marketplace_seismic_waveform_review` (742.6s)
- Max expert depth: `marketplace_seismic_waveform_review` (6)
- Max branch fanout: `marketplace_seismic_waveform_review` (5)
- Unique tools used: genomics_inspect_fasta, genomics_summarize_vcf, geospatial_inspect_geojson, mass_spec_inspect_mzml, materials_inspect_cif, ndp_get_dataset_details, ndp_list_organizations, ndp_search_datasets, ndp_stage_resource, sac_compute_trace_statistics, sac_fetch_earthscope_waveform, sac_inspect_archive, sac_plot_traces
- Data/input files referenced: 6
- Artifacts verified on disk: 1/1
- Root session logs captured: 6/6
- Child session logs captured: 0
- Active Agent Blueprints: genomics-review, geospatial-field-review, materials-crystal-review, proteomics-mzml-review, seismic-waveform-review

## Provider Lane Audit

| Criterion | Observed | Required | Status |
| --- | ---: | ---: | --- |
| all marketplace cases prove the requested active Agent Blueprint | 6 | 6 | pass |
| at least five distinct marketplace Agent Blueprints | 5 | 5 | pass |
| all marketplace cases call at least one blueprint expert tool | 6 | 6 | pass |

Provider evidence details:

- genomics-review
- geospatial-field-review
- materials-crystal-review
- proteomics-mzml-review
- seismic-waveform-review

## All Cases

| Case | Category | Blueprint | Mode | Source | Outcome | Agent | Handoffs | Tools | Children | Elapsed |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| marketplace_genomics_reference_review | marketplace-genomics | genomics-review | auto | user_agent | pass | reference | - | genomics_inspect_fasta | 0 | 31.7s |
| marketplace_genomics_variant_review | marketplace-genomics | genomics-review | auto | user_agent | pass | variants | - | genomics_summarize_vcf | 0 | 32.1s |
| marketplace_materials_crystal_review | marketplace-materials | materials-crystal-review | auto | user_agent | pass | crystal_structure | - | materials_inspect_cif | 0 | 41.7s |
| marketplace_geospatial_field_review | marketplace-geospatial | geospatial-field-review | auto | user_agent | pass | spatial_features | - | geospatial_inspect_geojson | 0 | 28.6s |
| marketplace_proteomics_mzml_review | marketplace-proteomics | proteomics-mzml-review | auto | user_agent | pass | mass_spec | - | mass_spec_inspect_mzml | 0 | 34.7s |
| marketplace_seismic_waveform_review | marketplace-seismic | seismic-waveform-review | auto | user_agent | pass | main | data x3, ndp_catalog x2, main x3, analysis x3, sac_format x2, visualization | ndp_search_datasets, ndp_search_datasets, ndp_get_dataset_details, ndp_stage_resource, ndp_search_datasets, ndp_search_datasets, ndp_list_organizations, ndp_search_datasets, ndp_search_datasets, sac_fetch_earthscope_waveform, sac_inspect_archive, sac_compute_trace_statistics, sac_fetch_earthscope_waveform, sac_inspect_archive, sac_compute_trace_statistics, sac_plot_traces | 0 | 742.6s |

## Best 10 Demo Prompts

### 1. Marketplace seismic waveform recovery review

Case: `marketplace_seismic_waveform_review`
Category: marketplace-seismic
Routing mode: `auto`
Status: pass
Selected agent: `main`
Active Agent Blueprint: `seismic-waveform-review`
Provider/model: `codex` / `gpt-5.5` via `codex://exec`
Provider settings: temperature=1.0, max_tokens=32000, context_length=0, thinking_budget=0
Route graph: orchestrator -> main; analysis -> sac_format -> analysis; main -> analysis -> main; main -> visualization -> main; main -> data -> main; data -> ndp_catalog -> data
Route metrics: depth=6, branches=5, sync_handoffs=5, child_sessions=0, tools=16
Expert handoffs: data x3, ndp_catalog x2, main x3, analysis x3, sac_format x2, visualization
Tools: ndp_search_datasets, ndp_search_datasets, ndp_get_dataset_details, ndp_stage_resource, ndp_search_datasets, ndp_search_datasets, ndp_list_organizations, ndp_search_datasets, ndp_search_datasets, sac_fetch_earthscope_waveform, sac_inspect_archive, sac_compute_trace_statistics, sac_fetch_earthscope_waveform, sac_inspect_archive, sac_compute_trace_statistics, sac_plot_traces
Data/input files: /home/jcernuda/clio-agent/tmp/clio-seismic-staging/earthscope_IU_ANMO_00_BHZ_2010-02-27T063000.sac
Setup turns: 0
Root session messages: 2
Child session logs: 0
Actions: none
Child sessions: none
Artifacts: /home/jcernuda/clio-agent/.clio-agent-artifacts/charts/sac_traces_earthscope_IU_ANMO_00_BHZ_2010-02-27T063000.png
Artifact evidence: /home/jcernuda/clio-agent/.clio-agent-artifacts/charts/sac_traces_earthscope_IU_ANMO_00_BHZ_2010-02-27T063000.png (ok, 195159 B)
Elapsed: 742.6s

Prompt:

```text
Using the active seismic waveform review agent, find bounded seismic waveform evidence through NDP discovery, recover with an alternate SAC source if NDP staging is blocked, inspect the waveform, compute trace statistics, and produce a PNG plot artifact without using stale local files.
```

What to see: CLIO runs the seismic-waveform-review marketplace Agent Blueprint, surfaces NDP staging blockers, recovers with an observed EarthScope SAC path, and creates a verified PNG artifact.

Why this is interesting: Proves the marketplace can carry the strongest hierarchical workflow, not just single-expert file inspection packages.

Observed excerpt:

```text
Waveform plot artifact is complete, but the parent still needs trace inspection/statistics evidence before finalizing the workflow.
//ds2.datacollaboratory.org/Earthscope_api_dec2024/generated_png/WWMT.CI.LY_.40.png
/home/jcernuda/clio-agent/.clio-agent-artifacts/charts/sac_traces_earthscope_IU_ANMO_00_BHZ_2010-02-27T063000.png
```

### 2. Marketplace genomics FASTA reference review

Case: `marketplace_genomics_reference_review`
Category: marketplace-genomics
Routing mode: `auto`
Status: pass
Selected agent: `reference`
Active Agent Blueprint: `genomics-review`
Provider/model: `codex` / `gpt-5.5` via `codex://exec`
Provider settings: temperature=1.0, max_tokens=32000, context_length=0, thinking_budget=0
Route graph: orchestrator -> reference
Route metrics: depth=1, branches=0, sync_handoffs=0, child_sessions=0, tools=1
Expert handoffs: none
Tools: genomics_inspect_fasta
Data/input files: /home/jcernuda/clio-agent/tmp/clio-benchmark-data/pathogen_reference.fasta
Setup turns: 0
Root session messages: 2
Child session logs: 0
Actions: none
Child sessions: none
Artifacts: none
Artifact evidence: none
Elapsed: 31.7s

Prompt:

```text
Review this reference FASTA for collaborator handoff: /home/jcernuda/clio-agent/tmp/clio-benchmark-data/pathogen_reference.fasta. Summarize contigs, composition evidence, and what should be verified before variant interpretation.
```

What to see: CLIO runs the genomics-review marketplace Agent Blueprint in this session and uses the reference expert's FASTA tool.

Why this is interesting: Proves a domain agent installed from the marketplace can be activated per session and execute its own expert/tool surface.

Observed excerpt:

```text
Reference FASTA: `/home/jcernuda/clio-agent/tmp/clio-benchmark-data/pathogen_reference.fasta`

Observed contents:
- Record count: 2
- Total length: 6,050 bp
- Overall GC fraction: 0.537851
- Overall base counts: A=1,406, C=1,673, G=1,581, T=1,390, N=0
- No ambiguous bases were observed.

Contigs:
- `chrA`
  - Description: `synthetic pathogen benchmark reference`
  - Length: 4,800 bp
  - GC fraction: 0.585833
  - Base counts: A=1,005, C=1,446, G=1,366, T=983, N=0
  - This is the longest record.
- `plasmidB`
  - Description: `synthetic pathogen benchmark reference`
  - Length: 1,250 bp
  - GC fraction: 0.3536
  - Base counts: A=401, C=227, G=215, T=407, N=0

Composition interpretation:
`chrA` is substantially more GC-rich than `plasmidB` based on the observed GC fractions. The reference has no observed `N` bases, so ambiguity in the reference sequence itself is not evident from this inspec
```

### 3. Marketplace genomics VCF variant review

Case: `marketplace_genomics_variant_review`
Category: marketplace-genomics
Routing mode: `auto`
Status: pass
Selected agent: `variants`
Active Agent Blueprint: `genomics-review`
Provider/model: `codex` / `gpt-5.5` via `codex://exec`
Provider settings: temperature=1.0, max_tokens=32000, context_length=0, thinking_budget=0
Route graph: orchestrator -> variants
Route metrics: depth=1, branches=0, sync_handoffs=0, child_sessions=0, tools=1
Expert handoffs: none
Tools: genomics_summarize_vcf
Data/input files: /home/jcernuda/clio-agent/tmp/clio-benchmark-data/pathogen_sample_variants.vcf
Setup turns: 0
Root session messages: 2
Child session logs: 0
Actions: none
Child sessions: none
Artifacts: none
Artifact evidence: none
Elapsed: 32.1s

Prompt:

```text
Review this VCF for collaborator handoff: /home/jcernuda/clio-agent/tmp/clio-benchmark-data/pathogen_sample_variants.vcf. Summarize variant types, likely effects, and what should be verified before analysis.
```

What to see: CLIO runs the genomics-review marketplace Agent Blueprint in this session and uses the variants expert's VCF tool.

Why this is interesting: Exercises a second expert in the same marketplace agent, proving the active blueprint changes the available expert surface.

Observed excerpt:

```text
VCF handoff summary for `/home/jcernuda/clio-agent/tmp/clio-benchmark-data/pathogen_sample_variants.vcf`:

Observed contents:
- Sample count: 1
- Sample: `sample_A`
- Variant count: 6
- Chromosomes/contigs:
  - `chrA`: 4 variants
  - `plasmidB`: 2 variants
- Filters:
  - `PASS`: 5
  - `LowQual`: 1

Variant types:
- SNPs: 3
- Deletions: 2
- Insertions: 1

Likely annotated effects:
- `missense`: 1
- `frameshift`: 1
- `insertion`: 1
- `synonymous`: 1
- `stop_gained`: 1
- `deletion`: 1

Notable variants:
- `chrA:128` `var001`, `A>G`, PASS, gene `repA`, effect `missense`
- `chrA:790` `var002`, `CT>C`, PASS, gene `membrane`, effect `frameshift`
- `chrA:1432` `var003`, `G>GA`, PASS, gene `polymerase`, effect `insertion`
- `chrA:3104` `var004`, `T>C`, LowQual, gene `hypothetical`, effect `synonymous`
- `plasmidB:217` `var005`, `C>T`, PASS, gene `resistance`, effect `stop_gained`
- `plasmidB:904`
```

### 4. Marketplace materials CIF review

Case: `marketplace_materials_crystal_review`
Category: marketplace-materials
Routing mode: `auto`
Status: pass
Selected agent: `crystal_structure`
Active Agent Blueprint: `materials-crystal-review`
Provider/model: `codex` / `gpt-5.5` via `codex://exec`
Provider settings: temperature=1.0, max_tokens=32000, context_length=0, thinking_budget=0
Route graph: orchestrator -> crystal_structure
Route metrics: depth=1, branches=0, sync_handoffs=0, child_sessions=0, tools=1
Expert handoffs: none
Tools: materials_inspect_cif
Data/input files: /home/jcernuda/clio-agent/tmp/clio-benchmark-data/strontium_titanate.cif
Setup turns: 0
Root session messages: 2
Child session logs: 0
Actions: none
Child sessions: none
Artifacts: none
Artifact evidence: none
Elapsed: 41.7s

Prompt:

```text
Review this CIF as a materials handoff: /home/jcernuda/clio-agent/tmp/clio-benchmark-data/strontium_titanate.cif. Summarize formula, symmetry, species, and what simulation metadata should be verified.
```

What to see: CLIO runs the materials-crystal-review marketplace Agent Blueprint and uses the crystal_structure expert.

Why this is interesting: Proves a separate materials marketplace agent can be loaded per session.

Observed excerpt:

```text
Observed materials handoff summary for `/home/jcernuda/clio-agent/tmp/clio-benchmark-data/strontium_titanate.cif`:

- Data block: `data_SrTiO3_benchmark`
- Formula sum: `Sr1 Ti1 O3`
- Structural formula: `SrTiO3`
- Space group: `P m -3 m`
- Cell: cubic, `a = b = c = 3.905 Å`, `alpha = beta = gamma = 90.0°`
- Cell volume: `59.547443 Å^3`
- Approximate density: `5.116632 g/cm3`
- Atom sites: `5`, not truncated
- Species counts: `Sr: 1`, `Ti: 1`, `O: 3`
- Occupancy-weighted species counts: `Sr: 1.0`, `Ti: 1.0`, `O: 3.0`

Atomic sites:
- `Sr1`: `Sr`, fractional `(0.0, 0.0, 0.0)`, occupancy `1.0`
- `Ti1`: `Ti`, fractional `(0.5, 0.5, 0.5)`, occupancy `1.0`
- `O1`: `O`, fractional `(0.5, 0.5, 0.0)`, occupancy `1.0`
- `O2`: `O`, fractional `(0.5, 0.0, 0.5)`, occupancy `1.0`
- `O3`: `O`, fractional `(0.0, 0.5, 0.5)`, occupancy `1.0`

Simulation metadata to verify before handoff:
- Intended symme
```

### 5. Marketplace geospatial GeoJSON review

Case: `marketplace_geospatial_field_review`
Category: marketplace-geospatial
Routing mode: `auto`
Status: pass
Selected agent: `spatial_features`
Active Agent Blueprint: `geospatial-field-review`
Provider/model: `codex` / `gpt-5.5` via `codex://exec`
Provider settings: temperature=1.0, max_tokens=32000, context_length=0, thinking_budget=0
Route graph: orchestrator -> spatial_features
Route metrics: depth=1, branches=0, sync_handoffs=0, child_sessions=0, tools=1
Expert handoffs: none
Tools: geospatial_inspect_geojson
Data/input files: /home/jcernuda/clio-agent/tmp/clio-benchmark-data/field_sites.geojson
Setup turns: 0
Root session messages: 2
Child session logs: 0
Actions: none
Child sessions: none
Artifacts: none
Artifact evidence: none
Elapsed: 28.6s

Prompt:

```text
Review this GeoJSON for field-site map readiness: /home/jcernuda/clio-agent/tmp/clio-benchmark-data/field_sites.geojson. Summarize feature types, bounds, properties, and map-overlay risks.
```

What to see: CLIO runs the geospatial-field-review marketplace Agent Blueprint and uses the spatial_features expert.

Why this is interesting: Proves a geospatial marketplace agent can be loaded per session.

Observed excerpt:

```text
GeoJSON map-readiness summary for `/home/jcernuda/clio-agent/tmp/clio-benchmark-data/field_sites.geojson`:

Observed structure:
- Type: `FeatureCollection`
- Feature count: 4
- Geometry types: 2 `Point`, 1 `LineString`, 1 `Polygon`
- Coordinate count: 10
- Invalid coordinates: 0
- Features truncated during inspection: false

Bounds:
- Bounding box: `[-105.292, 39.982, -105.238, 40.026]`
- Interpreted as longitude/latitude bounds, this covers a compact area near approximately 40°N, 105.26°W.

Properties:
- Shared property keys: `kind`, `site_id`, `status`
- Representative features:
  - Point: `kind=sensor`, `site_id=north_ridge`, `status=active`
  - Point: `kind=sensor`, `site_id=south_valley`, `status=maintenance`
  - LineString: `kind=transect`, `site_id=access_transect`, `status=active`
  - Polygon: `kind=boundary`, `site_id=study_boundary`, `status=active`

Map-overlay readiness:
- Th
```

### 6. Marketplace proteomics mzML review

Case: `marketplace_proteomics_mzml_review`
Category: marketplace-proteomics
Routing mode: `auto`
Status: pass
Selected agent: `mass_spec`
Active Agent Blueprint: `proteomics-mzml-review`
Provider/model: `codex` / `gpt-5.5` via `codex://exec`
Provider settings: temperature=1.0, max_tokens=32000, context_length=0, thinking_budget=0
Route graph: orchestrator -> mass_spec
Route metrics: depth=1, branches=0, sync_handoffs=0, child_sessions=0, tools=1
Expert handoffs: none
Tools: mass_spec_inspect_mzml
Data/input files: /home/jcernuda/clio-agent/tmp/clio-benchmark-data/proteomics_qc.mzML
Setup turns: 0
Root session messages: 2
Child session logs: 0
Actions: none
Child sessions: none
Artifacts: none
Artifact evidence: none
Elapsed: 34.7s

Prompt:

```text
Review this mzML run for proteomics handoff: /home/jcernuda/clio-agent/tmp/clio-benchmark-data/proteomics_qc.mzML. Summarize spectra, MS-level balance, m/z coverage, TIC evidence, and metadata risks.
```

What to see: CLIO runs the proteomics-mzml-review marketplace Agent Blueprint and uses the mass_spec expert.

Why this is interesting: Proves a proteomics marketplace agent can be loaded per session.

Observed excerpt:

```text
Proteomics handoff review for `/home/jcernuda/clio-agent/tmp/clio-benchmark-data/proteomics_qc.mzML`:

Observed evidence:
- Format: `mzML`
- Read status: `ok: true`
- Spectrum count: `4`
- MS-level balance: `2` MS1 spectra and `2` MS2 spectra
- Total peak count: `14`
- m/z coverage: `399.8` to `933.5`
- TIC total: `25140.0`
- TIC max: `9500.0`
- Spectra truncated: `false`
- Tool warnings: none reported

Representative spectra:
- `scan=1`: MS1, start time `0.12`, `4` peaks, TIC `9500.0`
- `scan=2`: MS2, start time `0.18`, `3` peaks, TIC `3630.0`
- `scan=3`: MS1, start time `0.25`, `4` peaks, TIC `8880.0`
- `scan=4`: MS2, start time `0.31`, `3` peaks, TIC `3130.0`

Interpretation:
The file contains a readable, very small mzML run with a balanced MS1/MS2 split. The TIC values show nonzero signal across all four spectra, with stronger MS1 TICs than MS2 TICs in the representative scans. The o
```

## Failures Fixed During This Campaign

- GACT compaction originally bypassed transient-provider retry and only updated the GACT transcript; compaction now retries provider throttles, updates ARC memory, and fails with structured errors if memory storage fails.
- Compact summaries could lose exact scientific identifiers at the ARC truncation boundary; compact memory now preserves a labeled exact evidence index for paths, variables, columns, artifacts, and caveats.
- Retained multi-file context could make analysis narrow to the first file or let CSV follow-ups be stolen by broad synthesis; explicit file paths now take precedence and retained multi-source synthesis is limited to true synthesis questions.
- Planner-selected tool actions used to make benchmark evidence look flat; reports now preserve parent-owned sync delegation returns such as `data -> ndp_catalog -> data` and audit missing parent-resume evidence.
- Provider throttles during expert dispatch, handoffs, and compaction could surface as brittle partial recoveries; expert paths now use bounded transient-provider retry and still surface structured errors if exhausted.

## Remaining Caveats

- This report is evidence for the recorded provider/session run, not a guarantee that provider availability, model latency, token freshness, or external data services will be identical later.
- Several high-event cases are intentionally fast because child/nanoagent workers use deterministic local tools after routing; elapsed time alone should not be treated as benchmark depth.
- The benchmark now covers the hierarchy and handoff classes listed here, but future providers, file formats, and per-expert model assignments still need their own evidence runs.
