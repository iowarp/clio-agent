---
id: clio.reminder.plan
title: The plan as working state
description: Shows the session plan while it is a draft or approved, so the agent can carry on from it. Switching this off also stops the person's plan edits from reaching the agent (they are offered under this source).
x_kind: reminder
x_active: true
x_trigger: at turn start when the plan changed and after a compaction, while the plan is draft or approved and the session is not in plan mode
x_variables: status, path, plan_text
---
## The plan ({{status}}, {{path}})

If this plan is relevant to the current work and not finished, continue on it, and update it with plan_update when your understanding or direction changes.

{{plan_text}}
