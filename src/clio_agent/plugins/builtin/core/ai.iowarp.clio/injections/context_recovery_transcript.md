---
id: clio.reminder.context_recovery_transcript
title: Say the context was recovered from the saved transcript
description: Tells the agent its context was rebuilt from the saved transcript, without the provider's continuation, and that nothing was replayed.
x_kind: reminder
x_active: true
x_trigger: source context_recovery; at the start of a forward whose context had to be restored from the saved transcript because no recorded context operations were found
---
Recovered this session's context from saved transcript; provider continuation is unavailable. No operation was replayed.
