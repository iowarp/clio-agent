# CLIO 1.0 Semantic Regression Benchmark Report

Generated: 2026-06-03 06:35:54 CDT
Evidence JSONL: `benchmark/MARKETPLACE_MCP_SCOPE_EVIDENCE.jsonl`
Benchmark lane: `semantic_regression`

This is a CLIO session-evidence audit. It is produced from real session JSONL rows. Review the embedded `session_log` root and child messages for prompt, route, tool, artifact, error, recovery, and final-answer evidence. Pytest coverage only guards the harness and tools; it is not the benchmark result.

Result: 1/1 clean passes, 0 expected surfaced errors, 0 expected cancellations, 0 partial recoveries, 0 failures.

Extended stress coverage: has optional gaps outside the per-lane pass/fail gate.

## Extended Stress Coverage Audit

| Criterion | Observed | Required | Status |
| --- | ---: | ---: | --- |
| at least ten complex collaborator-grade demos | 0 | 10 | gap |
| at least five long or high-event stress cases | 0 | 5 | gap |
| at least three cases with tier-3 agents or nanoagents | 0 | 3 | gap |
| at least three visualization artifacts from analyzed data | 0 | 3 | gap |
| at least two deliberate surfaced-error cases | 0 | 2 | gap |
| at least one context-pressure or compaction case | 0 | 1 | gap |
| at least one provider/model-swap stress case | 0 | 1 | gap |

## Evidence Summary

- Max elapsed case: `marketplace_mcp_calculator_scope` (30.2s)
- Max expert depth: `marketplace_mcp_calculator_scope` (1)
- Max branch fanout: `marketplace_mcp_calculator_scope` (0)
- Unique tools used: none
- Data/input files referenced: 0
- Artifacts verified on disk: 0/0
- Root session logs captured: 1/1
- Child session logs captured: 0
- Semantic trace events captured: 9 events across 1/1 cases (9 live-observed)
- Semantic event types: agent.invocation.completed, agent.invocation.started, hook.invocation.completed, hook.invocation.started, llm.request.started, llm.response.completed, turn.completed, turn.started
- Declared semantic proofs: command_mcp_skill_scope
- Observed semantic proofs: command_mcp_skill_scope
- Active Agent Blueprints: mcp-calculator-smoke

## Provider Lane Audit

| Criterion | Observed | Required | Status |
| --- | ---: | ---: | --- |
| semantic-regression lane declares required proof classes | 1 | 8 | gap |
| semantic-regression passing evidence covers required proof classes | 1 | 8 | gap |
| each declared case proof is observed in session evidence | 1 | 1 | pass |
| semantic-regression cases avoid shortcut route sources | 1 | 1 | pass |
| passing semantic-regression cases include route evidence | 1 | 1 | pass |
| nested semantic-regression delegations include sync return/resume | 1 | 1 | pass |
| observed semantic proof coverage by case | command_mcp_skill_scope=['marketplace_mcp_calculator_scope'], failure_recovery=[], marketplace_pack=[], nested_tier3=[], no_shortcuts=[], root_delegation=[], sync_parent_return=[], workspace_memory_scope=[] | reported | pass |

Provider evidence details:

- failure_recovery: failure/recovery behavior reaches downstream evidence
- marketplace_pack: marketplace Agent Blueprint activation is observed
- nested_tier3: nested tier-3 or child-worker execution is observed
- no_shortcuts: no deterministic or keyword-forced route sources
- root_delegation: root Agent delegates through declared experts
- sync_parent_return: sync child delegation returns control to the parent
- workspace_memory_scope: workspace/global memory scope policy is observed
- failure_recovery: failure/recovery behavior reaches downstream evidence
- marketplace_pack: marketplace Agent Blueprint activation is observed
- nested_tier3: nested tier-3 or child-worker execution is observed
- no_shortcuts: no deterministic or keyword-forced route sources
- root_delegation: root Agent delegates through declared experts
- sync_parent_return: sync child delegation returns control to the parent
- workspace_memory_scope: workspace/global memory scope policy is observed

## Semantic Proof Declarations

| Case | Declared | Observed |
| --- | --- | --- |
| marketplace_mcp_calculator_scope | command_mcp_skill_scope | command_mcp_skill_scope |

## All Cases

| Case | Category | Blueprint | Mode | Source | Outcome | Agent | Handoffs | Tools | Children | Elapsed |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| marketplace_mcp_calculator_scope | marketplace-mcp | mcp-calculator-smoke | auto | user_agent | pass | main | - | - | 0 | 30.2s |

## Best 10 Demo Prompts

### 1. Marketplace MCP descriptor scope

Case: `marketplace_mcp_calculator_scope`
Category: marketplace-mcp
Routing mode: `auto`
Status: pass
Selected agent: `main`
Active Agent Blueprint: `mcp-calculator-smoke`
Provider/model: `codex` / `gpt-5.5` via `codex://exec`
Provider settings: temperature=1.0, max_tokens=32000, context_length=0, thinking_budget=0
Route graph: orchestrator -> main
Route metrics: depth=1, branches=0, sync_handoffs=0, child_sessions=0, tools=0
Semantic trace: 9 events, 9 live, types=agent.invocation.completed, agent.invocation.started, hook.invocation.completed, hook.invocation.started, llm.request.started, llm.response.completed, turn.completed, turn.started
Expert handoffs: none
Tools: none
Data/input files: none
Setup turns: 0
Root session messages: 2
Child session logs: 0
Actions: none
Child sessions: none
Artifacts: none
Artifact evidence: none
Elapsed: 30.2s

Prompt:

```text
Using the active calculator MCP smoke agent, verify whether the packaged calculator capability is immediately usable. Explain the descriptor state, what tool it would expose, and what trust or enablement step is required before the tool can be called.
```

What to see: CLIO runs the mcp-calculator-smoke Agent Blueprint and surfaces that the pack-local calculator MCP descriptor is packaged but disabled until explicit trust/enablement, with calculator_add named as the scoped tool.

Why this is interesting: Covers the semantic-regression proof for command/MCP/skill capability scoping using marketplace blueprint metadata instead of a hardcoded tool path.

Observed excerpt:

```text
The packaged calculator capability is not immediately usable. The `calculator` MCP descriptor is present in the `mcp-calculator-smoke` pack, but it is disabled by default. It declares a stdio, pack-local MCP server and would expose the scoped tool `calculator_add`.

Before `calculator_add` can be called, the descriptor must be explicitly trusted and enabled; the trust policy is `explicit`.
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
