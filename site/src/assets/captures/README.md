# Current product captures

## Task walkthrough controls

The `docs-*` images accompany the task guides for comparison, refinement,
dashboard exports, and session exports. They show real development controls and
actual downloaded results using fictional bike counts. Comparison and refinement
stills are decoded from genuine Codex/Luna recordings. The complete shoot,
accepted and rejected takes, output files, captions, and review evidence are
documented in `site/video/edits/docs-20261010/README.md` and archived under
`D:/Libraries/Videos/clio_recordings/2026-10-10-docs-task-walkthroughs/`.

## Report overview and lineage

These light-mode browser captures show the development UI on 10 October 2026,
at one device pixel per CSS pixel. They are original product screenshots,
without painted controls, composited responses or private research data.

The fictional bike-station walkthrough is authored through the real
`create_artifact`, transform-recording and dashboard-publication tools. It is
not a model run. The source table branches into computed balance, availability
and capacity tables, then joins in the findings report. Only declared
relationships appear in Lineage.

| File | Viewport | What to inspect |
| --- | --- | --- |
| `current-dashboard.png` | 1440 x 1280 | Complete charts, map, legends and five-station table. |
| `current-data.png` | 1120 x 820 | All six registered files, including the source table. |
| `current-lineage.png` | 1440 x 560 | All nine nodes and ten relationships, with no clipped branch. |
| `current-report.png` | 1120 x 520 | The finding, its limits, and the three evidence tables. |

`capture-manifest.json` records source revisions and SHA-256 checksums. The
immutable originals and browser review screenshots are archived under
`D:/Libraries/Videos/clio_recordings/2026-10-10-website-showcase/`. Astro creates
responsive WebP derivatives; clicking a homepage image exposes its full-size
version.

Reproduce with `site/video/serve-website-showcase.py` in a new empty state
directory, then `site/video/review-current-showcase.mjs`. Both refuse private
session imports. The service bounds its private core to 64 MB and shuts it down
through `POST /review/close`; remove disposable state after review.

The separate Gantt/Timeline acceptance images use an explicitly labelled
fictional test fixture with four child agents and twelve tool calls. They test
the production React components, including duration geometry, agent filters,
keyboard activation and resize behavior. They are not website session captures
or evidence of fresh inference. The fixture lives only under the UI test tree.
## Memory recall — 10 October 2026

`docs-memory-search.png` is an original browser detail capture at two device
pixels per CSS pixel (1792 × 782), of the production
Memory settings UI searching retained fictional station conversations. It shows
the controls and the first of four matches. The matching Open link was followed
to the recorded message. No OPAL content, fabricated UI or new model inference
is included. The summary JSON under `public/examples/` is the actual read-only
tool result from that same retained session.

Raw contextual and detail screenshots, source responses and capture code are
archived under `D:/Libraries/Videos/clio_recordings/2026-10-10-docs-memory-evidence/`.

## Questions, operations, and injections — 12 October 2026

Four new light-mode detail captures accompany the end-user work guides:

| File | Source and meaning |
| --- | --- |
| `docs-question-choice.png` | A real native question from the retained public `question-04` qualification, replayed with the current production question response component. No answer is submitted. |
| `docs-question-visual-controls.png` | The actual website Choice Picker gallery, with Region selected through its control. An illustrative gallery view, not a pending agent question. |
| `docs-tool-call.png` | The actual recorded shell command, result, timing, and presentation from `docs-final-transcript.json`, replayed with the current compact tool component. The recorded successful result maps to the production `succeeded` state. |
| `docs-injection-detail.png` | The actual `loop_wake` text from that same transcript, expanded in the current production injection component. |

These are unmodified browser detail captures at 1.5 device pixels per CSS pixel.
The component replays preserve the recorded prompts, options, instructions,
commands, and results. They do not launch a model, rerun a command, or create a
fresh unanswered question. The website captions disclose the replay and gallery
sources. The guides also reuse the existing Evidence, conversation-family Gantt,
and Work captures with their recorded provenance.

Originals, selected source records, display projections, rejected framing,
browser review, and authoring notes are retained at
`D:/Libraries/Videos/clio_recordings/2026-10-12-user-work-guides/`.
`user-work-guides-manifest.json` records file checksums and source identity.
The initial guides were merged into develop in PR #1724. The gallery screenshot
is illustrative and is not visual-question submission evidence.

## Work guides visual expansion — 12 October 2026

Sixteen additional images replace the sparse arrangement in the four work
guides. Each page now introduces its subject visually before the reference
tables. `work-guides-visual-expansion-manifest.json` records the exact assets,
dimensions, checksums, source revisions and framing crops.

The native confirmation, text response, full visual question, open-question list,
todos, compact Work, Evidence inventory and report preview come from a new
isolated fictional bike-station documentation workspace. Real production tools
created its artifacts, chart surface, three durable async questions and five
todos. The real UI controls were opened, expanded and filled. No answer was
submitted and no model inference was started.

The command, wait and injection images replay retained public-example records in
the production components at UI commit `3d3ce9b2`. The unbounded wait uses the
actual `timeout_s: null` input and collected subagent result from
`delegation-acceptance-corrected/parent-transcript.json`. The loop wake and shell
operation are from the same turn in `long-horizon/docs-final-transcript.json`.
The goal judgment is shown separately. No source instruction or result text was
invented. These are still walkthroughs, not recordings of a new agent run.

Original browser captures, contextual frames, source excerpts, tool display
projections, authoring code, crop specifications and review evidence are under
`D:/Libraries/Videos/clio_recordings/2026-10-12-work-guides-visual-expansion/`.
Rejected edge crops and redundant frames remain in that archive; only the
manifest's selected images are installed in the site. The complete visual
question replaces the thin Choice Picker strip in the guide. A submitted
structured-answer-and-resumption video is still outside this capture's evidence.
