---
id: clio.reminder.plugin_change_added
title: Plugin change, added plugin
description: Names one added plugin; its instructions follow it in the note.
x_kind: reminder
x_active: true
x_trigger: plugin_change, once per plugin the pending change added, ahead of that plugin's instructions
x_variables: plugin
x_required: when a plugin change is applied as a note, this is the only way the agent learns of it until the next compaction; without it the agent keeps using removed plugins and never sees an added plugin's instructions, skills or tools
---
Added: {{plugin}}.
