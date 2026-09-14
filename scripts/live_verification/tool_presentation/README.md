# Tool-presentation live qualification

This folder preserves the September 2026 live qualification campaign that exercised CLIO's
ordinary tools, agent tools, declared skills and workflows, resources, memory, A2UI, provider
refresh, scientific workflows, review controls, compaction, and reconnect behavior.

It is evidence, not a blanket green claim. The source ledger contains successful, failed,
cancelled, and superseded attempts. The canonical replay set below selects the latest useful
session for each scenario and calls out incomplete coverage.

## Source snapshot

- Workspace: ws_9d80e5cb9a92
- Runtime snapshot: clio_develop_workspace/generations/20260905-112411-20732
- clio-agent candidate: def21cfbacd3e80bd0e8b6a3aaae5a859ed0b6a7
- GACT candidate: 91c6e232a3a693dfccaca740b7879630dd783a51
- Marketplace candidate: 0fa12654e2236c52a61f22e8c92c699afcda72c7

The workspace-local blueprints in blueprints/ are copied from the live qualification workspace.
The prompt corpus in prompts.md is normalized only where an absolute generated path or ephemeral
task/resource identifier must be supplied by a fresh run.

## Canonical scenario ledger

| # | Surface | Canonical session | Recorded outcome |
|---|---|---|---|
| 1 | File, shell, proposal, artifact | sess_06dbc64f81f3 | Completed sequence plus rejection and dedup probes |
| 2 | Plan review and execution | sess_e53de1d447ba | Rejected plan revised, approved, and executed |
| 3 | Todos, goal, loop | sess_b7a7175a70ea | Serialized status, wake, stop, and todo transitions |
| 4 | Schedules | sess_caae1e440da2 | Empty, create, list, delete, empty lifecycle |
| 5 | ask_user and A2UI | sess_5d1c681a1738 | Choice resume, form.submit, agent.submit, and rich A2UI |
| 6 | Spawn, Observe, Message, Wait, Collect | sess_f468e8f424bb | Causal single-child lifecycle completed |
| 7 | Parallel spawn and natural collection | sess_3a59ef8053e4 | Two-child fanout completed |
| 8 | Ordinary skill, child skill, workflow | sess_523775e5c24e | Declared sequence completed |
| 9 | Resource custody and retrieval | sess_8d82f4f99983 | Wait, list, inspect, structure, search, and read completed |
| 10 | Workspace memory | sess_ff07385387f5 | Search, summary, frame, and cross-workspace denial completed |
| 11 | Provider refresh | sess_8c4392bf36f8 | Eight provider outcomes returned |
| 12 | NDP scientific workflow | sess_c43cfba4571c | Honest blocker after live catalog returned no KOOT dataset |
| 13 | Cross-file parallel triage | sess_63024dfdb17e | Declared fanout and causal comparison completed |
| 14 | Dirty quality gate and review | sess_0c684d65bc01 | Failing gate, approved repair, recovery, and artifacts exercised |
| 15 | Compaction and reconnect | sess_74996fafcfec and supplemental sessions | Follow-up proof campaign; rerun required at release tip |

Supplemental proof sessions include sess_4fb7bc4d1d37 (Claude context accounting),
sess_bc67c3320822 (reconnect), sess_1daba315fe16 (cold transcript rehydration),
sess_1025b766961f (work semantics), sess_59948569b8bf (Wait returns output), and
sess_574421c902f6 (final child lifecycle wording).

## Coverage gaps that block a release claim

- A2UI was exercised live in sess_5d1c681a1738. Eight A2UI parts were recorded.
- No non-empty MCP App part was recorded in this workspace. A live MCP Apps replay is required.
- The release candidate tips were created after much of this campaign. The canonical scenarios
  must be rerun on the exact release candidates; historical success is regression evidence only.
- The NDP scenario proves honest failure handling, not successful acquisition, profiling, plot,
  or report publication. A separate data-available scientific happy path is still required if the
  release claims that complete experience.
- Session reload performance and progressive message delivery were not measured by this corpus.

## Replay rules

1. Start from a clean contained workspace populated with the listed fixtures and blueprints.
2. Replace every placeholder in prompts.md only with an identifier returned by the current run.
3. Preserve tool order and call counts. Do not compensate for a failed call by adding another call
   unless a new replay session is explicitly recorded.
4. Record provider, model, candidate SHAs, session ID, terminal status, and any semantic blocker.
5. A failed, cancelled, pending, skipped, or unrun scenario is not green.
6. Keep screenshots as presentation evidence, but ground behavioral verdicts in stored messages,
   semantic traces, and typed tool results.

