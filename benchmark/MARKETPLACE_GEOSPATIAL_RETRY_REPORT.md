# CLIO Marketplace Agent Benchmark Report

Generated: 2026-06-03 06:10:45 CDT
Evidence JSONL: `benchmark/MARKETPLACE_GEOSPATIAL_RETRY_EVIDENCE.jsonl`
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
| at least three visualization artifacts from analyzed data | 0 | 3 | gap |
| at least two deliberate surfaced-error cases | 0 | 2 | gap |
| at least one context-pressure or compaction case | 0 | 1 | gap |
| at least one provider/model-swap stress case | 0 | 1 | gap |

High-event or long-running cases:

- marketplace_geospatial_field_review (126.5s, 6 events)

## Evidence Summary

- Max elapsed case: `marketplace_geospatial_field_review` (126.5s)
- Max expert depth: `marketplace_geospatial_field_review` (2)
- Max branch fanout: `marketplace_geospatial_field_review` (1)
- Unique tools used: geospatial_inspect_geojson
- Data/input files referenced: 1
- Artifacts verified on disk: 0/0
- Root session logs captured: 1/1
- Child session logs captured: 0
- Semantic trace events captured: 19 events across 1/1 cases (19 live-observed)
- Semantic event types: agent.invocation.completed, agent.invocation.started, delegation.completed, delegation.parent_resumed, delegation.started, hook.invocation.completed, hook.invocation.started, llm.request.started, llm.response.completed, tool.call.completed, tool.call.started, turn.completed, turn.started
- Declared semantic proofs: none
- Observed semantic proofs: none
- Active Agent Blueprints: geospatial-field-review

## Provider Lane Audit

| Criterion | Observed | Required | Status |
| --- | ---: | ---: | --- |
| all marketplace cases prove the requested active Agent Blueprint | 1 | 1 | pass |
| at least five distinct marketplace Agent Blueprints | 1 | 5 | gap |
| all marketplace cases call at least one blueprint expert tool | 1 | 1 | pass |
| marketplace hierarchy cases prove root sync delegation | 1 | 1 | pass |
| at least three marketplace cases prove complex hierarchy depth | 0 | 3 | gap |
| marketplace shallow cases are reported as smoke coverage | 1 | reported | pass |

Provider evidence details:

- geospatial-field-review
- marketplace_geospatial_field_review: depth=2 branches=1 counts as load/tool smoke, not complex hierarchy proof

## All Cases

| Case | Category | Blueprint | Mode | Source | Outcome | Agent | Handoffs | Tools | Children | Elapsed |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| marketplace_geospatial_field_review | marketplace-geospatial | geospatial-field-review | auto | user_agent | pass | main | spatial_features x2, main x2 | geospatial_inspect_geojson, geospatial_inspect_geojson | 0 | 126.5s |

## Best 10 Demo Prompts

### 1. Marketplace geospatial GeoJSON review

Case: `marketplace_geospatial_field_review`
Category: marketplace-geospatial
Routing mode: `auto`
Status: pass
Selected agent: `main`
Active Agent Blueprint: `geospatial-field-review`
Provider/model: `codex` / `gpt-5.5` via `codex://exec`
Provider settings: temperature=1.0, max_tokens=32000, context_length=0, thinking_budget=0
Route graph: orchestrator -> main; main -> spatial_features -> main
Route metrics: depth=2, branches=1, sync_handoffs=1, child_sessions=0, tools=2
Semantic trace: 19 events, 19 live, types=agent.invocation.completed, agent.invocation.started, delegation.completed, delegation.parent_resumed, delegation.started, hook.invocation.completed, hook.invocation.started, llm.request.started, llm.response.completed, tool.call.completed, tool.call.started, turn.completed, turn.started
Expert handoffs: spatial_features x2, main x2
Tools: geospatial_inspect_geojson, geospatial_inspect_geojson
Data/input files: /tmp/clio-09-readiness/tmp/clio-benchmark-data/field_sites.geojson
Setup turns: 0
Root session messages: 2
Child session logs: 0
Actions: none
Child sessions: none
Artifacts: none
Artifact evidence: none
Elapsed: 126.5s

Prompt:

```text
Review this GeoJSON for field-site map readiness: /tmp/clio-09-readiness/tmp/clio-benchmark-data/field_sites.geojson. Summarize feature types, bounds, properties, and map-overlay risks.
```

What to see: CLIO runs the geospatial-field-review marketplace Agent Blueprint through its root expert and uses the spatial_features expert.

Why this is interesting: Proves a geospatial marketplace agent can be loaded per session and can delegate through its own hierarchy.

Observed excerpt:

```text
Field-site map readiness for `/tmp/clio-09-readiness/tmp/clio-benchmark-data/field_sites.geojson`:

Observed evidence:
- GeoJSON type: `FeatureCollection`
- Feature count: 4
- Geometry types: `Point` 2, `LineString` 1, `Polygon` 1
- Bounds / bbox: `[-105.292, 39.982, -105.238, 40.026]`
- Coordinate count inspected: 10
- Invalid coordinate count: 0
- Property keys: `kind`, `site_id`, `status`
- Representative features: `north_ridge`, `south_valley`, `access_transect`, `study_boundary`

Interpretation:
The file appears generally ready for a field-site map overlay. Coordinate validity risk is low because no invalid or out-of-range coordinates were reported, and the bounds look consistent with longitude/latitude coordinates near Boulder, Colorado. The properties are suitable for labels, filters, and status-based styling if downstream tooling preserves `kind`, `site_id`, and `status`.

Map-ov
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
