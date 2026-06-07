# CLIO Marketplace Agent Benchmark Report

Generated: 2026-06-04 22:00:18 CDT
Evidence JSONL: `/home/jcernuda/clio-agent/benchmark/case02-earthscope-csv-seismic-geography/trace-sophia-final-acceptance.jsonl`
Benchmark lane: `marketplace_agents`

This is a CLIO session-evidence audit. It is produced from real session JSONL rows. Review the embedded `session_log` root and child messages for prompt, route, tool, artifact, error, recovery, and final-answer evidence. Pytest coverage only guards the harness and tools; it is not the benchmark result.

Result: 1/1 clean passes, 0 expected surfaced errors, 0 expected cancellations, 0 partial recoveries, 0 failures.

Extended stress coverage: has optional gaps outside the per-lane pass/fail gate.

## Extended Stress Coverage Audit

| Criterion | Observed | Required | Status |
| --- | ---: | ---: | --- |
| at least ten complex collaborator-grade demos | 1 | 10 | gap |
| at least five long or high-event stress cases | 1 | 5 | gap |
| at least three cases with tier-3 agents or nanoagents | 0 | 3 | gap |
| at least three visualization artifacts from analyzed data | 1 | 3 | gap |
| at least two deliberate surfaced-error cases | 0 | 2 | gap |
| at least one context-pressure or compaction case | 0 | 1 | gap |
| at least one provider/model-swap stress case | 0 | 1 | gap |

High-event or long-running cases:

- marketplace_earthscope_gnss_region_review (260.3s, 54 events)

## Evidence Summary

- Max elapsed case: `marketplace_earthscope_gnss_region_review` (260.3s)
- Max expert depth: `marketplace_earthscope_gnss_region_review` (11)
- Max branch fanout: `marketplace_earthscope_gnss_region_review` (10)
- Unique tools used: ndp_filter_earthscope_station_catalog, ndp_get_dataset_details, ndp_plot_csv_timeseries, ndp_profile_csv_resource, ndp_search_datasets, ndp_stage_resource
- Data/input files referenced: 6
- Artifacts verified on disk: 3/5
- Root session logs captured: 1/1
- Child session logs captured: 0
- Semantic trace events captured: 67 events across 1/1 cases (67 live-observed)
- Semantic event types: agent.invocation.completed, agent.invocation.started, blueprint.delegation.completed, blueprint.delegation.parent_resumed, blueprint.delegation.started, hook.invocation.completed, hook.invocation.started, llm.request.started, llm.response.completed, tool.call.completed, tool.call.started, turn.completed, turn.started
- Invalid tool selections blocked: 0
- Declared semantic proofs: marketplace_pack, nested_tier3, root_delegation, sync_parent_return
- Observed semantic proofs: marketplace_pack, nested_tier3, root_delegation, sync_parent_return
- Active Agent Blueprints: earthscope-gnss-region

## Provider Lane Audit

| Criterion | Observed | Required | Status |
| --- | ---: | ---: | --- |
| all marketplace cases prove the requested active Agent Blueprint | 1 | 1 | pass |
| at least five distinct marketplace Agent Blueprints | 1 | 5 | gap |
| all marketplace cases call at least one blueprint expert tool | 1 | 1 | pass |
| marketplace hierarchy cases prove root sync delegation | 1 | 1 | pass |
| at least three marketplace cases prove complex hierarchy depth | 1 | 3 | gap |
| marketplace shallow cases are reported as smoke coverage | 0 | reported | pass |

Provider evidence details:

- earthscope-gnss-region
- marketplace_earthscope_gnss_region_review: depth=11 branches=10 sync_handoffs=10

## Semantic Proof Declarations

| Case | Declared | Observed |
| --- | --- | --- |
| marketplace_earthscope_gnss_region_review | marketplace_pack, root_delegation, nested_tier3, sync_parent_return | marketplace_pack, root_delegation, nested_tier3, sync_parent_return |

## All Cases

| Case | Category | Blueprint | Mode | Source | Outcome | Agent | Handoffs | Tools | Children | Elapsed |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| marketplace_earthscope_gnss_region_review | marketplace-earthscope | earthscope-gnss-region | auto | agent_blueprint | pass | main | geospatial x3, main x5, data x6, ndp_dataset_discovery x3, earthscope_station_catalog x3, ndp_resource_resolver x3, analysis x5, gnss_timeseries_analysis x3, station_network_analysis x3, visualization x3, synthesis x3 | ndp_search_datasets, ndp_search_datasets, ndp_get_dataset_details, ndp_get_dataset_details, ndp_search_datasets, ndp_search_datasets, ndp_get_dataset_details, ndp_stage_resource, ndp_filter_earthscope_station_catalog, ndp_search_datasets, ndp_stage_resource, ndp_stage_resource, ndp_profile_csv_resource, ndp_plot_csv_timeseries | 0 | 260.3s |

## Best 10 Demo Prompts

### 1. Marketplace EarthScope GNSS region review

Case: `marketplace_earthscope_gnss_region_review`
Category: marketplace-earthscope
Routing mode: `auto`
Status: pass
Selected agent: `main`
Active Agent Blueprint: `earthscope-gnss-region`
Provider/model: `argonne` / `openai/gpt-oss-120b` via `https://inference-api.alcf.anl.gov/resource_server/sophia/vllm/v1`
Provider settings: temperature=1.0, max_tokens=32000, context_length=0, thinking_budget=0
Route graph: orchestrator -> main; data -> ndp_resource_resolver -> data; main -> synthesis -> main; main -> data -> main; data -> ndp_dataset_discovery -> data; analysis -> gnss_timeseries_analysis -> analysis; main -> visualization -> main; analysis -> station_network_analysis -> analysis; main -> geospatial -> main; data -> earthscope_station_catalog -> data; main -> analysis -> main
Route metrics: depth=11, branches=10, sync_handoffs=10, child_sessions=0, tools=14
Semantic trace: 67 events, 67 live, types=agent.invocation.completed, agent.invocation.started, blueprint.delegation.completed, blueprint.delegation.parent_resumed, blueprint.delegation.started, hook.invocation.completed, hook.invocation.started, llm.request.started, llm.response.completed, tool.call.completed, tool.call.started, turn.completed, turn.started
Expert handoffs: geospatial x3, main x5, data x6, ndp_dataset_discovery x3, earthscope_station_catalog x3, ndp_resource_resolver x3, analysis x5, gnss_timeseries_analysis x3, station_network_analysis x3, visualization x3, synthesis x3
Tools: ndp_search_datasets, ndp_search_datasets, ndp_get_dataset_details, ndp_get_dataset_details, ndp_search_datasets, ndp_search_datasets, ndp_get_dataset_details, ndp_stage_resource, ndp_filter_earthscope_station_catalog, ndp_search_datasets, ndp_stage_resource, ndp_stage_resource, ndp_profile_csv_resource, ndp_plot_csv_timeseries
Data/input files: //nationaldataplatform.org/catalog/dataset/811f0bcc-99e5-455c-bcf6-7c63c2634f41/resource/a420cc30-2262-423a-8c63-3ad8d91f2a8f/download/earthscope_converted_data.csv, /home/jcernuda/clio-agent/.clio/artifacts/ndp-staging/earthscope_converted_data.csv, //ds2.datacollaboratory.org/Earthscope_api_dec2024/raw_csv/P475.CI.LY_.00.csv, //ds2.datacollaboratory.org/Earthscope_api_dec2024/raw_csv/P475.CI.LY_.20.csv, /home/jcernuda/clio-agent/.clio/artifacts/ndp-staging/P475.CI.LY_.20.csv, /home/jcernuda/clio-agent/.clio/artifacts/ndp-staging/P475.CI.LY_.20_timeseries.png
Setup turns: 0
Root session messages: 2
Child session logs: 0
Actions: none
Child sessions: none
Artifacts: /home/jcernuda/clio-agent/.clio/artifacts/ndp-staging/earthscope_converted_data.csv, /home/jcernuda/clio-agent/.clio/artifacts/ndp-staging/P475.CI.LY_.20.csv, /home/jcernuda/clio-agent/.clio/artifacts/ndp-staging/P475.CI.LY_.20_timeseries.png, /home/jcernuda/.clio/artifacts/ndp-staging/P475.CI.LY_.20.csv, io-agent/.clio/artifacts/ndp-staging/P475.CI.LY_.20.csv
Artifact evidence: /home/jcernuda/clio-agent/.clio/artifacts/ndp-staging/earthscope_converted_data.csv (ok, 153082 B), /home/jcernuda/clio-agent/.clio/artifacts/ndp-staging/P475.CI.LY_.20.csv (ok, 51608375 B), /home/jcernuda/clio-agent/.clio/artifacts/ndp-staging/P475.CI.LY_.20_timeseries.png (ok, 154896 B), /home/jcernuda/.clio/artifacts/ndp-staging/P475.CI.LY_.20.csv (missing, 0 B), io-agent/.clio/artifacts/ndp-staging/P475.CI.LY_.20.csv (missing, 0 B)
Elapsed: 260.3s

Prompt:

```text
Explore recent seismic activity around the San Diego area. Resolve the requested geography, find public EarthScope/NDP GNSS station or station time-series evidence for that region, stage a concrete CSV resource, analyze the station time series and uncertainty columns, produce a PNG artifact, and explain data freshness and event-catalog limitations. Do not use SAC waveform files unless the live catalog evidence makes waveform data necessary.
```

What to see: CLIO runs the earthscope-gnss-region marketplace Agent Blueprint through data, analysis, visualization, and synthesis domains; geospatial resolution is explicit before NDP discovery; NDP tools stage and profile a station CSV; visualization creates a PNG; the final answer cites source URL, local artifacts, station evidence, and limitations.

Why this is interesting: Replaces the SAC-first EarthScope demo with the corrected geography-driven NDP/EarthScope GNSS workflow and tests typed workflow-state continuation rather than benchmark string routing.

Observed excerpt:

```text
**Geographic scope**  
- **Region:** 50 km radius around San Diego, CA (center 32.7157 N, ‑117.1611 W).  
- **Provenance:** USGS GNIS centroid + standard 50 km seismic‑monitoring buffer (high confidence).  

**GNSS data source**  
- **Station:** **P475** (Network *NOTA*).  
- **Distance from region centre:** 9.5 km.  
- **Data URL (public NDP):** https://ds2.datacollaboratory.org/Earthscope_api_dec2024/raw_csv/P475.CI.LY_.20.csv  
- **Local staged file:** `/home/jcernuda/clio-agent/.clio/artifacts/ndp-staging/P475.CI.LY_.20.csv`  

**Time‑series characteristics**  
| Property | Value |
|----------|-------|
| **Time span** | 2024‑12‑01 00:00 → 2024‑12‑01 13:57 UTC (≈ 14 h) |
| **Rows scanned** | ~250 000 (scan‑limited to first 250 k rows) |
| **Columns** | `time`, `east`, `north`, `up`, `sigEE`, `sigNN`, `sigUU`, `qChannel` |
| **Median displacements** | East ≈ ‑0.042 m, North ≈ 0.002 m,
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
