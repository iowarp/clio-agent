---
id: clio.reminder.compaction_failed
title: Say that a compaction failed
description: The notice recorded in the conversation when summarizing the context failed. It is shown to the person, never to the model.
x_kind: reminder
x_active: true
x_trigger: when a compaction (auto or manual) fails; recorded as a notice where it happened, with the error's message
x_variables: error
---
Summarizing the context failed, so it was left as it was. {{error}}
