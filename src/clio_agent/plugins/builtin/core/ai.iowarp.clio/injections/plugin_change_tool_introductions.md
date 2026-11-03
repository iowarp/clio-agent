---
id: clio.reminder.plugin_change_tool_introductions
title: Plugin change, new tools introduced
description: Introduces the tools an added plugin brought, which run if called but are not in the request's tool list (one line per tool, with its arguments and description, follows).
x_kind: reminder
x_active: true
x_trigger: plugin_change, when the pending change added tools the request's tool list leaves out
x_variables: tools
x_required: when a plugin change is applied as a note, this is the only way the agent learns of it until the next compaction; without it the agent keeps using removed plugins and never sees an added plugin's instructions, skills or tools
---
New tools you can call now, though they are not in your tool list until the next compaction:
{{tools}}
