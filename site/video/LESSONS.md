# Lessons from the 2026-10-03 widget demo shoots

## Presentation and honesty

- Continuous recording is the source of motion. Encoding sparse screenshots at
  30 fps did not produce a smooth demonstration. Keep native MP4s, real pointer
  events, visible typing, actual working states and actual agent responses.
- A box selection communicates intent better than a fast point click. The first
  2.7-second drag, opening cursor journey and ten-second answer hold felt slow.
  Reshoots start at the control, use about 1.8 seconds for the box, faster real
  typing, and about five seconds for a short answer.
- Plan the whole frame: map, compact reference, empty composer, visible typing,
  question and answer. A giant expanded JSON reference or open sidebar can hide
  the evidence. Inspect normal and expanded widget states before shooting.
- The final figure required real refinements. Explicitly identify the intervening
  coastline, label and layout work in the edited video. Shortened waits need a
  visible disclosure; do not add an artificial pause to make an answer seem real.
- Keep videos on Human and agent, in two example tabs. Do not repeat unrelated
  live map/mesh widgets beneath the video or remove the docs left navigation.

## Capture failures and their fixes

- Puppeteer's bundled Chrome 152 lacked native recording support. Chrome 157
  worked; `page.record()` requires at least 153. Record the executable/version
  with the procedure rather than assuming Puppeteer's default will work.
- Chrome's default recording size produced 800-pixel trial takes even though
  browser layout appeared larger. Explicit `maxWidth:1280`, `maxHeight:870` and
  a matching viewport fixed accepted takes. Check encoded dimensions.
- Headless video did not include an OS cursor. A small SVG pointer attached to
  actual mousemove events made the gesture visible without inventing motion.
  The original document-level listener missed selection movement because the
  map stops propagation during capture. Window capture-phase listeners now
  follow the actual drag; a browser regression test exercises that exact case.
- The region tool already had Select active. Toggling it again disabled capture
  and dragged the map instead. Inspect active state; rejected take is retained.
- Changing browser fullscreen during capture produced a cropped encoded result
  even though the simultaneous screenshot looked correct. Decode the MP4 before
  accepting it. The corrected final shot records the verified exported PNG in
  normal browser view at the same viewport.
- Fixed coordinates are valid only for the inspected session, map zoom and
  layout. Re-inspect on reshoots. Use unique take names; the recorder now refuses
  existing MP4 filenames.
- Current-turn status must use v3 `run_id`, with the latest user message boundary
  for live tool messages that have not yet received a run association. Otherwise
  real tool activity incorrectly falls back to session preparation.
- After a crash, a restored demo namespace returned undecodable ARC event data.
  A failed response is rejected footage. Preserve the old store and use a fresh
  private core namespace plus session registry for reshooting; changing just the
  core directory still rehydrates old sessions from the default registry.

## Editing and machine limits

- Preserve native AV1, variable-frame-rate takes. Normalize a separate copy to
  30 fps H.264/yuv420p for editing. The full-range trial failed with `Output changed`;
  limited-range selection rendering succeeded. This observed sequence does not
  prove colour range was the only cause.
- Wall-clock action marks can drift from native media timestamps. Use decoded
  frames to locate the actual action and answer before choosing edit boundaries.
- Fast input seeking into the native AV1 take returned a misleading blank modal
  frame. Continuous decoding and the normalized H.264 copy showed the actual
  attachment image. Review with output seeking after `-i`, and check a sequence
  before diagnosing a preview regression from a single AV1 seek.
- Capture and render separately. Even one Remotion worker and a 64 MiB cache
  could not finish the region edit: Windows reported an allocation failure.
  Direct FFmpeg editing of the same video intervals completed successfully.
- Animated framing should be subtle. Native interactions provide the motion;
  aggressive zoom or cuts can hide map context and make small controls unreadable.
- A final screenshot is insufficient review. Decode actual source/output frames,
  examine a sequence during motion, and watch complete clips in both the embedded
  player and fullscreen. Keep rejected evidence so the next editor knows why a
  superficially good screenshot was not accepted.
- Detached hidden website preview avoids tying its lifetime to a chat command
  session. It still needs explicit recovery after process loss or machine restart;
  check HTTP availability instead of assuming a previous build is still served.

## Scientific output and agent review

- Review the actual scientific output. The first proposal used invented islands;
  it was rejected. Real geography and observations matter more than a quick
  attractive answer. The user subsequently requested bypass mode for these
  isolated demo sessions so permission prompts do not interrupt the clips.
  Set that session policy before shooting; retain real tool activity and review
  the generated files. Do not change global approval defaults.
- The first high-resolution coastline export failed under memory pressure.
  Natural Earth 50m was sufficient for this regional figure and rendered with
  real geography. Keep units, aspect ratio and both peak timestamps correct.
- Requested edits may target text absent from the real plotting script. Read the
  actual file and inspect the actual PNG after each material refinement.
- A transient artifact loading error is not proof the export is missing. Wait
  for the real file card, verify its file, and preserve exact output bytes.
- The original pre-reshoot PNG's SHA-256 is
  `91b7c1d210bf124490f272a09f86f6167d439bcfd9c3ab0a3c8cae6e820f0e34`.
  Preserve the CSV and plotting script alongside it. Do not replace missing
  Abaqus exports with unrelated or fabricated meshes in a future example.
- The corrected bypass reshoot PNG has SHA-256
  `fbc4bc42e408466c95ba09e4ed3aaf9dbed0ec92d50caec594762d00827247c5`.
  The 65.5-second edit includes the actual label-review request. It retains both
  file-opening actions from the final corrected response through expansion.
- Selection r6 is accepted: 23.1 seconds, one uninterrupted source interval at
  normal speed, 13 records, about three seconds typing and a five-second real
  answer hold. Both published clips have 60 unique frames in a two-second drag.

## Archival discipline

- The expanded artifact image had an auto-height scroll wrapper, which kept the
  fitting viewport small even in a maximized canvas. Give image previews the
  bounded panel height directly; retain scroll wrappers for documents and text.
  The actual expanded image refitted after this fix, with 12 focused tests passing.
- The bypass reshoot rendered real coastlines, but its first peak annotation used
  the first maximum row while hard-coding the other maximum's timestamp. Review
  coordinates and times together and annotate both tied maxima. The initial PNG
  and real correction request are retained; do not imply it was correct initially.
- The generic Markdown sanitizer rejected valid `artifact://` image references.
  Resolve registered images through the authenticated artifact transport, not
  an arbitrary URL allowlist. Generated figures should also be presented with
  the negotiated A2UI Image component. An output card alone does not establish
  that the image was displayed successfully in the answer.
- Multi-line terminal commands must share the bounded preview with their output.
  Bounding stdout alone leaves long plotting scripts free to fill the transcript.
- An activity collapse can deliver a delayed native scroll after its resize has
  already been followed. The trace recorded a 505-pixel jump with unchanged
  geometry, which silently disabled following. A short layout window now repairs
  that delayed scroll; explicit reader navigation still takes precedence.
  Fifteen focused conversation scroll tests pass, including both cases. A
  100 ms window missed a real 154 ms delayed scroll in r10; the window is now
  500 ms and still gives explicit reader navigation priority. The fresh r12
  take kept the current question and generated image visible.
- Keep Image views bounded inline and release that height bound in fullscreen.
  The kernel renderer must pass the negotiated Image variant through. Prompt
  guidance asks for one image view per figure, preserving the downloadable file.
- Region r7 proved actual Image rendering but reused an earlier PNG. Region r8
  generated a stylized diagram with invented coastline-like segments and lost
  the current response from view; both takes are retained, not published. Its
  real geographic refinement is retained too, but a development reload spoiled
  that recording. Region r9 generated a new PNG using OpenStreetMap coastlines,
  then displayed it with A2UI and opened its actual fullscreen control.
- A new image must use its own surface ID. In r11 the agent reused the source
  map ID, so the figure replaced the map in an earlier message. Prompt guidance
  now distinguishes a new figure from revisions of an existing figure. The r12
  agent created `dorian-stall-closeup-image` and preserved its source map.
- A successful Image view does not establish scientific quality. The r12 first
  figure used real but coarse Natural Earth 110m polygons and misplaced an
  island label. Retain that draft and show its actual geographic review rather
  than substituting an improved figure without the correction request.
- The Image fullscreen control must be exited with its actual **Exit full
  screen** button. Escape left that dialog open in the first r12 review attempt.
  Never type into a composer obscured by a media dialog; retain that failed take
  and restart from the same visible figure with the actual close action.
- A percentage image height bound does not fit a portrait image if the portal's
  immediate parent has auto height. Bound the fullscreen Image against the
  viewport minus its header and padding. The corrected portrait figure now fits
  from its title through both axes and the complete colour scale. The final
  opening is a separate real recording after this fix, not a composited image.
- Recorder IPC replies can be read while they are still being written. Write
  the reply to a temporary file and rename it atomically; clients retry a partial
  JSON parse. Attaching a second Puppeteer client must use `defaultViewport: null`
  to preserve the recorder's framing. Never resize the page during a take.

- A development temp directory is not a durable archive. Copy raw footage,
  timestamp marks, edits, dependency lock, captions, deliverables and evidence
  under `D:/Libraries/Videos/clio_recordings/`, then verify every copy by hash.
- Keep accepted and rejected takes clearly identified. The complete main region
  source is 471.9 seconds; its 90-second normalized editing copy is not the
  original. The separate refinement and corrected-result recordings complete
  the provenance of the published figure.
- Exclude browser profiles, credentials and installed dependencies. Store a
  readable procedure and exact manifests so a reshoot does not depend on chat
  history or the current running process.
