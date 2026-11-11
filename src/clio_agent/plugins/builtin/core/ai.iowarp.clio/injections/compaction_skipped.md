---
id: clio.reminder.compaction_skipped
title: Say that summarizing waits for more of the remaining room
description: The notice recorded when the context is still over the threshold right after a compaction, so the next auto compaction waits until the context fills the threshold's share of the room left above it. It is shown to the person, never to the model.
x_kind: reminder
x_active: true
x_trigger: once per forward, when the first measurement after an auto compaction is still over the session's threshold
---
Context is still over the summarizing threshold after summarizing; the next summary waits until more of the remaining room is used.
