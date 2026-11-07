---
id: clio.reminder.result_spilled
title: Say that a long tool result was cut
description: Heads a tool result that is longer than the model is shown, before its first characters; `where` is one of the result_spilled_read_on, result_spilled_json or result_spilled_unsaved pieces.
x_kind: reminder
x_active: true
x_trigger: source result_spilled; on every tool result that does not fit the room one result has in the context window (tools.result_share), or tools.recall_result_chars for recall_context, after the fixed marker [clio: result_spilled]
x_variables: chars, shown, where
x_required: the agent is shown only the head of the result; without this it would take the head for the whole result and not know where the rest is
---
This result is {{chars}} characters, more than the {{shown}} shown to you. The first characters follow; {{where}}.
