---
id: clio.reminder.replan
title: Suggest replanning after a stall
description: When recent turns show little progress, suggest that the agent consider replanning; the agent decides.
x_kind: reminder
x_active: true
x_trigger: at the next turn start after the stall monitor records little progress (no plan or checklist advancement, or the same tool call repeated across turns); once per suggestion, never in plan mode
---
## Replanning suggestion — sustained lack of progress detected

Your recent turns have shown little measurable progress — no plan-step or checklist advancement, and/or the same tool call repeated across turns. Consider whether the current approach is working.
- If it is NOT, you MAY re-enter plan mode to replan (invoke the `planning` skill) and revise your approach. This is your decision — nothing is forced.
- If you ARE making progress, disregard this notice and continue.
