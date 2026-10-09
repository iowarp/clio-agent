# Workspace integration review, 2026-10-09

The integration branch combines the reviewed workspace, drafts, dashboard,
question, session-column, file-opening, tool-timing, visual-feedback and
artifact-presentation branches. It also includes the published website guides.
The UI pin is `84582a460bc4188a8a0caa06d13b5f3ea7c72ef1`; canonical schema
commit `90f55a3b990e9fb7c29aa7aa73b9a410a33d2e2d` is reachable from schema main.

The merge review found a real map selection defect in Linux browser CI:
releasing Shift-drag over the composer left the rectangle active. Pointer
capture now completes or cancels that gesture. The original failing browser
check passes, 19 map unit tests pass, and all 13 map/data browser checks pass
on the final UI revision. A preceding full configured Windows browser run
passed all 85 checks; final-head CI is tracked separately.

A separate run failed the original Data/Work natural-height assertion by one
pixel. Instrumented browser measurement reproduced the cause: the last observed
header/content bounds still included the opening popover's scale transform;
the animation finished without another layout-size observation. Measurement now
uses untransformed fractional CSS border-box height. All four layout browser
checks pass with the original assertions unchanged, plus eight allocation/height
unit tests, lint and production/offline builds. The diagnostic logs and rejected
floating-point hypothesis are retained with the merge evidence.

An actual previously downloaded dashboard exposed a second defect when opened
with networking disabled: CSP blocked the glTF loader's embedded buffer fetch.
The production HTML generator now permits only `data:` and `blob:` connections
for interactive archives; script-free fallbacks still permit no connections.
External network access remains prohibited. All 41 export/dashboard regression
tests pass, including the HTTP download path and policy restrictions.

A report rebuilt through that production generator with its retained immutable
data and the final offline renderer passed a file-based Chromium check. All
three tabs rendered: the annotated scatter, all nine table rows and both mesh
canvases were visually inspected. This was a rebuilt retained report, not a new
UI export request. Map points remain usable offline; external basemap tiles
are not bundled. The website guide states that boundary.

The earlier native CTE daemon crash (`0xC0000005` during `put`) is unresolved.
Merging these changes does not establish that the dependency failure is fixed
or that larger investigations are release-qualified. Linux/macOS desktop
acceptance also remains separate from browser and native-build CI.

Evidence, rejected attempts, browser traces, the compressed native crash profile
and cleanup records are retained at
`D:/Libraries/Videos/clio_recordings/2026-10-09-merge-cleanup/`.
Automatic approval review rejected deleting the temporary website mirror and
the owned Rust cache and CRC-verified native test profiles with `blocked by policy`; no alternate deletion mechanism
was attempted.
