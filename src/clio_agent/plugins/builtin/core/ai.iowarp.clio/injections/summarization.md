---
id: clio.reminder.summarization
title: How to recall what a summary replaced
description: The last line of every compaction summary, naming the tool that returns the steps it replaced, when the context store can search text.
x_kind: reminder
x_active: true
x_trigger: source summarization; ends the summary text of every compaction (auto or manual) when the session's context store can search text (otherwise summarization_ids_only is used)
x_variables: recall_tool, compaction_id
x_required: a summary stands in for the history it folded; this line is the only way the agent learns that the originals still exist and how to get them back
---
[The steps this summary replaced are kept in full: {{recall_tool}}(ids=["{{compaction_id}}"]) returns the text they recorded; {{recall_tool}}(query="...") searches them.]
