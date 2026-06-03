# CLIO Benchmark Current Status

Updated: 2026-06-03

This file is the human entry point for the current benchmark evidence. Older
reports in this directory are useful debugging history, but they are not all
current release evidence.

## Current Marketplace Evidence

Use `MARKETPLACE_COMPLEX_HIERARCHY_REPORT.md` /
`MARKETPLACE_COMPLEX_HIERARCHY_EVIDENCE.jsonl` plus
`MARKETPLACE_GEOSPATIAL_RETRY_REPORT.md` /
`MARKETPLACE_GEOSPATIAL_RETRY_EVIDENCE.jsonl` as the current June 3 marketplace
evidence set.

Current result:

- The re-rendered full marketplace report records `5/6` clean passes after the
  benchmark criteria were corrected. It proves five active Agent Blueprints and
  shows five complex hierarchy cases: genomics FASTA, genomics VCF, materials
  CIF, proteomics mzML, and seismic waveform review. The raw JSONL preserves the
  original runner verdicts from before that criteria correction.
- The one full-run failure was `marketplace_geospatial_field_review`, where the
  root returned delegation prose instead of executable child work. CLIO issue
  #580 and marketplace issue #12 captured that defect.
- After the CLIO and marketplace fixes merged, the focused geospatial retry
  records `1/1` clean pass with live semantic events, root sync delegation
  `main -> spatial_features -> main`, and two `geospatial_inspect_geojson`
  tool calls.
- The combined evidence proves the current merged marketplace packs can load
  per session, activate the requested pack root, perform sync delegation and
  parent resume, expose pack-scoped tools, and execute multiple non-seismic
  nested expert paths.
- The seismic case still provides the strongest science chain:
  `orchestrator -> main -> data -> ndp_catalog -> data -> main -> analysis
  -> sac_format -> analysis -> main -> visualization -> main`.
- The seismic case creates and verifies a PNG artifact:
  `.clio-agent-artifacts/charts/sac_traces_earthscope_IU_ANMO_00_BHZ_2010-02-27T063000.png`.

Do not overclaim this as a single clean full-lane rerun after the geospatial
fix. It is a combined evidence set: one corrected full run plus a focused retry
for the only failed case.

## What Failed And Was Fixed

- NDP staging can fail on selected remote resources. The seismic marketplace
  case now records the NDP blocker and recovers with an observed EarthScope SAC
  source before analysis and visualization.
- Sync delegation originally looked flat or incomplete in reports. The route
  graph now records both handoff and return edges for delegated expert results.
- Child expert tool telemetry was not reliably attached to the parent turn and
  could leak across sessions. Sync child execution is now scoped to the active
  parent turn, so parent messages carry the correct child tool calls.
- Earlier marketplace cases proved direct child expert execution. They now prove
  root-owned Agent semantics: root `main` delegates to the child expert, receives
  the child result, and synthesizes the final answer.
- Keyword user-agent routing is opt-in, so real-orchestrator benchmarks do not
  silently pass through keyword shortcuts.
- Non-seismic marketplace packs were too shallow for broad hierarchy claims.
  Genomics, materials, and proteomics now include nested continuation contracts
  and the benchmark lane requires complex depth/branch evidence for those
  cases.
- Dynamic blueprint roots could finalize with prose like "delegating to the
  child expert" without returning executable `expert_handoffs`. CLIO now treats
  delegation prose as pending child work, and the geospatial pack was tightened
  to perform an actual `spatial_features` handoff before final synthesis.

## Historical Or Superseded Evidence

`FRESH_REAL_ORCHESTRATOR_REPORT.md` and
`FRESH_REAL_ORCHESTRATOR_EVIDENCE.jsonl` are retained as a historical isolated
real-orchestrator replay from before the marketplace seismic recovery path was
validated. They still show `ndp_seismic_waveform_to_plot` failing to reach the
SAC/PNG artifact and must not be cited as the current NDP/seismic status.

The current replacement evidence for that capability is
`marketplace_seismic_waveform_review` in
`MARKETPLACE_COMPLEX_HIERARCHY_REPORT.md`.

`MARKETPLACE_UNIFIED_REPORT.md`,
`MARKETPLACE_UNIFIED_EVIDENCE.jsonl`,
`MARKETPLACE_AGENT_BENCHMARK_REPORT.md`, and
`MARKETPLACE_AGENT_BENCHMARK_EVIDENCE.jsonl` are May 29 historical evidence.
They are useful for regression comparison, but they do not include the June 3
non-seismic complex hierarchy expansion.

## Remaining Future Stress Gaps

The current marketplace lane now has broad first-wave hierarchy proof across
seismic plus non-seismic packs. Remaining benchmark expansion items are:

- at least ten complex collaborator-grade demos,
- at least three tier-3 or nanoagent cases,
- at least three visualization artifacts,
- at least two deliberate surfaced-error cases,
- at least one context-pressure or compaction case,
- at least one provider/model-swap stress case.
- pack-defined custom MCP runtime evidence beyond the smoke calculator and OGC
  descriptor cases,
- more benchmark-design cases from
  `CLIO_HIERARCHICAL_AGENT_BENCHMARK_CASES_SOURCE.md`, especially genomics
  cohort QC, proteomics LFQ, HPC I/O regression, scientific format bridge, and
  terrain/NDP.

These are future benchmark coverage goals, not contradictions of the current
marketplace hierarchy pass.
