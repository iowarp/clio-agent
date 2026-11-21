---
id: clio.reminder.organization_branches
title: Suggest branches per organization
description: When one turn reaches NDP data from several organizations, remind the agent that each organization can be a branch run by a child.
x_kind: reminder
x_active: true
x_trigger: when NDP searches filtered by organization and NDP datasets connected in one turn reach `after` distinct organizations, and the agent can start children; at most once per turn
x_variables: count, organizations, spawn_tool
x_param_after: 3
---
This turn has reached NDP data from {{count}} organizations ({{organizations}}). Each organization's searching, reading and checks are independent of the others: if what remains for them would go faster side by side, {{spawn_tool}} can give each organization to a child (an `explore` child for searching and reading, a copy of yourself to connect or fetch) while you plan how to merge their results. If little remains for each, carry on yourself.
