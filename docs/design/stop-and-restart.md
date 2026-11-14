# Stop, restart and durability of started work

Status: implemented on `claude/stop-restart-durability` in clio-agent and gact-tui (Nov 14, 2026).
Owner request: "a stop command should stop a flow, or the flow needs a clear
mechanism to stop it, or maybe on stop we pop a message to stop ongoing tasks.
And then if asked to be restarted we have the semantics for that."

This note changes one decision in
[orchestration-semantics.md](sdk/orchestration-semantics.md) and
[agent-facing-async-tasks.md](agent-facing-async-tasks.md): Stop used to end only
the turn, always leaving accepted work running. Stop now has an explicit scope.
Everything else here reuses the task layer that already exists: subtree
cancellation, restart lineage, the flow journal and recovery.

## What exists, and what was missing

| Area | Before | Gap |
| --- | --- | --- |
| Stop | `POST /v1/sessions/{sid}/cancel` ends the turn and its waiter. Accepted work continues. | A deep-research flow, its researchers and their tool calls kept running after Stop, and nothing offered to stop them. |
| Cancel | `cancel_tasks` / `POST …/tasks/cancel` on a conversation assignment closes admission and settles its subtree (`conversation_controls._cancel_subtree`). A flow refuses new calls (`AdmissionClosed` → `CallCancelled`) and its run ends `cancelled`. | Reachable only one handle at a time, from the Runs rail. |
| Restart | `restart_agent` / `agent-controls/{handle}/restart` opens a linked assignment (`restart:` invocation). A flow resumes its earlier runs through `journal.resume_from` and keeps finished keyed calls. | None for the mechanism. |
| Crash recovery | `_restore_assignment` resumes interrupted assignments with a control notice; unanswered model calls get an `operation_outcome_uncertain` observation. | Spawn sites used `TASK_CALL.invocation_id or uuid4()`, empty for native tools, so nothing tied a started task to the interrupted call. The parent could not find the flow it started and called `run_flow` again (2 of 3 SIGKILL reproductions). The flow task was labelled "CLIO", so `query_tasks(query="deep-research")` missed it. |

## 1. Stop semantics

**Decision: Stop asks when there is work to ask about, and the API takes the
answer as an explicit parameter.**

`POST /v1/sessions/{sid}/cancel` takes an optional body
`{"children": "keep" | "stop"}`.

- `keep` (the default when the body is absent, so existing clients keep their
  behaviour) ends the turn and its waiter. Started work continues and reports
  back through the mailbox, as before.
- `stop` ends the turn and also stops everything the conversation started:
  child agents, flows (with their own children), workflows, questions and
  in-flight tool tasks. It does this by cancelling the conversation's current
  assignment through the ordinary cancellation path, which closes admission
  before cancelling and settles the whole subtree. If the assignment had
  already ended while its children ran on, each open child is cancelled
  instead. The intervention is recorded with `reason: "conversation_stop"`.

`GET /v1/sessions/{sid}/stop-scope` lists the open work Stop could reach: the
top-level open tasks started by the conversation, with handle, kind, label and
status. The UI reads it when Stop is pressed:

- nothing open: Stop ends the turn at once (no dialog);
- open work: a dialog names the running work and offers **Stop everything**
  (primary), **Stop reply only** and **Keep working** (dismiss).

Why ask instead of always stopping everything: the async-task design
deliberately lets downloads, indexing, HPC jobs and long shell commands outlive
Stop, with recorded evidence that people rely on that. A blanket cascade would
cancel a two-hour transfer because someone interrupted a reply. Why not always
keep: a flow is the conversation's own reasoning fanned out, and leaving it
running after Stop is what the owner reported as wrong. The dialog makes the
choice once, at the moment it matters, and the API names it so scripts and
agents never depend on a hidden default.

**Settlement.** Every task in a stopped subtree settles `cancelled` with
`settlement: confirmed` (or `task_state_uncertain` when an external server
cannot confirm, which is a typed outcome, never `active`). The assignment's
`cancellation_members` record which tasks the stop covered; the intervention
record carries `action: cancel`, `reason: conversation_stop` and the actor. The
cancellation notices are retained for the conversation's next deliberate
input; they do not wake the stopped parent (`Conversations.eligible`), so Stop
everything does not immediately start a new turn reacting to its own stop.

**Flows stop cooperatively.** Once the flow's assignment has a cancellation
request, `ctx.call`/`ctx.start`/`ctx.gather` refuse new work with
`CallCancelled` (admission is closed), running members are cancelled by the
subtree cancellation, and `ctx.step` checks the same request before running a
local step. A flow can also read `await ctx.stop_requested()` between phases.
The run's journal is saved with status `cancelled`, so every finished call
keeps its handle and result for a restart.

## 2. Restart semantics

Restart never repeats accepted work blindly. It opens a **linked assignment**
(`invocation_id = restart:<id>`, `predecessor = <handle>`) only after the
earlier one has confirmed settlement; an unconfirmed settlement refuses the
restart with a recorded `restart` outcome `not_started` and its reason.

| What | After Stop or cancel | After a server crash |
| --- | --- | --- |
| Child agent task | Restart opens a linked assignment in the same child conversation with a control notice: inspect the last confirmed operations, do not replay accepted operations. | Recovery resumes the interrupted assignment in place (no new handle) with the "service restarted" notice. |
| Flow (`run_flow`) | Restart resumes every earlier run of that flow in the session (`_earlier_runs`). Calls whose tasks completed return their stored results; cancelled or failed calls run again under new occurrence ids; nothing finished runs twice. | The flow's assignment resumes; `forward()` re-runs from the top and each call re-attaches to its handle by invocation id. A call whose acceptance is uncertain raises `TaskStateUncertain` to the flow. |
| Workflow (`run_workflow`) | Same as a flow: finished steps keep their handles, the rest run. | Same as a flow. |
| The conversation itself | Restart of its assignment opens a linked assignment; the model sees the cancelled tasks and decides what to restart. | Resumes in place. |
| A child of a stopped conversation | Stop everything cancelled the conversation's assignment, which closes admission below it. Restarting the child first opens a linked successor for the stopped conversation (`restart-child:<handle>`, no input of its own), so the child's result reaches a live conversation and wakes it. Stopped ancestors are resumed the same way, top down. | Not applicable. |

Who can invoke it:

- **Person, API**: `POST /v1/sessions/{sid}/agent-controls/{handle}/restart`,
  `POST /v1/sessions/{sid}/tasks/cancel`, and Stop with `children`.
- **Person, UI**: Stop (with the dialog above) in the composer; Stop and
  Restart on each run row and in the work record of a child task.
- **Model, when asked**: `cancel_tasks` and `restart_agent` (already in the
  core runtime tools). Nothing in core restarts on its own; the model decides.

## 3. Durability across server restarts

1. **Every started task is linked to the model call that started it.** Spawn
   sites (`run_flow`, `run_workflow`, `spawn_agent_task`,
   `spawn_agents_parallel`, skill effects) take their
   invocation id from `gact/spawn_invocation.py`: the task-call id when the
   call is task-capable, otherwise the tool observer's call id joined with the
   model's own tool-call id (`<observer id>~<model call id>`), otherwise a
   fresh id. The observer part keeps handles unique even for providers that
   reuse tool-call ids; the model part lets recovery find the task. A call a
   script makes (`run_code`) carries the id of the model call that ran the
   script. External MCP tasks keep their own `TASK_CALL` id.
2. **The interrupted-call observation names what the call started.**
   `_record_interrupted_calls` looks up the conversation's direct tasks whose
   invocation carries the unanswered call's id and adds them to the
   `operation_outcome_uncertain` observation (`started_tasks`: handle, kind,
   label, status), with text saying the task exists, was not started again and
   can be observed or waited on. It reports what is recorded; it does not
   decide for the model.
3. **Flow tasks carry the flow's name.** A started flow's child conversation is
   titled `<flow id> flow` unless a name was given, so its task label and
   `query_tasks(query=...)` find it.
4. **Nothing stays active without progress.** Recovery settles each
   interrupted assignment: it resumes, settles with `startup_retirement_error`
   as `task_state_uncertain`, or continues cancellation that was requested
   before the crash. MCP calls in flight at the crash become
   `task_state_uncertain`, which counts as settled for a flow's call
   (`run_view._SETTLED`) and for its wait. An uncertain call whose owning
   assignment has settled no longer counts as an ancestor's pending work
   (`gact/assignment_pending.py`), so it cannot hold a finished flow `yielded`.
5. **A restarted run reads its kept calls' evidence.** The subtrees of the runs
   it resumes are its own (`flows/tool_evidence.py`); read authorization still
   applies.

## 4. UI (gact-tui)

- The composer's Stop reads `stop-scope`. With open work it shows a dialog
  that lists each task by label and status text ("Running", "Waiting for
  input"), never by colour alone, with the three actions above. Copy is in
  Lingui.
- Run rows and the work record keep their Stop and Restart controls. A stopped
  run's card reads "Cancelled" and its graph marks the step where it stopped as
  "Stopped", both in text.

## Verification

Tests (`tests/test_gact/test_stop_children.py`, `tests/test_flows/test_stop_restart.py`):

- Stop with `children: stop` while a child agent runs settles the child and the
  conversation's assignment `cancelled`; with `keep` the child runs on.
- Stop of a flow mid-gather settles the flow and every member; no task stays
  open; the journal records `cancelled`.
- Restart after that Stop keeps the finished members' handles and runs only
  the cancelled ones.
- An interrupted call's uncertainty observation names the flow it started.
- Spawn invocation ids carry the model call id and stay unique when a provider
  reuses ids.

Live checks are recorded in the PR body; scripted or tool-directed checks are
mechanism verification, not evidence of agent judgement.

## Live evidence (Nov 14, 2026)

Stack: `scripts/cloud/stack.sh` with the `claude_code` provider (Haiku 5.5), agent
`deep-researcher`, a separate `CLIO_AGENT_HOME` under `.cloud-run/`. Request in every
run: "Which HDF5 filter IDs are registered for the ZFP and SZ3 compressors, and what
error-bound modes does each offer? Keep it to at most two directions and no follow-up
rounds." Screenshots are in
[evidence/stop-restart-2026-11-14](evidence/stop-restart-2026-11-14/), captured with
`scripts/cloud/ui_check.mjs --scale 2` at 1440x900 and 390x844 (the phone dialog,
04, at scale 1).

### Stop, before and after

| | Desktop | Phone |
| --- | --- | --- |
| Before (develop `8051d77d`, its own server and UI): Stop pressed; 60 s later the reply reads "Stopped" while the run card reads "Running" and a check is still executing | [01](evidence/stop-restart-2026-11-14/01-before-develop-desktop.png) | [02](evidence/stop-restart-2026-11-14/02-before-develop-phone.png) |
| After: Stop asks, naming the open work in words | [03](evidence/stop-restart-2026-11-14/03-after-stop-dialog-desktop.png) | [04](evidence/stop-restart-2026-11-14/04-after-stop-dialog-phone.png) |
| After "Stop everything": the card reads "Cancelled", both checks did not run, the graph marks "Stopped"; every task is `cancelled` or `completed` and `stop-scope` is empty | [05](evidence/stop-restart-2026-11-14/05-after-stopped-desktop.png) | [06](evidence/stop-restart-2026-11-14/06-after-stopped-phone.png) |

### Worked example 1: restart a stopped run

Stopped with "Stop everything" while both checks ran, then the person wrote "Please
restart the research run I stopped." No tool or step was named, so this is evidence
of the agent's own choice. The agent loaded `restart_agent` and restarted the
stopped flow. Screens: stopped [07](evidence/stop-restart-2026-11-14/07-restart-stopped-before.png),
the request and the agent's restart [08](evidence/stop-restart-2026-11-14/08-restart-request-desktop.png) /
[09](evidence/stop-restart-2026-11-14/09-restart-request-phone.png), the answer
[10](evidence/stop-restart-2026-11-14/10-restart-answer-desktop.png). Task trees:
[after stop](evidence/stop-restart-2026-11-14/e2b_after_stop.txt),
[after restart](evidence/stop-restart-2026-11-14/e2b_after_restart.txt).

| | |
| --- | --- |
| Kept | Plan; both researchers (`task_fd6fd259e`, `task_c57e39e79`) and every search and fetch they made. The restarted run accepted no new researcher. |
| Re-ran | Both checks and the critic (new handles under `restart:337ec18a…`), then the report. |
| Lost | The two checks' partial work at the stop (their model turns were cancelled). |
| Gaps | The parent conversation draws no run card for the restarted run (a card follows only a `run_flow` call), and the stopped card does not link to its successor. |

The first attempt at this example (before the fixes in `ea19d05a`) found three
defects, all fixed and retested here: the agent called `run_flow` again, because no
description said restart keeps finished work; the restarted run was refused reading
its kept researchers' evidence; a run resumed after a crash stayed `yielded`.

### Worked example 2: restart after a crash

SIGKILL of the server's process group while both checks ran (17:26:30 UTC), restart on
the same home, then "The service crashed while the research was running. Please
restart the research run." Again no tool was named. Screens: before the crash
[11](evidence/stop-restart-2026-11-14/11-crash-before-desktop.png), after
[12](evidence/stop-restart-2026-11-14/12-crash-after-desktop.png) /
[13](evidence/stop-restart-2026-11-14/13-crash-after-phone.png). Task trees:
[before](evidence/stop-restart-2026-11-14/e3_before_crash.txt),
[after the crash](evidence/stop-restart-2026-11-14/e3_after_crash.txt),
[after the restart](evidence/stop-restart-2026-11-14/e3_after_restart.txt).

After the crash, recovery resumed the flow, both checks and the parent on their
original handles. Reading the recovery notice, the agent wrote "I'll check the
deep-research run I already started rather than launching it again". Asked to
restart, it queried the run, saw it unfinished and called `restart_agent`.

| | |
| --- | --- |
| Kept | Plan; both researchers (`task_1c116ef79`, `task_c2a27d54f`) and their 25 completed searches and fetches. |
| Re-ran | Both checks and the critic under `restart:d4b5c402…`. |
| Lost | The checks' model turns in flight at the kill, and the progress the resumed checks made before the restart cancelled them. |
| Gaps | The restart cancelled checks that recovery had already resumed; waiting would have kept that progress. The agent's summary says the first run "was cancelled when the service crashed"; the restart cancelled it. At this kill no MCP call was in flight (the fetches finished during the screenshot). In the earlier SIGKILL of the same day, with 7 searches in flight, those settled `task_state_uncertain`, the flow and researchers resumed on their handles, and no duplicate run started. |

In both examples one deep-research run existed at a time: no duplicate run.
