# Record, edit and reshoot CLIO widget demos

## Where everything is kept

Permanent archive: `D:/Libraries/Videos/clio_recordings/2026-10-03-widget-interactions/`.
Bypass reshoots: `D:/Libraries/Videos/clio_recordings/2026-10-03-bypass-reshoots/`.
Image-widget reshoots: `D:/Libraries/Videos/clio_recordings/2026-10-03-image-widget-reshoots/`.
Work in `D:/Libraries/Documents/projects/clio-agent/site/video/`.
The archive's `raw/` files are unmodified native browser MP4s, with adjacent
JSON action timestamps. Rejected takes are retained and identified in
`take-catalog.json`. `normalized/` contains editing copies; `deliverables/`
contains the published edits. These are distinct files.

The current Image workflow uses session `sess_f8e88d435bc7`. Its r12 native
recording shows capture, attachment preview and the first figure. The actual
geographic review is `region-20261003-r12-geographic-review2`; the final opening
is `region-20261003-r12-result-fitted`, recorded after fixing portrait fitting.
`region-image.edit.json` gives the exact three-source timeline. Work intervals
play at 8x and typing at 2x/3x; gestures and reading holds remain at normal speed.
The superseded `region.edit.json` retains the historical r5/r6 timeline.

For a reshoot, inspect the real map's frame before setting coordinates in
`take-region-image.mjs`; then use a new recording name. Use `take-image-review.mjs`
for the actual refinement and `take-image-result.mjs` only when a new real
opening is needed. Exit Image fullscreen with **Exit full screen** before typing;
Escape did not close it. Keep the first figure, request and corrected result in
the edit so a geographic correction is visible. Preserve each take's original
MP4 and marks before normalizing or editing copies.

The original archive includes the complete 471.9-second region take, its later refinement
take and the corrected result shot. The edit uses only the first 90 seconds of
the normalized main region take; the full original is preserved in `raw/`.
The later refinement recording is provenance, not a shot in the published edit.

`authoring/` preserves the capture and editing code, package lock, manifests,
captions and this guide. Browser profiles, credentials and node_modules are
excluded. `source-data/` preserves the CSV, actual exported PNG and the agent's
plotting script. `evidence/` holds review images, motion checks and render logs.
`manifest.json` gives the original source path and SHA-256 of every archived file.

To create another snapshot from the authoring working directory, use
`node archive.mjs D:/Libraries/Videos/clio_recordings/NEW-DATED-FOLDER`.
The script creates files exclusively and fails if a destination file already
exists. Use a new empty folder; review its source paths and take classifications
before archiving a different shoot. It verifies every copied file by SHA-256.

## Recover the website after a chat crash

The built site remains on disk. Run this PowerShell command from the repository
root to start a detached, hidden preview. First check that port 4322 is free;
reuse a healthy existing preview instead of starting a duplicate.

```powershell
Get-NetTCPConnection -LocalPort 4322 -State Listen -ErrorAction SilentlyContinue
$previewRoot = 'D:\Libraries\Documents\projects\clio-agent\site'
$previewLogRoot = Join-Path $previewRoot 'video\out'
New-Item -ItemType Directory -Path $previewLogRoot -Force | Out-Null
Start-Process -FilePath (Get-Command node).Source `
  -ArgumentList @('node_modules/astro/bin/astro.mjs','preview','--host','0.0.0.0','--port','4322') `
  -WorkingDirectory $previewRoot -WindowStyle Hidden `
  -RedirectStandardOutput (Join-Path $previewLogRoot 'preview-server.log') `
  -RedirectStandardError (Join-Path $previewLogRoot 'preview-server-error.log')
Invoke-WebRequest 'http://10.0.0.170:4322/docs/widgets/interaction/' -Method Head
```

Allow startup to finish before the HTTP check. This hidden process is not a
durable service: a Codex crash can still stop it. Check availability after a
crash or machine restart. A reshoot additionally requires the real CLIO application and backend
on port 5176. The website preview serves existing videos without that backend.

## Prepare a real conversation

1. Run `npm ci` in `site/video`. FFmpeg and ffprobe must be on PATH. Dependencies
   are pinned in the lockfile; the current capture browser is Chrome 157.0.8083.0.
   Native `page.record()` requires Chrome 153 or later. The bundled Chrome 152
   did not support it. The official Puppeteer browser installer was used to
   install a compatible Chrome build.
2. Set the executable for this machine, then launch the recorder in one terminal:

   ```powershell
   $env:DEMO_CHROME_PATH = 'C:\Users\jaime\.cache\puppeteer\chrome\win64-157.0.8083.0\chrome-win64\chrome.exe'
   node capture.mjs
   ```

   Wait for `CAPTURE_READY`. This launches a separate browser profile. Set the
   reviewed **1600 x 1088 CSS pixels, device scale 0.8** framing before a take:

   ```powershell
   node command.mjs '{"op":"viewport","width":1600,"height":1088,"scale":0.8}'
   ```

   It does not reuse the user's browser profile.
3. From a second terminal in `site/video`, navigate to a real CLIO session:

   ```powershell
   node command.mjs '{"op":"goto","url":"http://10.0.0.170:5176/workspaces/ws_f51a72e2d428/sessions/sess_16d7cd995a35"}'
   node command.mjs '{"op":"inspect"}'
   node command.mjs '{"op":"shot","name":"reshoot-layout-review"}'
   ```

   The URL is the original session; create a fresh conversation for a reshoot
   and replace it. Inspect controls and coordinates in the current UI before
   driving them. Existing coordinates in `take-selection.mjs` belong to the
   inspected map zoom, viewport and layout, not arbitrary sessions.
4. Use the real CSV in `source-data/atlantic-hurricanes-noaa.csv`. It contains
   212 NOAA HURDAT2-derived records. Prepare the interactive answer with:

   > Use atlantic-hurricanes-noaa.csv in this workspace. Show Dorian near the
   > Bahamas from 29 August to 4 September 2019 on an interactive map, with the
   > ordered path coloured by wind speed. Keep the explanation short.

   The original session used Codex / Luna. Wait for the actual map and data to
   load. Choose light mode, collapse the CLIO app's left panel for the recording,
   and frame the map plus composer. The website's docs navigation remains visible
   on the actual documentation page. Do not change browser fullscreen or viewport
   dimensions during a take.

   The user explicitly requested bypass mode for these isolated demo sessions.
   Create the session with `approval_mode: "bypass"` before the first turn and
   confirm **Bypass checks** in the composer. Keep normal global defaults.
   This removes approval pauses; it does not remove actual agent/tool latency.

   For the reshoot, `viewport` sets a fixed 1600 x 1088 CSS viewport at scale 0.8
   before recording. This fits the initial question, map and explanation in one
   frame. Inspect the encoded dimensions (1278 x 870 in the trial) before cropping.
   Never change the viewport while a take is recording.

   If a restarted private store reports undecodable event data, retain it and
   its failed take. The launcher accepts `--review-root` for a fresh private
   namespace **and** session registry. The current reshoot uses
   `D:/clioqa/widget-video-20261003-r4`; the old default root remains intact.

## Record the complete interaction

Use a new dated take name for each attempt. The recorder refuses existing
MP4 filenames. Never use an
archived filename as a new output. Recording starts before the selection and
continues through the actual answer; screenshots are review evidence only.

```powershell
$takeName = 'selection-' + (Get-Date -Format 'yyyyMMdd-HHmmss')
if (Test-Path -LiteralPath "public/takes/$takeName.mp4") { throw 'Take already exists' }
node command.mjs (@{op='start'; name=$takeName} | ConvertTo-Json -Compress)
node command.mjs '{"op":"mark","label":"selection begins"}'
# Replace coordinates after inspecting the live map.
node command.mjs '{"op":"drag","from":[610,360],"to":[905,475],"ms":1800}'
```

The visible pointer follows real mouse events. Move slowly enough to see the
gesture: about 1.8 seconds for a box, with a short pause before and after.
Use actual controls and inspect after each transition. Use `click` with a
verified selector or coordinates; `type` types incrementally, with 25 ms or less
between characters in reshoots. Inspect encoded typing time: browser input
processing adds overhead. `fill` is for setup, not demonstrated typing.

### Select and reference

Select a visible group, use **Reference this**, and show the compact reference
beside the empty composer before typing. Type and submit:

> When were the winds strongest here?

Mark attachment, typing, submission, working and answer. Keep the real working
state, then hold a short answer for about five seconds. The original box selected
14 observations. The accepted bypass reshoot selects 13; its answer identifies **160 kt on 1 September 2019 at 16:40
and 18:00 UTC**. A different selection requires its own real answer.

### Capture a labelled region and request a figure

Open **Capture labelled regions**. Inspect whether Select is already active;
clicking an active toggle can turn selection off. Draw a slow box around the
Bahamas bend, comment **“Dorian slows here, then turns north.”**, add the region
to the message and show the compact image reference. Type:

> Attach a PNG map of this bend with coastlines and peak UTC labels.

Record the real working state. In the user-authorized bypass reshoot, there are
no permission prompts. Review the actual generated image. The original
invented-island proposal was rejected. Its
follow-up requested real coastlines and both peak observations with short UTC
dates. Further layout refinements were required. Preserve those takes and
disclose that refinement in the edit; do not imply the final figure was the
initial answer.

Review the actual exported PNG. The accepted version uses 50m Natural Earth
coastlines, fourteen observations, both 160 kt labels and a horizontal wind
colour bar. Open the captured attachment before submitting. When the generated
file arrives, click its real file card and maximize the application canvas in
the same take. Hold the result for about five seconds. Keep browser dimensions fixed.

The later Image-widget reshoot uses `take-region-image.mjs`. It opens the
captured attachment before submission, then shows the actual generated PNG
inline with its prompt, prose answer and downloadable file. Its final click
uses the Image widget's **Full screen** control. Inspect that control inside
`[data-slot="a2ui-media-image"]`; a map's identically named button is unrelated.
The agent's standard prompt and presentation skill request one Image view per
figure, without a duplicate Markdown image. Verify the agent actually followed
that guidance and used real geographic data before accepting a take.

`diagnose-follow.mjs` records scroll and resize events for a rejected or review
take. When attaching another Puppeteer client to the recorder browser, always
pass `defaultViewport: null`. Its default otherwise silently resizes the existing
page to 800 x 600. Save diagnostics to a unique evidence filename. Do not edit
the application during an accepted take: a development reload can invalidate
the UI and spoil the recording even though the backend continues working.

### Finish each take

```powershell
node command.mjs '{"op":"mark","label":"answer hold ends"}'
node command.mjs '{"op":"stop"}'
node command.mjs '{"op":"close"}'
```

`stop` finalizes the native MP4 and adjacent JSON timestamps. `close` also
finalizes an active take and closes this recorder's browser. Copy raw material
to a new dated archive before trimming or rendering. Preserve unsuccessful takes.

## Normalize a copy, then edit

Stop capture before rendering. Check native dimensions, duration, codec and
colour range with ffprobe. The accepted originals are native AV1 recordings
with variable frame rate; 30 fps is a requested rate, not proof of motion.

```powershell
ffprobe -v error -show_entries 'stream=codec_name,width,height,color_range,r_frame_rate,avg_frame_rate:format=duration' -of json public/takes/selection-final.mp4
ffmpeg -y -i public/takes/selection-final.mp4 -an `
  -vf 'fps=30,scale=1280:870:in_range=full:out_range=tv,format=yuv420p' `
  -c:v libx264 -preset fast -crf 17 -threads 1 -color_range tv `
  -movflags +faststart public/takes/selection-final.limited.mp4
```

Use new derivative filenames on reshoots. The example range conversion matches
these source takes; inspect future inputs instead of assuming their range.
For the existing region edit, the normalized main take intentionally keeps
only the first 90 seconds (`-t 90`), while the raw file keeps the full recording.

The edit JSON defines real video intervals, crops, gentle zooms and captions.
Update `source` and each `from`/`to` using decoded source frames and the take's
timestamp marks. Wall-clock marks can drift from variable-frame-rate media
timestamps; they are navigation hints, not exact edit boundaries. Update
VTT cues and posters when timing changes. Never splice in a scripted answer.

```powershell
npm run render -- out/selection-final.mp4 --props=selection.edit.json --browser-executable="$env:DEMO_CHROME_PATH"
```

Remotion uses one worker and a 64 MiB video cache. The region render failed
under Windows memory pressure, so its accepted edit uses the lower-memory path:

```powershell
npm run render:ffmpeg -- region.edit.json out/region-final.mp4
```

This fallback crops, scales, animates framing and captions directly from video
frames. It does not compile screenshot sequences. Close only the recording
browser when reducing load; avoid terminating unrelated user applications.

## Review the encoded result and publish

1. Decode actual output frames at selection, attachment, typing, working and
   final answer. A browser screenshot does not prove the recorded frame matches.
   `ffmpeg -ss 40 -i out/selection-final.mp4 -frames:v 1 out/answer-review.png`
   is an example; choose timestamps from the actual edit.
2. Watch the **complete** encoded video, then watch it embedded in the website
   and fullscreen. Check pointer visibility, smooth gesture and typing, readable
   references, question and answer together, full map/figure, aligned controls,
   captions and final reading time. Inspect both tabs and docs left navigation.
3. Check motion over a known active gesture, not a static hold. The reviewed
   outputs had 84 unique frames over 2.8 seconds of selection and 90 over three
   seconds of region capture. Duplicate frames in a still hold are expected.
4. Keep real working frames. Use `speed` in the edit manifest for a long agent
   wait and label the accelerated interval. Retain every source interval through
   submission, file arrival, click and expansion; avoid a one-frame cut from
   working state to an already-open image. The selection reshoot plays at normal
   speed throughout.
5. Copy reviewed edits to `site/public/media/clio-select-reference.mp4` and
   `clio-region-figure.mp4`, with their JPG posters and VTT captions. The final
   agent PNG is `dorian-bahamas-figure.png`. Update media README and page text.
   To strip a silent AAC track without recompressing the video:

   ```powershell
   ffmpeg -y -i out/selection-final.mp4 -map 0:v:0 -c:v copy -an -movflags +faststart ../public/media/clio-select-reference.mp4
   ```

6. Run `pnpm build` from `site`, restart/reuse preview, and check the page and
   both media URLs. The last successful build produced 60 pages. A build plus
   playback review does not claim a full application regression suite passed.
7. Archive the final deliverables, manifests, evidence and source data alongside
   the raw takes. Verify source and archive SHA-256 hashes. Do not move or delete
   originals from the working checkout as part of archival.

## Restore an edit from the archive

Copy `authoring/` to a new working folder, run `npm ci`, and copy accepted
`normalized/*.mp4` to `public/takes/` there. The manifests' `takes/...` paths
are relative to the Remotion public directory. Run the rendering commands above.
Restore raw footage separately if regenerating derivatives. Browser profiles
are deliberately absent; authenticate normally if recording a new live session.
