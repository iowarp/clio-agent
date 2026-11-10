---
id: clio.reminder.summon_subagent
title: The person asked for a subagent
description: Says the person @-named one of the agent's subagents for this message, and which call starts it. The agent writes the brief from the conversation and keeps every tool.
x_kind: reminder
x_active: true
x_trigger: at the start of the turn of a message that @-names a subagent
x_variables: name, description
x_required: an @-named subagent is the person's request; without this line the agent does not know they asked for it
---
The person asked for {{name}} to work on this message{% if description %} ({{description}}){% endif %}. Do whatever groundwork the brief needs, then call spawn_agent_task with agent="{{name}}" and a self-contained task written from this conversation, setting any other parameters the work needs, and use its report.
