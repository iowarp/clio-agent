---
id: clio.reminder.session_facts_resources
title: Session facts, uploaded resources
description: Names the files uploaded to this workspace, newest first, so the agent knows them without a listing call. Part of session_facts; only when the workspace has a resource.
x_kind: reminder
x_active: true
x_trigger: session_facts, when the workspace has an uploaded resource
x_variables: resources, total
---
Uploaded resources in this workspace ({{ total }}{{ ", newest " ~ resources|length ~ " shown" if total > resources|length else "" }}):
{% for r in resources %}- {{ r.name }} ({{ r.mime }}, {{ r.size }} bytes, {{ r.state }}; id {{ r.id }})
{% endfor %}Read them with the workspace_resource tools (search_tools loads them).
