# Agent-to-agent communication: notes for the next campaign

Written 2026-09-11 while landing the release stack (#1339 level 9). Not a plan. These are the
facts and judgments gathered about the agent-to-agent tool family so the campaign that rebuilds
it (A2A, ACP, inter-agent messaging) starts from evidence instead of from memory. Every code
reference is to the release-stack tip (`fix/1339-compaction-checkpoint`, which carries Codex's
`codex/tool-presentation-contract` history); re-verify against develop when the campaign opens.

## 1. The family as it exists today

| tool | declared in | title on the row | row shape | what it does |
|---|---|---|---|---|
| `spawn_agent_task` | `gact/agents/spawn_runtime.py` | Spawn Agent | handoff card | start one declared child as a real child session |
| `spawn_agents_parallel` | `gact/agents/spawn_runtime.py` | Spawn Agents | handoff card | start several children at once |
| `run_workflow` | `gact/agents/spawn_runtime.py` | Run Workflow | handoff card | run a declared deterministic workflow |
| `spawn_skill_task` | `gact/agents/skill_runtime.py` | Start child agent | handoff card | start a child from a skill |
| `wait_agent_tasks` | `gact/agents/native_presenters.py` | Wait | row | block until the named children are terminal; collects them (exactly-once) |
| `observe_agent_tasks` | `gact/agents/observe_runtime.py` | "Get status" in code, "Observe" in the test | row | the watcher (section 2) |
| `get_agent_task_output` | `gact/agents/agent_task_output_digest.py` | Collect | row | fetch a finished child's full output past the digest |
| `message_agent` | `gact/agent_messaging.py` | Message Agent | row | send a message into a running agent's inbox (steer) |
| `ask_user` | `gact/ask_user_tool.py` | Ask User | row | ask the human |

Facts that shape the campaign:

- `check_agent_tasks` no longer exists. `CLAUDE.md`'s tool list still names it next to
  `wait_agent_tasks`, and `get_agent_task_output`'s argument description still says "from
  spawn/wait/check". `observe_agent_tasks` is what replaced it. Both stale mentions should go.
- There is no cancel tool. A parent that observes a failing child can steer it with
  `message_agent` but cannot stop it; the only stop is the session-level cancel from the UI.
- The three postures after a fire-and-forget spawn are documented in `observe_runtime.py`'s
  module docstring: WAIT (`wait_agent_tasks`, blocking, terminal-seeking, consuming), OBSERVE
  (`observe_agent_tasks`, incremental, non-consuming), CONTINUE (observe-later injection at the
  parent's next turn, `gact/enrichment.py`, `TurnState.pending_notification_task_ids`). The
  CONTINUE posture is already a wake channel: child outcomes land in the parent's next prompt
  without a tool call.

## 2. `observe_agent_tasks`: what it is, what it was meant to be

Implemented semantics (`observe_runtime.py`, the OBSERVE posture from #1000, mirrors
`clio-relay`'s `relay_observe` / `_observe_job`):

- **Cursor.** Reads each child's event stream from a resumable cursor (the process-global event
  id) and returns `next_cursor`; two sequential observes never miss and never re-read
  (`EventBus.session_events_since`).
- **Pattern hold.** With `pattern` (a regex), the call scans every already-buffered page, and if
  nothing matches it subscribes to the bus (`wait_for_session_events` under the same lock as the
  history append, closing the snapshot-then-subscribe race) and holds the ONE call open until a
  new event matches or a requested child is terminal. No timeout, no polling ladder. The regex
  runs only over a bounded excerpt window (`OBSERVE_MATCH_MAX_CHARS`), never a raw payload; an
  invalid regex is a typed `invalid_pattern` row.
- **Non-consuming.** Never touches `notify_pending` / `consumed_at`, never emits a delegation
  terminal; the delegation stays open, so the parent can observe again, steer with
  `message_agent`, and let `wait_agent_tasks` collect. Codex's 7fac63a7 fixed the release on a
  child's terminal and added tests.
- **Curation.** Only the semantic event families reach the rows (react steps, extract landings,
  delegation stage transitions, skill loads, memory searches, failures); deltas, heartbeats and
  status changes never do.

The owner's intended shape (stated 2026-09-11): an asynchronous monitor.

```
observe([agent_1, agent_2], "ERROR|WARNING")  ->  monitoring_id
... parent waits or continues ...
"Monitor <id> triggered on agent_1 by: ERROR: ..."   (a wake, not a return value)
parent peers into agent_1's transcript, then cancels or steers it
```

Gap analysis: the capability is present, the SHAPE differs. Today the patterned call is a
synchronous hold whose return value IS the trigger; there is no monitor id, no "many monitors
outstanding", no wake delivered while the parent does other work. The CONTINUE posture is the
natural carrier for the async form: a registered pattern watch could inject its hit into the
parent's next turn exactly the way observe-later outcomes are injected now. A parent that wants
to block still has the hold.

## 3. The retitle incident, and the decision

Codex's commit 745a8d81 ("fix: present semantic tool outcomes", 2026-09-09) changed the tool's
title from "Observe" to "Get status" and nothing else in the module; the title table in
`tests/test_gact/test_tool_instrumentation.py` still expects "Observe", so that test is red on
every branch above it. The title is the label the transcript row shows the user, and "Get
status" describes a status poll, the one thing this tool is not. Decision (owner, 2026-09-11):
the title goes back to "Observe" when the release chain restacks; "Get status" is reserved for a
DIFFERENT tool (section 4).

What the qualification transcript showed for the observe row on Codex's tip (scenario 6):
"Get status (research_methodologist #1) 179 ms Succeeded / was running when checked / Context
received expert research_methodologist started". Three things to fix in the row, all
presentation: the title, the status line "was running when checked" (true for a snapshot, wrong
vocabulary for a watcher; the row should say what was observed: cursor range, pattern, hit or
no hit, terminal or running), and the excerpt line, which shows a lifecycle event rather than
the curated evidence the tool returned.

## 4. What the campaign should decide

1. **Split discovery from observation.** `get_status` (new): an open call that lists every agent
   alive in the current ARC for this session tree with its status, a "find" over the runtime,
   no task ids required. `observe_agent_tasks`: the watcher, unchanged in meaning, better
   tested, possibly with the async monitor shape from section 2.
2. **The async monitor shape.** Monitor ids, several outstanding, the hit delivered as a wake
   into the parent's next turn (reuse the CONTINUE injection), the hold kept as the blocking
   form. Decide whether a hit should also be a semantic event (it is evidence of the parent's
   decision to watch).
3. **Cancel and steer.** `message_agent` exists; a cancel tool does not. A parent that observes
   an error has no sanctioned stop.
4. **The row vocabulary.** Titles are human names (the title register rule from 0991b754 and
   1a57844a); rows must say what happened in the tool's own terms. The test's title table is the
   contract; change both sides together.
5. **A2A / ACP mapping.** The relay's `relay_observe` contract is the precedent for observe; the
   inter-agent messaging seam is `message_agent` plus the child-session inbox. When mapping to
   A2A or ACP, keep the three postures explicit (wait / observe / continue) rather than
   collapsing them into one "get task" call, which is how the retitle drift started.

## 5. What to test better

- The patterned hold, live: a child that emits an ERROR line; the parent's single observe call
  returns the hit with the right cursor, then a second observe from `next_cursor` returns nothing
  new; then `message_agent`, then `wait_agent_tasks` collects exactly once.
- Terminal release: a child that finishes without matching releases the hold with a terminal row.
- Cursor continuity across a chunk boundary of the child's event log and across a backend
  restart (the child's bus history is in-memory; say what the parent sees after a restart).
- The wake path (CONTINUE): an outcome that lands while the parent is idle appears in the next
  turn's prompt once, and its delegation terminal is emitted at the commit-to-run seam, not at
  enrichment (`TurnState.pending_notification_task_ids`).
- The row: title, status line and excerpt asserted from the presenter, and one screenshot.

## 6. Pointers

- `docs/design/agents-creating-agents-2026-07.md` (#948, the spawn substrate, S4 children as real
  sessions, S6 observe-later).
- #1000 (the OBSERVE posture), commit 88cf6bb0; Codex's e76c6726 "align agent task observation
  contracts", 7fac63a7 "release observation on child terminal", 848e32e2 / 745a8d81 (presentation
  contract, the retitle).
- `gact/agents/observe_runtime.py`, `gact/agents/spawn_runtime.py`, `gact/agent_messaging.py`,
  `gact/agent_tasks.py` (the task registry and the `agent.task.*` event catalog),
  `gact/enrichment.py` (the CONTINUE injection), `tests/test_gact/test_observe_agent_tasks.py`,
  `tests/test_gact/test_tool_instrumentation.py` (the title table).
- clio-relay `src/clio_relay/mcp_server.py::_observe_job` (the contract observe mirrors).
