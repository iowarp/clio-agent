---
id: clio.reminder.plugin_change
title: Plugin change, opening line
description: Opens the note that tells the agent about plugin changes its cached prompt does not show yet.
x_kind: reminder
x_active: true
x_trigger: every turn while a plugin change applied as a note is pending (until the next compaction rebuilds the prompt)
x_required: when a plugin change is applied as a note, this is the only way the agent learns of it until the next compaction; without it the agent keeps using removed plugins and never sees an added plugin's instructions, skills or tools
---
The person changed this conversation's plugins.
