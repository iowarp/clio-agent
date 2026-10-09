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
selection semantics rather than inventing another interaction model. For definition
changes to saved dashboards, edit the source document and publish a new immutable version.

Distinguish server state from a rendered view. A producer's `rendered=true`
means the definition was accepted, not that you inspected its pixels.
`inspect_a2ui_surface` reports fresh mounted viewers, declared bindings,
current values, cameras, active tabs, data references and readiness. Choose
the viewer you mean when several are open. Pass the inspected revision to
updates, then capture the new revision with `capture_a2ui_surface`. Its result
attaches real pixels to your next model step and retains a hashed image artifact
with the viewer state. For a saved dashboard, pass its immutable `artifact_id`
to inspection and capture. Review a temporary live surface when you need to
change a saved definition; publish the reviewed document as a new version.
To navigate or change declared controls in an open saved dashboard, use
`update_a2ui_data_model` with its `artifact_id`, surface/revision, selected
`viewer_id` and inspected `expected_view_revision`. This changes only that
view's local binding. Inspect its acknowledgement and capture the new epoch;
it does not edit the immutable artifact or submit a question answer.
Bind `Tabs.activeTab` to review each tab; its value is a tab child id.
Bind `clio.map.v1.camera` to pan, zoom or rotate a map, sharing the path when
views should move together. Keep reference-backed queries and selections
authoritative. An image URI, export link or unrelated earlier screenshot is
not visual inspection. Verify the capture's revision and state before reasoning.
Use the inspected component's exact binding path; map and mesh cameras can
have different paths and shapes in the same dashboard. Change that binding
instead of replacing the root data model with a guessed set of properties.
Compare the actual captured camera, frame and filters with the requested
values. If a declared control did not move, inspect its binding and correct
the update, then capture again before claiming the change worked.
If a viewer is still loading, omit the optional `expected_view_revision`
to let capture wait for readiness within its deadline. Pin a view epoch
when an already-ready human view must match exactly; a loading epoch
intentionally becomes stale as tiles or layout finish.
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
