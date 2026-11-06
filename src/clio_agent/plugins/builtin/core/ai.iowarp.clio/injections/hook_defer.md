---
id: clio.reminder.hook_defer
title: Continue after a Stop hook
description: What the agent is told when a Stop hook asked the person whether the turn may end, the person chose to continue, and the hook gave no reason.
x_kind: reminder
x_active: true
x_trigger: a Stop hook deferred the end of a turn without a reason or context, and the person denied ending it; the agent is re-driven with this text
x_required: a person denied ending the turn; without this text the agent would be re-driven with an empty message
---
The person chose not to end the turn here (a Stop hook asked them first); continue working.
