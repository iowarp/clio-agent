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
| Rich local benchmark datasets | `scripts/create_benchmark_data.py` creates HDF5, clean Parquet, dirty Parquet, CSV, and ADIOS/BP5 benchmark inputs under `tmp/clio-benchmark-data`. | Verified |
| Real provider execution | Final run used GACT against LM Studio at `http://127.0.0.1:1234/v1`, model `qwopus3.5-9b-v3`. | Verified for local Qwopus |
| DataExpert | `hdf5_structure` selected `data` and called HDF5 tools. | Verified |
| AnalysisExpert | `parquet_profile` and `csv_schema` selected `analysis` and called Parquet/CSV tools. | Verified |
| VisualizationExpert | `visualization_artifact` selected `visualization`, called `plot_summary`, and returned existing PNG artifacts. | Verified |
| Natural nano-agent fan-out | `nanoagent_parallel_tool_use` used a human-natural cross-file prompt, created child sessions, and recorded HDF5, Parquet, CSV, and ADIOS/BP5 tool provenance without the user naming nano-agents or tools. | Verified |
| Multi-turn state | `visualization_artifact` runs after Parquet and CSV turns in the same session and resolves "the Parquet file we just profiled" back to the profiled Parquet file. | Verified |
| Dirty data quality | `dirty_parquet_quality_review` runs against `facility_measurements_dirty.parquet` and records schema/statistics tool calls. | Verified |
| Real HDF5 calls | `hdf5_analyze_file` and `hdf5_list_datasets` ran against `fusion_run.h5`. | Verified |
| Real Parquet calls | `parquet_analyze_schema` and repeated `parquet_compute_statistics` ran against `facility_measurements.parquet`. | Verified |
| Real CSV calls | `csv_read_table` ran against `sensor_events.csv`. | Verified |
| Real ADIOS/BP5 calls | `adios_inspect_file` ran against `gray scott noise 0.01 data.bp5` and surfaced profiling/container metadata. | Verified for container/profiling |
| Tool-result feedback | Answers include tool-derived dataset names, units, schema fields, statistics, and artifact paths. | Verified |
| Artifact generation | `plot_summary` produced PNG artifacts recorded in audit rows. | Verified |
| Error surfacing | Missing HDF5 file produced `tool_error` with no normal assistant answer. | Verified |
| Cancellation surfacing | Cancellation produced `cancelled` with no normal assistant answer. | Verified |
| Streaming provenance | Chat streaming case completed with `stream_source="live"` and `delta_count=226`; suite also accepts explicit batch fallback metadata when provider streaming is unavailable. | Verified |
| Evidence per scenario | `CLIO_STRESS_AUDIT_LOG` JSONL rows include prompt, provider, dataset, selected expert, routing decision, elapsed runtime, child sessions, tool calls, artifacts, errors, stream metadata, answer excerpt, and caveats. | Verified |
| Human demo guide | `docs/STRESS_BENCHMARK.md` includes setup, LM Studio/Qwopus configuration, and named collaborator demo prompts with expected behavior and rationale. | Verified |
| ALCF secondary smoke | `scripts/list_alcf_models.py --json` listed active Sophia/Metis models; a CLIO CLI query through `CLIO_LM_PROVIDER=argonne` returned `ALCF_CLIO_OK` with no `error_info`. | Smoke verified |
| clio-kit/NDP core path | `ndp_core_expert_catalog_discovery` used Qwopus/GACT, selected `analysis`, and called `ndp_search_datasets`/`ndp_list_organizations` through the CLIO gateway. | Verified with caveat |
| clio-kit/NDP direct external MCP | `clio_kit_ndp_external_mcp` installed `clio-kit mcp-server ndp` through GACT and called `list_organizations` directly. | Verified |
| Verified-vs-gap distinction | This report and the demo guide separate local Qwopus evidence from optional ALCF/Claude expansion lanes and from clean-vs-recovered planner routes. | Verified |

## Final Local Run

Command:

```powershell
$env:CLIO_INTEGRATION_BASE='http://127.0.0.1:17944'
$env:CLIO_BENCHMARK_DATA_DIR=(Resolve-Path 'tmp\clio-benchmark-data-adios-check').Path
$env:CLIO_STRESS_AUDIT_LOG=(Join-Path (Resolve-Path 'tmp').Path 'clio-stress-audit-qwopus-registry-guard-full-17944.jsonl')
.\.venv\Scripts\python.exe -m pytest tests\test_stress_benchmark -m "integration and benchmark" -vv -s
```

Result:

```text
7 passed in 201.46s (0:03:21)
```

Route metadata follow-up:

```powershell
$env:CLIO_INTEGRATION_BASE='http://127.0.0.1:17943'
$env:CLIO_STRESS_AUDIT_LOG=(Join-Path (Resolve-Path 'tmp').Path 'clio-stress-audit-qwopus-registry-guard-17943.jsonl')
.\.venv\Scripts\python.exe -m pytest tests\test_stress_benchmark\test_local_scientific_workflows.py::test_local_adios_bp5_container_inspection_is_grounded -m "integration and benchmark" -vv -s
```

Observed:

```text
1 passed in 1.64s
route_source=guard
route_reason="Registry guard delegated .bp5 file request to data based on guard_direct_suffixes metadata."
```

clio-kit NDP core path follow-up:

```powershell
$env:CLIO_INTEGRATION_BASE='http://127.0.0.1:17951'
$env:CLIO_STRESS_AUDIT_LOG=(Join-Path (Resolve-Path 'tmp').Path 'clio-stress-audit-ndp-core-17951.jsonl')
.\.venv\Scripts\python.exe -m pytest tests\test_stress_benchmark\test_local_scientific_workflows.py::test_local_ndp_catalog_discovery_is_visible_to_core_expert_path -m "integration and benchmark" -vv -s
```

Observed:

```text
1 passed in 140.09s (0:02:20)
selected_agent=analysis
tools=ndp_search_datasets, ndp_list_organizations, ndp_search_datasets
stream_source=live
```

The NDP run is not counted as a clean planner route. Qwopus made successful NDP
tool calls and CLIO answered from those observations, but a later planner step
returned malformed JSON. The audit row carries a partial
`post_observation_planning` `routing_error` so the recovery is visible.

clio-kit NDP direct external MCP follow-up:

```powershell
$env:CLIO_INTEGRATION_BASE='http://127.0.0.1:17947'
$env:CLIO_STRESS_AUDIT_LOG=(Join-Path (Resolve-Path 'tmp').Path 'clio-stress-audit-clio-kit-ndp-17947.jsonl')
.\.venv\Scripts\python.exe -m pytest tests\test_stress_benchmark\test_local_scientific_workflows.py::test_local_clio_kit_ndp_external_mcp_server_is_callable -m "integration and benchmark" -vv -s
```

Observed:

```text
1 passed in 4.24s
tool=clio-kit-ndp.list_organizations
result._meta.status=success
```

No-guard planner probe:

```powershell
$env:CLIO_ROUTING_GUARDS='0'
$env:CLIO_LM_PLANNER_MAX_TOKENS='1024'
$env:CLIO_INTEGRATION_BASE='http://127.0.0.1:17941'
$env:CLIO_STRESS_AUDIT_LOG=(Join-Path (Resolve-Path 'tmp').Path 'clio-stress-audit-qwopus-no-guards-17941.jsonl')
.\.venv\Scripts\python.exe -m pytest tests\test_stress_benchmark\test_local_scientific_workflows.py::test_local_natural_cross_file_triage_uses_tool_backed_nanoagents tests\test_stress_benchmark\test_local_scientific_workflows.py::test_local_adios_bp5_container_inspection_is_grounded -m "integration and benchmark" -vv -s
```

Observed:

```text
2 passed in 497.51s (0:08:17)
```

The BP5 single-file case was planner-selected and clean. The natural cross-file
case completed through deterministic recovery after Qwopus failed to emit a
valid planner action. Before this report was updated, that recovery route was
misreported as `route_source="dspy"`; the code now reports
`route_source="recovery"` for this path so benchmark evidence does not confuse
recovery with planner success.

Qwopus/Qwen local reasoning models now enforce a `4096` planner-token floor.
Earlier runs with `CLIO_LM_PLANNER_MAX_TOKENS=1024` repeatedly cut off planner
JSON and produced structured `routing_error` turns. Keeping
`CLIO_LM_MAX_TOKENS=8192` is still recommended for expert answers.

Provider reported by audit log:

```json
{
  "provider": "lm_studio",
  "model": "qwopus3.5-9b-v3",
  "api_base": "http://127.0.0.1:1234/v1",
  "transport": null
}
```

## ALCF Secondary Smoke

The local benchmark remains the release gate, but ALCF authentication and one
provider-path smoke were checked because valid Globus auth was available.

Model discovery:

```powershell
.\.venv\Scripts\python.exe scripts\list_alcf_models.py --json
```

Result included active Sophia and Metis models, including:

```text
sophia: openai/gpt-oss-20b
sophia: meta-llama/Llama-4-Scout-17B-16E-Instruct
metis: gpt-oss-120b
metis: Llama-4-Maverick-17B-128E-Instruct
```

CLIO provider smoke:

```powershell
$env:CLIO_LM_PROVIDER='argonne'
$env:CLIO_LM_API_BASE='https://inference-api.alcf.anl.gov/resource_server/sophia/vllm/v1'
$env:CLIO_LM_MODEL='openai/gpt-oss-20b'
$env:CLIO_LM_MAX_TOKENS='512'
$env:CLIO_LM_PLANNER_TEMPERATURE='0'
.\.venv\Scripts\python.exe src\clio_agent\ui\cli.py --query "Return exactly ALCF_CLIO_OK and nothing else." --json
```

Observed:

```json
{
  "answer": "ALCF_CLIO_OK",
  "selected_expert": "chat",
  "duration_ms": 4588.547468185425,
  "error_info": null,
  "status": "success"
}
```

This smoke also found and fixed a structured-output parser gap: ALCF returned a
valid JSON action followed by DSPy's `[[ ## completed ## ]]` marker. CLIO now
accepts that known marker after a complete leading action object instead of
surfacing a false routing failure.

## Scenario Evidence

| Case | Selected Agent | Tools | Artifacts | Error | Stream |
| --- | --- | --- | --- | --- | --- |
| `hdf5_structure` | `data` | `hdf5_analyze_file`, `hdf5_list_datasets` | 0 | none | `batch` |
| `parquet_profile` | `analysis` | `parquet_analyze_schema`, four `parquet_compute_statistics` calls | 0 | none | `batch` |
| `csv_schema` | `analysis` | `csv_read_table` | 0 | none | `batch` |
| `visualization_artifact` | `visualization` | `plot_summary` | 2 PNG paths | none | `batch` |
| `workflow_pass` | `visualization` | `plot_summary` | 2 PNG paths | none | `batch` |
| `nanoagent_parallel_tool_use` | `analysis` | HDF5, ADIOS/BP5, Parquet, and CSV tool calls | 0 | none | `batch` |
| `adios_bp5_container_inspection` | `data` | `adios_inspect_file` | 0 | none | `batch` |
| `dirty_parquet_quality_review` | `analysis` | `parquet_analyze_schema`, three `parquet_compute_statistics` calls | 0 | none | `batch` |
| `missing_hdf5_error_surface` | `data` | `hdf5_list_datasets` | 0 | `tool_error` | `batch` |
| `cancellation_surface` | `chat` | none | 0 | `cancelled` | `batch` |
| `streaming_provenance` | `chat` | none | 0 | none | `live`, `delta_count=257` |
| `ndp_core_expert_catalog_discovery` | `analysis` | `ndp_search_datasets`, `ndp_list_organizations`, `ndp_search_datasets` | 0 | partial `routing_error` after successful tools | `live` |
| `clio_kit_ndp_external_mcp` | direct external MCP | `clio-kit-ndp.list_organizations` | 0 | none | `batch` |

Earlier local runs exposed two issues now fixed by this branch: single-file
Parquet prompts could accidentally enter the nano-agent aggregation path, and
Qwopus could degrade Windows paths from `D:\...` to `D:...`. The final run above
uses the fixed behavior.

## Prompt-To-Artifact Mapping

| Prompt Area | Test Case | Concrete Artifact Or Evidence |
| --- | --- | --- |
| HDF5 tooling | `hdf5_structure` | Answer includes `electron_temperature`, `density`, `heat_flux`, and units `eV`, `m^-3`, `MW/m^2`; metadata includes HDF5 tool calls. |
| Parquet analysis | `parquet_profile` | Answer includes schema/statistics for `temperature_k`, `pressure_pa`, `humidity_pct`, and `anomaly_score`; metadata includes schema/statistics tools. |
| CSV inspection | `csv_schema` | Answer includes `event_id`, `temperature_k`, `pressure_pa`, `status`; metadata includes `csv_read_table`. |
| Visualization | `visualization_artifact` | Answer returns `.png` paths; at least one recorded artifact path exists on disk. |
| ADIOS/BP5 | `adios_bp5_container_inspection` | Answer includes BP5 container/profiling metadata and explicitly reports that ADIOS2 is needed for variable-level metadata when unavailable. |
| Nano-agents | `nanoagent_parallel_tool_use` | Natural cross-file triage prompt; parent answer includes `data_validator`, `analysis_validator`, `csv_validator`, `adios_validator`; child sessions contain HDF5, Parquet, CSV, and ADIOS/BP5 tool provenance. |
| Dirty data | `dirty_parquet_quality_review` | Answer is grounded in `facility_measurements_dirty.parquet`; metadata includes Parquet schema/statistics tools. |
| Error hardening | `missing_hdf5_error_surface` | Assistant text is empty; `error_info.error == "tool_error"`. |
| Cancellation | `cancellation_surface` | Assistant text is empty; `error_info.error == "cancelled"`. |
| Streaming truth | `streaming_provenance` | `message.part.delta` events observed; completed metadata reports `stream_source="live"`. |
| NDP core path | `ndp_core_expert_catalog_discovery` | Natural catalog prompt; selected `analysis`; called `ndp_` tools through the CLIO gateway; answer excerpt includes NOAA-related dataset/resource-format evidence. |
| NDP direct external MCP | `clio_kit_ndp_external_mcp` | GACT installed `clio-kit-ndp` over stdio and called `list_organizations` against the global NDP catalog. |

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
- `fix(agent): infer natural multi-file decomposition`
- `fix(agent): avoid nanoagent fan-out for single-file profile prompts`
- `fix(agent): repair drive-relative Windows paths from local planner output`
- `fix(agent): accept DSPy completed marker after valid planner JSON`
- `feat(tools): add ADIOS/BP container inspection`
- `fix(agent): preserve mixed scientific path order and BP5 suffixes`
- `fix(gact): expose route source/reason on routing_decision parts`
- `fix(agent): make routing guards configurable and registry-declared for planner benchmarks`
- `fix(agent): label deterministic recovery as recovery, not dspy`
- `feat(tools): expose clio-kit NDP through the CLIO gateway`
- `fix(tools): normalize malformed NDP planner args and compact catalog payloads`
- `fix(agent): harden Qwopus answer synthesis with no-think instructions`

## Verified Behavior

The local Qwopus baseline now verifies that CLIO can:

- route real scientific prompts to the right expert path;
- generate valid or repairable tool arguments for HDF5, Parquet, CSV, ADIOS/BP5,
  NDP/clio-kit, and plotting tools;
- carry real tool outputs into answers;
- generate and report visualization artifacts;
- preserve multi-turn file context;
- spawn tool-backed nano-agent child sessions from natural cross-file prompts
  and aggregate their findings;
- surface missing-file and cancellation failures without fake assistant text;
- label streaming provenance truthfully as live or batch;
- label route provenance truthfully as planner, guard, or recovery;
- use clio-kit/NDP through both core gateway tools and direct external MCP calls;
- write machine-readable evidence for each benchmark case.

## Remaining Gaps And Expansion Lanes

These are not claimed as verified by the current benchmark:

- ALCF provider behavior has a CLIO CLI smoke, but not the full HDF5/Parquet/CSV
  GACT benchmark matrix. Follow-up: #281.
- Claude/Claude Code provider behavior is not part of the final green evidence
  run. Follow-up: #285.
- ADIOS/BP5 container and profiling inspection is verified locally, but ADIOS2
  variable-level reads are not verified on Windows because no compatible wheel
  was available. CLIO surfaces this as `adios2_missing` instead of inventing
  variable metadata.
- Routing guards are no longer hardcoded to the original three experts. They are
  declared through agent capability metadata (`guard_direct_suffixes`,
  `guard_coordinator_intents`, and `coordinated_file_suffixes`) and refuse to
  fire when multiple registered experts could own the same guard. This keeps
  production guard behavior useful while allowing a future 25-expert registry to
  scale without more semantic `if expert == ...` branches. Unit coverage now
  registers 25 extra experts and verifies that the guard still selects the
  registry-declared coordinator rather than depending on a three-expert table.
- With `CLIO_ROUTING_GUARDS=0`, Qwopus still struggles to produce valid planner
  output for the natural four-file cross-file prompt even after `/no_think`,
  compact capability retry, and the `4096` token floor. The workflow recovers
  and completes, but this is not counted as clean planner routing. The production
  registry guard remains useful, and the no-guard path remains a focused planner
  hardening target.
- clio-kit/NDP is now verified through CLIO's core gateway-visible `ndp_` tools
  and through GACT's direct external MCP install/call lane. What remains
  unverified is a dedicated tier-2 NDP expert, hierarchical NDP subagents, and a
  clean no-recovery Qwopus planner route for the natural NDP prompt. Follow-up:
  #284.
- Cancellation is verified as structured best-effort surfacing. The benchmark
  does not prove hard upstream provider/tool abort. Follow-up: #283.

Those gaps are deliberately separated from the verified local Qwopus baseline.
They should become separate benchmark issues/PRs rather than being treated as
already proven by this suite.
