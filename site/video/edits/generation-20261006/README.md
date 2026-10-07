# Report and presentation generation recordings

These clips show fresh CLIO conversations generating files from two fictional
source files. They begin with the actual prompt and include real tool activity,
file arrival and opening the result. Brief result review occupies the final
15.15 seconds of the Word clip and 15.6 seconds of the presentation clip.

The operator prepared only `sensor-notes.md` and `sensor-summary.json`. CLIO
authored the document, chart, deck, speaker notes, PDFs and layout corrections
during the recorded sessions. No operator-authored document was substituted.

| Recording | Accepted native take | Published length |
| --- | --- | --- |
| Word report | `word-generation-20261006-take2.mp4` | 66.233 seconds |
| PowerPoint briefing | `slides-generation-20261006-take1.mp4` | 93.9 seconds |

The JSON edits select chronological portions of each continuous native take.
The pointer follows actual browser events. Typing/work acceleration is labelled
2–3x; idle gaps are omitted. Cropping makes prompts and tool review readable.
The published H.264 files are 1280 x 870, 30 fps, limited-range yuv420p with
fast-start metadata. FFmpeg encoding used one thread.

## Actual generation and correction

The Word session produced a two-page report with a native table and embedded
comparison chart. CLIO recovered from an invalid requested page range, rendered
the two pages and inspected their contact sheet before registering the outputs.
The published PDF is the version-bound preview opened in the recorded reader.

The PowerPoint session produced six slides with a native editable chart and six
nonempty speaker notes. Its first draft had poor cover contrast and a crowded
final heading. One correction request was submitted and appears in the clip.
After fixing the spacing, CLIO inspected its render and independently corrected
the remaining cover contrast. The final download is that corrected deck and
CLIO's published PDF. A later navy-specific clarification was drafted but never
submitted; that discarded typing and idle work are omitted from the edit.

The first Word take on the older service failed with empty required tool
arguments before any document was generated. It is retained as rejected evidence.
The first presentation edit was rejected during complete playback because a
caption obscured the correction prompt and its preview-opening interval started
too early. The accepted edit fixes both; the earlier encode and review remain
archived separately.

## Validation

- Actual generated files have two/six PDF pages; native structures, notes,
  fictional-data labels and source values were checked. Both Word pages and
  the full slide overview were visually inspected, with full slide detail for
  the corrected cover, comparison chart and next steps.
- Both final clips played completely in the website player and fullscreen.
  Decoded-frame contact sheets were reviewed chronologically. Playback completed
  without player/page errors. Desktop and 390px phone article views were reviewed
  in light and dark modes; there was no horizontal overflow.
- Astro build: 73 pages, all internal links valid. Astro check: zero errors and
  warnings, five existing hints. One focused `product showcase` Playwright case
  passed with one worker, including desktop/phone navigation and output downloads.
  Checks ran sequentially with 768 MiB build/check and 1 GiB browser heap caps.

The PDF render review does not establish acceptance in native Microsoft Office
or native CLIO Desktop. These are actual browser interactions with the isolated
CLIO service, using Codex/Luna and UI head
`0e843b3f9c3afa4436f6c66743c2b9eb654da97b`.

## Preserved evidence and reuse

Native takes, timestamps, accepted/rejected decisions, actual authoring scripts,
input hashes, generated files, render manifests, edit manifests, complete playback
review and build/test logs are preserved in
`D:/Libraries/Videos/clio_recordings/2026-10-06-document-generation`.
`proof.json` identifies the published files and hashes. The archive's
`SHA256.json` covers every retained file except itself. Browser profiles,
credentials and private service/account state are excluded.

Use the existing `capture.mjs` / `command.mjs` workflow in `site/video`, with the
browser-driver authorization already granted in this task. Start a fresh session
with only the same source files, record continuously before typing, and retain
the original take before editing. Put the native accepted takes in `takes/` and
render these manifests with `render-ffmpeg.mjs`; do not replace tool results with
scripted interface states. Capture used Chrome 157.0.8083.0, Puppeteer 25.10.0,
Node 22.22.3 and FFmpeg 7.1.1.
