---
id: clio.reminder.plan_stale
title: Mention a plan that has not been updated
description: When an active plan has gone untouched for a while, say so in one line; the agent decides whether it needs updating.
x_kind: reminder
x_active: true
x_trigger: after `steps` recorded steps without plan_update, write_todos, plan_enter or plan_exit while the session has an active plan
x_variables: path, steps
x_param_steps: 25
---
The plan ({{path}}) has not been updated in {{steps}} steps. If your understanding or direction has changed, update it with plan_update.
