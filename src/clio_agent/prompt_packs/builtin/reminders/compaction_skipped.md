---
id: clio.reminder.compaction_skipped
title: Say that auto compaction stopped for this turn
description: The notice recorded when the context is still over the threshold right after a compaction, so auto compaction stops repeating. It is shown to the person, never to the model.
x_kind: reminder
x_active: true
x_trigger: once per forward, when the first measurement after an auto compaction is still over the session's threshold
---
Context is still near the limit after summarizing; continuing without summarizing again this turn.
