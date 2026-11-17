---
id: clio.reminder.result_spilled
title: Say that a long tool result was cut
description: Heads a tool result that was saved to a file instead of shown, before its sample; `outline` is the structure of a JSON result (else empty) and `where` is one of the result_spilled_read_on, result_spilled_json or result_spilled_unsaved pieces.
x_kind: reminder
x_active: true
x_trigger: source result_spilled; on every tool result that does not fit the room one result has in the context window (tools.result_share), or tools.recall_result_chars for recall_context, after the fixed marker [clio: result_spilled]
x_variables: tokens, chars, shown, where, outline
x_required: the agent is shown only the head of the result; without this it would take the head for the whole result and not know where the rest is
---
This result is about {{tokens}} tokens ({{chars}} characters), too large to put in your context: {{where}}.
{%- if outline %}
Its structure, to write that query:
{{outline}}
{%- endif %}
Below is a sample, its first {{shown}} characters, to show what the data looks like; it is not the whole result.
