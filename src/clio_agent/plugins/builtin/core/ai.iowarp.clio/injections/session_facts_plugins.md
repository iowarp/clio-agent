---
id: clio.reminder.session_facts_plugins
title: Session facts, plugins not in this conversation
description: Names the plugins and connectors the person has that this conversation does not have, each with a one-line summary, so the agent knows what exists beyond its tools and skills and can offer one when a request needs it. The person adds it to the conversation in its card; the agent never does. Part of session_facts; only when there is one.
x_kind: reminder
x_active: true
x_trigger: session_facts, when a plugin or connector is not in this conversation
x_variables: plugins, total
---
Plugins and connectors the person has that are not in this conversation ({{ total }}{{ ", " ~ plugins|length ~ " shown" if total > plugins|length else "" }}). What they bring (tools, skills, a connection to a service or program) is not yours until the person adds one here; offer_plugin asks them, and on Add it is yours from your next step in this same turn. A plugin's [marketplace] is where the person added it from:
{% for p in plugins %}- {{ p.name }}{{ " (" ~ p.id ~ ", connector, " ~ ("on" if p.enabled else "off") ~ ")" if p.kind == "connector" else ("" if p.name == p.id else " (" ~ p.id ~ ")") ~ " [" ~ p.marketplace ~ "]" }}: {{ p.summary }}
{% endfor %}{{ "offer_plugin with no plugin lists them all." if total > plugins|length else "" }}
