---
id: clio.reminder.session_facts_node_folder
title: Session facts, node folder
description: For one node of an isolated workflow run, names its folder and says writes are confined to it. Part of session_facts.
x_kind: reminder
x_active: true
x_trigger: session_facts, after the workspace root, in place of the notes on where to write, when the session is a node of an isolated workflow run
x_variables: folder
---
Your node folder: {{folder}}
This session is one node of an isolated workflow run. Your shell starts in your node folder, and relative output paths land there. You may write only inside it; you may read the whole workspace.
