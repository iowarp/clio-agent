# Agent control and visual feedback

Accepted design, expanded after the 2026-10-08 review. This is a general
visual investigation and presentation capability. VIGIL integration is
deferred; no research-runtime or simulation-steering integration is part of
this scope.

## Purpose

An agent can inspect a visual system, change its declared view controls,
receive the actual resulting pixels and use both those pixels and the source
data to investigate, explain or improve the presentation:

**Inspect → control → render → screenshot → reason → adjust.**

The target includes maps, charts, tables, images, models and interconnected
dashboards. It is not restricted to mesh cameras or scientific rendering.
The agent can change a filter, zoom or pan to a region, compare encodings,
select a time interval, annotate a feature, switch tabs or simplify a crowded
overview. It chooses the changes based on the question and the person who
will use the result. A screenshot is evidence of a view, not ground truth for
the data or proof of a statistical or physical claim.

## Current implementation boundary

The pending dashboard/question stack already supports structural inspection
through `inspect_a2ui_surface`, component revisions through
`update_a2ui_components`, and declared data-model bindings through
`update_a2ui_data_model`. Mesh camera, frame, field and threshold bindings
drive the real view. Existing guarded Vega-Lite layered chart specs can mark
and label selected observations. Registered data references, shared selection
and queries remain authoritative.

`capture_a2ui_surface` now requests a fresh PNG from one mounted viewer and
hydrates its immutable image snapshot into native model media. Inspection
reports declared bindings, current values, actual map/mesh cameras, active
tabs, reference/query/selection context, viewport and readiness. The map's
canonical `camera` binding supports pan, zoom, bearing and pitch;
`Tabs.activeTab` supports agent navigation through tab child IDs. Shared
bindings retain ordinary human interaction and existing linked selections.
Neither an export link nor a server-side `rendered=true` result establishes
agent inspection of the browser frame. Only a delivered image does.

Saved dashboards remain immutable artifact versions. Review uses a temporary
preview against a named version; deliberate definition changes publish a new
version through the existing report tool.
`update_a2ui_data_model` can also change a declared local binding in an open
saved dashboard by naming the pinned artifact, viewer and expected view epoch.
The viewer acknowledges a new epoch before a subsequent matching capture;
the immutable definition and other open viewers are retained.

## Controls and inspection

Extend inspection with a schema-derived control manifest, not a separate
interaction language. Each control identifies the component, property,
supported operation or bound data-model path, value shape, current value and
whether it is producer or viewer state. Include current viewport and active
tab, data/query/selection provenance and readiness. Do not infer that a
viewer-local button is controllable by writing an arbitrary data-model path.

Reuse ordinary component and data-model updates for declared bindings. Add
missing controls to the canonical schema, validator and renderer together.
Examples include a map center/zoom/bounds, a chart domain or grouped-series
filter, a table sort/page, and shared dashboard tab/filter state. Controls
must preserve the underlying records and stable selection identity.

View updates accept expected definition revisions and report their new revision.
Concurrent producer revisions reject atomically. Viewer epochs independently
detect human navigation during capture; a human's local bound values are reported
by the renderer and are not a substitute for a persisted definition revision.
Definition revisions and viewer-state revisions are distinct; the same
definition can have many cameras, filters and tabs. Ordinary inspection,
camera changes, filtering, annotation and capture do not answer a question.

## Bounded capture

The capture operation names the surface or saved artifact version, optional
displayed component, expected definition and optional view-state revisions,
and a timeout up to 20 seconds. It records the actual viewport rather than
resizing the person's window. Select tabs and framing through declared controls
before capture. Whole-view and component captures retain images up to 5 MB and
4096 pixels per dimension. A request belongs to one viewer; different viewers
cannot complete it or race to substitute their state.

The viewer renders the producer state and waits for fonts, required data,
chart layout, map tiles and WebGL frames as appropriate. Report failure for
missing data, failed rendering, unavailable pixels or timeout. Do not return a
previous frame as success. Recheck the view after capture: a human change
during the operation makes it stale. Hidden or unmounted content is unavailable
unless a separately declared headless viewer uses the same renderer and data.
No headless viewer is implemented in this patch; the intended view must be open.

A successful result retains an immutable image artifact and native model
image delivery, with its pixel hash, dimensions and timestamp; source surface
or artifact version and definition/view-state revisions; referenced dataset
IDs/revisions; query, filters and selection; active tab and relevant camera,
field, time or domain state; readiness/degradations; and command/capture
correlation. Retain compact media descriptors in traces rather than repeated
base64 expansions. Use existing image semantics for the actual pixels.

## Pointing at evidence

An agent can revise an existing view to show a red circle, labelled point,
arrow, range or region as an explanation or as context inside an owned
question. For charts, current supported layered marks already cover selected
points and ranges. A generic cross-component overlay is a separate catalog
extension, not an undocumented parameter to `update_a2ui_components`.

An annotation has a stable ID, target component and an explicit coordinate
frame: data coordinates or entity identity for a chart; geographic coordinates
for a map; object/world coordinates for a model; or normalized image/view
coordinates tied to a captured version and viewport. Include the geometry,
style, label and evidence reference. A pixel or normalized-view annotation
must not silently move to a different camera, resize or tab. Data-anchored
marks should follow the data projection and visibility rules.

Annotations survive normal reference and export semantics with their target
and lineage. Allow revision/removal without changing measured data. Keep
labels readable and provide an accessible textual description. A mark can
direct attention; `ask_user` still owns the explicit answer submission.

## Investigation examples

- **Which storm passed near Chicago?** Filter the relevant time and complete
  track records, change the declared map region/zoom, inspect candidates, then
  check source times and distances before naming one. Annotate the relevant
  location and track for the explanation.
- **Does this data show correlation or a linear relationship?** Inspect a
  scatter view, narrow or compare groups, change a supported scale/domain,
  inspect the result, and check correlation, fit and residuals from the data.
  Keep visual pattern, numerical evidence and any causal claim distinct.
- **Look at this part of the graph.** Update supported annotation marks around
  an actual selected observation or range, inspect the result, and use it in
  an explanation or question without changing its values.
- **Make this report valuable to a human.** Review the actual docked overview,
  each relevant tab and expanded view. Fix contrast, labels, clipping,
  framing, duplicate graphs and crowded legends. Choose a useful default
  subset or aggregate. Keep additional meaningful series/layers/views behind
  labelled selectors, filters, tabs or optional-view controls, preserving the
  full source and visible inclusion state. The agent authors the presentation;
  the person can reveal more detail.

## Skills and prompt

`review-visual-presentation` holds the general investigation/review loop and
worked decision patterns. The interactive-analysis and dashboard authoring
skills call it for complex views. A2UI producer experts discover it alongside
their shape catalogs; ordinary skill precedence remains intact.

The main chat prompt already mentions A2UI, so one short reference routes
complex visuals to the review skill and available controls/capture. Detailed
procedures stay in skills. Instructions must not claim unavailable capture or
control capabilities. Stop when the relevant question is resolved and views
are useful; repeat captures to resolve actual remaining concerns.

## Runtime acceptance

The implementation now includes the mounted-view bridge, saved-view local
controls, native screenshot delivery, map camera and tab bindings, and ordinary
PNG/HTML download preparation. It is still a draft, not fully accepted for
release. See [the recorded acceptance review](../verification/widget-visual-feedback-2026-10-09.md)
for real model runs, production-session evidence and remaining blockers.

Prove agent control and native matching-image delivery on a map and chart as
well as a mesh, using registered data and current declared controls. Reject
stale revisions, absent viewers, missing data, timeout and failed tile/WebGL
frames. Test two viewers and a person's intervening change. Check that a
question remains pending through control, annotation and capture.

Review a dashboard tab, fix a visible problem and capture the correction,
then publish a new version preserving definition/data/annotation lineage.
Exercise dense defaults and revealed detail, linked controls, reference and
image/HTML export. Record real model acceptance separately from deterministic
tests; the earlier synthetic-loop pilot is not an accuracy or speed benchmark.
