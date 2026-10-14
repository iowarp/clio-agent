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

## Obsidian vault setup — 13 October 2026

The knowledge-source guide adds four production-browser captures: creating a
new vault, attaching an existing Markdown folder, the Obsidian row in Data
sources, and the installed official Sync client ready for sign-in. They use
light mode at two device pixels per CSS pixel, with fictional names and notes.
CLIO created the new vault through its actual connection form; the folder
picker attached a normal Markdown folder read only without adding `.obsidian`.
No remote Sync account was used in these captures.

The four graph, file-list, and tool images retain the fictional notebook captures
from 11 October. The tool image is that recorded actual Codex/Luna turn, not a
new model run. `knowledge-source-manifest.json` records all eight image hashes,
source revisions, and the unchanged example ZIP. Originals, conversion code,
rendered-page review, and verification receipts are archived under
`D:/Libraries/Videos/clio_recordings/2026-10-13-obsidian-managed-vaults/`.
The personal-vault acceptance capture and file manifests remain private and are
excluded from the website and public capture archive.
