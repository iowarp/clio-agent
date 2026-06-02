# CLIO Benchmark Current Status

Updated: 2026-05-29

This file is the human entry point for the current benchmark evidence. Older
reports in this directory are useful debugging history, but they are not all
current release evidence.

## Current Passing Evidence

Use `MARKETPLACE_UNIFIED_REPORT.md` and
`MARKETPLACE_UNIFIED_EVIDENCE.jsonl` as the current proof for the
marketplace-based smoke/load work and the current seismic complex hierarchy
case. Do not cite it as broad marketplace hierarchy coverage yet.

Current result:

- `6/6` marketplace cases pass under their case-specific smoke/tool criteria.
- Five Agent Blueprints are loaded from `clio-agent-marketplace`:
  `genomics-review`, `geospatial-field-review`,
  `materials-crystal-review`, `proteomics-mzml-review`, and
  `seismic-waveform-review`.
- Every marketplace case selects the pack root expert `main`.
- Every marketplace case records synchronous root-to-child delegation and
  return provenance.
- Every marketplace case records at least one blueprint expert tool call.
- The seismic case reaches the full current science chain:
  `orchestrator -> main -> data -> ndp_catalog -> data -> main -> analysis
  -> sac_format -> analysis -> main -> visualization -> main`.
- The seismic case creates and verifies a PNG artifact:
  `.clio-agent-artifacts/charts/sac_traces_earthscope_IU_ANMO_00_BHZ_2010-02-27T063000.png`.
- Strict marketplace lane criteria now require at least three complex
  marketplace hierarchy cases before the lane can be cited as broad hierarchy
  coverage. Complex means depth >= 3, branch count >= 2, sync handoff count >=
  2, and complete parent-return provenance.

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

## Historical Or Superseded Evidence

`FRESH_REAL_ORCHESTRATOR_REPORT.md` and
`FRESH_REAL_ORCHESTRATOR_EVIDENCE.jsonl` are retained as a historical isolated
real-orchestrator replay from before the marketplace seismic recovery path was
validated. They still show `ndp_seismic_waveform_to_plot` failing to reach the
SAC/PNG artifact and must not be cited as the current NDP/seismic status.

The current replacement evidence for that capability is
`marketplace_seismic_waveform_review` in `MARKETPLACE_UNIFIED_REPORT.md`.

## Remaining Future Stress Gaps

The current marketplace lane intentionally proves Agent Blueprint activation,
root-owned delegation, and representative domain tools. It is not yet broad
complex hierarchy evidence. Remaining benchmark expansion items are:

- at least three complex marketplace Agent Blueprint cases, not only seismic,
- at least ten complex collaborator-grade demos,
- at least five long or high-event stress cases,
- at least three tier-3 or nanoagent cases,
- at least three visualization artifacts,
- at least two deliberate surfaced-error cases,
- at least one context-pressure or compaction case,
- at least one provider/model-swap stress case.

These are future benchmark coverage goals, not contradictions of the current
marketplace hierarchy pass.
