# CLIO Real-Orchestrator Benchmark Report

Generated: 2026-05-28 22:40:17 CDT
Evidence JSONL: `/home/jcernuda/clio-agent/benchmark/CROSS_FILE_TRIAGE_EVIDENCE.jsonl`
Benchmark lane: `real_orchestrator`

This is a CLIO session-evidence audit. It is produced from real session JSONL rows and should be reviewed as prompt, route, tool, artifact, error, and final-answer evidence. Pytest coverage only guards the harness and tools; it is not the benchmark result.

Result: 1/1 clean passes, 0 expected surfaced errors, 0 expected cancellations, 0 partial recoveries, 0 failures.

Stress coverage: does not yet meet the documented benchmark standard.

## Stress Coverage Audit

| Criterion | Observed | Required | Status |
| --- | ---: | ---: | --- |
| at least ten complex collaborator-grade demos | 1 | 10 | gap |
| at least five long or high-event stress cases | 1 | 5 | gap |
| at least three cases with tier-3 agents or nanoagents | 1 | 3 | gap |
| at least three visualization artifacts from analyzed data | 0 | 3 | gap |
| at least two deliberate surfaced-error cases | 0 | 2 | gap |
| at least one context-pressure or compaction case | 0 | 1 | gap |
| at least one provider/model-swap stress case | 0 | 1 | gap |

High-event or long-running cases:

- reasoning_cross_file_triage_nanoagents (7.1s, 11 events)

## Evidence Summary

- Max elapsed case: `reasoning_cross_file_triage_nanoagents` (7.1s)
- Max expert depth: `reasoning_cross_file_triage_nanoagents` (1)
- Max branch fanout: `reasoning_cross_file_triage_nanoagents` (4)
- Unique tools used: adios_inspect_file, csv_read_table, hdf5_analyze_file, hdf5_list_datasets, parquet_analyze_schema, parquet_compute_statistics
- Data/input files referenced: 3
- Artifacts verified on disk: 0/0

## Provider Lane Audit

| Criterion | Observed | Required | Status |
| --- | ---: | ---: | --- |
| all selected cases avoid shortcut route sources | 1 | 1 | pass |
| passing cases include structured route/tool evidence | 1 | 1 | pass |
| artifact-producing cases verify artifacts on disk | 0 | 0 | pass |
| planner multi-file hierarchy case passes | 1 | 1 | pass |
| dirty cross-file quality gate passes | 0 | 1 | gap |
| NDP waveform benchmark reaches verified SAC/PNG artifact | 0 | 1 | gap |
| NDP full SAC/PNG chain verified | 0 | 1 | gap |

Provider evidence details:

- full SAC/PNG path not reached in this run

## All Cases

| Case | Category | Mode | Source | Outcome | Agent | Handoffs | Tools | Children | Elapsed |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| reasoning_cross_file_triage_nanoagents | planner-hardening | reasoning_only | dspy | pass | analysis | analysis | hdf5_analyze_file, hdf5_list_datasets, adios_inspect_file, parquet_analyze_schema, parquet_compute_statistics, csv_read_table | 4 | 7.1s |

## Best 10 Demo Prompts

### 1. No-guard cross-file triage

Case: `reasoning_cross_file_triage_nanoagents`
Category: planner-hardening
Routing mode: `reasoning_only`
Status: pass
Selected agent: `analysis`
Provider/model: `codex` / `gpt-5.5` via `codex://exec`
Provider settings: temperature=1.0, max_tokens=32000, context_length=0, thinking_budget=0
Route graph: analysis -> [csv_validator subagent, analysis_validator subagent, adios_validator subagent, data_validator subagent]
Route metrics: depth=1, branches=4, tools=6
Expert handoffs: analysis
Tools: hdf5_analyze_file, hdf5_list_datasets, adios_inspect_file, parquet_analyze_schema, parquet_compute_statistics, csv_read_table
Data/input files: /home/jcernuda/clio-agent/tmp/clio-benchmark-data/fusion_run.h5, /home/jcernuda/clio-agent/tmp/clio-benchmark-data/facility_measurements.parquet, /home/jcernuda/clio-agent/tmp/clio-benchmark-data/sensor_events.csv
Setup turns: 0
Actions: none
Child sessions: csv_validator subagent, analysis_validator subagent, adios_validator subagent, data_validator subagent
Artifacts: none
Artifact evidence: none
Elapsed: 7.1s

Prompt:

```text
I have four related files from the same experiment: /home/jcernuda/clio-agent/tmp/clio-benchmark-data/fusion_run.h5, /home/jcernuda/clio-agent/tmp/clio-benchmark-data/facility_measurements.parquet, /home/jcernuda/clio-agent/tmp/clio-benchmark-data/sensor_events.csv, and "/home/jcernuda/clio-agent/tmp/clio-benchmark-data/gray scott noise 0.01 data.bp5". Give me a cross-file triage summary: what is in each file, whether the measurements look ready for downstream analysis, and what I should check next.
```

What to see: With routing guards disabled for the session, the planner still reaches analysis and tool-backed child workers.

Why this is interesting: Separates planner capability from the production registry guard, which matters as CLIO grows beyond a few built-in experts.

Observed excerpt:

```text
analysis | success | planner_dispatch | Parallel validation completed with tool-backed nanoagents. data_validator: Inspected HDF5 file /home/jcernuda/clio-agent/tmp/clio-benchmark-data/fusion_run.h5. It contains 7 datasets and 4 groups. - axes/diagnostic_channel: shape=[12], dtype=int32, size=48 B - axes/radius_norm: shape=[64], dtype=float64, size=512 B - axes/time_hours: shape=[96], dtype=float64, size=768 B - diagnostics/heat_flux: shape=[96, 12], dtype=float32, size=4.5 KiB, units=MW/m^2 - plasma/density: shape=[96,...[truncated]
Parallel validation completed with tool-backed nanoagents.

data_validator:
Inspected HDF5 file /home/jcernuda/clio-agent/tmp/clio-benchmark-data/fusion_run.h5. It contains 7 datasets and 4 groups.
- axes/diagnostic_channel: shape=[12], dtype=int32, size=48 B
- axes/radius_norm: shape=[64], dtype=float64, size=512 B
- axes/time_hours: shape=[96], dtype=flo
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
