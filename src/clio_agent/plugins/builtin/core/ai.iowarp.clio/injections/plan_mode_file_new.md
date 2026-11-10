---
id: clio.reminder.plan_mode_file_new
title: Plan mode, no plan yet
description: Says where a new plan is saved. First bullet of the plan_mode contract.
x_kind: reminder
x_active: true
x_trigger: plan_mode, in the full contract, when the plan file does not exist yet
x_variables: path
---
No plan file exists yet. Write your plan with plan_update(content=...); it is saved at {{path}}, the only writable path in plan mode.
