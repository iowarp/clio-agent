---
title: "Session coordination"
description: "Durable session run handles, authorized peer observation and independent completion notifications."
source: https://clio.iowarp.ai/docs/tasks/session-coordination/
---

# Session coordination

Durable session run handles, authorized peer observation and independent completion notifications.

Consider two sessions: one acquires data while another evaluates the current
analysis. Shared task handles let authorized peers observe progress, wait for the
required outcome, exchange feedback and continue without a human copying messages
between them.

> **Development preview:** these guides describe the task updates under review. Availability depends on your build.

[Open the recorded workshop conversations](docs/tasks/recorded-conversations.md)
to follow two independent agents discovering, inspecting and messaging each other
from short user requests.

## One human owner, several cooperating sessions

The human owns each root assignment. Sessions retain stable addresses and
mailboxes; particular executions receive durable run/task handles before
acceptance returns. Delegation records its actual parent separately from a peer relationship.

| Identity | Role |
| --- | --- |
| Human owner | Owns root work and grants coordination or control rights. |
| Session | Retains the conversation, address and mailbox across executions. |
| Run/task handle | Identifies one assignment or execution, its status and outcome. |
| Workflow | Groups related work; scheduler and distributed dependency adapters require their own implementation. |

An idle session is not necessarily finished with its assignment. Keep activity
such as busy, idle or paused separate from work outcomes such as completed,
failed or cancelled. A wait needs a defined run or completion event; it must not
silently switch to a later run when another message arrives.

Yielding, receiving a task result and resuming after a pause continue the same
assignment. Explicit completion fixes its outcome. A later eligible message or
result can start a linked assignment in the same session, with a new handle.
The earlier handle continues to identify its original outcome.

## Reuse the shared controls

Root runs appear as `Session` tasks through the existing task system, allowing
authorized peers to query, observe and wait on them. Messages enter
the existing mailbox at the next iteration or an idle wake. Starting, pausing,
stopping and cancelling retain their distinct effects on accepted work.

An observer's subscription has its own delivery record. Reading or waiting
on another run must not consume that run's own parent notification. Permission to
message or observe a peer does not grant lifecycle control.
Human interventions continue to notify the actual owning agent.

In **Runs**, select **All states** to include earlier outcomes. The **Agents**
filter shows human-started assignments and delegated children together. Open
**Details** for an assignment or follow its name to the original conversation.

![Runs lists completed Session assignments and cancelled Subagent assignments, with their details collapsed and conversation links available](plugin-asset://guide/captures/tasks-session-runs-desktop.webp)
*Parent assignments and delegated children share the Runs list. Their outcomes remain separate: completing a parent does not rewrite a cancelled child's history.*

## Choose what another session can do

1. Open the receiving session's **Work** view and expand **Access to other sessions**.
2. Choose the target conversation. Select read, observation and messaging rights
   according to the work you want these sessions to share.
3. Select lifecycle control separately if that agent may pause, restart or cancel
   the target. Including delegated children is also a separate choice.
4. Choose **Grant access**. Use **Revoke** to remove that exact relationship later.

An observation-only grant exposes status and numeric progress, not assignments,
answers or output. Subscribing to the eventual result requires both read and
observation rights. Revocation is checked again before queued content is injected.

![Task access offers separate Read, Observe, Message and Control choices for a peer conversation](plugin-asset://guide/captures/tasks-session-grant-desktop-current.webp)
*Select the rights this conversation needs. In this real-service example, Read, Observe and Message are selected; Control and delegated children remain unselected.*

![The granted peer conversation has Read, Observe and Message rights beside a Revoke button](plugin-asset://guide/captures/tasks-session-access-current.webp)
*The saved relationship shows its exact rights. Revoke removes this access without changing either conversation's history.*

![The phone-width access view shows the peer grant and its Revoke button](plugin-asset://guide/captures/tasks-session-revoke-phone-current.webp)
*At phone width, the same grant remains accessible. Pressing Enter on Revoke removed the rights in Core while the peer's pending Question stayed open.*

<details>
<summary>Find a run and subscribe to its result</summary>

These are agent tool calls. Broader reads require a coordination grant created
by an authenticated human or trusted configuration. A model's description of
user intent creates no permissions. Authorization is applied before filtering
and pagination; knowing a handle does not bypass it.

```python
query_tasks("Session", scope="authorized")
observe_tasks(run_handle, notify=True)
wait_tasks(run_handle, timeout_s=0)
```

Use a returned run handle for a particular outcome. Use the stable session
address for messages across executions. A subscribed completion enters the next
model iteration when busy or wakes the observer when idle. It does not consume
the owner's notification. A later message can start a new run with a new handle.

</details>

## Read the work behind a task

An authorized agent can inspect another session's original transcript, including
its tool observations, before deciding what to ask or reuse. The same reads work
for a parent's delegated children.

```python
query_tasks(kind="Session", scope="authorized", query="archive storage")
observe_tasks(tasks=run_handle, view="transcript")
observe_tasks(tasks=run_handle, view="summary")
observe_tasks(tasks=run_handle, view="artifacts")
```

Search matches the supplied words in readable assignments and retained text.
Follow query cursors, including after a short page. Transcript pages keep source
message and part IDs; large rows return resumable JSON fragments. Follow the
returned cursor while `has_more` is true.

A summary view contains recorded compaction summaries or the assignment's stored
outcome, with coverage and source references. It explicitly reports missing
summaries. It does not generate a new whole-session summary. An artifact view
lists linked versions; pass a listed `artifact_id` to the same view to read its
bounded content and recorded hash through the normal artifact permissions.

The default `history="assignment"` stays on that finite run. Reading predecessor
context with `history="session"` requires stable-session read permission. An
exact historical grant does not expand to later assignments. These views do not
consume result notifications, and copied text retains its original attribution.

Recorded Codex/Luna and Claude Opus runs exercised transcript inspection followed
by messaging the original cancelled-child handle. The linked continuation used
retained context without repeating the original operation. These are scoped
demonstrations. The current candidate also has scoped summary/artifact evidence;
the workshop conversations above show a model reading a peer's recorded summary,
listing its artifact and reading the saved note before sharing it. The screenshots
of grants alone do not qualify every transcript view.

## A possible workflow

This is a design example, not a recorded demonstration:

1. An acquisition session accepts five scheduler jobs and returns handles tied
   to the actual scheduled work.
2. An analysis session observes those handles and works on the existing data.
3. When the jobs settle, its subscription wakes it with the available outcomes.
4. It collects the results, messages the acquisition session with revised inputs,
   and starts the next iteration.

The scheduler adapter must distinguish job submission from job completion. The
workflow also needs explicit outcomes for partial failure, interruption and
cancellation; a finished submission command cannot stand in for them.

## What the current demonstration proves

Actual Codex/Luna agents in two distinct declared workflows used the replacement
service on Windows through the direct SSE bridge. Separate root and child runs discovered
authorized peers, exchanged attributed messages, observed and subscribed to work,
expired a wait while the work continued, and were denied cancellation without
control rights. A queued reply remained private after revocation and arrived once
access was restored. Child-owner and subscriber completion records were independent;
a completed human-owned root did not wake itself.

The root run also demonstrated observation-only redaction, a denied result read,
desktop granting and phone revocation through the real UI. One model initially
declined messaging and needed one feedback clarification before using the exposed
control.

A later recorded Codex/Luna run completed the busy-message and historical-handle
cases between two independent declared workflows. A peer request arrived while
the recipient waited on real Shell work, and its reply continued the other
session without human steering. Revocation withheld pending messages and results;
the agent's result read was denied. Restored access admitted each retained notice
once, while the owner's delivery remained independent. A linked successor could
read the completed original handles without rerunning either workflow.

These scoped demonstrations do not accept every inference route, lifecycle or
recovery race. They do not demonstrate a distributed scheduler or Slurm integration.

Read [Messages and feedback](docs/tasks/messages.md) for the implemented addressing
and mailbox behavior, and [Background work](docs/tasks/index.md) for accepted task execution.

On the current candidate, a separate real-service check granted Read, Observe and
Message at desktop width, then revoked the relationship with the keyboard at
390-pixel phone width. Core confirmed the rights before and after; Control was
never granted, the peer's Question stayed pending, and the page had no overflow
or browser errors. This interface check complements the separate actual-model
workflow and permission-boundary evidence.
