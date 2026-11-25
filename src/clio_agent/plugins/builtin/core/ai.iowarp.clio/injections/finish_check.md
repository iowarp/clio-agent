---
id: clio.reminder.finish_check
title: Carry the request through, mid-turn
description: Once per turn, after a run of calls in a root session on a Claude provider, the turn-start reminder to finish the work is said again close to where the answer is written.
x_kind: reminder
x_active: true
x_trigger: after `after` calls in one turn of a root session whose provider is one of `providers`; at most once per turn
x_variables: calls
x_param_after: 10
x_param_providers: claude_code,anthropic
x_required: in live runs the turn-start reminder faded over a long turn and the answer again listed years not rerun and plots not inspected
---
You have made {{calls}} calls in this turn. Before you answer, look at what you would report as not checked, not rerun, not inspected or unresolved. If you can do it here (a narrower window, a mask for an outlier, another source, a look at a plot, or a plugin the person has that does what your own tools cannot, through offer_plugin), do it now rather than listing it. Ask with ask_user only what the person alone can decide; keep only the limits that cannot be lifted here.
