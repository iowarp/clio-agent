# Comments and visual feedback: implementation plan

This plan gives every surface where CLIO shows the person something one way to point at a part of it and say what should change: select text, drag a region, or pick an element. It covers documents, images an agent produces for a paper or a talk, HTML artifacts, pages in the in-app browser, A2UI surfaces and the agent's own answers. It takes over the comment system from the LaTeX and comments plan and workstream 5 of the browser plan, so neither has to wait for the other. It has six workstreams, and each builds on the ones before it:

1. **Removing the embedded Office editors.** The ONLYOFFICE and Collabora integrations are deleted from both repositories. Office files are shown through their LibreOffice PDF rendition, keep their native comments, and still open in the desktop's own application. This moves here from the LaTeX plan because it clears the viewer this plan rebuilds.
2. **One anchor model.** Every comment, stored or attached, points at its target with one `DocumentAnchor`. Text, region and element anchors carry what is needed to find the target again and to show it to the agent. Images get their own document profile.
3. **One comment box and one thread model.** Every viewer offers the modes its format supports through one comment control. Comments on artifacts are stored as threads bound to a version, with replies and re-anchoring. Comments on things that are not versioned (a live page, a transcript, an A2UI surface) travel as attachments to the next message.
4. **What the agent receives.** The server resolves every anchor and renders readable entries with the selected text or element, the context around it, a cropped image of the area, and, for an image or HTML file a script produced, the script that produced it. The raw anchor JSON goes.
5. **Figures made on the fly.** The look-comment-revise loop for figures in papers and talks: a region comment on a plot names the script behind it, the agent edits the script, the new version links back to the thread, and the thread shows before and after.
6. **Pages in the in-app browser.** The same three modes on a live page, with the comment reaching the agent as a target its script can act on.

The work runs to completion without stopping for review. The human reviews the finished branches.

## Contract

This plan is the contract for the six workstreams. The megachange design documents still govern everything they cover. Decisions D1 to D29 in `acceptance-ledger.md`, the F decisions of `delegation-fork-science-plan.md`, the M decisions of `long-term-memory-plan.md`, the model fleet decisions, the TD decisions of `tool-calling-plan.md` and the BR decisions of `browser-ledger.md` stay in force. When code and this plan disagree, change the code. If the plan turns out wrong, choose the fix that best keeps its intent, update this document in the same change, and record the decision in the ledger.

| Document | Governs |
| --- | --- |
| This plan | Office editor removal, the anchor model, the comment box, threads, review dispatch and rendering, figure feedback, comments on browser pages |
| `docs/design/comments-ledger.md` (new) | This plan's decisions and acceptance evidence |
| `latex-and-document-comments-plan.md` with its addendum A | Tectonic builds, templates, SyncTeX mapping and the LaTeX workspace; it plugs its `source-map` resolver into this plan's resolver registry |
| `browser-python-control-plan.md` with addenda A and B | The browser service, its tools and the live view; workstream 5 of that plan is replaced by workstream 6 here |
| `artifact-provenance-design.md` and `gact/artifacts/` | Artifact versions and lineage, which "Produced by" reads |

**Base.** The head of the browser branch, `claude/gracious-goldberg-gagoo4`, in clio-agent, gact-tui and clio-agent-marketplace (read at clio-agent `c90ca484`, which has merged the tool-calling branch, and gact-tui `941e6f21`). Workstream 6 needs `gact/browser/` and the live view, which exist only there. Rebase onto `develop` once the browser branch merges. If the LaTeX plan's branch exists when this work starts, coordinate through the resolver registry below rather than waiting.

Decisions use the prefix `FB`; qualification scenarios use `K`. Neither collides with another plan.

## Rules

The megachange rules carry over unchanged: delete and replace, the task layer is the only execution authority, the model decides, no silent fallback, one mechanism per concept, no fifth store, the repository rules in `AGENTS.md`, and evidence over claims. Five more apply here:

1. **One anchor model.** Document reviews, composer annotations, A2UI region captures and page comments each define or capture their own selection today. After this change they all use `DocumentAnchor`, and the duplicate types and capture code are deleted.
2. **One comment box.** Every surface uses the same component, the same mode control and the same renderer for the agent. No surface gets its own dialog.
3. **Source is canonical.** A comment on a rendition, a compiled PDF or a script's output is about the source when there is one, and the agent is told which source. The agent never edits a rendition, a PDF that has a source, or the pixels of an image a script produced.
4. **The viewer stays a thin client.** Anchor resolution, cropping for the agent, re-anchoring and "Produced by" run on the clio-agent server. The UI captures selections and draws what the server returns.
5. **Frames stay opaque.** The host never reads a sandboxed HTML frame's DOM. Everything the picker reports comes from the picker script inside the frame, over the existing channel protocol.

## Out of scope

Do not implement, stub or reserve fields for these:

- Comments between people: mentions, notifications, assignment, or a second person replying. A thread holds the person's comment and the agent's reply.
- Real-time co-editing, and typing into document source in the UI. Those belong to the document editing rework.
- Comments on a 3D camera pose or on a time step of a running simulation. A region on an A2UI surface captures what is on screen, as it does today.
- Comments on the person's own desktop through desktop control.
- Voice comments.
- Comments on video or audio.
- Comments inside the `cdp` browser backend's real Chrome window. Page comments are made in CLIO's live view of the page.
- Exporting threads into a document's native comment format (writing Word comments back).

## Current state

What exists at the base, read on `claude/gracious-goldberg-gagoo4` at `c90ca484` and gact-tui `941e6f21`, so nothing is rebuilt and nothing is missed:

- **Profiles and anchors.** `gact/documents/models.py` defines `AnchorProfile` (`text-quote`, `pdf-quad`, `dom`, `sheet-range`, `slide-shape`, `native-comment`, `source-map`) and one `DocumentAnchor` with fields for all of them. `gact/documents/profiles.py` gives HTML `text-quote` and `dom`, PDF `text-quote` and `pdf-quad`, and LaTeX `text-quote` and `source-map`. There is no image profile: `.png`, `.jpg` and `.svg` fall to `binary`. There is no region anchor beyond `pdf-quad`.
- **Reviews.** `ArtifactReview` binds one anchor and a comment to an artifact id, version and SHA-256, with statuses `queued`, `dispatched`, `human-note`, `failed` and `stale`. Creating one dispatches it at once as an agent turn. `_dispatch_review` in `gact/routes/documents.py` posts a text part from `_review_prompt`, which dumps the anchor as raw JSON followed by the comment, plus a structured `artifact_review` part the model does not read. A region carries no image. Nothing ties the agent's answer back to the review.
- **Native comments.** `gact/documents/native_comments.py` reads comments from Word, Excel, PowerPoint and OpenDocument files, and comments starting with `@clio` become agent instructions.
- **Capture in the UI.** `document-workspace.tsx` and `document-pdf-viewer.tsx`: selecting text in Markdown or HTML produces a `text-quote` with `exact` only. Selecting text in a PDF produces a `pdf-quad`. Nothing produces a dragged region on a document, a `dom` anchor, or a `source-map` anchor. A "Review selection" button opens a dialog, and reviews are listed in a separate Reviews tab. Nothing is drawn in the document. `artifact-image-preview.tsx` and `image-resource-view.tsx` show images with no comment control.
- **HTML preview.** `html-preview.tsx` renders authored HTML in an opaque sandboxed frame. `html-preview-bridge.ts` injects a host script that speaks `clio.html-preview.height`, `mounted`, `theme` and `read-text` over a channel-scoped `postMessage`. There is no picker.
- **Chat annotations.** `lib/composer-annotations.ts` defines `text-quote` (a run of an agent answer), `data-zone-quote` (a chart, map or table zone) and `region-capture` (a dragged screenshot of an A2UI surface, from `a2ui-region-capture.tsx` and `use-region-capture-annotation.ts`). They travel as message text, not as anchors.
- **Office editing.** The ONLYOFFICE and Collabora editors are still present: `gact/documents/editors.py`, `editor_callbacks.py`, the editor and WOPI routes in `gact/routes/documents.py`, `EditorProvider` and the editor-session models in `models.py`, `embedded_editors` in `profiles.py`, the capability entry in `gact/routes/system.py`, the client SDK in `client/client.py` and `client/types.py`, and their tests; in gact-tui the editor component, launch and open-menu paths (`document-launch.ts`, `document-open-menu.tsx`), the editor branch of `document-workspace.tsx`, `document-working-copy.tsx`, the `packages/core` editor types and `contract/SPEC.md`.
- **Lineage.** `ArtifactRegistry.transforms_generating(artifact_id)` (`gact/artifacts/registry.py`) returns the transforms that produced an artifact. Each `TransformRecord` (`gact/artifacts/transforms.py`) carries the call id, session, tool, and an `instrument` built by `_script_instrument` from the tool name, its arguments and the files it used.
- **The browser.** `gact/browser/` serves the browser tools (`browser_open`, `browser_navigate`, `browser_snapshot`, `browser_read`, `browser_act`, `browser_wait`, `browser_screenshot`, `browser_evaluate` and the rest), the host gate and the live view (`live.py`; gact-tui `browser-surface.tsx`, `browser-live-view.tsx`, `browser-live-input.ts`). It injects no script into pages and exposes no binding. The agent's cursor and element highlight are drawn only during agent calls. Ledger rows W13 and W14 are `pending` on the comment system.

## Design: removing the embedded Office editors

As written in the LaTeX plan's "Design: removing the embedded Office editors", which this plan now owns. Every piece listed under Office editing above is deleted. Office and OpenDocument files keep their profiles, their LibreOffice PDF rendition as the in-app view, native comment ingestion with `@clio`, working copies and opening in the desktop's own application. The capability advertisement and `contract/SPEC.md` drop the editor entries in the same change. Nothing keeps a disabled or hidden editor path.

## Design: one anchor model

**Profiles.** A new document profile `image` covers `.png`, `.jpg`, `.jpeg`, `.webp` and `.gif` with the `region` anchor. `.svg` joins `image` with `region` and `dom`, since an SVG figure has elements worth picking (a legend, an axis, a series). HTML gains `region`. PDF and every Office rendition gain `region`. Markdown keeps `text-quote`.

**Anchors.** `AnchorProfile` gains `region` and keeps the rest. `DocumentAnchor` is completed, not duplicated:

| Anchor | Fields that must be set | Captured on |
| --- | --- | --- |
| `text-quote` | `exact`, `prefix`, `suffix` (up to 64 characters each), `start` and `end` in the rendered text | Markdown, HTML, agent answers, browser pages |
| `pdf-quad` | `page_index`, `quads`, and `exact` when the selection has text | PDF and renditions |
| `region` | `page_index` when paged, `rect` (normalized x, y, width, height), the natural size of what was shown, and `crop_asset` (the cropped PNG at 2x, stored as a session asset) | Images, PDF pages, HTML, A2UI surfaces, browser pages |
| `dom` | `selector`, `tag`, `text_excerpt`, `html_excerpt` (truncated to `comments.html_excerpt_chars`, default 400), `rect`, `viewport_width`; an image also carries `src` (its `currentSrc`), with its alt as `text_excerpt`; a browser page element carries its accessibility target as `source.ax` (`role`, `name`) | HTML, SVG, browser pages |
| `source-map` | `source_path`, start and end line, the outline node | LaTeX (resolved by the LaTeX plan's resolver) |

`quads` stay as they are. `rect` is one new field used by `region` and `dom`. A browser page anchor also carries `source.page` (the page id, URL and title) and `source.captured_at`.

**Resolvers.** `gact/documents/anchors.py` holds a registry of resolvers by document profile. A resolver does two things: it resolves an anchor against a version (what the agent sees, below), and it re-anchors an anchor onto a newer version. The built-in resolvers cover text, PDF, region, DOM and Office renditions. The LaTeX plan registers its `source-map` resolver here; until it does, a comment on a LaTeX PDF resolves like any PDF comment.

**Chat annotations move onto the model.** `text-quote` on an agent answer, `data-zone-quote` and `region-capture` become `DocumentAnchor`s with a `source` naming the message, the A2UI surface and zone. Their separate types in `composer-annotations.ts` and `use-region-capture-annotation.ts` are deleted, and `a2ui-region-capture.tsx` becomes the region capture used everywhere (below), not a second one.

## Design: one comment box and one thread model

**Modes per surface.** Each viewer offers the modes its format supports in a small dropdown beside the comment control. Unsupported modes are not shown. Text stays the default where text exists; region is the default on images.

| Surface | Text | Region | Element |
| --- | --- | --- | --- |
| Markdown preview | Yes | | |
| PDF, Word, Excel and PowerPoint renditions | Yes | Yes | |
| Raster image | | Yes | |
| SVG image | | Yes | Yes |
| HTML artifact | Yes | Yes | Yes |
| Browser page | Yes | Yes | Yes |
| A2UI surface | | Yes | |
| Agent answer in the transcript | Yes | | |

**Region capture.** One drag tool, generalized from `a2ui-region-capture.tsx`, draws a rectangle over any viewer and reports it in the viewer's own coordinates (page and normalized rectangle for a PDF, natural pixels for an image, CSS pixels for HTML and pages). The server makes the crop from the version itself (the rendition page, the image file, a server-side render of the HTML, a fresh page screenshot), never from what the person's screen happened to show, so the agent's crop is sharp and matches the version.

**The element picker.** A picker script joins the HTML frame's host script and speaks `clio.html-preview.pick.start`, `pick.hover`, `pick.selected` and `pick.cancel` on the existing channel. Hovering outlines the element under the pointer; clicking reports the selector (an `id` first, then stable data attributes, then the shortest unique path), tag, text excerpt, truncated `outerHTML` and the rectangle. In an SVG it picks SVG elements the same way. The same script is what browser pages use (workstream 6).

**The comment box.** One component, opened next to the selection: a text field and a "Send to agent" checkbox, on by default. Unchecked, the comment is stored as a `human-note` and the agent is not asked to act.

**Batching.** Several comments made in a row on one artifact collect as a pending review with a "Send N comments" control and go to the agent as one turn. A single comment can still be sent at once.

**Stored threads or attachments.** A comment on an artifact (a document, an image, an HTML file) is stored as a thread on that artifact version through `ArtifactReview`, extended with a thread id and a reply. A comment on something that is not a versioned artifact (a browser page, an agent answer, an A2UI surface) is an attachment to the next message, rendered the same way, and is not stored as a review. A transcript is not an artifact, and a live page has no version.

**Replies.** The agent needs no tool to answer. When the turn a review started finishes, its final message is stored as the reply on every thread in that review, and the version the turn produced, read from the artifact change feed, is linked from each thread. A turn that produced no new version links none.

**Threads in the document.** Comments are drawn where they point: highlights for text, outlines for regions and elements, a margin marker per thread. A thread holds the comment, the agent's reply and a link to the version it made. The Review selection dialog and the Reviews tab are replaced by this layer and a thread list in the side panel. Status is shown as icons with detail on hover.

**New versions.** When the agent makes a new version, each open thread is re-anchored by its resolver. A text anchor moves by its exact text and context; an element by its selector and text; a LaTeX anchor through the new SyncTeX index. A region does not move on its own, because the same rectangle on a changed image or page is not the same thing. A region thread stays on the version it was made on and shows that crop beside the same rectangle on the new version, so the person sees before and after and closes the thread or comments again. A thread that re-anchors cleanly is marked moved; one that does not is marked outdated against its version. Nothing is carried to a new version without being marked.

**Office formats.** Native comments keep working with `@clio`. The rendition takes text and region comments like any PDF, and the resolver maps a rendition comment back to the paragraph, cell range or slide and shape it came from, using the existing `sheet-range` and `slide-shape` anchors.

## Design: what the agent receives

**The renderer.** `_review_prompt` and its JSON dump are replaced by one renderer in `gact/documents/review_render.py`, used for stored reviews and for attachments. It resolves every anchor first and writes one header per target and one numbered entry per comment. A batch is one turn. The structured `artifact_review` part stays for the UI and the thread. Each entry carries:

- the mode, the selected text or element, and the comment;
- the text before and after a text selection;
- the source location when the target has a source (a LaTeX file and lines, the paragraph or slide and shape in an Office file);
- for a region or an element, the server's crop at 2x as an image part referenced from the entry;
- for an image or HTML file a transform produced, a "Produced by" line naming the tool call and the script it ran, from `transforms_generating`; when the producer was `run_code`, the cell; when no transform is recorded, the line says the producer is unknown rather than leaving it out.

A text comment on an Office rendition:

```
Review of report.docx, viewed as its PDF rendition
(artifact art_2b71, version 4, sha256 9c1e...77ad). Edit report.docx, not the PDF.

Comment 1 (text)
  Selected: "throughput rose by 40% in all configurations"
  Before:   "...after enabling the burst buffer, "
  After:    ". The exception is the 4 KB case."
  Source:   paragraph 31, under heading "Results"
  Comment:  Not all of them. Say which, and cite Table 2.
```

A region comment on a figure a script made:

```
Review of figs/throughput.png (artifact art_5d07, version 2, sha256 c41f...09bd).
Produced by: shell_bash call c_81f0 running scripts/plot_throughput.py
(reads results/sweep.csv). Change the script, not the image.

Comment 1 (region)
  Region:   x 8% to 52%, y 11% to 39% of the image (attached as image 1)
  Comment:  The legend covers the 1024-node bars. Move it outside the plot.

[image part: the cropped region at 2x]
```

An element comment on an HTML artifact:

```
Review of dashboard.html (artifact art_19ab, version 3, sha256 77c0...e512).

Comment 1 (element)
  Element:  <button id="run-sim">, selector main > section.controls > #run-sim
  Text:     "Run simulation"
  HTML:     <button id="run-sim" class="btn primary" disabled>Run simulation</button>
  Box:      412 x 36 px at (96, 540) in a 1280 px wide view (attached as image 1)
  Comment:  Enable this once a mesh is loaded, and say what it runs.
```

## Design: figures made on the fly

**Figures inside documents.** A region on a rendition page that falls on an embedded picture names that picture: in a PowerPoint or Word file by its slide and shape or its inline position, read from the file's own structure; in a LaTeX PDF through the LaTeX resolver's `\includegraphics`. When the embedded picture's bytes match a workspace image artifact by SHA-256, the entry also names that file and its "Produced by" line, so a comment on a chart in a slide deck reaches the script that drew the chart.

**The loop.** The `visuals` plugin's existing `review-visual-presentation` skill, which already teaches the inspect, change and re-inspect loop, gains a section on acting on a figure comment: change the producing script, rerun it, look at the new image with `view_image`, and only then reply. It adds no tool. When the rerun produces a new version of the image, the thread links it, and a deck or paper that embeds the image is rebuilt by the agent as its own skill says.

**Before and after.** A region thread on an image shows the crop from its version beside the same rectangle on the newest version, with a toggle between the two whole images. This is the view the region re-anchoring rule above calls for, not a second one.

## Design: pages in the in-app browser

**Modes.** The Browser surface's comment control offers text, region and element. For element mode the service injects the picker script into managed pages with `add_init_script` and receives its reports through `expose_binding`. The live view forwards pointer positions while picking and draws the hover outline from the picker's rectangles. Region mode drags a rectangle on the live view, mapped to page CSS pixels. Text mode reports the page's selection through the picker script.

**What the agent receives.** A page comment is an attachment to the next message, rendered by the same renderer. Each entry carries the page id, URL and title, the mode, the selected text or element, the comment, the server's crop from a fresh `browser_screenshot` of that area, and, for an element, a target spec the agent's script can use directly. The spec is built on the server from the element's role and accessible name, read through Chromium's accessibility tree for that node, falling back to the picker's selector:

```
Comment on page p2, https://example.org/settings (element)
  Element:  <button>, role button "Save changes"
  Target:   {"steps": [{"by": "role", "role": "button", "name": "Save changes"}]}
  Text:     "Save changes"
  Box:      132 x 36 px at (980, 640) in a 1280 px wide view (attached as image 1)
  Comment:  This is disabled after I edit the email field. Find out why.
```

**Pins and anchors that move.** While a page comment session is open, the controlling live view has the page follow its element and text marks (`marks.watch` on the live socket). The picker script finds each again after a reload, a navigation back or a change to the page: an element by its selector (checked against `src` for an image), then by image source, accessible role and name, or text; a text mark by its quote, scored by prefix and suffix. It reports where each is (`marks`), and the view pins it there with its label, as a region is pinned where it was drawn. A mark the page cannot find gets the note "Couldn't find this anchor on the page", like a comment on an older version of a file, and an element found under a new selector takes that selector. Marks made on another address are not pinned until the page is back on it. When the comment is sent the server locates each mark the same way: the entry says the element was found again by its image source, role and name, or text, or carries a note that the element or text is no longer on the page.

**Who sees it.** Page comments are made only by the viewer that sends input to the page, which addendum A of the browser plan defines as the first viewer. The `cdp` backend's real Chrome window has no picker; its live view in CLIO does.

**The browser ledger.** W13 and W14 in `browser-ledger.md` are replaced by K17 and K18 here, and the browser ledger points at this one.

## Defaults for the open questions

Take these and record each in the ledger.

| Question | Default |
| --- | --- |
| Default mode | Text where text exists; region on images and A2UI surfaces |
| Send to agent | On by default; off stores a human note |
| Batching | Comments queue as a pending review; one comment can be sent at once |
| Region on a new version | Stays on its version, shown beside the same rectangle on the new one |
| Crop source | The server, from the version itself; never the person's screen |
| HTML excerpt length | 400 characters |
| Context length for text | 64 characters before and after |
| Page comments | Attachments, not stored reviews |
| Producer unknown | Said so in the entry |
| SVG | Region and element |

## Phase 0: inventory and decisions

No production code changes in this phase.

1. Confirm the editor inventory against the base, including the marketplace and any install or Docker files, so Phase 1 removes everything in one change.
2. List every place that defines or captures a selection, an annotation or a region in gact-tui and clio-agent, so Phase 2 deletes all the duplicates.
3. Confirm that a figure a script writes into the workspace (through `shell_bash` and through `run_code`) is registered as an artifact version with a generating transform whose `instrument` names the script, and record what `transforms_generating` returns for each. If one of the two paths records no transform, fix it in this plan rather than leaving "Produced by" empty.
4. Pick the server-side HTML render for crops (the browser service's managed Chromium, which is already installed on first use, or the rendition service) and record it.
5. Check how to read an embedded picture's bytes and position from a rendition region in PowerPoint and Word files, and record the approach.
6. On the browser branch, confirm `add_init_script` and `expose_binding` work with the managed backend and the live view, and that Chromium's accessibility tree for a node gives role and name through CDP.
7. Write `docs/design/comments-ledger.md` with the megachange ledger's columns, decisions FB1 onward and the scenarios below. Commit, push, open draft PRs, and continue.

## Phase 1: remove the embedded editors

1. Delete the editor code, routes, models, capability entry, client SDK methods and types, environment variables and tests in clio-agent.
2. Delete the editor component, launch and open-menu paths, the editor branch of `document-workspace.tsx`, the `packages/core` types, the contract text, and their tests and fixtures in gact-tui.
3. Open a `.docx`, a `.pptx` and an `.odt` in the restarted stack and confirm each shows its rendition and offers only desktop-app opening.

## Phase 2: the anchor model and resolvers

1. The `image` profile, `region` on every profile above, `rect`, `crop_asset` and the completed `text-quote` and `dom` fields.
2. The resolver registry and the built-in resolvers, with re-anchoring and the moved and outdated states.
3. Server-side crops for images, PDF pages, renditions, HTML and A2UI surfaces.
4. Move the chat annotations onto `DocumentAnchor` and delete their types.

## Phase 3: threads and dispatch

1. Thread ids and replies on `ArtifactReview`, pending reviews and batch dispatch with one turn per batch.
2. The renderer, with source locations, crops as image parts and "Produced by"; delete `_review_prompt`.
3. Storing the turn's final message as the reply and linking the produced version.
4. Attachments for comments on pages, answers and A2UI surfaces, through the same renderer.

## Phase 4: the UI in gact-tui

1. The comment box and the mode dropdown, shared by every viewer and the transcript.
2. The region tool generalized from `a2ui-region-capture.tsx`, on PDFs, renditions, images, HTML and A2UI surfaces.
3. The picker script in the HTML host script, the `pick.*` messages, and SVG picking.
4. The in-document comment layer, the thread list, before and after for region threads. Delete the Review selection dialog and the Reviews tab.

Verify each UI change as the megachange plan describes: restart the stack, run `node scripts/cloud/ui_check.mjs` with a `--steps` script at 1440 by 900 and 390 by 844, read every screenshot, and check that `consoleErrors` and `failedRequests` are empty.

## Phase 5: figures and pages

1. Embedded-picture naming for renditions and the hash match to workspace images.
2. The figure-comment section of `review-visual-presentation` in the `visuals` plugin.
3. The picker in managed browser pages through `add_init_script` and `expose_binding`, the three modes in the Browser surface, page crops and target specs.
4. Replace W13 and W14 in the browser ledger with pointers to K17 and K18.

## Phase 6: qualification

Run focused tests and `ruff` on touched paths while working. Then run these against a freshly restarted stack (`claude_code` provider), driving the real UI, and record each in the ledger with commands, handles and the screenshots read. Each request is what a person would type, naming no tool.

| ID | Scenario | Passes when |
| --- | --- | --- |
| K1 | Open a `.docx`, a `.pptx` and an `.odt` | Each shows its rendition; no embedded editor is offered |
| K2 | A text comment on a sentence in a Markdown report, sent | The agent receives the entry with before and after text and no anchor JSON, edits the source, and the thread shows its reply and the new version |
| K3 | Ask the agent for a throughput plot from a CSV, then a region comment on its legend | The entry has the crop as an image part and a "Produced by" line naming the script; the agent edits the script, reruns it and looks at the new image; the thread links version 2 and shows before and after |
| K4 | The same as K3 when the agent drew the plot inside `run_code` | "Produced by" names the `run_code` cell |
| K5 | An element comment on a series in an SVG figure | The anchor's selector matches one element, and the agent's change affects that series |
| K6 | An element comment on a button in an HTML artifact | The selector matches exactly one element, and the agent's edit changes that element |
| K7 | A region comment on part of an HTML artifact | The crop comes from the server's render of that version |
| K8 | A region comment on a chart on slide 4 of a `.pptx` whose picture is a workspace PNG | The entry names slide 4, the shape, the PNG and its producing script; the agent fixes the script and rebuilds the deck |
| K9 | A text comment on a `.docx` rendition | The entry names the paragraph, and the agent edits the `.docx` |
| K10 | A native `@clio` comment in a `.docx` | It becomes a review as today |
| K11 | Three comments on one PDF queued and sent together | One turn carries all three as numbered entries; each thread shows the reply and the new version |
| K12 | A comment with "Send to agent" off | Stored as a human note; no turn starts |
| K13 | The agent edits the document after K2 and K11 | Text threads that still match are marked moved; one whose text was rewritten is outdated; region threads stay on their version with before and after |
| K14 | "Add to chat" on an agent answer, and a region on an A2UI chart | Both use the shared comment box, travel as attachments to the next message, and render as entries |
| K15 | A figure produced by a call whose transform was not recorded | The entry says the producer is unknown |
| K16 | Any viewer at 390 by 844 | The comment box, mode control and thread list fit; a region can be dragged with touch |
| K17 | An element comment on a fixture button in the in-app browser | The agent receives the entry with the crop and a role-and-name target spec, and its script acts on that exact element |
| K18 | A region comment over the fixture table in the in-app browser | The crop arrives as an image part and the agent answers about that area |
| K19 | A text selection on a browser page | The entry carries the selected text with before and after |
| K20 | Search both repositories for the editor names, routes and variables, the old annotation types, `_review_prompt`, the Review selection dialog and the Reviews tab | Nothing remains outside the ledgers and this plan |

The cloud session cannot qualify the Tauri desktop or Windows and macOS installs; those rows are recorded as local phase. Nothing else may be blocked. A failing scenario is fixed, not deferred.

## Done

1. Every phase is implemented, and the ONLYOFFICE and Collabora integrations, `_review_prompt`, the duplicate annotation types, the Review selection dialog and the Reviews tab are gone.
2. Focused tests pass, and `ruff check` and `ruff format --check` are clean on touched paths.
3. Every cloud-phase scenario passes against a freshly restarted stack, each shown with commands, handles and the screenshots read; local-phase rows say so.
4. The ledger holds every decision with its evidence, and the browser ledger's W13 and W14 point here.
5. The branches are pushed in clio-agent, gact-tui and clio-agent-marketplace, the submodule pins point at the pushed commits, and the draft PRs carry a summary and the evidence.

## Working conventions

- Use the branch the cloud session is set up to push to, with the same name in clio-agent, gact-tui and clio-agent-marketplace, and write it in the ledger header. Open draft PRs against `develop`.
- Small Conventional Commits per step, with test evidence in each PR description.
- Keep runtime state in `.cloud-run/`. Stop the stack when you finish a work block.
