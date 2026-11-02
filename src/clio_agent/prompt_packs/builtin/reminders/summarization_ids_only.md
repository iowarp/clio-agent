---
id: clio.reminder.summarization_ids_only
title: How to recall what a summary replaced (no text search)
description: The last line of every compaction summary when the context store cannot search text, so recall works by id only.
x_kind: reminder
x_active: true
x_trigger: source summarization; ends the summary text of every compaction (auto or manual) when the session's context store cannot search text (otherwise summarization is used)
x_variables: recall_tool, compaction_id
x_required: a summary stands in for the history it folded; this line is the only way the agent learns that the originals still exist and how to get them back
---
[The steps this summary replaced are kept in full: {{recall_tool}}(ids=["{{compaction_id}}"]) returns them byte-exact; text search is unavailable here. Use ids, not a query.]
