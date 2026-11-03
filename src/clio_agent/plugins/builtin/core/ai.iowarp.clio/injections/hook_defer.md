---
id: clio.reminder.hook_defer
title: Continue after a Stop hook
description: What the agent is told when a person denies ending the turn and the Stop hook gave no reason.
x_kind: reminder
x_active: true
x_trigger: a Stop hook deferred the end of a turn without a reason or context, and the person denied ending it; the agent is re-driven with this text
x_required: a person denied ending the turn; without this text the agent would be re-driven with an empty message
---
A Stop hook reported the task is not complete; continue working.
