# CLIO Real-Orchestrator Benchmark Report

Generated: 2026-05-28 22:29:59 CDT
Evidence JSONL: `/home/jcernuda/clio-agent/benchmark/VISUAL_MULTITURN_EVIDENCE.jsonl`
Benchmark lane: `real_orchestrator`

This is a CLIO session-evidence audit. It is produced from real session JSONL rows and should be reviewed as prompt, route, tool, artifact, error, and final-answer evidence. Pytest coverage only guards the harness and tools; it is not the benchmark result.

Result: 2/2 clean passes, 0 expected surfaced errors, 0 expected cancellations, 0 partial recoveries, 0 failures.

Stress coverage: does not yet meet the documented benchmark standard.

## Stress Coverage Audit

| Criterion | Observed | Required | Status |
| --- | ---: | ---: | --- |
| at least ten complex collaborator-grade demos | 2 | 10 | gap |
| at least five long or high-event stress cases | 0 | 5 | gap |
| at least three cases with tier-3 agents or nanoagents | 0 | 3 | gap |
| at least three visualization artifacts from analyzed data | 2 | 3 | gap |
| at least two deliberate surfaced-error cases | 0 | 2 | gap |
| at least one context-pressure or compaction case | 0 | 1 | gap |
| at least one provider/model-swap stress case | 0 | 1 | gap |

## Evidence Summary

- Max elapsed case: `csv_status_visual_summary` (33.1s)
- Max expert depth: `csv_status_visual_summary` (1)
- Max branch fanout: `csv_status_visual_summary` (0)
- Unique tools used: plot_bar_chart, plot_summary
- Data/input files referenced: 2
- Artifacts verified on disk: 2/2

## Provider Lane Audit

| Criterion | Observed | Required | Status |
| --- | ---: | ---: | --- |
| all selected cases avoid shortcut route sources | 2 | 2 | pass |
| passing cases include structured route/tool evidence | 2 | 2 | pass |
| artifact-producing cases verify artifacts on disk | 2 | 2 | pass |
| planner multi-file hierarchy case passes | 0 | 1 | gap |
| dirty cross-file quality gate passes | 0 | 1 | gap |
| NDP waveform benchmark reaches verified SAC/PNG artifact | 0 | 1 | gap |
| NDP full SAC/PNG chain verified | 0 | 1 | gap |

Provider evidence details:

- full SAC/PNG path not reached in this run

## All Cases

| Case | Category | Mode | Source | Outcome | Agent | Handoffs | Tools | Children | Elapsed |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| csv_status_visual_summary | visualization | auto | dspy | pass | visualization | visualization | plot_bar_chart | 0 | 33.1s |
| dirty_quality_dashboard_multi_turn | visualization | auto | dspy | pass | visualization | visualization | plot_summary | 0 | 28.1s |

## Best 10 Demo Prompts

### 1. CSV status distribution chart

Case: `csv_status_visual_summary`
Category: visualization
Routing mode: `auto`
Status: pass
Selected agent: `visualization`
Provider/model: `codex` / `gpt-5.5` via `codex://exec`
Provider settings: temperature=1.0, max_tokens=32000, context_length=0, thinking_budget=0
Route graph: visualization
Route metrics: depth=1, branches=0, tools=1
Expert handoffs: visualization
Tools: plot_bar_chart
Data/input files: /home/jcernuda/clio-agent/tmp/clio-benchmark-data/sensor_events.csv
Setup turns: 1
Actions: none
Child sessions: none
Artifacts: /tmp/clio-agent-artifacts/charts/bar_chart_status.png
Artifact evidence: /tmp/clio-agent-artifacts/charts/bar_chart_status.png (ok, 20259 B)
Elapsed: 33.1s

Prompt:

```text
Create a PNG bar chart of the event status distribution from the CSV stream we just inspected. Tell me where it was saved and what field was plotted.
```

What to see: Visualization resolves the prior CSV context and plots the status field.

Why this is interesting: Exercises a CSV analysis-to-visualization handoff and verifies that charting is not limited to Parquet dashboards.

Observed excerpt:

```text
visualization | success | planner_dispatch | Generated a PNG bar chart showing the distribution of values in the `status` field from `/home/jcernuda/clio-agent/tmp/clio-benchmark-data/sensor_events.csv`. A bar chart was chosen because `status` is categorical and the goal was to compare event status frequencies.
Visualization: Generated a PNG bar chart showing the distribution of values in the `status` field from `/home/jcernuda/clio-agent/tmp/clio-benchmark-data/sensor_events.csv`. A bar chart was chosen because `status` is categorical and the goal was to compare event status frequencies.

File: /tmp/clio-agent-artifacts/charts/bar_chart_status.png
```

### 2. Dirty data dashboard after quality review

Case: `dirty_quality_dashboard_multi_turn`
Category: visualization
Routing mode: `auto`
Status: pass
Selected agent: `visualization`
Provider/model: `codex` / `gpt-5.5` via `codex://exec`
Provider settings: temperature=1.0, max_tokens=32000, context_length=0, thinking_budget=0
Route graph: visualization
Route metrics: depth=1, branches=0, tools=1
Expert handoffs: visualization
Tools: plot_summary
Data/input files: /home/jcernuda/clio-agent/tmp/clio-benchmark-data/facility_measurements_dirty.parquet
Setup turns: 1
Actions: none
Child sessions: none
Artifacts: /tmp/clio-agent-artifacts/charts/summary_facility_measurements_dirty.png
Artifact evidence: /tmp/clio-agent-artifacts/charts/summary_facility_measurements_dirty.png (ok, 125470 B)
Elapsed: 28.1s

Prompt:

```text
Create a compact dashboard PNG for the dirty Parquet export we just reviewed. Use it to support the quality review, and tell me where the artifact was saved.
```

What to see: Visualization resolves the reviewed dirty Parquet file from memory and creates a real dashboard artifact.

Why this is interesting: Stresses multi-turn analysis-to-visualization over intentionally dirty data, not only clean demo fixtures.

Observed excerpt:

```text
visualization | success | planner_dispatch | The generated PNG is a compact dataset summary dashboard showing data type composition, null counts, numeric distributions, and numeric correlation structure. This chart type was chosen because it provides a quick visual overview of schema shape and quality issues in the dirty Parquet export.
Visualization: The generated PNG is a compact dataset summary dashboard showing data type composition, null counts, numeric distributions, and numeric correlation structure. This chart type was chosen because it provides a quick visual overview of schema shape and quality issues in the dirty Parquet export.

File: /tmp/clio-agent-artifacts/charts/summary_facility_measurements_dirty.png
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
