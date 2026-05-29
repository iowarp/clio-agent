# CLIO Real-Orchestrator Benchmark Report

Generated: 2026-05-29 06:48:03 CDT
Evidence JSONL: `/home/jcernuda/clio-agent/benchmark/NDP_WAVEFORM_RECOVERY_EVIDENCE.jsonl`
Benchmark lane: `real_orchestrator`

This is a CLIO session-evidence audit. It is produced from real session JSONL rows. Review the embedded `session_log` root and child messages for prompt, route, tool, artifact, error, recovery, and final-answer evidence. Pytest coverage only guards the harness and tools; it is not the benchmark result.

Result: 1/1 clean passes, 0 expected surfaced errors, 0 expected cancellations, 0 partial recoveries, 0 failures.

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

- ndp_seismic_waveform_to_plot (199.7s, 25 events)

## Evidence Summary

- Max elapsed case: `ndp_seismic_waveform_to_plot` (199.7s)
- Max expert depth: `ndp_seismic_waveform_to_plot` (4)
- Max branch fanout: `ndp_seismic_waveform_to_plot` (0)
- Unique tools used: ndp_get_dataset_details, ndp_list_organizations, ndp_search_datasets, ndp_stage_resource, sac_compute_trace_statistics, sac_fetch_earthscope_waveform, sac_inspect_archive, sac_plot_traces
- Data/input files referenced: 2
- Artifacts verified on disk: 1/1
- Root session logs captured: 1/1
- Child session logs captured: 0

## Provider Lane Audit

| Criterion | Observed | Required | Status |
| --- | ---: | ---: | --- |
| all selected cases avoid shortcut route sources | 1 | 1 | pass |
| passing cases include structured route/tool evidence | 1 | 1 | pass |
| artifact-producing cases verify artifacts on disk | 1 | 1 | pass |
| nested expert handoffs include sync return/resume provenance | 1 | 1 | pass |
| planner multi-file hierarchy case passes | 0 | 1 | gap |
| dirty cross-file quality gate passes | 0 | 1 | gap |
| NDP waveform benchmark reaches verified SAC/PNG artifact | 1 | 1 | pass |
| NDP full SAC/PNG chain verified | 1 | 1 | pass |

## All Cases

| Case | Category | Blueprint | Mode | Source | Outcome | Agent | Handoffs | Tools | Children | Elapsed |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| ndp_seismic_waveform_to_plot | hierarchical-science | - | auto | dspy | pass | analysis | data x2, ndp_catalog x2, sac_format x4 | ndp_list_organizations, ndp_search_datasets, ndp_search_datasets, ndp_search_datasets, ndp_get_dataset_details, ndp_stage_resource, ndp_stage_resource, ndp_stage_resource, ndp_get_dataset_details, ndp_stage_resource, ndp_stage_resource, ndp_get_dataset_details, ndp_stage_resource, sac_fetch_earthscope_waveform, sac_inspect_archive, sac_compute_trace_statistics, sac_plot_traces | 0 | 199.7s |

## Best 10 Demo Prompts

### 1. NDP seismic waveform discovery to plot

Case: `ndp_seismic_waveform_to_plot`
Category: hierarchical-science
Routing mode: `auto`
Status: pass
Selected agent: `analysis`
Active Agent Blueprint: `-`
Provider/model: `codex` / `gpt-5.5` via `codex://exec`
Provider settings: temperature=1.0, max_tokens=32000, context_length=0, thinking_budget=0
Route graph: orchestrator -> data; analysis -> sac_format -> analysis; data -> ndp_catalog -> data
Route metrics: depth=4, branches=0, tools=17
Expert handoffs: data x2, ndp_catalog x2, sac_format x4
Tools: ndp_list_organizations, ndp_search_datasets, ndp_search_datasets, ndp_search_datasets, ndp_get_dataset_details, ndp_stage_resource, ndp_stage_resource, ndp_stage_resource, ndp_get_dataset_details, ndp_stage_resource, ndp_stage_resource, ndp_get_dataset_details, ndp_stage_resource, sac_fetch_earthscope_waveform, sac_inspect_archive, sac_compute_trace_statistics, sac_plot_traces
Data/input files: /home/jcernuda/clio-agent/tmp/clio-waveform-recovery/earthscope_IU_ANMO_00_BHZ_2024-01-01T000000.sac, /home/jcernuda/clio-agent/tmp/clio-waveform-recovery/earthscope_IU_ANMO_00_BHZ_2024-01-01T000000.png
Setup turns: 0
Root session messages: 2
Child session logs: 0
Actions: none
Child sessions: none
Artifacts: /home/jcernuda/clio-agent/tmp/clio-waveform-recovery/earthscope_IU_ANMO_00_BHZ_2024-01-01T000000.png
Artifact evidence: /home/jcernuda/clio-agent/tmp/clio-waveform-recovery/earthscope_IU_ANMO_00_BHZ_2024-01-01T000000.png (ok, 136776 B)
Elapsed: 199.7s

Prompt:

```text
Find a bounded seismic waveform dataset from a seismological or Earth-science organization in the National Data Platform. Choose a usable resource, stage it if it is small enough, inspect the waveform content, compute representative trace statistics, and produce a plot artifact. If a candidate is too large or unavailable, surface that as the result instead of inventing a plot.
```

What to see: CLIO delegates NDP discovery to ndp_catalog, stages a bounded waveform resource, analyzes SAC traces through sac_format, and creates a PNG plot.

Why this is interesting: This is the core hierarchical science demo: provider discovery, data access, format-specific analysis, and visualization without the user naming internal agents.

Observed excerpt:

```text
NDP staging found seismic candidates but Hive downloads timed out and Salton Sea was too large, so a bounded EarthScope recovery SAC waveform was staged at /home/jcernuda/clio-agent/tmp/clio-waveform-recovery/earthscope_IU_ANMO_00_BHZ_2024-01-01T000000.sac, inspected as 1 trace with npts=12000, delta=0.025 s, min=-133, max=2550, mean=1204.794, std=439.723, peak_abs=2550, and plotted at /home/jcernuda/clio-agent/tmp/clio-waveform-recovery/earthscope_IU_ANMO_00_BHZ_2024-01-01T000000.png.
/home/jcernuda/clio-agent/tmp/clio-waveform-recovery/earthscope_IU_ANMO_00_BHZ_2024-01-01T000000.png
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
