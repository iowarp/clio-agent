# Benchmark Case Matrix

Updated: 2026-06-04

This is the clean 12-case benchmark target. These are contracts, not pass claims.

## Known Problems To Fix Before Running Final Benchmark

Guardrail: proper EarthScope CSV/tabular event, station, or channel evidence is required before optional waveform work.
Guardrail: SAC is a waveform file format, not a geography or discovery semantic.

- Place names must resolve through a generic geospatial resolver that returns a
  provenance-bearing region object. Domain tools must not hide hardcoded city hints.
- EarthScope seismic benchmark work must start from proper EarthScope
  CSV/tabular event, station, or channel evidence where available. SAC is a
  waveform file format, not a geography or discovery semantic. SAC-specific
  analysis can be a later stage only after discovery/staging semantics are
  correct.
- NDP cases must prove catalog search, candidate selection, bounded staging or
  feature query, analysis, visualization, and provenance. A shallow catalog
  answer is not enough.
- Unit tests must cover state space around parsers, validators, resolver
  failure modes, malformed data, missing data, oversized data, permissions, and
  artifact paths. One happy-path test is not meaningful coverage.
- Live provider runs must be inspected case by case. Passing JSONL counters do
  not establish semantic correctness.

## Cases

| Case | Folder | Primary Domain | Core Semantics |
| --- | --- | --- | --- |
| 01 | `case01-ndp-geographic-hazard-brief` | NDP/geospatial hazards | Place/state query to region, NDP catalog, feature query, hazard synthesis, map/JSON artifact |
| 02 | `case02-earthscope-csv-seismic-geography` | EarthScope/seismology | Place to region, EarthScope CSV/tabular event/station evidence, seismic interpretation, optional waveform stage |
| 03 | `case03-ndp-wildfire-weather-fusion` | NDP hazards/weather | Wildfire features plus weather/context fusion, multi-branch analysis, caveats |
| 04 | `case04-ndp-cimis-fire-risk-profile` | NDP/CIMIS/weather | Station resource selection, CSV staging, profile, plot, fire-risk interpretation |
| 05 | `case05-genomics-cohort-qc` | Genomics | Cohort QC, per-sample fanout, manifest reconciliation, variant/reference review |
| 06 | `case06-genomics-memory-followup` | Genomics/memory | Workspace-scoped memory follow-up with no cross-workspace leakage |
| 07 | `case07-proteomics-lfq-cohort-review` | Proteomics | mzML/LFQ quality, search readiness, differential signal, collaborator handoff |
| 08 | `case08-hpc-io-regression-root-cause` | HPC I/O | Baseline/candidate trace comparison, regression diff, root-cause synthesis |
| 09 | `case09-format-bridge-integrity` | Scientific formats | Source inspection, conversion policy, integrity validation, lossy caveats |
| 10 | `case10-terrain-lidar-suitability` | Terrain/lidar | Catalog/staging, gridding/derivation, suitability, visualization |
| 11 | `case11-custom-mcp-scientific-workflow` | MCP/tooling | Pack-local MCP in a scientific workflow with trust, scope, launch, call evidence |
| 12 | `case12-workspace-marketplace-swap` | Marketplace/workspace | Two packs, workspace isolation, memory scoping, pack swap semantics |

## Minimum Complexity

Each final case should require at least three meaningful stages. Most should
show four or more:

1. discovery or input inspection;
2. candidate selection or validation;
3. domain analysis;
4. artifact or structured evidence generation;
5. parent synthesis with caveats and next actions.

At least six cases must show explicit multi-branch or fanout/merge behavior.
At least four cases must use live external data. At least four cases must
generate verified artifacts. At least three cases must include a bounded
failure or recovery path.
