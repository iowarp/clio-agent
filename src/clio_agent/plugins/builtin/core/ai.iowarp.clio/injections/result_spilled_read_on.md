---
id: clio.reminder.result_spilled_read_on
title: Where the rest of a cut result is (text)
description: The `where` part of the result_spilled note when the full result was saved as a text file, with the exact read that continues it.
x_kind: reminder
x_active: true
x_trigger: source result_spilled; when a cut result was saved to a file that is not JSON
x_variables: path, offset
x_required: it is the only place the agent learns where the rest of the result is
---
the full result is in `{{path}}`; read on with fs_read_file(path="{{path}}", offset={{offset}}), or search it with fs_search_text
