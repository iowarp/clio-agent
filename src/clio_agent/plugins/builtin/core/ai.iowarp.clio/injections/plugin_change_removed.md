---
id: clio.reminder.plugin_change_removed
title: Plugin change, removed plugins
description: Names the removed plugins, which stay in the cached prompt, and asks the agent not to use them.
x_kind: reminder
x_active: true
x_trigger: plugin_change, when the pending change removed plugins
x_variables: plugins
x_required: when a plugin change is applied as a note, this is the only way the agent learns of it until the next compaction; without it the agent keeps using removed plugins and never sees an added plugin's instructions, skills or tools
---
Removed: {{plugins}}. Do not use their tools or follow their instructions, even though they still appear above.
