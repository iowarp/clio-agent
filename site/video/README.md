# CLIO demo video editing

For the explicitly labelled screenshot walkthroughs covering files, sources, and
marketplaces, see [SOURCE-GUIDES.md](SOURCE-GUIDES.md). The continuous-recording
workflow below remains the workflow for demonstrating live motion and gestures.

Record a continuous, real interaction before editing. The website's earlier
recordings were assembled from sparse browser screenshots; their nominal
30 fps output does not supply the missing motion.

This separate authoring project uses Puppeteer for continuous browser recording,
Remotion for an editable timeline and animated framing, and FFmpeg for encoding.
It adds no runtime dependencies to the website.

Keep the original take, action timestamps, and the edit manifest. Preserve actual
typing, pointer movement, the selection gesture, and the real agent response.
Accelerate long work intervals with an explicit speed caption. Never replace a
response with a scripted UI state or cut straight to an image without its click.

Review the complete rendered clip at the website player's size and fullscreen.
The attachment, question, and answer must remain readable together. Check actual
motion during the drag and typing, not just selected still frames.

## Capture

`npm ci` installs the pinned authoring dependencies. The bundled Puppeteer Chrome
152 lacks `Page.startScreenRecording`; install a Chrome build at 153 or later and
set `DEMO_CHROME_PATH` to its executable before running `node capture.mjs`.
The current local recording build is Chrome 157.0.8083.0.

The recorder runs in its own browser profile. `node command.mjs '{"op":"inspect"}'`
reads the visible UI; `goto`, `click`, `move`, `drag`, `type`, `start`, `mark`, and
`stop` drive and record real interactions. Command files are atomically published.
The viewport is explicitly 1600 x 1088 CSS pixels at scale 0.8, producing native
1278 x 870 media in this shoot. Chrome otherwise defaults to an 800-pixel capture.
Source footage uses Chrome's variable frame rate.

The editorial pointer follows actual mouse events. It does not invent intermediate
positions over still screenshots. `shot` saves review evidence only.
Window capture-phase listeners keep it moving during map selection, whose
capture handler stops event propagation. Run `node --test pointer-guide.test.mjs`
with `DEMO_CHROME_PATH` set to exercise that behavior in the actual browser.
The recorder refuses existing take names and viewport changes during a take.

`take-selection.mjs` is the reviewed gesture for the fresh Dorian conversation at
the inspected map zoom. Inspect its live controls and geometry before rerunning it.
It saves one continuous MP4 through selection, attachment, typing, working, and
the actual agent response. It leaves the recording running if approval is needed.

## Edit

Edit JSON chooses intervals from the original MP4, crops, and optional camera zoom.
Render using `npm run render -- out/demo.mp4 --props=your.edit.json` and an explicit
`--browser-executable` when using the installed capture browser. Use a small
`--offthreadvideo-cache-size-in-bytes=67108864` and one rendering worker on this
machine (the render script sets both). Capture and rendering should run separately
to avoid contention.

Normalize a copy of Chrome's AV1 recording before rendering: use FFmpeg's
`fps=30,scale=1280:870:in_range=full:out_range=tv,format=yuv420p`, H.264, and
`-color_range tv`. Preserve the native recording. The full-range H.264 trial
failed with `Output changed` in Remotion's Windows compositor; a limited-range
copy rendered successfully with one worker and a 64 MiB video cache. This
sequence does not establish colour range as the sole cause of the failure.

If the Windows compositor cannot allocate memory, use
`npm run render:ffmpeg -- region.edit.json out/region-final.mp4`. This lower-memory
path applies the same timeline, crop, framing zoom and captions directly to the
recorded video with FFmpeg. It never assembles source screenshots. The published
region edit uses this path after a compositor allocation failure; the selection
current reshoots use direct FFmpeg editing. The earlier selection edit used Remotion.

`selection.edit.json` and `region-image.edit.json` are the current website edits.
`region.edit.json` preserves the superseded region timeline. The current region edit
combines the original marked-region request with the actual result after review
and refinements, identified explicitly in its caption. It does not claim that
the refined figure was the first answer. Matching VTT files keep instructions
available without covering the interface throughout the clip.

## Permanent archive and reshoots

The original recordings are also preserved outside the development checkout:
`D:/Libraries/Videos/clio_recordings/2026-10-03-widget-interactions/`.
The user-requested bypass reshoots are preserved separately under
`D:/Libraries/Videos/clio_recordings/2026-10-03-bypass-reshoots/`.
The archive keeps every native take and its timestamps, including rejected
trials, accepted normalized copies, edit manifests, the pinned authoring project,
published deliverables, source data and review evidence. `manifest.json` records
source paths, sizes and SHA-256 hashes; `manifest.sha256` supports later audits.

See [PROCEDURE.md](./PROCEDURE.md) for capture, editing, recovery and publication
steps, and [LESSONS.md](./LESSONS.md) for the specific failures and decisions from
these shoots. `take-catalog.json` in the archive distinguishes accepted material
from rejected trials. Never overwrite an existing raw take when reshooting.
