# Files, sources, and marketplace guides

These five short videos are **screenshot walkthroughs**, explicitly labelled in
the player and in the encoded image. They contain real application states with
reading holds and intentional cuts. They do not simulate clicks, typing, loading,
or agent responses. The existing widget interaction recordings remain unchanged.

## Authoring and originals

- Timeline: `source-marketplace-guides.edit.json` (source, pixel crop, seconds, caption).
- Renderer: `render-still-guide.py` (Pillow framing and FFmpeg H.264 encoding).
- Durable archive: `D:/Libraries/Videos/clio_recordings/2026-10-04-sources-marketplaces-1951/`.
- Untouched captures: archive `raw/`; editing code and versions: `authoring/`.
- Real example inputs: `source-data/`; rendered frame sequences and final media: `deliverables/`.
- Review images, encoded-media diagnostics, and build logs: `evidence/`.

Render from the repository root, using the archive path appropriate to the editing machine:

```powershell
uv run python site/video/render-still-guide.py site/video/source-marketplace-guides.edit.json "$archive/raw" "$archive/deliverables"
```

The timeline's font defaults to Windows Segoe UI. Change that path when editing
on another OS. Keep the same aspect ratio and inspect all captions after a font
change. Copy only the top-level delivered JPG/MP4/VTT files into `site/public/media`.
Rebuild the website before reviewing a static preview; it serves `dist`, not the
authoring output folder.

## Reshoot

Use the actual light theme in CLIO and the website. The accepted captures use a
dedicated browser tab at 1280 × 720, device scale 1. Keep original JPEG bytes.

1. Create an isolated workspace and register the example Project notes folder.
2. Capture + → Attach, local upload choices, and expanded Your sources.
3. Show each mapping choice. For Update originals on save, show the complete
   unchecked warning and disabled Link folder button; do not enable it for a demo.
4. Download an editable copy. Show the real folder attachment and an unsent request.
5. Type @brief, select the result, and open its chip in the canvas. Capture each
   completed state. The new-conversation page must actually open that reference.
6. Capture Sources management and the removal explanation; cancel the removal.
7. In marketplace settings, filter installed entries and open Details/Instructions.
8. Add the example Research examples collection to the demo workspace. Check Ready,
   its installed count, and the workspace-only installed row. Change the fixture
   from 1.0.0 to 1.0.1, Reload, then confirm the installed version changed.

The archive contains both marketplace fixture versions. No messages were sent
to an agent and no cloud source originals were modified for these walkthroughs.
The example marketplace/workspace remain available in the qualification preview
for reshoots; their IDs are recorded in the archive's capture context.

## Review and rejection notes

Captures 01–06 are retained early framing attempts, not publishing inputs.
They used an unsuitable scaled browser surface; 06 captured the wrong UI state.
Captures 11 and the first crops of 12/17 were rejected for clipping. The manifest
uses the complete warning in 33 and corrected composer crops. Capture 22 was
replaced by 38 so the filtered result is complete. Intermediate rendered outputs
are superseded; the archive retains the raw captures and final editable timeline.

Review every encoded shot/cut, captions, and the complete player playback both
embedded and fullscreen. Check mobile overflow, image enlargement, captions and
internal links separately from application regression tests. Never infer an
installation succeeded from a catalog's Available version alone.
