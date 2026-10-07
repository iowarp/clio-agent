# File-guide recording refresh, 2026-10-06

These manifests edit continuously recorded browser footage. The camera sources
are native Chrome recordings of actual CLIO input, model turns and review
interactions, with an editorial pointer following real mouse events. Still
screenshots are review evidence and posters; they are never motion sources.
Cuts remove idle waits. The Word clip labels its selected 8x waiting interval;
input and result review remain at normal speed.

| Manifest | Delivery | Scope |
| --- | --- | --- |
| `current-pdf.edit.json` | `files-sensor-pdf-read` | Open a two-page report PDF, resize Files, scroll and zoom. |
| `current-word.edit.json` | `files-sensor-word-revision` | Reference and revise a structured Word report, then inspect the published two-page preview and Open in choices. |
| `current-slides.edit.json` | `files-sensor-slide-revision` | Revise a six-slide briefing, correct formatting after visual review, bind its PDF and inspect the saved result. |
| `current-opal.edit.json` | `files-opal-report-review-current` | Navigate the previously corrected three-page OPAL report and eleven-slide deck. This does not claim fresh generation. |

The guide's sensor values are fictional. The reference builders, original
editable files and corrected revisions are downloadable from the worked-example
page. Word retains native headings and tables; PowerPoint retains six slides,
one native chart, two editable tables and speaker notes. Source-file hashes were
checked against the shipped references; sources are unchanged.

The actual revision model was Codex/Luna at its default medium effort. Earlier
Word attempts were rejected and retained rather than presented as success. The
accepted Word take revises the existing recommendations table. In the slide
take, assigning shape text lost the template's run formatting. Overview review
caught that defect; a natural follow-up requested a revision from the original
deck using its existing runs. The corrected source was rendered and reviewed.
Another follow-up requested publication with its version-bound PDF, because an
independently exported PDF did not bind to the first source artifact. The exact
reviewed PDF was also copied into outputs by the operator after the model's
earlier copy approval was denied. The final preview-navigation footage is a
second continuous take of those corrected saved files. No model result or
successful tool call is fabricated in the edit.

## Reproduce the edit

Use a capture workspace whose `public/takes/` contains the named raw recordings.
Copy a manifest into its video directory, then run:

```powershell
node render-ffmpeg.mjs current-word.edit.json <site-media>/files-sensor-word-revision.mp4
node finish-current-file-media.mjs current-word.edit.json <site-media>
```

FFmpeg uses one worker, converts the input's declared colour range to limited
range H.264, and retains native interaction footage. The finishing helper uses
the actual encoded segment durations for VTT cues and extracts the poster from
the encoded movie. Do not infer the final duration from raw cut intervals alone.

The originals, rejected attempts, manifests, corrected files, native screenshots,
decoded playback contact sheets and hash manifest are archived locally in
`D:/Libraries/Videos/clio_recordings/2026-10-06-files-video-refresh`. Browser
profiles and private connection files are excluded. Review covers the actual
built website and browser UI at `gact-tui` commit
`ce62046333fc4583f4c23c4a4a935ed4c4755cf7`. It does not establish public deployment
or local native Desktop acceptance.
