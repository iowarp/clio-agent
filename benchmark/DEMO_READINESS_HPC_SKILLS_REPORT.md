# CLIO Marketplace Agent Benchmark Report

Generated: 2026-06-03 10:13:38 CDT
Evidence JSONL: `/tmp/clio-09-readiness/benchmark/DEMO_READINESS_HPC_SKILLS_EVIDENCE.jsonl`
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

- marketplace_hpc_io_regression (288.0s, 13 events)

## Evidence Summary

- Max elapsed case: `marketplace_hpc_io_regression` (288.0s)
- Max expert depth: `marketplace_hpc_io_regression` (5)
- Max branch fanout: `marketplace_hpc_io_regression` (4)
- Unique tools used: hpc_compare_darshan_traces, hpc_parse_darshan_text
- Data/input files referenced: 2
- Artifacts verified on disk: 0/0
- Root session logs captured: 1/1
- Child session logs captured: 0
- Semantic trace events captured: 31 events across 1/1 cases (31 live-observed)
- Semantic event types: agent.invocation.completed, agent.invocation.started, delegation.completed, delegation.parent_resumed, delegation.started, hook.invocation.completed, hook.invocation.started, llm.request.started, llm.response.completed, tool.call.completed, tool.call.started, turn.completed, turn.started
- Declared semantic proofs: none
- Observed semantic proofs: none
- Active Agent Blueprints: hpc-io-regression

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

- hpc-io-regression
- marketplace_hpc_io_regression: depth=5 branches=4 sync_handoffs=4

## All Cases

| Case | Category | Blueprint | Mode | Source | Outcome | Agent | Handoffs | Tools | Children | Elapsed |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| marketplace_hpc_io_regression | marketplace-hpc | hpc-io-regression | auto | user_agent | pass | main | trace_ingest x2, baseline_ingest, main x3, regression_diff, root_cause | hpc_parse_darshan_text, hpc_parse_darshan_text, hpc_parse_darshan_text, hpc_parse_darshan_text, hpc_compare_darshan_traces | 0 | 288.0s |

## Best 10 Demo Prompts

### 1. Marketplace HPC I/O regression

Case: `marketplace_hpc_io_regression`
Category: marketplace-hpc
Routing mode: `auto`
Status: pass
Selected agent: `main`
Active Agent Blueprint: `hpc-io-regression`
Provider/model: `codex` / `gpt-5.5` via `codex://exec`
Provider settings: temperature=1.0, max_tokens=32000, context_length=0, thinking_budget=0
Route graph: orchestrator -> main; trace_ingest -> baseline_ingest -> trace_ingest; main -> root_cause -> main; main -> trace_ingest -> main; main -> regression_diff -> main
Route metrics: depth=5, branches=4, sync_handoffs=4, child_sessions=0, tools=5
Semantic trace: 31 events, 31 live, types=agent.invocation.completed, agent.invocation.started, delegation.completed, delegation.parent_resumed, delegation.started, hook.invocation.completed, hook.invocation.started, llm.request.started, llm.response.completed, tool.call.completed, tool.call.started, turn.completed, turn.started
Expert handoffs: trace_ingest x2, baseline_ingest, main x3, regression_diff, root_cause
Tools: hpc_parse_darshan_text, hpc_parse_darshan_text, hpc_parse_darshan_text, hpc_parse_darshan_text, hpc_compare_darshan_traces
Data/input files: /tmp/clio-09-readiness/tmp/demo-readiness-data/baseline_darshan.txt, /tmp/clio-09-readiness/tmp/demo-readiness-data/candidate_darshan.txt
Setup turns: 0
Root session messages: 2
Child session logs: 0
Actions: none
Child sessions: none
Artifacts: none
Artifact evidence: none
Elapsed: 288.0s

Prompt:

```text
Compare these two HPC I/O traces before collaborator handoff: baseline /tmp/clio-09-readiness/tmp/demo-readiness-data/baseline_darshan.txt and candidate /tmp/clio-09-readiness/tmp/demo-readiness-data/candidate_darshan.txt. Identify the main I/O regression, stable metrics, and likely root cause.
```

What to see: CLIO runs the hpc-io-regression marketplace Agent Blueprint, parses both traces through its ingest path, compares them, and synthesizes root-cause evidence.

Why this is interesting: Adds the first-wave HPC I/O regression case to the marketplace lane, including paired inputs and tier-3 ingest workers.

Observed excerpt:

```text
Compared baseline `/tmp/clio-09-readiness/tmp/demo-readiness-data/baseline_darshan.txt` with candidate `/tmp/clio-09-readiness/tmp/demo-readiness-data/candidate_darshan.txt`.

Main I/O regression: the candidate shifted from mostly collective MPI-IO writes to mostly independent writes, with a large write-path slowdown. POSIX write time increased from `12.0 s` to `28.0 s` (`+16.0 s`, `+133.333333%`), while total runtime increased from `100.0 s` to `118.0 s` (`+18.0%`).

Regressed evidence:
- MPI-IO independent writes increased from `8.0` to `88.0` (`+1000.0%`).
- MPI-IO collective writes decreased from `96.0` to `24.0` (`-75.0%`).
- Collective write fraction dropped from `0.923077` to `0.214286` (`-76.785685%`).
- POSIX metadata time increased from `1.2 s` to `5.8 s` (`+383.333333%`).
- Write transfer size hint decreased from `1048576 bytes` to `131072 bytes` (`-87.5%`).
- Average write si
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
