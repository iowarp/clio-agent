---
id: clio.reminder.media_unavailable_result
title: Say a tool's image or document can no longer be shown
description: Stands in for the image or document a tool returned earlier when its bytes can no longer be loaded.
x_kind: reminder
x_active: true
x_trigger: when a recorded tool result's image or document cannot be loaded again for the next model request; replaces that result
x_variables: error
x_required: it stands in for a tool result; without it the agent would see an empty result and not know the media is gone
---
[clio: media_unavailable] The media this call returned can no longer be shown: {{error}}
