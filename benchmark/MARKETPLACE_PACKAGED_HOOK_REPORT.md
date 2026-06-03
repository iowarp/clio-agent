# CLIO 1.0 Semantic Regression Benchmark Report

Generated: 2026-06-03 07:09:42 CDT
Evidence JSONL: `/tmp/clio-hook-benchmark/MARKETPLACE_PACKAGED_HOOK_EVIDENCE.jsonl`
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

- Max elapsed case: `marketplace_packaged_hook_blocked_turn` (0.0s)
- Max expert depth: `marketplace_packaged_hook_blocked_turn` (0)
- Max branch fanout: `marketplace_packaged_hook_blocked_turn` (0)
- Unique tools used: none
- Data/input files referenced: 0
- Artifacts verified on disk: 0/0
- Root session logs captured: 1/1
- Child session logs captured: 0
- Semantic trace events captured: 4 events across 1/1 cases (4 live-observed)
- Semantic event types: hook.invocation.started, hook.pre_message.blocked, turn.failed, turn.started
- Declared semantic proofs: packaged_hook_invocation
- Observed semantic proofs: packaged_hook_invocation
- Active Agent Blueprints: hook-smoke

## Provider Lane Audit

| Criterion | Observed | Required | Status |
| --- | ---: | ---: | --- |
| semantic-regression lane declares required proof classes | 1 | 10 | gap |
| semantic-regression passing evidence covers required proof classes | 1 | 10 | gap |
| each declared case proof is observed in session evidence | 1 | 1 | pass |
| semantic-regression cases avoid shortcut route sources | 1 | 1 | pass |
| passing semantic-regression cases include route evidence | 0 | 1 | gap |
| nested semantic-regression delegations include sync return/resume | 1 | 1 | pass |
| observed semantic proof coverage by case | command_mcp_skill_scope=[], enabled_mcp_execution=[], failure_recovery=[], marketplace_pack=[], nested_tier3=[], no_shortcuts=[], packaged_hook_invocation=['marketplace_packaged_hook_blocked_turn'], root_delegation=[], sync_parent_return=[], workspace_memory_scope=[] | reported | pass |

Provider evidence details:

- command_mcp_skill_scope: command, MCP, or skill capability scoping is observed
- enabled_mcp_execution: marketplace MCP descriptor is trusted, launched, and callable
- failure_recovery: failure/recovery behavior reaches downstream evidence
- marketplace_pack: marketplace Agent Blueprint activation is observed
- nested_tier3: nested tier-3 or child-worker execution is observed
- no_shortcuts: no deterministic or keyword-forced route sources
- root_delegation: root Agent delegates through declared experts
- sync_parent_return: sync child delegation returns control to the parent
- workspace_memory_scope: workspace/global memory scope policy is observed
- command_mcp_skill_scope: command, MCP, or skill capability scoping is observed
- enabled_mcp_execution: marketplace MCP descriptor is trusted, launched, and callable
- failure_recovery: failure/recovery behavior reaches downstream evidence
- marketplace_pack: marketplace Agent Blueprint activation is observed
- nested_tier3: nested tier-3 or child-worker execution is observed
- no_shortcuts: no deterministic or keyword-forced route sources
- root_delegation: root Agent delegates through declared experts
- sync_parent_return: sync child delegation returns control to the parent
- workspace_memory_scope: workspace/global memory scope policy is observed
- marketplace_packaged_hook_blocked_turn: route_metrics={'expert_depth': 0, 'branch_count': 0, 'child_session_branch_count': 0, 'sync_handoff_count': 0, 'unique_experts': 0, 'unique_tools': 0, 'tool_call_count': 0, 'artifact_count': 0}

## Semantic Proof Declarations

| Case | Declared | Observed |
| --- | --- | --- |
| marketplace_packaged_hook_blocked_turn | packaged_hook_invocation | packaged_hook_invocation |

## All Cases

| Case | Category | Blueprint | Mode | Source | Outcome | Agent | Handoffs | Tools | Children | Elapsed |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| marketplace_packaged_hook_blocked_turn | marketplace-hooks | hook-smoke | auto | - | pass | - | - | - | 0 | 0.0s |

## Best 10 Demo Prompts

### 1. Marketplace packaged hook blocked turn

Case: `marketplace_packaged_hook_blocked_turn`
Category: marketplace-hooks
Routing mode: `auto`
Status: pass
Selected agent: `-`
Active Agent Blueprint: `hook-smoke`
Provider/model: `-` / `-` via `-`
Provider settings: temperature=1.0, max_tokens=32000, context_length=0, thinking_budget=0
Route graph: -
Route metrics: depth=0, branches=0, sync_handoffs=0, child_sessions=0, tools=0
Semantic trace: 4 events, 4 live, types=hook.invocation.started, hook.pre_message.blocked, turn.failed, turn.started
Expert handoffs: none
Tools: none
Data/input files: none
Setup turns: 0
Root session messages: 1
Child session logs: 0
Actions: agent_blueprint_hook_enable=ok, packaged_hook_probe=ok
Child sessions: none
Artifacts: none
Artifact evidence: none
Elapsed: 0.0s

Prompt:

```text
Using the active hook smoke agent, verify that the packaged pre_message hook is disabled by default, explicitly enabled with trust, and invoked with packaged provenance during a blocked benchmark probe.
```

What to see: CLIO exposes the hook-smoke packaged hook descriptor, explicitly trusts and enables pre_message, then records hook.pre_message.blocked semantic events whose handler provenance points back to the Agent Blueprint hook file.

Why this is interesting: Closes the marketplace packaged-hook evidence gap without relying on final answer wording or a provider call.

Observed excerpt:

```text
<no assistant text>
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
