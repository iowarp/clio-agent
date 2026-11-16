---
title: "Background work"
description: "Keep working while tools and subagents run, follow their progress, and receive their results automatically."
source: https://clio.iowarp.ai/docs/tasks/
---

# Background work

Keep working while tools and subagents run, follow their progress, and receive their results automatically.

Ask CLIO to start a long operation and continue with something else. When the
operation accepts background execution, the agent receives a **task handle**
immediately. That handle identifies the work while it runs and after it finishes.

> **Development preview:** these guides describe the task updates under review. Availability depends on your build.

[Watch two agents work together](docs/tasks/recorded-conversations.md) in complete
portable conversations: inspect a peer's work, exchange reviews, run a background
calculation and answer a Question after the agent becomes idle.

## Start work, then keep going

```text
Start the data download in the background. While it runs, inspect the existing
analysis script and list what needs to change. Tell me when the download finishes.
```

This is an example request, not a recorded transcript. The tool must support
background tasks; an ordinary tool call does not become asynchronous merely
because the prompt says “in the background.” See [Tools and sources](docs/tasks/tools-and-sources.md).

1. The tool accepts the operation and returns its handle and current status.
2. The agent can call another tool, inspect progress, or wait for a result.
3. When the task settles, CLIO delivers its result automatically. A busy agent
   receives it before its next model iteration. An idle agent wakes to process it.

An **iteration** is the next model call within ongoing work. Feedback and task
results can enter there; they do not have to wait until the entire response to
your original request has finished. A long foreground tool call still needs to
return before that next iteration can begin.

## Open Work to see what is running

Open **Work** in the session's information surface. **Background work** lists
tasks with their name, kind, status and available progress. Open a row to read
its assignment and result. A subagent's row also opens its conversation.

![Work shows the running parent Session, its paused child and an open Question, with the child's assignment expanded](plugin-asset://guide/captures/tasks-workflow-work.webp)
*Current replacement service, actual Codex/Luna: a workflow child is paused while its Question remains open. The original assignment appears only when its row is expanded. This is a qualification assignment.*

Assignments and results stay behind the row disclosure. **Technical details**
contains the handle, host and backend state when you need to diagnose an issue.
The observability view and Runs controls expose the same underlying work.

Choose **Show completed work** to inspect earlier outcomes or restart a completed
subagent. **Load more completed work** reads another history page. Current work
stays visible while you browse history; **Hide completed work** closes that
disclosure without dismissing or cancelling any task.

## Stop, pause and cancel

| Action | Effect |
| --- | --- |
| **Stop** | Stops the current response and its waiter. Background tasks continue and may wake the agent later. A foreground Shell is cancelled and cleaned up. |
| **Pause** | Holds the selected agent's model execution and context until resume. Background work continues; messages and results accumulate. |
| **Cancel task** | Requests cancellation. A Subagent cancellation also cancels its descendants. Settlement waits for the required cleanup. |

Stopping a response does not cancel a download. Pausing a child does not kill its
running command. Use the task's cancellation control when you want to end the work.

## Completion, failure and questions

Completed, failed, cancelled and interrupted outcomes remain inspectable. A
failed task can notify the agent through the same delivery path as a successful
one. The stored outcome is not a promise that the work succeeded.

A task may need an answer or permission while it runs. Answer the attributed
question in CLIO. Pausing one agent or waiting for one task does not stop every
other task in the workspace.

- [Observe and wait](docs/tasks/observe-and-wait.md): Read progress, collect mixed results, and let a wait expire without cancelling work.
- [Control subagents](docs/tasks/subagents.md): Pause, resume, cancel or restart a child without confusing those actions.
- [Send messages](docs/tasks/messages.md): Steer running work and coordinate with an authorized peer session.
- [Tools and sources](docs/tasks/tools-and-sources.md): Understand which calls return tasks and what survives reconnection.
