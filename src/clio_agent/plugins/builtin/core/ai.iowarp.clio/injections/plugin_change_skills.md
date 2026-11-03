---
id: clio.reminder.plugin_change_skills
title: Plugin change, skills to load
description: Heads the list of skills the change made loadable now (one line per skill follows).
x_kind: reminder
x_active: true
x_trigger: plugin_change, when the pending change added skills
x_required: when a plugin change is applied as a note, this is the only way the agent learns of it until the next compaction; without it the agent keeps using removed plugins and never sees an added plugin's instructions, skills or tools
---
Skills you can load now with load_skill:
