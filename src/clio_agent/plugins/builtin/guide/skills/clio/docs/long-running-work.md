---
title: "Continue work over time"
description: "Give CLIO a finishing condition, monitor a running job or schedule a future check, then inspect and stop that work."
source: https://clio.iowarp.ai/docs/long-running-work/
---

# Continue work over time

Give CLIO a finishing condition, monitor a running job or schedule a future check, then inspect and stop that work.

> **Preview:** The continued-work controls pictured here (in development builds). Availability depends on your build.

Some work needs more than one response: preparing a comparison, waiting for a
simulation, or checking for new results later. Tell CLIO what should happen,
what counts as finished and when it should come back to you.

| What you want | CLIO's name for it | Example |
| --- | --- | --- |
| Keep working toward a result | **Goal** | Compare the approved candidates and save a report with the supporting checks. |
| Check something repeatedly | **Loop** | Check a running simulation every five minutes and report when it ends. |
| Do something at a future time | **Schedule** | At 3pm, inspect the results and prepare a short summary. |

These controls belong to the conversation where you start them. They do not
create a separate conversation in the sidebar.

## Start a goal

Describe an outcome that can be inspected:

```text
Keep working until you have compared the approved candidates and saved a report
linking each result and its checks. Preserve the original measurements. Ask me
before changing the comparison criteria. Limit this attempt to five work turns.
```

A goal can continue after an ordinary response. CLIO checks progress against the
objective and can adjust its approach. Open **Work** to read the saved objective,
state and limits; confirm that they match what you intended.

A completion judgment is a check against the stated objective. It does not
independently validate a scientific interpretation. Open the saved results and
their sources before using them.

![A goal continues until the third checkpoint is saved, then appears under Previous as Completed](plugin-asset://guide/captures/docs-goal-completed.webp)
*The goal continues while a required checkpoint is missing. After the last file is saved, Work keeps the completed goal under Previous.*

## Wait for a simulation or other task

If a tool started a background task, CLIO can wait for its completion result.
Ask it to use that result before continuing the analysis. You can also continue
independent work while the task runs.

A task's completion notice means the operation ended. Read its returned result
to determine whether it succeeded. Waiting does not restart the operation;
[Follow work in progress](docs/following-work.md) explains where to find it.

![Wait for all tasks with no deadline, one collected result and the child agent's answer](plugin-asset://guide/captures/docs-wait-all-result.webp)
*The wait returns the task's answer for inspection. Read the result before deciding how the analysis should continue.*

## Monitor with a loop

For a service that needs periodic checking, specify the frequency, finishing
condition and a useful limit:

```text
Check the simulation job every five minutes, up to twelve checks. Notify me
when it finishes or fails, then stop monitoring. Ask before submitting any new job.
```

Inspect the loop in **Work**. Goals and monitoring are independent: finishing a
goal does not automatically stop a separate loop. Check both when the project ends.

![Two Loop wake entries followed by separate saved monitoring reports](plugin-asset://guide/captures/docs-loop-injections.webp)
*Each Loop wake carries the monitoring instruction into another check. The reports below the wake entries retain each check's output.*

## Schedule a future instruction

Give the instruction, date, time and time zone:

```text
At 3pm today in America/Chicago, inspect the latest simulation outputs and prepare
a short summary in this conversation. Do this once and preserve the source files.
```

Read the resolved next-run time in **Work**. If the conversation is already busy,
the scheduled work waits for that turn rather than starting a second turn at once.

![A scheduled instruction recorded in the conversation before the resulting work](plugin-asset://guide/captures/docs-schedule-injection.webp)
*The saved instruction appears as Scheduled wake when due. Open it to read the instruction, then inspect the resulting work.*

## Inspect and control Work

Open the **Work** view from the conversation's work summary. Expand **Details**
to inspect an objective, schedule and limits. **Previous** retains completed or
stopped controls.

![Work showing a completed goal, its details and an independent monitoring loop](plugin-asset://guide/captures/docs-work-goal.webp)
*Review what finished and what is still active. The goal and monitoring loop have separate controls.*

- **Stop** prevents future continuation or scheduled checks for that control.
- **Restart** starts a fresh run with the retained instruction and limits.
- **Cancel and delete** stops the control and removes its Work record; existing
  conversation messages and files remain.

Stopping a control does not undo files or automatically cancel an operation
already submitted to a tool. Use that task's cancellation control when needed.
The conversation's **Stop** cancels the active turn and pauses automatic continuation.

## Keep the service available and review its limits

Closing a browser tab can leave server-side work running. Quitting a local
service or shutting down its computer can interrupt it. The execution host,
model access and required services must remain available. A schedule cannot wake
a computer that is switched off.

Limits are optional and can be **Unlimited**. For a first attempt, choose a short
run or explicit number of checks and inspect the saved limits before leaving it.
Limits are checked at work boundaries; they do not stop a tool at an exact cost
threshold. If work pauses, inspect its state and pending questions before restarting.

For command flags, judge configuration, restart behavior and API arguments, use
[Background work: limits and API reference](docs/background-work-reference.md).
