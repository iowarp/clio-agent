---
id: clio.reminder.explore_nudge
title: Suggest explore while searching
description: After a run of reading calls, remind the agent that an explore child can take the rest of the search.
x_kind: reminder
x_active: true
x_trigger: after `after` read-only calls in one turn with no delegation, when the agent can start an explore child; at most once per turn
x_variables: calls, explore_model, spawn_tool
x_param_after: 6
---
You have made {{calls}} reading calls in this turn without delegating. If the rest of this search can go to an `explore` child (it runs on {{explore_model}} and returns only its findings, with paths), {{spawn_tool}} may be faster and keep your context smaller. If you are close to the answer, carry on.
