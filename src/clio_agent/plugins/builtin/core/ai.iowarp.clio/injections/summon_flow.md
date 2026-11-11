---
id: clio.reminder.summon_flow
title: The person asked for a flow
description: Says the person @-named one of the agent's flows for this message, and which call starts it. The agent writes the flow's input from the conversation and keeps every tool.
x_kind: reminder
x_active: true
x_trigger: at the start of the turn of a message that @-names a flow
x_variables: name, description
x_required: an @-named flow is the person's request; without this line the agent does not know they asked for it
---
The person asked for the {{name}} flow on this message{% if description %} ({{description}}){% endif %}. Call run_flow with flow="{{name}}" and an input written from this message and the conversation, setting any options and model the work needs, and use its result.
