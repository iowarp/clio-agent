---
id: clio.reminder.context_recovery_uncertain
title: Mark a tool call whose outcome was lost
description: The error result given to a tool call that had no recorded result when the context was recovered.
x_kind: reminder
x_active: true
x_trigger: source context_recovery; once per tool call left without a result when a session's context is restored
x_required: it stands in for the call's missing result; without it the call would have no result at all and the agent could not tell that its outcome is unknown
---
Operation outcome is uncertain after context loss. No operation was replayed; inspect retained handles and effects before retrying.
