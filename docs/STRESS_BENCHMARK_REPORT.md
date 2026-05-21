# CLIO Stress Benchmark Verification Report

Date: 2026-05-21

This report records what the real-provider benchmark suite currently proves,
which commands produced the evidence, and which ideas remain outside the
verified scope.

## Objective Checklist

The benchmark objective was to prove real CLIO/GACT scientific workflows
end-to-end, not just isolated provider or tool smoke tests.

| Requirement | Evidence | Status |
| --- | --- | --- |
| Rich local benchmark datasets | `scripts/create_benchmark_data.py` creates HDF5, clean Parquet, dirty Parquet, and CSV files under `tmp/clio-benchmark-data`. | Verified |
| Real provider execution | Final run used GACT against LM Studio at `http://127.0.0.1:1234/v1`, model `qwopus3.5-9b-v3`. | Verified for local Qwopus |
| DataExpert | `hdf5_structure` selected `data` and called HDF5 tools. | Verified |
| AnalysisExpert | `parquet_profile` and `csv_schema` selected `analysis` and called Parquet/CSV tools. | Verified |
| VisualizationExpert | `visualization_artifact` selected `visualization`, called `plot_summary`, and returned existing PNG artifacts. | Verified |
| Nano-agent fan-out | `nanoagent_parallel_tool_use` created child sessions and recorded HDF5, Parquet, and CSV tool provenance. | Verified |
| Multi-turn state | `visualization_artifact` runs after Parquet profiling in the same session and uses the profiled Parquet file. | Verified |
| Real HDF5 calls | `hdf5_analyze_file` and `hdf5_list_datasets` ran against `fusion_run.h5`. | Verified |
| Real Parquet calls | `parquet_analyze_schema` and repeated `parquet_compute_statistics` ran against `facility_measurements.parquet`. | Verified |
| Real CSV calls | `csv_read_table` ran against `sensor_events.csv`. | Verified |
| Tool-result feedback | Answers include tool-derived dataset names, units, schema fields, statistics, and artifact paths. | Verified |
| Artifact generation | `plot_summary` produced PNG artifacts recorded in audit rows. | Verified |
| Error surfacing | Missing HDF5 file produced `tool_error` with no normal assistant answer. | Verified |
| Cancellation surfacing | Cancellation produced `cancelled` with no normal assistant answer. | Verified |
| Streaming provenance | Chat streaming case completed with `stream_source="live"` and `delta_count=226`; suite also accepts explicit batch fallback metadata when provider streaming is unavailable. | Verified |
| Evidence per scenario | `CLIO_STRESS_AUDIT_LOG` JSONL rows include prompt, provider, dataset, selected expert, child sessions, tool calls, artifacts, errors, stream metadata, answer excerpt, and caveats. | Verified |
| Human demo guide | `docs/STRESS_BENCHMARK.md` includes setup, LM Studio/Qwopus configuration, and named collaborator demo prompts with expected behavior and rationale. | Verified |
| Verified-vs-gap distinction | This report and the demo guide separate local Qwopus evidence from optional ALCF/clio-kit expansion lanes. | Verified |

## Final Local Run

Command:

```powershell
$env:CLIO_INTEGRATION_BASE='http://127.0.0.1:17922'
$env:CLIO_BENCHMARK_DATA_DIR=(Resolve-Path 'tmp\clio-benchmark-data').Path
$env:CLIO_STRESS_AUDIT_LOG=(Join-Path (Resolve-Path 'tmp').Path 'clio-stress-audit-qwopus-final-17922.jsonl')
.\.venv\Scripts\python.exe -m pytest tests\test_stress_benchmark -m "integration and benchmark" -vv -s
```

Result:

```text
5 passed in 255.95s (0:04:15)
```

Provider reported by audit log:

```json
{
  "provider": "lm_studio",
  "model": "qwopus3.5-9b-v3",
  "api_base": "http://127.0.0.1:1234/v1",
  "transport": null
}
```

## Scenario Evidence

| Case | Selected Agent | Tools | Artifacts | Error | Stream |
| --- | --- | --- | --- | --- | --- |
| `hdf5_structure` | `data` | `hdf5_analyze_file`, `hdf5_list_datasets` | 0 | none | `batch` |
| `parquet_profile` | `analysis` | `parquet_analyze_schema`, four `parquet_compute_statistics` calls | 0 | none | `batch` |
| `csv_schema` | `analysis` | `csv_read_table` | 0 | none | `batch` |
| `visualization_artifact` | `visualization` | `plot_summary` | 2 PNG paths | none | `batch` |
| `workflow_pass` | `visualization` | `plot_summary` | 2 PNG paths | none | `batch` |
| `nanoagent_parallel_tool_use` | `analysis` | HDF5, Parquet, and CSV tool calls | 0 | partial `routing_error` at `parallel_validation_recovery` | `batch` |
| `missing_hdf5_error_surface` | `data` | `hdf5_list_datasets` | 0 | `tool_error` | `batch` |
| `cancellation_surface` | `chat` | none | 0 | `cancelled` | `batch` |
| `streaming_provenance` | `chat` | none | 0 | none | `live`, `delta_count=226` |

The partial `routing_error` in the nano-agent case is intentional and
non-blocking. It records that the local planner failed to emit valid JSON before
the explicit parallel-validation request was recovered through deterministic
analysis dispatch. The worker tool calls and child sessions are still real and
audited.

## Prompt-To-Artifact Mapping

| Prompt Area | Test Case | Concrete Artifact Or Evidence |
| --- | --- | --- |
| HDF5 tooling | `hdf5_structure` | Answer includes `electron_temperature`, `density`, `heat_flux`, and units `eV`, `m^-3`, `MW/m^2`; metadata includes HDF5 tool calls. |
| Parquet analysis | `parquet_profile` | Answer includes schema/statistics for `temperature_k`, `pressure_pa`, `humidity_pct`, and `anomaly_score`; metadata includes schema/statistics tools. |
| CSV inspection | `csv_schema` | Answer includes `event_id`, `temperature_k`, `pressure_pa`, `status`; metadata includes `csv_read_table`. |
| Visualization | `visualization_artifact` | Answer returns `.png` paths; at least one recorded artifact path exists on disk. |
| Nano-agents | `nanoagent_parallel_tool_use` | Parent answer includes `data_validator`, `analysis_validator`, `csv_validator`; child sessions contain HDF5, Parquet, and CSV tool provenance. |
| Error hardening | `missing_hdf5_error_surface` | Assistant text is empty; `error_info.error == "tool_error"`. |
| Cancellation | `cancellation_surface` | Assistant text is empty; `error_info.error == "cancelled"`. |
| Streaming truth | `streaming_provenance` | `message.part.delta` events observed; completed metadata reports `stream_source="live"`. |

## Fixes Landed During Benchmarking

The benchmark work found and fixed several real integration failures before the
final run:

- `fix(agent): synthesize after post-tool planner failure`
- `fix(agent): raise default planner step budget`
- `fix(agent): repair tool filepaths from context`
- `fix(agent): repair expert filepaths from context`
- `fix(agent): repair relative expert filepaths`
- `fix(agent): materialize tool-backed nanoagents`
- `fix(agent): summarize tool-backed nanoagents`
- `fix(gact): preserve agent trace tool results`
- `fix(gact): infer agent trace tool status`
- `fix(data): surface hdf5 dataset units`
- `test(benchmark): require hdf5 units evidence`

## Verified Behavior

The local Qwopus baseline now verifies that CLIO can:

- route real scientific prompts to the right expert path;
- generate valid tool arguments for HDF5, Parquet, CSV, and plotting tools;
- carry real tool outputs into answers;
- generate and report visualization artifacts;
- preserve multi-turn file context;
- spawn tool-backed nano-agent child sessions and aggregate their findings;
- surface missing-file and cancellation failures without fake assistant text;
- label streaming provenance truthfully as live or batch;
- write machine-readable evidence for each benchmark case.

## Remaining Gaps And Expansion Lanes

These are not claimed as verified by the current benchmark:

- ALCF provider behavior is documented as optional but was not part of the final
  green evidence run. Follow-up: #281.
- Claude/Claude Code provider behavior is not part of the final green evidence
  run. Follow-up: #285.
- `clio-kit`/NDP-MCP/ADIOS/BP5 workflows were considered as future expansion,
  but no new tier-2 expert or hierarchical NDP/ADIOS subagent is implemented in
  this benchmark. Follow-up: #284.
- Cancellation is verified as structured best-effort surfacing. The benchmark
  does not prove hard upstream provider/tool abort. Follow-up: #283.
- The dirty Parquet dataset is generated for future quality/anomaly scenarios,
  but the current green suite does not yet include a separate dirty-data
  benchmark case. Follow-up: #282.

Those gaps are deliberately separated from the verified local Qwopus baseline.
They should become separate benchmark issues/PRs rather than being treated as
already proven by this suite.
