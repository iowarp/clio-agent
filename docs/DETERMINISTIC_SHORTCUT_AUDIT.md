# Deterministic Shortcut Audit

Updated: 2026-06-02

This document classifies deterministic paths that could be confused with CLIO's
model-driven hierarchy semantics. The release benchmark may use deterministic
checks to verify evidence, but it must not use deterministic routing or
continuation logic to make a workflow succeed.

## Rule

Real semantic-proof lanes must start from the root Agent/Expert selected by the
active session or Agent Blueprint, let the model-driven planner choose tool or
expert actions, and verify the resulting evidence after the turn. They must not
force a domain route, select a leader from output keywords, execute a tier-3
child directly, or continue a failed workflow with harness-only code.

## Classification

| Path | Classification | Status | Rationale |
| --- | --- | --- | --- |
| `scripts/run_demo_benchmark.py` lane expectations, route metrics, artifact checks, and sync-return checks | Valid verification infrastructure | Keep | These checks run after the CLIO turn and fail missing evidence. They do not choose the route or execute recovery work. |
| `scripts/run_demo_benchmark.py` real-orchestrator forbidden route sources: `guard`, `user_agent_keyword`, `recovery` | Release gate | Keep | The default `real_orchestrator` lane rejects shortcut route sources and requires structured route/tool evidence. |
| GACT keyword user-agent routing | Explicit opt-in legacy behavior | Keep guarded | It is disabled unless `CLIO_ENABLE_KEYWORD_USER_AGENT_ROUTING` is set. Real benchmark cases forbid `user_agent_keyword`. |
| ARC `MultiAgentCoordinator.create_plan` keyword planner | Legacy compatibility path | Disabled by default | This v0.2 coordinator splits on words such as `then` and maps task text to fixed expert keywords. It is not used by the active CLIO agent loop and now requires `allow_legacy_keyword_planning=True`. |
| Capability and skill `keywords` fields | Discovery metadata | Keep | These fields describe capabilities for users, UI search, and model context. They must not be treated as automatic runtime routing decisions. |
| File suffix compatibility guards | Valid safety infrastructure | Keep | These checks prevent an expert from being asked to inspect incompatible concrete files. They are guardrails after a planner action, not domain route selectors. |
| NDP/SAC recovery path guards | Valid safety infrastructure | Keep under audit | These checks prevent hallucinated downstream SAC paths after an NDP blocker. Recovery must still be chosen by the planner and proven by observed local paths/artifacts. |
| Benchmark report rendering and JSONL rehydration | Valid reporting infrastructure | Keep | Rehydration preserves evidence for review; it must not turn missing evidence into a pass. |

## Regression Expectations

- Default CLIO runtime orchestration uses the model-driven planner in
  `clio_agent.agent`, not ARC `MultiAgentCoordinator`.
- ARC `MultiAgentCoordinator.create_plan` raises by default unless legacy
  keyword planning is explicitly enabled.
- Real benchmark lanes default to `real_orchestrator` and reject shortcut route
  sources.
- Any new shortcut kept for test fixtures must be opt-in, named as a fixture or
  legacy path, and excluded from semantic-proof lanes.

## Open Edge

The NDP/SAC recovery guards remain intentionally domain-specific safety checks.
They should be reviewed after the next real-provider benchmark run to ensure the
planner, not the harness, selected any alternate provider, download, analysis,
or visualization action.
