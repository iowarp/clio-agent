---
id: clio.reminder.task_result_truncated
title: Say where a cut notification is kept
description: Ends a notification cut to the step's notification budget (limits.model_tool_result_chars) with where its full content is kept; a new line precedes it.
x_kind: reminder
x_active: true
x_trigger: source task_result (or another non-atomic mail kind): the first notification of a step is longer than the budget and is cut
x_variables: scope, task
x_required: the agent is shown only part of the notification; it must know where the full content is kept
---
[Full content retained at {{scope}}; task {{task}}.]
