---
id: clio.reminder.plugin_change_deferred_tools
title: Plugin change, tools a deferred server added
description: Names the tools a deferred MCP server added mid-session, which the agent loads with search_tools.
x_kind: reminder
x_active: true
x_trigger: plugin_change, every turn after a deferred MCP server added tools (tools/list_changed) until the prompt is rebuilt; on its own when no plugin change is pending
x_variables: tools
---
Tools added since your prompt was built (load them with search_tools): {{tools}}.
