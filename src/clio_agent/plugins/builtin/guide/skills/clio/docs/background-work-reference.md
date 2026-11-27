---
title: "Background work: limits and API reference"
description: "Pursue an objective, monitor a job, or schedule a future turn, with controls in Work."
source: https://clio.iowarp.ai/docs/background-work-reference/
---

# Background work: limits and API reference

Pursue an objective, monitor a job, or schedule a future turn, with controls in Work.

This technical reference records command flags, tool arguments and scheduling behavior. Start with [Continue work over time](docs/long-running-work.md) for the user workflow.

Use a **goal** for a project that needs continued planning, action and evaluation.
Use a **loop** to monitor something that changes over time. Use a **schedule** to
run an instruction later. These are independent controls in the same conversation.
Starting a goal, loop or schedule does not create another sidebar conversation.
Only conversations you start or fork appear there; delegated agents are followed
from their owning conversation's work views.

| Need | Control | Example |
|---|---|---|
| Keep pursuing an observable result | Goal | Explore candidate designs and save a validated comparison |
| Check a running job periodically | Loop | Monitor HPC job status and report when it finishes |
| Act at a particular time | Schedule | At 3pm, inspect the latest results and prepare a report |

> **Preview:** The continued-work controls pictured here (in development builds). Availability depends on your build.

## Start a goal

A goal starts work immediately. Include the objective, useful instructions and
what evidence counts as completion:

```text
/goal Compare the approved candidate designs. Read plan.md, evaluate each candidate with the agreed checks, and save comparison.md linking the outputs and validation results. The goal is complete when all candidates have recorded outcomes and the comparison is saved.
```

After each work turn, an independent judge reads recent conversation text and
bounded tool results. It checks completion against the objective and gives
concrete guidance for the next step. An unmet goal continues without a separate
user message. If an approach fails, the agent can change its plan and tools.
Completion stops goal continuation. It does not stop an independent loop.

For the difference between tools, background tasks, and todos, see
[Follow work in progress](docs/following-work.md). The
[injection guide](docs/injections.md) explains recorded reminders and wake instructions.

Automatic continuation appears in the conversation as a **Goal judgment injection**,
between the previous response and the next work. **Loop wake** and **Scheduled wake**
use the same injection display. These are CLIO-provided context, never messages
labelled “You”. Expand an injection to read the exact instruction the agent received.

![Three checkpoint responses separated by expandable Goal judgment injections](plugin-asset://guide/captures/docs-goal-injections.webp)
*A real goal saved one checkpoint per turn. Goal judgment injections continued the work between responses without inventing another human message.*

**Limits are optional.** An omitted or zero iteration, time or token limit means
unlimited. For a short trial you can explicitly choose limits:

```text
/goal --max-iters=5 --max-hours=0.25 Save three numbered checkpoint files. Write exactly one new checkpoint per turn, then finish that turn. Continue with the next missing file until all three have successful tool results.
```

The agent can also use `set_goal(condition, max_goal_iters=0, max_wallclock_s=0,
max_tokens=0)` when you ask it to pursue a long-running objective. It cannot
replace an active goal or erase a user stop or exhausted limit. Only you can
restart or replace those controls. The agent cannot declare its own goal complete.

The judge checks evidence available in the conversation; it is not a scientific
validator. It reads at most the latest 40 messages and 32,000 characters, plus a
retained ledger of verified progress from previous checks, bounded to 6,000
characters. Keep a concise progress report with output references current during long projects.
Independent inspection is still needed for scientific claims.

## Wait for a simulation or other task

A goal can remain active while accepted background work runs. The agent has two
ways to wait on the task handles returned by CLIO:

| Tool | Behavior |
|---|---|
| `wait_for(task_handle)` | Park the goal, finish the current turn, and resume through the task completion mailbox |
| `wait_for([handle1, handle2], return_when="all")` | Park until every selected task settles; `"any"` resumes after the first |
| `wait_tasks(task_handle)` | Wait inside the current turn; the default wait has no deadline |

Parking does not run repeated model checks or create a polling schedule. Task
completion wakes the owning conversation and delivers the retained result.
Failure, cancellation and interruption also return control to the agent so it
can inspect the outcome and adapt. They do not mean the objective was achieved.
Task handles must belong to this conversation; waiting neither collects nor
resubmits the operation.

When an external service offers no completion event, a monitoring loop or future
scheduled check can be useful. A goal itself is not a timer.

## Monitor with a loop

```text
/loop 5m --stop-text=DONE Check the HPC job's status. Report meaningful changes. When the job finishes, report its outcome and write DONE alone on the last line.
```

The first check runs after the interval. Each completed iteration schedules the
next automatically. The agent may use `loop_wakeup` to change an existing loop's next delay or
stop voluntarily. It creates a loop with `set_loop(prompt, interval_s=300,
stop_text="", max_iters=0, max_wallclock_s=0, max_tokens=0, max_usd=0)`.

Loops default to a five-minute interval and **no iteration, time, token or cost
limit**. To choose exactly five checks, add `--max-iters=5`. The optional stop text
is case-sensitive and matches a whole trimmed line in the final response. A
sentence such as “Not DONE yet” does not match `DONE`.

Intervals are clamped to one minute through one hour. The scheduler checks on
minute boundaries, so a requested wake can start almost a minute late. A busy
conversation waits for its current turn; two turns do not run concurrently.

Goals continue independently of loop pacing. `/loop stop` ends only that loop;
`/goal clear` ends only that goal.

![Two monitoring responses with an expanded Loop wake injection showing its recorded instruction](plugin-asset://guide/captures/docs-loop-injections.webp)
*This explicitly bounded loop ran two monitoring checks. Show what it got expands the recorded wake instruction.*

## Schedule a future instruction

Ask in ordinary language:

```text
Schedule a turn for 3pm today: inspect the simulation outputs and prepare a short report.
```

The agent translates this to `cron_create` with an instruction and exactly one
trigger: `delay_s` for a delay, `run_at` for an ISO-8601 instant, or `cron` for a
calendar expression. An optional IANA `timezone` controls calendar interpretation.
`max_fires` and `until` provide explicit run-count and deadline limits. Zero or
empty values leave them unlimited. A configured server lifetime policy can impose
an explicit deployment limit; the default is unlimited.

You can also use `/schedule` (an alias of `/cron`) or the schedules UI. Inspect the
resolved next-run time and time zone in **Work**. A one-shot removes its pending
schedule after firing. A recurring schedule advances until stopped or its explicit
limit is reached. Busy runs queue; failed staging uses bounded retries and exposes
its failure rather than silently disappearing.

![Expanded Scheduled wake injection followed by its saved report](plugin-asset://guide/captures/docs-schedule-injection.webp)
*The scheduled instruction appears as an injection, followed by the agent's work and saved output.*

## Inspect and control Work

Click the bottom **Session work** status, or open the conversation's **Data / Work**
finder and choose **Work**. Goals,
loops and schedules show their state. Expand **Details** for the objective,
configured limits, stop text, waiting handles and judge guidance. Zero limits are
shown as **Unlimited**. Completed and stopped controls remain under **Previous**.

![Completed goal with three iterations, Unlimited limits, judge guidance and verified progress, beside an independent active monitoring loop](plugin-asset://guide/captures/docs-work-goal.webp)
*A real three-turn goal saved plan.md, results.md and report.md. Expand Previous and Details to inspect its completion evidence or restart it. The monitoring loop remains active independently.*

- **Stop** prevents future goal continuation, loop wakeups or schedule occurrences.
- **Restart** explicitly starts a fresh goal or loop identity with the retained
  instruction and limits; restarting a stopped schedule resets its occurrence count.
- **Cancel and delete** stops the control and removes its Work record. Conversation
  messages and already created files remain.

Stopping a control does not undo file writes or cancel an already accepted task.
Use the task's own cancellation control for that operation. Conversation Stop
also cancels the active turn and pauses automatic continuation.

## Judges and interruptions

In **Settings → Fleet**, choose separate judges for **Goals**, **Best of N and
refinement**, and **Automode permissions**. Each defaults to the conversation's
model and can be reset to **Use conversation model**. Explicit selections use the
selected provider's own configuration.

![Fleet settings with separate Goals, Best of N and refinement, and Automode permissions model selectors, each set to Conversation model](plugin-asset://guide/captures/docs-fleet-judges.webp)
*Choose a judge independently for each role. Conversation model follows the model used for that conversation's turn.*

Automode's reviewer classifies operations as **none**, **low**, **medium**, or
**high** risk. None through medium proceed automatically; high risk asks you for
approval. Missing, invalid, failed or timed-out reviews also ask you. Explicit
permission rules and denials take precedence over the reviewer.

A goal needing an essential human decision waits for input. A failed goal judge
or failed work turn pauses the goal and retains the objective for inspection and
explicit restart. Ten consecutive identical nonempty final responses stop stalled
control continuation. Declared task waits do not count toward that safeguard.

Closing the browser leaves server-side work running. The backend, execution host,
model authentication and required services must remain available. On backend
restart, a retained goal resumes by inspecting recorded task results and workspace
state before planning further work. A task-waiting goal keeps its dependency;
accepted operations are not replayed. A loop with a durable pending wake can
resume; an interrupted in-flight loop stops with an interruption reason.

| Optional limit | Goal | Loop |
|---|---|---|
| `--max-iters=N` | Completed evaluated work turns, including the initial turn | Completed iterations |
| `--max-hours=N` | Elapsed time since setting the goal, including waits | Elapsed time since starting the loop |
| `--max-tokens=N` | Recorded conversation token increase | Recorded conversation token increase |
| `--max-usd=N` | Unavailable | Recorded cost increase when reported by the provider |

Limits are checked at control boundaries; waiting goal time limits are also
checked by the minute scheduler. They do not interrupt a tool at an exact spending
threshold. Usage rollups may be estimated and exclude some secondary calls.
Restart is an explicit new run, with new counters and budget baselines.

For recorded outputs and their inputs, see [Check answers and sources](docs/evidence.md).
