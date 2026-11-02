---
id: clio.reminder.result_spilled_unsaved
title: The rest of a cut result could not be saved
description: The `where` part of the result_spilled note when writing the full result to a file failed.
x_kind: reminder
x_active: true
x_trigger: source result_spilled; when a cut result could not be written to the session's tool-output folder
x_variables: error
x_required: without it the agent would look for a file that does not exist
---
the full result could not be saved ({{error}}), so the rest is not available
