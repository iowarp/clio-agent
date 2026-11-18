---
id: clio.reminder.view_nudge
title: Suggest a view while working
description: After a run of calls in which nothing was shown or looked at, remind the agent that results with a shape belong in a view it then checks.
x_kind: reminder
x_active: true
x_trigger: after `after` calls in one turn of a root session with no view created, changed or captured, when the agent can create and capture views; at most once per turn
x_variables: calls
x_param_after: 5
---
You have made {{calls}} calls in this turn and shown the person nothing yet. If something you have computed or fetched has a shape a person checks by eye (a series, profiles, a section, positions or a field on a map, a distribution), put it in a view now with create_a2ui_surface from the files you wrote, look at it with capture_a2ui_surface, and let what you see check your numbers before you answer. If nothing has that shape yet, carry on.
