# CLIO Marketplace Agent Benchmark Report

Generated: 2026-05-29 08:17:01 CDT
Evidence JSONL: `/home/jcernuda/clio-agent/benchmark/MARKETPLACE_SEISMIC_EVIDENCE.jsonl`
Benchmark lane: `marketplace_agents`

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

- marketplace_seismic_waveform_review (742.6s, 30 events)

## Evidence Summary

- Max elapsed case: `marketplace_seismic_waveform_review` (742.6s)
- Max expert depth: `marketplace_seismic_waveform_review` (6)
- Max branch fanout: `marketplace_seismic_waveform_review` (0)
- Unique tools used: ndp_get_dataset_details, ndp_list_organizations, ndp_search_datasets, ndp_stage_resource, sac_compute_trace_statistics, sac_fetch_earthscope_waveform, sac_inspect_archive, sac_plot_traces
- Data/input files referenced: 1
- Artifacts verified on disk: 1/1
- Root session logs captured: 1/1
- Child session logs captured: 0
- Active Agent Blueprints: seismic-waveform-review

## Provider Lane Audit

| Criterion | Observed | Required | Status |
| --- | ---: | ---: | --- |
| all marketplace cases prove the requested active Agent Blueprint | 1 | 1 | pass |
| at least five distinct marketplace Agent Blueprints | 1 | 5 | gap |
| all marketplace cases call at least one blueprint expert tool | 1 | 1 | pass |

Provider evidence details:

- seismic-waveform-review

## All Cases

| Case | Category | Blueprint | Mode | Source | Outcome | Agent | Handoffs | Tools | Children | Elapsed |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| marketplace_seismic_waveform_review | marketplace-seismic | seismic-waveform-review | auto | user_agent | pass | main | data x3, ndp_catalog x2, main x3, analysis x3, sac_format x2, visualization | ndp_search_datasets, ndp_search_datasets, ndp_get_dataset_details, ndp_stage_resource, ndp_search_datasets, ndp_search_datasets, ndp_list_organizations, ndp_search_datasets, ndp_search_datasets, sac_fetch_earthscope_waveform, sac_inspect_archive, sac_compute_trace_statistics, sac_fetch_earthscope_waveform, sac_inspect_archive, sac_compute_trace_statistics, sac_plot_traces | 0 | 742.6s |

## Best 10 Demo Prompts

### 1. Marketplace seismic waveform recovery review

Case: `marketplace_seismic_waveform_review`
Category: marketplace-seismic
Routing mode: `auto`
Status: pass
Selected agent: `main`
Active Agent Blueprint: `seismic-waveform-review`
Provider/model: `codex` / `gpt-5.5` via `codex://exec`
Provider settings: temperature=1.0, max_tokens=32000, context_length=0, thinking_budget=0
Route graph: orchestrator -> main; main -> visualization -> main; analysis -> sac_format -> analysis; main -> analysis -> main; data -> ndp_catalog -> data; main -> data -> main
Route metrics: depth=6, branches=0, tools=16
Expert handoffs: data x3, ndp_catalog x2, main x3, analysis x3, sac_format x2, visualization
Tools: ndp_search_datasets, ndp_search_datasets, ndp_get_dataset_details, ndp_stage_resource, ndp_search_datasets, ndp_search_datasets, ndp_list_organizations, ndp_search_datasets, ndp_search_datasets, sac_fetch_earthscope_waveform, sac_inspect_archive, sac_compute_trace_statistics, sac_fetch_earthscope_waveform, sac_inspect_archive, sac_compute_trace_statistics, sac_plot_traces
Data/input files: /home/jcernuda/clio-agent/tmp/clio-seismic-staging/earthscope_IU_ANMO_00_BHZ_2010-02-27T063000.sac
Setup turns: 0
Root session messages: 2
Child session logs: 0
Actions: none
Child sessions: none
Artifacts: /home/jcernuda/clio-agent/.clio-agent-artifacts/charts/sac_traces_earthscope_IU_ANMO_00_BHZ_2010-02-27T063000.png
Artifact evidence: /home/jcernuda/clio-agent/.clio-agent-artifacts/charts/sac_traces_earthscope_IU_ANMO_00_BHZ_2010-02-27T063000.png (ok, 195159 B)
Elapsed: 742.6s

Prompt:

```text
Using the active seismic waveform review agent, find bounded seismic waveform evidence through NDP discovery, recover with an alternate SAC source if NDP staging is blocked, inspect the waveform, compute trace statistics, and produce a PNG plot artifact without using stale local files.
```

What to see: CLIO runs the seismic-waveform-review marketplace Agent Blueprint, surfaces NDP staging blockers, recovers with an observed EarthScope SAC path, and creates a verified PNG artifact.

Why this is interesting: Proves the marketplace can carry the strongest hierarchical workflow, not just single-expert file inspection packages.

Observed excerpt:

```text
Waveform plot artifact is complete, but the parent still needs trace inspection/statistics evidence before finalizing the workflow.
//ds2.datacollaboratory.org/Earthscope_api_dec2024/generated_png/WWMT.CI.LY_.40.png
/home/jcernuda/clio-agent/.clio-agent-artifacts/charts/sac_traces_earthscope_IU_ANMO_00_BHZ_2010-02-27T063000.png
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
