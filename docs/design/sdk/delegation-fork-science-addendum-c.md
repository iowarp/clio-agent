# Addendum C to the delegation, fork and science log plan: viewers, links and size (issue #1823)

This addendum follows [Addendum B](delegation-fork-science-addendum-b.md) and answers [#1823](https://github.com/iowarp/clio-agent/issues/1823): a parent conversation's overview dashboard over many branches, with 3D results that follow the selected case. The owner set the direction on 2026-10-27 (ledger FD41 to FD45).

It does five things:

1. **Find and fix the crash at its root** before changing any viewer.
2. **Any content property can follow the selection**, so one list can drive any viewer.
3. **Viewers mount when visible**, so a large dashboard does not exhaust the browser.
4. **Links** go from a dashboard, and from the study tree, to the views they name.
5. **The component cap is removed.**

## Rules

1. **Keep artifact semantics.** A viewer shows an artifact. Bindable content changes which artifact a viewer shows, not how bytes are trusted. A workspace path a dashboard names is still registered through the existing mint funnel (streamed, content-addressed).
2. **Fix causes, not symptoms.** The crash is reproduced and its cause fixed. Viewing files without registering them is not the fix.
3. **Load what is shown.** Bytes, scenes and GPU contexts exist only for viewers that are visible.

## What the investigation found

- **Registration is not the memory problem.** `export_workspace_paths` → `ingest_artifact_identity` streams files in 1 MiB chunks: files up to 16 MiB go into the workspace CAS, larger ones stay workspace-referenced, and files over 64 MiB are pinned by stat.
- **Viewer paths are never exported.** The export walk rewrites only the `url`, `uri` and `dataUri` keys. `meshUri`, `rasterUri`, `materialUri` and `geojsonUri` are not covered, so agents register meshes one by one (`create_artifact`, 8 per turn by default).
- **Dashboard export loads everything at once.**
  - The client (`captureSessionReview`) reads every artifact the surface or its data model references, in full, into one base64 snapshot and posts it in one request.
  - The server (`export_dashboard_report`) then holds several full copies at once: dict, JSON, gzip, base64, HTML, and encoded twice. With dozens of meshes this is the load-everything pattern, and the prime suspect for the crash.
- **The browser also has a limit.** Every mounted mesh viewport opens its own WebGL context. Browsers keep roughly 16, nothing mounts viewports lazily, and nothing handles a lost context. Tabs already mount only the active tab.
- **The cap.** `MAX_A2UI_COMPONENTS = 256` is enforced per message and on the merged state (`a2ui.py`, `a2ui_component_fold.py`, `validation.py`, the REST route, the refusal catalog and two tests). The per-message 256 KiB byte limit and the 512-message retention remain without it.
- **Actions.** Buttons run client functions (`openArtifact`, `selectData`, `focusWorkflow`) or send events. `openArtifact` opens only same-session artifacts. Workflow nodes have no action, and node clicks change only local state.

## Design

### 1. The crash

- **Reproduce.** A test workspace holds 48 STL files sized like the issue's (tens of MB each). A dashboard references them through a selection-driven layout and through the issue's per-case tabs. Publish it, then export it, measuring the server's peak memory and the export request size.
- **Fix the export.** Export by reference, not by full copy:
  - The snapshot carries each artifact once, identified by hash.
  - The server writes the HTML in a stream: a file artifact's bytes are copied from the CAS or the workspace in chunks, never built into one string, and never base64'd twice.
  - The client uploads the snapshot in parts rather than one base64 body.
  - A size budget per export (`artifacts.dashboard_export_max_bytes`, in config) refuses an export that would exceed it, with a typed reason naming the total. Files over the budget are not dropped silently.
- **Small fix.** `artifacts/export.py` reads a CAS blob before checking its size; check first.
- **The criterion.** The reproduction no longer kills the server, and peak memory grows with the largest file, not with the sum of all files.
- **Measured (2026-10-27).** 48 random-geometry STL meshes of 4 MB (192 MB) in one dashboard. The old export held 1.03 GB beyond its 256 MB request, about 6.7 times the files, so the issue's tens-of-MB meshes need about 10 GB. The streamed export peaks at 9.9 MB for any number of files. Registering the 48 meshes one by one also met the 8-per-turn `create_artifact` limit; dashboards now register workspace mesh paths at publish instead. The issue's crash happened while registering 48 STLs in one turn; on current code that step does not reproduce: 48 random STLs of 30 MB (kept by reference) and of 12 MB (copied into the CAS) each register in about 8 s with a 3.6 MB Python peak and flat RSS (`.cloud-run/f30/register-result*.txt`). The reporters attributed that crash to clio-core and are reporting it separately; the export path and the browser mounting every viewer are the memory problems this addendum fixes.

### 2. Content that follows the selection

- **clio-schemas.** A `DynamicArtifactUri` type (an artifact URI, a data binding or a string function call) replaces the plain pattern string on `meshUri`, `materialUri`, `rasterUri` and `geojsonUri`. The catalog is regenerated and the viewer instructions say a viewer can bind its file to the selection.
- **Selection by row.** The existing shared selection holds a field's values. A table whose selection field is a column of artifact URIs (or of workspace paths, registered at publish) drives a viewer bound to `/selection/<key>/values/0`. No new selection mechanism is added.
- **clio-agent.**
  - The export walk covers every content key (including `meshUri`, `materialUri`, `rasterUri` and `geojsonUri`), and also the data-model values those keys bind to, so a table of workspace mesh paths is registered when the surface is made.
  - Bound values are checked when the data model is written, the same way literal values are checked when the components are written.
- **gact-tui.**
  - The viewer schemas take the dynamic type.
  - A resolved value that is not an artifact URI shows a worded error in the viewer.
  - The raster viewer resets its extent when its file changes.

### 3. Viewers mount when visible

- **Mounting.** Mesh and raster viewports mount their scene only while near the viewport (the existing `use-near-viewport` hook), and dispose it when they leave.
- **Lost contexts.** A lost WebGL context is handled: the viewer says so in words and offers to reload the scene.
- **Fewer contexts.** A selection-driven viewer replaces one viewer per case, so a dashboard needs one context per study, not one per case.

### 4. Links

- **The `openView` function.** A new client function `openView`, with `{view, selection?}`, where `view` is a study `view` entry id or a dashboard artifact URI from any session of the same workspace. It opens the view in the canvas and applies the selection if one is given. Buttons and maps can use it.
- **Workflow nodes.**
  - `clio.workflow.v1` nodes may carry an `action` (`additionalProperties` on `WorkflowNode` is extended), so the experiment tree can open a branch's view or select a case.
  - `selected` becomes bindable.
- **The study tree.** In the Science log, a branch node lists the views that branch attached and opens them in one click.

### 5. The component cap

Delete `MAX_A2UI_COMPONENTS`, both checks, `A2UIComponentLimitExceededError`, the `a2ui_component_limit_exceeded` reason, its hint and refusal entry, the REST 422 branch, and the two tests. The per-message byte limit, the per-string limit and the message retention remain.

## Defaults

| Question | Default |
| --- | --- |
| Dashboard export budget | 2 GiB per export (`artifacts.dashboard_export_max_bytes`) |
| Viewer mount distance | One viewport height |
| Selection-bound file not an artifact | A worded error in the viewer; nothing loads |

## Phase 8b: viewers, links and size

1. Reproduce the crash, then fix the export path, and add the regression test from the reproduction.
2. Make content bindable in clio-schemas (on the same clio-schemas branch as Addendum B), then in clio-agent and gact-tui.
3. Mount viewers lazily and handle lost contexts.
4. Add `openView`, workflow node actions and the tree's view links.
5. Remove the cap.

## Scenarios

| ID | Scenario | Prompt | Passes when |
| --- | --- | --- | --- |
| F30 | The 48-mesh reproduction: publish and export | No prompt | The server stays up, peak memory stays within the largest file plus a fixed margin, and the export opens offline with every mesh |
| F31 | A table of 12 cases with a mesh column drives one viewer | Mechanism | Selecting a row loads that case's mesh in the one viewer, with one WebGL context; a non-artifact value shows the worded error |
| F32 | A dashboard with 40 mesh viewers in a scrolling page | No prompt | Only the visible viewers hold contexts, and none is lost while scrolling |
| F33 | A study overview whose tree opens each branch's view, and a button that opens another session's dashboard with a case selected | Mechanism | Each opens the right view with the selection applied |
| F34 | A surface with 600 components | Mechanism | It is accepted and renders |

## Done

1. F30 to F34 pass with the Verification section's evidence.
2. #1823 is answered on the issue with what changed and what it asked that was decided otherwise. Viewing files without registering them was not added; the cause it was meant to work around is fixed.
