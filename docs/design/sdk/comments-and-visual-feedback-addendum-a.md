# Comments and visual feedback: addendum A

This addendum amends [comments-and-visual-feedback-plan.md](comments-and-visual-feedback-plan.md) so that every surface follows the semantics the A2UI region capture already has in gact-tui (`a2ui-region-capture.tsx`, `a2ui-region-evidence.ts`). The person enters a select mode, makes numbered marks with a comment each, and sends them together. The agent sees the labels, an overview with every mark drawn, and what lies under each mark. Where this addendum and the plan disagree, this addendum wins. Record each change as an `FB` decision in `docs/design/comments-ledger.md`, citing this addendum.

## What A2UI does today

Checked on gact-tui `941e6f21`:

- **Select mode.** "Select regions" on a surface turns it into a capture target. The person drags boxes, labelled `S1`, `S2` and onward, and each box has its own comment ("What should the agent notice here?"). A movable panel lists the marks, adds or deletes them, and sends them with "Add N regions to message".
- **The image.** `labelledPng` renders the whole surface, including map tiles and live WebGL canvases, and draws every box with its label badge.
- **The context.** `captureText` sends the labels and comments, then a JSON block with the surface, its revision, the component and its data reference, the active filters and selection, and per box its rectangle plus the data under it: visible text, chart rows, map features and mesh nodes.
- **Delivery.** The capture is a composer annotation attached to the next message. Nothing is stored.

## 1. One select session on every surface

**The panel.** The capture panel becomes the comment panel for every surface in the plan's modes table. It is generalized from `a2ui-region-capture.tsx`, not rewritten. The mode dropdown sits in the panel and applies to the next mark, so one session can mix regions, picked elements and text selections. Each mark gets its own comment field. "Send to agent" and the send control live in the same panel. With several marks the control reads "Send N comments" on an artifact and "Add N marks to message" elsewhere.

**Labels.** Marks are labelled `S1`, `S2` and onward within a session, as today. The same label appears on the badge in the viewer, in the panel, in the thread list and in what the agent receives. On an artifact, labels continue across sessions on the same version (a second session starts at `S4` if `S1` to `S3` exist), so a thread's label never changes.

**Batching.** The plan's pending review is this session. Closing the panel without sending keeps the marks as a draft on that artifact version. On a surface without a version, closing it discards the marks, as today. The plan's "Send N comments" control and its separate pending-review UI are replaced by the panel.

**Attention set.** "Add to attention set" stays where it is, beside the panel. It is not a comment.

## 2. What the agent receives

The plan's renderer changes in three ways.

**Labels, not numbers.** Entries are headed by their mark label (`S1 (region)`), not "Comment 1".

**An overview plus crops.** For every session the agent receives one labelled overview, the whole view with every mark drawn and labelled, in the style `labelledPng` draws today. Each region and element also gets its crop at 2x. The overview is the first image part, and the crops follow in label order. Paged documents get one overview per page that holds a mark.

**What is under each mark.** Each entry lists what lies under its mark, read by the surface's resolver:

| Surface | Under a region or element |
| --- | --- |
| A2UI surface | Visible text, chart rows, map features, mesh nodes (today's fields) |
| PDF and renditions | The text inside the box, and the paragraph, cell range or slide shape it belongs to |
| HTML artifact and browser page | Text inside the box and the elements it covers, as tag and accessible name |
| SVG image | The SVG elements inside the box, with their ids and text |
| Raster image | Nothing; the crop carries it |

The A2UI JSON block goes. Its fields become readable lines in the entry, cut to `comments.under_mark_chars` (default 1,200) per mark, with the rest written to a session file the entry names. This removes the last place where raw selection JSON reaches the model.

A session of two marks on a figure:

```
Review of figs/throughput.svg (artifact art_5d07, version 1, sha256 c41f...09bd).
Produced by: shell_bash call c_81f0 running scripts/plot_throughput.py
(reads results/sweep.csv). Change the script, not the image.
Image 1 is the figure with S1 and S2 drawn on it.

S1 (region, attached as image 2)
  Box:      x 65% to 97%, y 4% to 26%
  Under:    <g id="legend">: "burst buffer", "baseline"
  Comment:  Move the legend outside the plot.

S2 (element, attached as image 3)
  Element:  <polyline id="series-baseline">, selector #series-baseline
  Comment:  Use a dashed line so it prints in grayscale.
```

## 3. Where images come from

- **Artifacts** (documents, renditions, images, HTML files): the server makes the overview and crops from the version itself, as the plan says.
- **Browser pages:** the server makes them from a `browser_screenshot` taken when the session is sent.
- **A2UI surfaces:** the browser makes them, as `labelledPng` does today, since the surface lives only in the client. The resolver still adds the data under each mark from the surface's own data.
- **Agent answers in the transcript:** text marks only; no image.

## 4. Threads

On an artifact each mark is its own thread, labelled with its mark. One agent reply answers the whole session and is stored on every thread in it, as the plan already says. The before and after view and the re-anchoring rules are unchanged. On surfaces without a version, the session is an attachment and leaves no threads, as today.

## Changes to the defaults table

| Question | Default |
| --- | --- |
| Mark labels | `S1`, `S2` and onward per session, continuing per artifact version |
| Batching | One select session is one send |
| Images to the agent | One labelled overview per session (per page when paged), then a crop per region or element |
| Under-mark text | 1,200 characters per mark, the rest in a session file |
| Closing the panel unsent | Draft on an artifact version; discarded elsewhere |

## Scenario changes

K11 now reads: three marks of mixed modes on one PDF in one session, sent together. It passes when one turn carries the overview, three entries labelled `S1` to `S3` with their crops and the text under each, and no JSON, and each thread shows the reply and the new version.

Added:

| ID | Scenario | Passes when |
| --- | --- | --- |
| K21 | Today's A2UI capture on a chart and a map, through the generalized panel | The agent receives the labelled overview, the rows and features under each box as readable lines, and no JSON block; the behaviour matches the base otherwise |
| K22 | A region over a dense table in an HTML artifact | The text under the mark is cut at the limit and the entry names the session file holding the rest |
| K23 | Close the panel with two unsent marks on a document, reopen the document | Both marks come back as a draft with their labels and comments |
| K24 | A second session on the same artifact version after a session with `S1` to `S3` | The new marks start at `S4` |
