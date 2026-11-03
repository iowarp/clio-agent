---
id: clio.reminder.session_facts_workspace
title: Session facts, where to write
description: Tells the agent to keep what it generates inside the workspace. Part of session_facts.
x_kind: reminder
x_active: true
x_trigger: session_facts, after the workspace root, when the session is not a node of an isolated workflow run
---
Write generated artifacts (files, charts, exports) inside the active workspace root using absolute paths so they stay with the session. Read inputs from wherever the user points you.
