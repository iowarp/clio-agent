# Widget interaction recordings

Recorded on 2026-10-03 in the real CLIO light interface using Codex / Luna and
NOAA HURDAT2-derived observations. The current clips use isolated demo sessions
with bypass mode, explicitly requested by the user. Global approval defaults
are unchanged.

- `clio-select-reference`: 23.1 seconds at normal speed. A moving pointer draws a
  box selecting thirteen observations, attaches the reference, types the question,
  submits it and shows the real answer identifying both 160 kt timestamps.
- `clio-region-figure`: 63.5 seconds. Draw a region, comment, attach it, open the
  actual captured image and request a PNG. The agent presents it directly with
  A2UI Image. Open and review that coarse draft, then request detailed coastlines
  and a tighter view. The new Image and its download appear beside the request.
  Open the complete portrait figure. Actual work intervals are labelled 8x;
  typing plays at 2x and 3x. Gestures, reading and image opening stay at normal speed.

The historical r5 first figure used a maximum row's coordinates but the other tied
maximum's timestamp. It was rejected. Its actual review request corrected the
annotations for both 1 September 2019 observations: 16:40 UTC at 26.5 N, 77.0 W,
and 18:00 UTC at 26.5 N, 77.1 W. The current r12 final PNG includes real Natural
Earth 10m coastlines and both labels. Its first 110m draft had coarse geography
and a misplaced island label; that actual refinement request is shown. The
r5/r6 recordings exposed a Markdown renderer
bug: valid `artifact://` image references were blocked even though the registered
output file cards worked. Those historical originals remain in the archive.

Puppeteer 25.10.0 records continuous native MP4 with Chrome 157. FFmpeg normalizes
separate copies of AV1 footage, then edits actual video frames. Screenshots are
review evidence only. The visible pointer follows real capture-phase mouse
events, including the selection drag. Both current edits contain sixty distinct
frames over a two-second gesture check; static holds naturally repeat frames.

Authoring code, source intervals, speed changes and captions are in `site/video`.
`selection.edit.json` and `region-image.edit.json` are the exact current timelines.
`region.edit.json` retains the superseded r5/r6 edit. The r12 final opening was
recorded again after fixing portrait fullscreen fitting; it opens the same actual
registered file. Each original take and the exact source intervals are preserved.
Remotion remains available; direct FFmpeg editing avoids the Windows compositor
allocation failures encountered in earlier takes. Capture and rendering run
separately.

Original archive: `D:/Libraries/Videos/clio_recordings/2026-10-03-widget-interactions/`.
Historical bypass archive: `D:/Libraries/Videos/clio_recordings/2026-10-03-bypass-reshoots/`.
Current archive: `D:/Libraries/Videos/clio_recordings/2026-10-03-image-widget-reshoots/`.
Unmodified native takes, timestamps, rejected versions, normalized derivatives,
source CSV/PNG, authoring files, deliverables and review evidence are preserved
and verified by SHA-256. These are local archives, not off-machine backups.
See `site/video/PROCEDURE.md` and `site/video/LESSONS.md` for reshooting and recovery.

The downloadable `dorian-bahamas-figure.png` matches the refined r12 agent output:
SHA-256 `994419849a6cb4dbabbaa130eb6eb19cffadccc97a576f26bcd824969a200559`.
The CSV download has 212 observations; the demo map uses a 30-row Dorian subset.

Selection session: `sess_0eb6bfc4ba32`.
Historical region review session: `sess_4f4b5a89a780`.
Current Image and geographic review session: `sess_f8e88d435bc7`.
The historical screenshot encoder is retained but is not used for these clips.
