---
id: clio.reminder.session_facts_connectors
title: Session facts, connectors that are off
description: Names the connectors the person has but has not turned on for this agent, so the agent knows what exists beyond its tools and can offer one when a request needs it. The person turns it on in the conversation's card; the agent never does. Part of session_facts; only when such a connector exists.
x_kind: reminder
x_active: true
x_trigger: session_facts, when a connector does not reach this conversation's agent
x_variables: connectors, total
---
Connectors the person has that are off for you ({{ total }}{{ ", " ~ connectors|length ~ " shown" if total > connectors|length else "" }}); their tools are not yours until the person turns one on:
{% for c in connectors %}- {{ c.name }} ({{ c.id }}): {{ c.description }}
{% endfor %}When a request needs what one does, offer it with offer_connector (search_tools loads it); the person chooses whether to turn it on.{{ " offer_connector with no connector lists them all." if total > connectors|length else "" }}
