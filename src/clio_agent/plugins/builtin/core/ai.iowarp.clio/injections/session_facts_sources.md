---
id: clio.reminder.session_facts_sources
title: Session facts, connected sources
description: Names the data sources connected to this workspace, so the agent knows them without a status call. Part of session_facts; only when the workspace has a source.
x_kind: reminder
x_active: true
x_trigger: session_facts, when the workspace has a connected data source
x_variables: sources
---
Connected data sources in this workspace:
{% for s in sources %}- {{ s.label }} ({{ s.provider }}, {{ "connected" if s.connected else "not connected" }}, {{ s.access }}{{ ", at " ~ s.path if s.path else "" }}; id {{ s.id }})
{% endfor %}Open or download them with the connected_data tools (search_tools loads them).
