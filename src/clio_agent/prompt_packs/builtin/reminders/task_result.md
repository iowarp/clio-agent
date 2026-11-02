---
id: clio.reminder.task_result
title: Head a notification
description: The line that heads every notification delivered to an agent: who sent it, its kind, the task and its status, and the notification id. The content follows on the next line.
x_kind: reminder
x_active: true
x_trigger: source task_result (and every other mail kind: attention, question_answer, control, peer, a person's message): each notification an agent receives at a step
x_variables: sender, kind, task, notification
x_required: it tells the agent who sent the notification, which task it is about and its status, and the id to cite; without it the content arrives unattributed
---
[{{sender}}; {{kind}}{{task}}; notification {{notification}}]
