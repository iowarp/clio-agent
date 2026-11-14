---
id: clio.reminder.file_view_context
title: Tell the agent what an open file view shows
description: One block per file the person has open in a view their browser draws (a 3D model of a STEP, STL, 3MF, OBJ, PLY or glTF file) - the file, the kind of view and the view state the viewer reports (camera, section, exploded parts, selected and visible parts, units, measurements). The person sees the same view, so this is what "the model I have open" refers to.
x_kind: reminder
x_active: true
x_trigger: at the start of a turn and after a compaction, while the file view is on screen and its state changed
x_variables: path, kind, state
x_required: the person is looking at the file view; without this the agent cannot know what part, section or measurement they see
---
The person has the {{kind}} view of {{path}} open on screen. The state below is the viewer's own text report; it does not show the model's shape or what a section or the camera reveals. look_at_view returns the picture the person sees, with this state (load it with search_tools).
{%- if state %}
The view state it reports:
{{state}}
{%- endif %}
