---
name: review-visual-presentation
description: Inspect and refine data widgets, annotated explanations and substantial dashboards through available view controls and rendered-image evidence. Use during visual investigation or before presenting a complex view.
---

Use visual interaction to investigate a question and to improve what a person
will see. The loop is: inspect the evidence and current view, change a declared
control or component, inspect the resulting image, reason, and refine.
Choose changes that resolve a concrete uncertainty or presentation problem.
This applies to maps, charts, tables, images, models and interconnected reports.

Read the current surface with `inspect_a2ui_surface` and load the relevant
active catalog schemas. `update_a2ui_components` replaces complete definitions
for the named components; preserve the other properties, references and links.
`update_a2ui_data_model` changes a declared binding. Reuse shared filter and
selection semantics rather than inventing another interaction model. For saved
dashboards, edit the source document and publish a new immutable version.

Distinguish server state from a rendered view. Surface inspection returns
definitions and producer data-model updates; it does not report every
viewer-local filter, camera or selected tab. A producer's `rendered=true`
means the definition was accepted, not that you inspected its pixels.
Use a declared viewer inspection/capture tool when one is available, requiring
the image to identify the requested revision, view state, tab and viewport.
View a saved workspace screenshot with `view_image` when available. An image
URI, export link or unrelated earlier screenshot is not visual inspection.
If capture or a requested control is unavailable, make supported structural
corrections and state the remaining verification limit; do not fabricate a
capture or describe a map camera as controllable because the person can zoom it.

Inspect at the size the person will use. Check the overview, relevant tabs,
labels, legends, units, contrast, clipping, framing and controls. Ask whether
the view answers the question and is valuable to a human: can the reader find
the important comparison without deciphering a dense wall of marks?
When a view is crowded, show the meaningful subset or aggregate first. Retain
the complete source data and explain the initial slice. Make additional series,
layers or views available through catalog-supported selectors, filters, tabs
or optional-view controls, with readable labels and visible inclusion state.
Do not invent a visibility property or hide contrary evidence to improve the
story. Check the revealed detail as well as the quiet default.

Use annotations to point to evidence during explanations and questions.
Update a chart with supported layered marks for a labelled point, red outline,
range or trend; keep the original rows and quantitative encodings intact.
Use real data coordinates or stable entity IDs when a mark should follow
zoom/filter changes. A pixel-positioned mark belongs to one captured viewport;
do not reuse it on a different view as if it still identifies the same data.
Annotations clarify evidence; they do not change measurements or submit an
answer. `ask_user` continues to own question submission independently.

For worked decision patterns, load `references/investigation-patterns.md`
from this skill. Stop once the question is resolved and the relevant views
are readable; further captures should answer a remaining concern. Keep visual
observations, calculations and unresolved uncertainty distinct in the answer.
