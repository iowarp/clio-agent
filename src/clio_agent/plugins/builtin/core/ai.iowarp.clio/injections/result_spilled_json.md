---
id: clio.reminder.result_spilled_json
title: Where the rest of a cut result is (JSON)
description: The `where` part of the result_spilled note when the full result was saved as one JSON document.
x_kind: reminder
x_active: true
x_trigger: source result_spilled; when a cut result was saved to a .json file
x_variables: path
x_required: it is the only place the agent learns where the rest of the result is
---
the full result is in `{{path}}`; it is one JSON document, so search it with fs_search_text or load it in a script
