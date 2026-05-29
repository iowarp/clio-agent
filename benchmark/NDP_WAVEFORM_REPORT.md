# CLIO Real-Orchestrator Benchmark Report

Generated: 2026-05-28 22:09:07 CDT
Evidence JSONL: `/home/jcernuda/clio-agent/benchmark/NDP_WAVEFORM_EVIDENCE.jsonl`
Benchmark lane: `real_orchestrator`

Result: 1/1 clean passes, 0 expected surfaced errors, 0 expected cancellations, 0 partial recoveries, 0 failures.

Stress coverage: does not yet meet the documented benchmark standard.

## Stress Coverage Audit

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

- ndp_seismic_waveform_to_plot (96.3s, 22 events)

## Evidence Summary

- Max elapsed case: `ndp_seismic_waveform_to_plot` (96.3s)
- Max expert depth: `ndp_seismic_waveform_to_plot` (5)
- Max branch fanout: `ndp_seismic_waveform_to_plot` (0)
- Unique tools used: ndp_get_dataset_details, ndp_list_organizations, ndp_search_datasets, ndp_stage_resource, sac_compute_trace_statistics, sac_fetch_earthscope_waveform, sac_inspect_archive, sac_plot_traces
- Data/input files referenced: 1
- Artifacts verified on disk: 1/1

## Provider Lane Audit

| Criterion | Observed | Required | Status |
| --- | ---: | ---: | --- |
| all selected cases avoid shortcut route sources | 1 | 1 | pass |
| passing cases include structured route/tool evidence | 1 | 1 | pass |
| artifact-producing cases verify artifacts on disk | 1 | 1 | pass |
| planner multi-file hierarchy case passes | 0 | 1 | gap |
| dirty cross-file quality gate passes | 0 | 1 | gap |
| NDP waveform benchmark reaches verified SAC/PNG artifact | 1 | 1 | pass |
| NDP full SAC/PNG chain verified | 1 | 1 | pass |

## All Cases

| Case | Category | Mode | Source | Outcome | Agent | Handoffs | Tools | Children | Elapsed |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| ndp_seismic_waveform_to_plot | hierarchical-science | auto | dspy | pass | visualization | data, ndp_catalog, analysis, sac_format, visualization | ndp_list_organizations, ndp_search_datasets, ndp_search_datasets, ndp_search_datasets, ndp_get_dataset_details, ndp_stage_resource, ndp_stage_resource, ndp_stage_resource, ndp_get_dataset_details, ndp_stage_resource, ndp_stage_resource, ndp_get_dataset_details, ndp_stage_resource, sac_fetch_earthscope_waveform, sac_inspect_archive, sac_compute_trace_statistics, sac_plot_traces | 0 | 96.3s |

## Best 10 Demo Prompts

### 1. NDP seismic waveform discovery to plot

Case: `ndp_seismic_waveform_to_plot`
Category: hierarchical-science
Routing mode: `auto`
Status: pass
Selected agent: `visualization`
Provider/model: `codex` / `gpt-5.5` via `codex://exec`
Provider settings: temperature=1.0, max_tokens=32000, context_length=0, thinking_budget=0
Route graph: visualization -> data -> ndp_catalog -> analysis -> sac_format -> visualization
Route metrics: depth=5, branches=0, tools=17
Expert handoffs: data, ndp_catalog, analysis, sac_format, visualization
Tools: ndp_list_organizations, ndp_search_datasets, ndp_search_datasets, ndp_search_datasets, ndp_get_dataset_details, ndp_stage_resource, ndp_stage_resource, ndp_stage_resource, ndp_get_dataset_details, ndp_stage_resource, ndp_stage_resource, ndp_get_dataset_details, ndp_stage_resource, sac_fetch_earthscope_waveform, sac_inspect_archive, sac_compute_trace_statistics, sac_plot_traces
Data/input files: /home/jcernuda/clio-agent/tmp/clio-seismic-staging/earthscope_IU_ANMO_00_BHZ_2010-02-27T063000.sac
Setup turns: 0
Actions: none
Child sessions: none
Artifacts: /home/jcernuda/clio-agent/.clio-agent-artifacts/charts/sac_traces_earthscope_IU_ANMO_00_BHZ_2010-02-27T063000.png
Artifact evidence: /home/jcernuda/clio-agent/.clio-agent-artifacts/charts/sac_traces_earthscope_IU_ANMO_00_BHZ_2010-02-27T063000.png (ok, 96135 B)
Elapsed: 96.3s

Prompt:

```text
Find a bounded seismic waveform dataset from a seismological or Earth-science organization in the National Data Platform. Choose a usable resource, stage it if it is small enough, inspect the waveform content, compute representative trace statistics, and produce a plot artifact. If a candidate is too large or unavailable, surface that as the result instead of inventing a plot.
```

What to see: CLIO delegates NDP discovery to ndp_catalog, stages a bounded waveform resource, analyzes SAC traces through sac_format, and creates a PNG plot.

Why this is interesting: This is the core hierarchical science demo: provider discovery, data access, format-specific analysis, and visualization without the user naming internal agents.

Observed excerpt:

```text
data | success | planner_dispatch | Queried the National Data Platform catalog through clio-kit MCP. Organizations matched: 0 Datasets matched: 9 - Data for: Improved Characterization of Ultralow-velocity Zones through Advances in Bayesian Inversion of ScP Waveforms (marriott_library_hive; PDF, TAR, TXT; resources: Pachhai_etal_2023_ScP_data.tar) - Data for: A Compositional Component to the Samoa Ultralow-velocity Zone Revealed through 2- and 3-D Waveform Modeling of SKS and SKKS Differential Travel-times and Amplit...[truncated]
data -> ndp_catalog | success | planner_dispatch_child | Queried the National Data Platform catalog through clio-kit MCP. Organizations matched: 0 Datasets matched: 9 - Data for: Improved Characterization of Ultralow-velocity Zones through Advances in Bayesian Inversion of ScP Waveforms (marriott_library_hive; PDF, TAR, TXT; resources: Pachhai_etal_2023_ScP_dat
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
