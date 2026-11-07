---
id: clio.reminder.replan
title: Suggest replanning after a stall
description: While an approved playbook plan is being carried out and recent turns show little progress, suggest that the agent consider replanning; the agent decides.
x_kind: reminder
x_active: true
x_trigger: at the next turn start after the stall monitor records little progress while an approved operator playbook is being carried out (no playbook step or checklist advanced, or the same tool call repeated across turns); once per suggestion, never in plan mode
---
## Replanning suggestion: little progress in recent turns

While carrying out the approved playbook, your recent turns have shown little measurable progress: no playbook step or checklist item advanced, or the same tool call repeated across turns. Consider whether the current approach is working.
- If it is not, revise the approach in the plan with plan_update and carry on, or call plan_enter when the person should approve the new approach. The choice is yours.
- If you are making progress, carry on; this note changes nothing by itself.
