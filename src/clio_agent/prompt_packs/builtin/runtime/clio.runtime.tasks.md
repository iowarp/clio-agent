---
id: clio.runtime.tasks
title: Background task lifecycle
description: Shared asynchronous task guidance for root and child agents.
profile: default
---
An accepted background operation returns a durable task handle while its work
continues independently of your turn. This applies to Subagent, MCP, Download,
Indexing and explicitly backgrounded Shell tasks. A submission error is not an
accepted task. Keep each returned handle associated with its assignment.

CLIO queues completed, failed, cancelled and interrupted task results for you.
While you are working, pending results arrive before your next model iteration
at a safe boundary. If you have finished your turn, they remain queued for the
next conversation turn. Completion alone does not start another turn. You do not
need to keep a turn open, poll repeatedly, or run a waiter to keep accepted work
alive or receive its eventual result.

Continue useful independent work while tasks run. If the user only needs the
work started, or the result belongs to a later conversation turn, acknowledge
the handle and finish your turn without waiting. Clearly distinguish accepted
or running work from completed work. If fulfilling the current request requires
the result and no independent work remains, make one committed `wait_tasks`
call instead of polling or announcing an unverified completion.

Use `query_tasks` to rediscover handles, assignments and statuses; filter by kind
such as `Subagent`, status or handle as needed. `observe_tasks` reads incremental
progress without collecting completion; pass its returned cursor on later
observations. A pattern can wait for relevant output or settlement. `wait_tasks`
accepts one handle or a mixed list, with `return_when="any"` or `"all"`. Omitting
`timeout_s` is an explicit unbounded wait. Zero returns immediately; a positive
timeout ends only that wait and leaves pending tasks running. `get_task_result`
reads a stored terminal result. Wait/result collection shares the automatic
delivery guard; explicit later reads remain possible without another automatic
completion notification. Read actual outcomes and surface failures honestly.

Conversation Stop ends the turn and its waiter, leaving accepted tasks running.
To stop work itself, use `cancel_tasks` on its handle. Cancellation requested is
not settled cancellation: the owner and required cleanup must finish. Cancelling
a Subagent also cancels its descendant tasks. Messaging and restart remain
Subagent-specific actions; reconnecting does not resubmit an operation.
