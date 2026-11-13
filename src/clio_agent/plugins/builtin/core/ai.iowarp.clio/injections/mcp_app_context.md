---
id: clio.reminder.mcp_app_context
title: Tell the agent what an open app shows
description: One block per live app that reported context or offers its own tools - the server, the tool that opened the view, the context the view last reported, and the view's tools. The person sees the same view, so this is what "what am I looking at?" refers to.
x_kind: reminder
x_active: true
x_trigger: at the start of a turn and after a compaction, once per live app whose reported context or tools changed; and with every message a view sends itself
x_variables: server, tool, context, tools, shown
x_required: the person is looking at the view; without this the agent cannot see what it shows
---
The person has the {{server}} view open (opened by {{tool}}).
{%- if context %}
What the view last reported:
{{context}}
{%- endif %}
{%- if shown %}
It is on screen: look_at_view shows you what it shows now (load it with search_tools).
{%- endif %}
{%- if tools %}
The view offers its own tools, which act on what it shows. Load them with search_tools: {{tools}}
{%- endif %}
