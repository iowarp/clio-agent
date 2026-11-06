---
id: clio.reminder.summon_workflow
title: The person asked for a workflow
description: Says the person @-named one of the agent's declared workflows for this message, and which call runs it with which inputs. The agent fills the inputs from the conversation and keeps every tool.
x_kind: reminder
x_active: true
x_trigger: at the start of the turn of a message that @-names a declared workflow
x_variables: name, description, inputs
x_required: an @-named workflow is the person's request; without this line the agent does not know they asked for it
---
The person asked for the {{name}} workflow on this message{% if description %} ({{description}}){% endif %}. Call run_workflow with workflow="{{name}}"{% if inputs %} and inputs for {{inputs}}, filled from this message and the conversation{% endif %}, setting any other parameters the work needs, and use its result.
