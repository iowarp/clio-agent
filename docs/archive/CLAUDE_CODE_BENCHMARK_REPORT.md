# CLIO Claude Code Real-Provider Benchmark Report

Generated: 2026-05-27 22:28:14 CDT
Evidence JSONL: `/home/jcernuda/clio-agent/docs/CLAUDE_CODE_BENCHMARK_EVIDENCE.jsonl`
Benchmark lane: `claude_code`

Result: 3/5 clean passes, 1 expected surfaced errors, 1 expected cancellations, 0 partial recoveries, 0 failures.

Stress coverage: does not yet meet the documented benchmark standard.

## Stress Coverage Audit

| Criterion | Observed | Required | Status |
| --- | ---: | ---: | --- |
| at least ten complex collaborator-grade demos | 1 | 10 | gap |
| at least five long or high-event stress cases | 1 | 5 | gap |
| at least three cases with tier-3 agents or nanoagents | 1 | 3 | gap |
| at least three visualization artifacts from analyzed data | 0 | 3 | gap |
| at least two deliberate surfaced-error cases | 1 | 2 | gap |
| at least one context-pressure or compaction case | 0 | 1 | gap |
| at least one provider/model-swap stress case | 0 | 1 | gap |

High-event or long-running cases:

- reasoning_cross_file_triage_nanoagents (9.5s, 11 events)

## Provider Lane Audit

| Criterion | Observed | Required | Status |
| --- | ---: | ---: | --- |
| provider/model recorded for every case | 5 | 5 | pass |
| planner JSON/routing reliability case passes | 1 | 1 | pass |
| tool-call argument generation cases pass | 2 | 2 | pass |
| stream provenance captured | 5 | 5 | pass |
| cancellation surfaces as structured cancelled turn | 1 | 1 | pass |
| provider-specific failures stay visible | 1 | 1 | pass |

Provider evidence details:

- workflow_hdf5_overview: stream_source=batch, fallback=provider_streaming_unsupported
- workflow_parquet_profile: stream_source=batch, fallback=provider_streaming_unsupported
- reasoning_cross_file_triage_nanoagents: stream_source=batch, fallback=provider_streaming_unsupported
- missing_hdf5_error: stream_source=batch, fallback=provider_streaming_unsupported
- claude_cancellation_surface: stream_source=batch, fallback=provider_streaming_unsupported

## All Cases

| Case | Category | Mode | Source | Outcome | Agent | Handoffs | Tools | Children | Elapsed |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| workflow_hdf5_overview | tooling | auto | dspy | pass | data | data | hdf5_analyze_file, hdf5_list_datasets | 0 | 10.0s |
| workflow_parquet_profile | analysis | auto | dspy | pass | analysis | analysis | parquet_analyze_schema, parquet_compute_statistics, parquet_compute_statistics, parquet_compute_statistics, parquet_compute_statistics | 0 | 9.5s |
| reasoning_cross_file_triage_nanoagents | planner-hardening | reasoning_only | dspy | pass | analysis | analysis | hdf5_analyze_file, hdf5_list_datasets, adios_inspect_file, parquet_analyze_schema, parquet_compute_statistics, csv_read_table | 4 | 9.5s |
| missing_hdf5_error | hardening | auto | dspy | expected_error | data | data | hdf5_list_datasets | 0 | 19.6s |
| claude_cancellation_surface | provider-hardening | auto | dspy | cancelled | chat | - | - | 0 | 0.0s |

## Best 10 Demo Prompts

### 1. No-guard cross-file triage

Case: `reasoning_cross_file_triage_nanoagents`
Category: planner-hardening
Routing mode: `reasoning_only`
Status: pass
Selected agent: `analysis`
Provider/model: `claude_code` / `sonnet` via `claude-code://exec`
Provider settings: temperature=1.0, max_tokens=32000, context_length=0, thinking_budget=0
Route graph: analysis -> [csv_validator subagent, analysis_validator subagent, adios_validator subagent, data_validator subagent]
Expert handoffs: analysis
Tools: hdf5_analyze_file, hdf5_list_datasets, adios_inspect_file, parquet_analyze_schema, parquet_compute_statistics, csv_read_table
Setup turns: 0
Actions: none
Child sessions: csv_validator subagent, analysis_validator subagent, adios_validator subagent, data_validator subagent
Artifacts: none
Elapsed: 9.5s

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

### 2. Parquet facility profile

Case: `workflow_parquet_profile`
Category: analysis
Routing mode: `auto`
Status: pass
Selected agent: `analysis`
Provider/model: `claude_code` / `sonnet` via `claude-code://exec`
Provider settings: temperature=1.0, max_tokens=32000, context_length=0, thinking_budget=0
Route graph: analysis
Expert handoffs: analysis
Tools: parquet_analyze_schema, parquet_compute_statistics, parquet_compute_statistics, parquet_compute_statistics, parquet_compute_statistics
Setup turns: 0
Actions: none
Child sessions: none
Artifacts: none
Elapsed: 9.5s

Prompt:

```text
Profile the facility measurements in /home/jcernuda/clio-agent/tmp/clio-benchmark-data/facility_measurements.parquet. I care about schema, row groups, and whether temperature_k, pressure_pa, humidity_pct, and anomaly_score look sane.
```

What to see: Analysis expert reads Parquet schema and computes statistics for named fields.

Why this is interesting: Checks statistical tool calls and model feedback from multiple numeric observations.

Observed excerpt:

```text
analysis | success | planner_dispatch | Inspected Parquet file /home/jcernuda/clio-agent/tmp/clio-benchmark-data/facility_measurements.parquet. It has 3000 rows, 10 columns, and 8 row groups. - sample_id: int64, nullable=True - run_id: string, nullable=True - site: string, nullable=True - temperature_k: double, nullable=True - pressure_pa: double, nullable=True - humidity_pct: double, nullable=True - vibration_mm_s: double, nullable=True - anomaly_score: double, nullable=True - quality_flag: string, nullable=True - vali...[truncated]
Inspected Parquet file /home/jcernuda/clio-agent/tmp/clio-benchmark-data/facility_measurements.parquet. It has 3000 rows, 10 columns, and 8 row groups.
- sample_id: int64, nullable=True
- run_id: string, nullable=True
- site: string, nullable=True
- temperature_k: double, nullable=True
- pressure_pa: double, nullable=True
- humidity_pct: double, nullable=T
```

### 3. HDF5 fusion file overview

Case: `workflow_hdf5_overview`
Category: tooling
Routing mode: `auto`
Status: pass
Selected agent: `data`
Provider/model: `claude_code` / `sonnet` via `claude-code://exec`
Provider settings: temperature=1.0, max_tokens=32000, context_length=0, thinking_budget=0
Route graph: data
Expert handoffs: data
Tools: hdf5_analyze_file, hdf5_list_datasets
Setup turns: 0
Actions: none
Child sessions: none
Artifacts: none
Elapsed: 10.0s

Prompt:

```text
I need to brief collaborators on this fusion output: /home/jcernuda/clio-agent/tmp/clio-benchmark-data/fusion_run.h5. What datasets are inside, what shapes and units matter, and what compression details should I mention?
```

What to see: Data expert uses HDF5 tools and summarizes datasets, units, and compression.

Why this is interesting: Proves real HDF5 path handling, tool argument generation, and grounded synthesis.

Observed excerpt:

```text
data | success | planner_dispatch | Inspected HDF5 file /home/jcernuda/clio-agent/tmp/clio-benchmark-data/fusion_run.h5. It contains 7 datasets and 4 groups. - axes/diagnostic_channel: shape=[12], dtype=int32, size=48 B - axes/radius_norm: shape=[64], dtype=float64, size=512 B - axes/time_hours: shape=[96], dtype=float64, size=768 B - diagnostics/heat_flux: shape=[96, 12], dtype=float32, size=4.5 KiB, units=MW/m^2 - plasma/density: shape=[96, 64], dtype=float64, size=48.0 KiB, units=m^-3 - plasma/electron_temperatur...[truncated]
Inspected HDF5 file /home/jcernuda/clio-agent/tmp/clio-benchmark-data/fusion_run.h5. It contains 7 datasets and 4 groups.
- axes/diagnostic_channel: shape=[12], dtype=int32, size=48 B
- axes/radius_norm: shape=[64], dtype=float64, size=512 B
- axes/time_hours: shape=[96], dtype=float64, size=768 B
- diagnostics/heat_flux: shape=[96, 12], dtype=float32, size=4.
```

### 4. Missing file error surfacing

Case: `missing_hdf5_error`
Category: hardening
Routing mode: `auto`
Status: expected_error
Selected agent: `data`
Provider/model: `claude_code` / `sonnet` via `claude-code://exec`
Provider settings: temperature=1.0, max_tokens=32000, context_length=0, thinking_budget=0
Route graph: data
Expert handoffs: data
Tools: hdf5_list_datasets
Setup turns: 0
Actions: none
Child sessions: none
Artifacts: none
Elapsed: 19.6s

Prompt:

```text
Inspect this HDF5 file and tell me what datasets are inside: /home/jcernuda/clio-agent/tmp/clio-benchmark-data/missing_fusion_run.h5. If the file is unavailable, surface the real error.
```

What to see: CLIO returns structured error_info and no normal fake assistant answer.

Why this is interesting: Verifies errors are surfaced, not hidden behind canned or repeated text.

Observed excerpt:

```text
data | failure | direct_tool
```

### 5. Claude Code cancellation surface

Case: `claude_cancellation_surface`
Category: provider-hardening
Routing mode: `auto`
Status: cancelled
Selected agent: `chat`
Provider/model: `claude_code` / `sonnet` via `claude-code://exec`
Provider settings: temperature=1.0, max_tokens=32000, context_length=0, thinking_budget=0
Route graph: chat
Expert handoffs: none
Tools: none
Setup turns: 0
Actions: none
Child sessions: none
Artifacts: none
Elapsed: 0.0s

Prompt:

```text
Cancellation benchmark: prepare a very detailed scientific review plan for /home/jcernuda/clio-agent/tmp/clio-benchmark-data/fusion_run.h5, /home/jcernuda/clio-agent/tmp/clio-benchmark-data/facility_measurements.parquet, and /home/jcernuda/clio-agent/tmp/clio-benchmark-data/sensor_events.csv. Include staged reasoning, validation checks, schema comparisons, and collaborator handoff notes so this turn should remain active long enough for the benchmark runner to cancel it.
```

What to see: CLIO settles the GACT envelope as a structured cancelled turn.

Why this is interesting: The Claude lane must prove cancellation surfacing separately from successful tool and planner cases, without claiming hard upstream abort.

Observed excerpt:

```text
<no assistant text>
```

## Failures Fixed During This Campaign

- GACT compaction originally bypassed transient-provider retry and only updated the GACT transcript; compaction now retries provider throttles, updates ARC memory, and fails with structured errors if memory storage fails.
- Compact summaries could lose exact scientific identifiers at the ARC truncation boundary; compact memory now preserves a labeled exact evidence index for paths, variables, columns, artifacts, and caveats.
- Retained multi-file context could make analysis narrow to the first file or let CSV follow-ups be stolen by broad synthesis; explicit file paths now take precedence and retained multi-source synthesis is limited to true synthesis questions.
- Visualization-intent follow-ups could route to analysis or a data tool even when the user asked for a chart/dashboard; file-grounded visual artifact requests are promoted to the visualization expert.
- Direct planner-selected NDP and Parquet/statistical tool actions could flatten expert ownership; NDP catalog work is promoted to the nested `ndp_catalog` expert, and statistical Parquet triage is promoted to `analysis`.
- Provider throttles during expert dispatch, handoffs, and compaction could surface as brittle partial recoveries; expert paths now use bounded transient-provider retry and still surface structured errors if exhausted.

## Remaining Caveats

- This report is evidence for the recorded ALCF run, not a guarantee that ALCF availability, model latency, or token freshness will be identical later.
- Several high-event cases are intentionally fast because child/nanoagent workers use deterministic local tools after routing; elapsed time alone should not be treated as benchmark depth.
- Two cases are deliberate surfaced-error checks. They are counted as successful hardening cases only because they returned structured errors without normal-looking fake assistant text.
- The benchmark now covers the hierarchy and handoff classes listed here, but future providers, file formats, and per-expert model assignments still need their own evidence runs.
