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

Use `MARKETPLACE_MCP_SCOPE_REPORT.md` /
`MARKETPLACE_MCP_SCOPE_EVIDENCE.jsonl` as the focused semantic-regression proof
for pack-defined MCP descriptor scope.

Use `MARKETPLACE_MCP_ENABLED_EXECUTION_REPORT.md` /
`MARKETPLACE_MCP_ENABLED_EXECUTION_EVIDENCE.jsonl` as the focused
semantic-regression proof that a marketplace pack-local MCP descriptor can be
explicitly trusted, launched, probed, and called through CLIO.

Use `MARKETPLACE_PACKAGED_HOOK_REPORT.md` /
`MARKETPLACE_PACKAGED_HOOK_EVIDENCE.jsonl` as the focused semantic-regression
proof that a marketplace pack-local hook descriptor can be explicitly trusted,
enabled, and invoked with packaged provenance during session message handling.

Use `MARKETPLACE_WORKSPACE_MEMORY_SCOPE_REPORT.md` /
`MARKETPLACE_WORKSPACE_MEMORY_SCOPE_EVIDENCE.jsonl` as the focused
semantic-regression proof that CLIO's agent-callable memory tools enforce
same-workspace intent and deny other-workspace reads.

Use `GENOMICS_COHORT_QC_INFRASTRUCTURE.md` as the current infrastructure note
for progress toward the benchmark-source Genomics Cohort QC case. It documents
the new reusable VCF cohort QC tool and the marketplace `cohort_qc` expert, but
does not claim the final planted-defect end-to-end benchmark has run yet.

Use `PROTEOMICS_LFQ_INFRASTRUCTURE.md` as the current infrastructure note for
progress toward the benchmark-source Proteomics LFQ differential-abundance
case. It documents the new reusable LFQ matrix tool, but does not claim the
final spike-in end-to-end benchmark has run yet.

Use `HPC_DARSHAN_INFRASTRUCTURE.md` as the current infrastructure note for
progress toward the benchmark-source HPC I/O regression case. It documents the
new reusable Darshan text parser and two-trace comparison tool, but does not
claim the final two-branch end-to-end benchmark has run yet.

Use `FORMAT_BRIDGE_INFRASTRUCTURE.md` as the current infrastructure note for
progress toward the benchmark-source scientific format bridge case. It
documents the new reusable HDF5-to-Parquet conversion/integrity tool, but does
not claim the final inspect -> convert/policy -> integrity -> visualization
end-to-end benchmark has run yet.

Use `TERRAIN_SUITABILITY_INFRASTRUCTURE.md` as the current infrastructure note
for progress toward the benchmark-source terrain/lidar site-suitability case.
It documents the new reusable DEM terrain and point-cloud gridding tools, but
does not claim the final NDP/OpenTopography end-to-end benchmark has run yet.

Use `FIRST_WAVE_MARKETPLACE_RUNNER_COVERAGE.md` as the current runner-coverage
note for the expanded first-wave marketplace benchmark lane. It documents that
the runner now has deterministic fixtures and canonical cases for cohort QC,
LFQ, HPC regression, format bridge, and terrain suitability, but does not claim
the final real-provider marketplace lane has run yet.

Use `DEMO_READINESS_HPC_SKILLS_REPORT.md` /
`DEMO_READINESS_HPC_SKILLS_EVIDENCE.jsonl` as the current focused watched
evidence for the HPC I/O regression marketplace pack after adding pack-local
skill bodies in `JaimeCernuda/clio-agent-marketplace#29`. It proves one
codex-provider run with live semantic events, root-owned sync delegation,
HPC parser/comparison tools, and runtime skill provenance resolving declared
HPC skills from the installed pack with `missing=[]`.

Use `DEMO_READINESS_PROTEOMICS_SKILLS_REPORT.md` /
`DEMO_READINESS_PROTEOMICS_SKILLS_EVIDENCE.jsonl` as the focused non-HPC watched
evidence after `JaimeCernuda/clio-agent-marketplace#30` added pack-local skill
bodies for the remaining first-wave packs. It proves a codex-provider
proteomics LFQ run with live semantic events, `main -> lfq_differential -> main`
sync delegation, the LFQ differential-abundance tool, and runtime skill
provenance resolving both root routing skills and LFQ expert skills from
`scope=pack` with `missing=[]`.

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
- The focused MCP scope case proves CLIO can load the `mcp-calculator-smoke`
  marketplace pack and expose its pack-local MCP descriptor in session
  metadata. The descriptor is disabled by default, derives a stdio launch from
  `mcp/calculator_server.py`, declares `calculator_add`, and records
  `trust.policy=explicit` / `trusted=false`.
- The focused enabled MCP execution case proves the same pack-local descriptor
  can be explicitly trusted, launched as a real stdio FastMCP server, probed to
  ready, and called through `/v1/mcp/servers/{server_id}/call`. This is an
  action-only infrastructure proof, so it does not claim model reasoning,
  hierarchy depth, or provider-turn success.
- The focused packaged hook case proves CLIO can load the `hook-smoke`
  marketplace pack, expose its disabled-by-default `pre_message` hook
  descriptor, explicitly trust and enable it, and record live
  `hook.pre_message.blocked` semantic events with `source=agent_blueprint`,
  checksum, trust, installed path, and Agent Blueprint provenance. This is an
  action-only infrastructure proof, so it does not claim model reasoning,
  hierarchy depth, or provider-turn success.
- The focused workspace memory case proves CLIO denies cross-session memory
  search without user intent, allows same-workspace memory search with explicit
  intent and `gact_memory_tool` provenance, and denies another workspace's
  session-summary read with `deny_other_workspace`.
- The focused HPC skill-resolution case proves the `hpc-io-regression` pack can
  load pack-local skills into expert runtime provenance while preserving the
  observed hierarchy chain `main -> trace_ingest -> baseline_ingest ->
  trace_ingest -> main -> regression_diff -> main -> root_cause -> main`.
- The focused proteomics skill-resolution case proves the broader marketplace
  skill-body fix is visible to a non-HPC pack at runtime: root route skills and
  `lfq_differential` skills are loaded from the installed pack, and the LFQ tool
  identifies the planted spike-in behavior in the generated benchmark fixture.

Do not overclaim this as a single clean full-lane rerun after the geospatial
fix. It is a combined evidence set: one corrected full run plus focused proofs
for the geospatial retry, MCP descriptor scope, enabled MCP execution, and
packaged hook invocation, workspace memory scope, and HPC pack-local skill
resolution, plus focused proteomics pack-local skill resolution.

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
- more benchmark-design cases from
  `CLIO_HIERARCHICAL_AGENT_BENCHMARK_CASES_SOURCE.md`, especially genomics
  cohort QC, proteomics LFQ, HPC I/O regression, scientific format bridge, and
  terrain/NDP final end-to-end runs. The runner now has canonical marketplace
  cases for those first-wave packs; the pending work is the watched
  real-provider execution and evidence review.
- watched full-lane evidence after `JaimeCernuda/clio-agent-marketplace#28`
  closed the remaining pack-local skill-body gap. The focused proteomics run
  proves runtime resolution for one non-HPC pack; it is not a substitute for
  rerunning every first-wave pack.

These are future benchmark coverage goals, not contradictions of the current
marketplace hierarchy pass.
