---
id: clio.reminder.session_facts_connectors
title: Session facts, connectors not in this conversation
description: Names the connectors the person has that this conversation does not have (turned on or off), so the agent knows what exists beyond its tools and can offer one when a request needs it. The person adds it to the conversation in its card; the agent never does. Part of session_facts; only when such a connector exists.
x_kind: reminder
x_active: true
x_trigger: session_facts, when a connector is not in this conversation
x_variables: connectors, total
---
Connectors the person has that are not in this conversation ({{ total }}{{ ", " ~ connectors|length ~ " shown" if total > connectors|length else "" }}); their tools are not yours until the person adds one here:
{% for c in connectors %}- {{ c.name }} ({{ c.id }}, {{ "on" if c.enabled else "off" }}): {{ c.description }}
{% endfor %}When a request needs what one does, offer it with offer_connector (search_tools loads it); the person chooses whether to add it to this conversation.{{ " offer_connector with no connector lists them all." if total > connectors|length else "" }}
