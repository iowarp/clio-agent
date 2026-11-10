---
id: clio.reminder.hook_arguments_changed
title: Hook changed a tool call's arguments
description: Tells the agent that a PreToolUse hook ran its call with other arguments.
x_kind: reminder
x_active: true
x_trigger: source hook: a PreToolUse hook modified a tool call's arguments; added to the call's result
x_variables: arguments
x_required: the operator configured this hook; the agent must know that a hook, not the tool, shaped what it sees
---
A PreToolUse hook changed this call's arguments to {{arguments}}.
