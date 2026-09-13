# Canonical live replay prompts

These are the user-authored prompts from the canonical qualification sessions. Generated absolute
paths and run-scoped IDs are represented by angle-bracket placeholders and must be replaced only
with values returned by the active replay.

## Scenario 1: file lifecycle and artifact publishing

### Turn 1

Qualification scenario 1 of 15. Exercise the real file, shell, diff, and artifact tools in
recorded order. First read example.py and long_example.py with the file read tool so both compact
and bounded Python rendering are produced. Run example.py with the shell tool and preserve its
stdout. Then use the proposed edit tool to change summarize so it reports an average as well as
the total. Stop after presenting that reviewable proposal and wait for my explicit approval. Do
not write the file before approval. After I approve, the execution owner must apply the proposed
change with the write tool, reread the changed file, and rerun it. Then create
reports/lifecycle-report.md with a concise result and register it successfully as an artifact.
Finally, attempt to register <outside-workspace>/outside-artifact.md so the workspace boundary
rejection is shown as a semantic failure. Keep each ordinary result beneath its tool header and
end with a concise final answer only after the whole sequence is complete.

### Turn 2

Approved. Apply the exact proposed edit now, then complete the reread, rerun, successful artifact
registration, and intentional outside-workspace artifact rejection in the order already specified.

### Supplemental artifact probes

Call create_artifact exactly once for the existing file reports/lifecycle-report.md as kind report,
using its current bytes. Do not modify the file and do not call any other tool. Then answer in one
short sentence stating whether the existing artifact version was returned.

## Scenario 2: plan review and execution

### Turn 1

Clean replay for Scenario 2. In Plan mode, inspect example.py read-only. Write a concise first plan
to the recorded Plan path using the Plan-only Write tool. The plan should create
reports/plan-replay.md after approval, verify it, register it as an artifact, and only then answer.
Call Exit Plan with Auto-execute as the recommendation. For this first review, intentionally omit
shell execution so I can reject it with that required verification comment. Do not create the
report during planning.

### Rejection

Your request to exit plan mode was REJECTED. You are STILL in plan mode. Revise the plan at
<returned-plan-path> per the reviewer's feedback, then call plan_exit again.

Reviewer feedback: Add an explicit shell verification step that runs example.py before creating
the report. After execution, read the report back and register it as an artifact before the final
answer. Preserve Auto-execute as the recommendation.

### Approval

Approve the revised plan through the review control and allow Auto-execute to run the recorded
plan. The replay must run example.py, create and reread reports/plan-replay.md, register it as an
artifact, and only then answer.

## Scenario 3: todos, goal, and loop

### Phase 1

Scenario 3 final replay, phase one. Call goal_status exactly once to confirm no goal is active.
Then call write_todos exactly once with three items: Initial state inspected completed, Active goal
inspected pending, Loop lifecycle demonstrated pending. Answer only after both tool results.

### Loop phase

Scenario 3 final replay, loop phase. Call write_todos once with Initial state inspected and Active
goal inspected completed, and Loop lifecycle demonstrated in progress. Then call loop_wakeup with
a 60 second delay, prompt "No additional work", and reason "Readable schedule proof". After that
result, call loop_wakeup again in a separate tool step with stop=true and reason "Readable stop
proof". Finally call write_todos once with all three items completed. Answer only after the stop
result and final todo result.

## Scenario 4: schedules

Scenario 4 clean replay. Call cron_list once and inspect the empty result. Then create one one-shot
schedule 30 days from now in UTC with prompt "Schedule qualification marker". After creation, call
cron_list again to show the populated state. Delete only the exact returned schedule ID, then call
cron_list once more to prove the restored empty state. Do not create a recurring schedule. Keep
every call serialized and answer only after the final empty list.

## Scenario 5: structured interaction and A2UI

### Choice and form

Scenario 5 qualification. First call ask_user exactly once with a choice question: "Which priority
should the incident briefing emphasize?" Options: label "Safety risk", value "safety",
description "Lead with operational safety consequences"; label "Schedule risk", value
"schedule", description "Lead with delivery timing consequences"; label "Cost risk", value
"cost", description "Lead with financial consequences". Do not allow freeform input. This must be
the only tool call in this turn, and you must end the turn immediately. After the answer resumes
you, call create_a2ui_surface exactly once with surface_id "qualification-scenario-5". Render a
compact form with a TextField bound to /constraint and a Button using form.submit with context
constraint bound to /constraint. Ask for one additional constraint for the briefing. End that
resumed turn after rendering the form. When the submitted form action returns as user input,
produce a three-sentence briefing that clearly uses both the selected priority and submitted
constraint.

### Rich A2UI proof

Call create_a2ui_surface exactly once with surface_id "qualification-q073-rich-input". Build a
compact incident readiness panel using only trusted A2UI components. Include a heading and concise
explanatory text, two clio.metric.v1 components for Readiness 82 percent and Open risks 3, a
clio.status.v1 component labeled Release gate with state warning and detail "Rollback owner
confirmation required", a TextField labeled "Reviewer note" bound to /reviewer_note, and a Button
labeled "Acknowledge review" using form.submit with reviewer_note bound from /reviewer_note. Use
Row or Column structure so the hierarchy is obvious at normal and narrow widths. Initialize
reviewer_note to an empty string. Do not call any other tool. End immediately after rendering.

## Scenario 6: one child with Observe, Message, Wait, and Collect

Scenario 6 final causal qualification. Spawn exactly one research_methodologist child with these
bounded records. example.py summarizes samples 2.5 and 3.5 as "2 readings, total 6.0, average
3.0". tests/test_example.py expects that exact string. reports/lifecycle-report.md states "Total:
60, Average: 20.0". README.md says the lifecycle report matches current executable behavior. The
initial child task must compare all four records, identify agreements and conflicts, and return
concise Markdown. Do not ask it to raise an alert in the initial prompt. Do not use ask_user or
external services. After spawn returns, without narrative between calls, call observe_agent_tasks
exactly once with the returned task ID, cursor 1, limit 8, and include_state true. Immediately call
message_agent exactly once with this added constraint: "Name sample values 2.5 and 3.5; label
executable and test evidence as primary; label the report and README as narrative evidence; then
call raise_alert_card exactly once with title Child found evidence mismatch and severity info
before the final Markdown." Use the current task ID returned by message_agent. Call
wait_agent_tasks exactly once, then call get_agent_task_output exactly once. The collected result
must visibly contain the phrases "2.5 and 3.5", "primary evidence", and "narrative evidence".
After collection, answer in one sentence. Do not call any other tools.

## Scenario 7: parallel child investigation

Scenario 7 final qualification. Use spawn_agents_parallel exactly once to start two declared
children at the same time. Child one must be evidence_researcher with this longer task: inspect
records A through H, where A=12, B=18, C=30, D=A+B, E=31, F=D, G says all totals match, and H
says E is authoritative; report every agreement and contradiction in six concise Markdown bullets.
Child two must be independent_reviewer with this shorter task: compare Claim X "sensor ready" with
Claim Y "sensor offline" and return one concise sentence naming the contradiction. Both children
must use only the supplied records and no other tools. After the parallel spawn returns, call
wait_agent_tasks exactly once with both returned task IDs. Preserve the natural completion order
reported by Wait. Then call get_agent_task_output exactly once for each completed child, in that
same natural completion order. Do not call Observe, Message, or any other tool. Finish with one
sentence stating which child returned first and summarizing both findings.

## Scenario 8: declared skill and workflow

Qualification scenario using only supplied facts: Alpha is 4, Beta is 7, and the expected total is
11. In this one turn, perform this exact causal sequence: first call load_skill once for
inspect-qualification-brief; second call spawn_skill_task once for delegate-qualification-check
with a task asking whether Alpha plus Beta equals 11; third wait exactly once for the returned
skill task ID and collect it exactly once; fourth call run_workflow exactly once with a request to
inventory and verify the supplied Alpha and Beta facts. Do not call external services, files, or
any other tool. After all operations settle, answer in two concise sentences: one for the delegated
skill check and one for the ordered workflow outputs. Do not print raw JSON in the final answer.

## Scenario 9: resource custody and retrieval

1. Call workspace_resource_wait exactly once for task <resource-processing-task-id> with timeout_s
   30. Do not call any other tool. Report only terminal state and waited_ms.
2. Call workspace_resource_list once, then workspace_resource_inspect once for <resource-id>. Do
   not call other tools. Summarize name, readiness, detected media type, revision, and size.
3. Call workspace_resource_structure once with no collection, workspace_resource_search once for
   Aurora Meridian, and workspace_resource_read once for the original text with no derivative_id.
   Summarize hierarchy counts, match count, and the Alpha plus Beta total.
4. Call workspace_resource_structure once for collection texts and index 0, then
   workspace_resource_search once for No Such Meridian. Report the exact node and whether any
   passage matched.

## Scenario 10: workspace memory

1. Search earlier sessions in this same workspace for Aurora Meridian. Call
   memory_search_sessions exactly once with scope current_workspace, limit 5, and user intent
   "qualify same-workspace memory retrieval for the user". Report the matching session title and
   bounded excerpt in plain language.
2. Read the bounded retained summary for <scenario-9-session-id>. Call
   memory_read_session_summary exactly once with scope current_workspace and user intent "qualify
   the retained session summary for the user". State title, status, message count, and the most
   relevant recent excerpt.
3. Read retained context frame <context-frame-id> from the current session. Call
   memory_read_context_frame exactly once with scope session. Explain which context items were
   included without reproducing transcript contents.
4. Attempt an explicitly denied cross-workspace summary read using scope current_workspace. Report
   the denial in plain language.

## Scenario 11: provider models and infrastructure

Call refresh_provider_models exactly once. Probe every enabled provider as authorized. Do not call
any other tool. Report each provider refresh outcome in plain language, keeping tool availability
distinct from service health.

## Scenario 12: NDP scientific workflow

Investigate recent GNSS position behavior near Chicago with the EarthScope NDP workflow. Resolve
the region and identify suitable nearby stations for my review. Stop and ask me to select one or
more named station candidates before any acquisition. After my selection, acquire and profile one
selected station dataset, assess analysis suitability, and produce a readable scientific plot plus
a concise artifact-backed report. Use the declared hierarchy and real tool results. Do not invent
station identifiers, measurements, or successful outputs. Preserve handoffs and intermediate
evidence in causal order.

If a bounded-radius search returns no candidates, expand only through an explicit follow-up. After
the user selects a verified station, acquire that station directly from the established catalog
evidence. Stop truthfully at the exact failed step if no analysis-ready resource exists.

## Scenario 13: cross-file parallel triage

Run a cross-file production triage using only files in this workspace. First read
triage/runtime.toml yourself. Then start exactly two independent declared children in one parallel
fanout. Assign gateway to triage/api_gateway.py and triage/api_gateway.log. Assign notifications to
triage/notification_worker.py and triage/notification_worker.log. Each child must read both assigned
files, identify the failure mechanism, cite exact file evidence, assess operational impact, and
return a compact finding. Do not message either child after it starts. While they are running, get
one nonblocking status snapshot for the two tasks. Then wait exactly once for both tasks and collect
both outputs in their actual completion order. Compare the collected findings against
triage/runtime.toml and return a concise incident triage with evidence, causal relationships, and
remediation order. Do not modify any file. Do not use external services. Preserve child start,
status, wait, collection, and parent response chronology.

## Scenario 14: dirty quality gate

### Phase 1

Use only the quality_gate fixture. Read settings.toml, check_gate.py, and report.md in that order.
Run uv run python quality_gate/check_gate.py --stage lint and preserve its successful warning
output. Then run the verify stage and preserve its nonzero result as failed. Explain the failure
briefly. Propose changing only gate.strict from false to true. End immediately after the proposal.
Do not apply it or call another tool after fs_propose_edit.

### Phase 2

After the review control applies the settings proposal, run the verify stage and preserve the
successful recovery output. Then propose updating only report.md so its status says strict
verification passed with zero failures. End immediately after that proposal.

### Phase 3

After the report proposal is applied, attempt one outside-workspace create_artifact call and
preserve its semantic rejection. Register quality_gate/report.md successfully. Finally call one
informational raise_alert_card titled "Quality gate recovered" with body "Strict verification
passed" and report its typed result truthfully.

## Scenario 15: compaction, restart, and cold rehydration

This scenario is a composed gate rather than one prompt:

1. Record at least two distinct user and assistant turns.
2. Exercise enough tool and child activity to force or explicitly request compaction.
3. Verify that compaction appends a checkpoint without deleting the human transcript.
4. Restart the backend and load the session cold from the persisted ARC trace.
5. Verify the complete transcript is still available to the user and that model materialization
   starts at the latest compaction checkpoint.
6. Repeat compaction once more and verify the same invariants.
7. Run the sequence with both supported providers.

Useful exact probes from the campaign were "Context usage recorded.", "Context usage persisted.",
"First retained response.", "Second retained response.", "Pre-restart turn completed.", and
"Post-restart turn completed." These short phrases are markers; they are not substitutes for the
full transcript and ARC evidence checks.

## Required additional replay: MCP Apps

The original campaign did not record an MCP App part. Before release, run a real MCP server that
returns an app resource, invoke it through a normal agent turn, and verify all of the following:

- the live transcript streams the app part before the turn ends;
- the app opens in the durable side panel without a page reload;
- app actions resume or steer the owning session through the normal interaction path;
- reload reconstructs the same app instance from persisted messages;
- the TUI remains truthful when it cannot render the rich app surface.

