---
id: clio.reminder.hook_tool_answered
title: Hook answered a tool call
description: Tells the agent that a PreToolUse hook answered its call and the tool did not run.
x_kind: reminder
x_active: true
x_trigger: source hook: a PreToolUse hook synthesized a tool call's result; added before that result
x_required: the operator configured this hook; the agent must know that a hook, not the tool, shaped what it sees
---
A PreToolUse hook answered this call without running the tool.
