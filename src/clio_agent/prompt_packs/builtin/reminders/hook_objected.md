---
id: clio.reminder.hook_objected
title: Hook objected to a tool call
description: Gives the agent a PostToolUse hook's objection to a call that already ran.
x_kind: reminder
x_active: true
x_trigger: source hook: a PostToolUse hook denied a call after it ran; added after the result with the hook's reason
x_variables: reason
x_required: the operator configured this hook; the agent must know that a hook, not the tool, shaped what it sees
---
A PostToolUse hook objected: {{reason}}
