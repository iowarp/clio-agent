---
id: clio.reminder.summon_plugin
title: The person asked for a plugin
description: Says the person @-named a plugin or connector they have that this conversation does not carry, and that offer_plugin adds it. The person's Add on the card still decides; the agent keeps every tool.
x_kind: reminder
x_active: true
x_trigger: at the start of the turn of a message that @-names a plugin or connector not in the conversation
x_variables: name, description
x_required: an @-named plugin is the person's request; without this line the agent does not know they asked for it
---
The person asked for {{name}} on this message{% if description %} ({{description}}){% endif %}. It is not in this conversation yet: call offer_plugin with plugin="{{name}}", required true and a short reason, and once they add it use it for this message.
