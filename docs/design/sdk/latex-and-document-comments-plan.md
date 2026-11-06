# LaTeX builds and document comments: implementation plan

This plan covers two items on the CLIO 1.0 list: LaTeX, and feedback inside documents. It has six workstreams, and each builds on the ones before it:

1. **Removing the embedded Office editors.** The ONLYOFFICE and Collabora integrations are deleted from both repositories. Office files are shown through their LibreOffice PDF rendition, keep their native comments, and still open in the desktop's own application.
2. **Bundled LaTeX builds.** Tectonic ships with clio-agent. A paper with several source files, a bibliography and figures builds to PDF, and the agent gets structured diagnostics instead of a raw log.
3. **Paper templates.** IEEE and ACM templates work out of the box, and the agent can check a build against a venue's rules (page limit, anonymity, required packages).
4. **Source and PDF mapping.** A LaTeX paper gets the explicit structure we already have for Word and PowerPoint: an outline of sections, figures, tables, equations, labels and citations, each tied to its source lines and to where it lands in the PDF.
5. **One comment system.** Every document viewer offers three ways to point at something: select text, drag a region, or pick an HTML element. They share one comment box, one stored anchor and one thread model, and the chat's existing "Add to chat" annotations move onto the same model.
6. **The LaTeX workspace in the UI.** Source and compiled PDF side by side, click-through in both directions, build problems linked to their lines, and comments drawn in the document instead of in a separate tab.

## Contract

This plan is the contract for these six workstreams. The megachange design documents still govern plugins, the SDK and flows, and decisions D1 to D29 in [acceptance-ledger.md](acceptance-ledger.md) stay in force. When code and this plan disagree, change the code. If the plan turns out wrong, choose the fix that best keeps its intent, update this document in the same change, and record the decision in this plan's ledger. Decisions use the prefix `L`, so they cannot collide with the megachange (`D`) or the delegation and science plan (`F`).

Base: `claude/sdk-plugins-flows` in clio-agent, gact-tui and clio-agent-marketplace (read at clio-agent `fef65d1`, gact-tui `e9a3e21`, marketplace `a0737c3`). If the team has merged it, branch from its merge target instead. The document code on that branch matches `main`.

## Rules

The megachange rules carry over: delete and replace, no silent fallback, the repository rules in `AGENTS.md`, and evidence over claims. Four more apply here:

1. **One compile path.** The rendition service and the `author-latex-pdf` skill call Tectonic separately today. After this change there is one build function in clio-agent, exposed to the agent as a tool and used by the rendition service. The skill's `render_latex.py` is deleted.
2. **One anchor model.** Document reviews and composer annotations each define their own text-quote today. After this change both use `DocumentAnchor`, and the duplicate types and capture code are deleted.
3. **Source is canonical.** A comment on a compiled PDF resolves to source through the mapping. The agent never edits a PDF that has a source.
4. **The viewer stays a thin client.** Compiling, mapping and venue checks run on the clio-agent server. The UI only displays PDFs, outlines and anchors it receives.

## Out of scope

Do not implement, stub or reserve fields for these:

- An offline package bundle, cache pre-warming, or any mechanism for building on machines without network access. A build that needs a package it cannot download fails with a message that says so (below). This is a rare case: on an HPC node the agent's output is the source text, and the visual side of compiling serves the UI, which runs where the network is.
- Shell escape. Packages that need it (`minted`, `pythontex`, `gnuplottex`) are reported as unsupported.
- A full TeX Live install, or using a system TeX Live in preference to the bundled Tectonic.
- TeX compiled to WebAssembly in the browser. It would put the compiler in the client.
- Voice comments, and comment notifications to other people.
- Real-time co-editing between people (several users typing in one document with live cursors). Between the agent and the user there is nothing to merge in this plan: the user comments and the agent edits, each agent edit is a new immutable version, and comments re-anchor or are marked outdated. Live agent and user editing of the same source belongs to the document editing rework, together with typing in the source pane.
- Taking comments on pages in the in-app browser. That browser is its own 1.0 item; it reuses the element picker from this plan when it lands.

## Current state

What exists at the base, so nothing is rebuilt:

- **Formats.** `gact/documents/profiles.py` registers `.tex` as profile `latex` with anchors `text-quote` and `source-map` and a PDF rendition. PDF has `text-quote` and `pdf-quad`. HTML has `text-quote` and `dom`. The server advertises all seven anchor kinds in `x_clio_document_artifacts` (`gact/routes/system.py`).
- **Compiling.** `_convert_to_pdf` in `gact/documents/renditions.py` runs `tectonic --only-cached` in the shell sandbox with `NET_DENY`. When Tectonic is missing it falls through to pandoc, which cannot build IEEEtran or acmart papers. `_source_path` copies the one `.tex` file into a temporary directory by itself, so `\input`, `\include`, `.bib` files and figures are not there and a multi-file paper cannot build.
- **Provisioning.** `runtime/document_install.py` installs the Office renderer and the GitHub CLI after app installation, each a pinned download checked by SHA-256 (`runtime/github_cli.py` is the pattern). Tectonic is not installed by anything.
- **The LaTeX plugin.** Marketplace `plugins/latex` (requires `document-runtime`) ships `author-latex-pdf`, whose `scripts/render_latex.py` is a second `--only-cached` Tectonic call. Its review rules already say source is canonical, shell escape stays off, and a clean exit is not success without a log scan. The `documents` pack lists `latex` with pdf, docx, xlsx, pptx and opendocument.
- **Reviews.** `POST` on the document review route stores an `ArtifactReview` bound to artifact id, version and SHA-256, and dispatches it right away as an agent turn carrying an `artifact_review` part. Statuses are `queued`, `dispatched`, `human-note`, `failed` and `stale`. `documents-review/review-document-artifact` and its `anchor-semantics.md` tell the agent how to resolve each anchor kind.
- **What the agent receives.** `_dispatch_review` in `gact/routes/documents.py` posts a user message with two parts: a text part from `_review_prompt`, which is the only thing the model reads and dumps the anchor as raw JSON (normalized quads, offsets) followed by the comment, and a structured `artifact_review` part that the transcript keeps for the UI. A region has no image in the message. Nothing ties the agent's answer back to the review.
- **Native comments.** `gact/documents/native_comments.py` reads comments out of Word, Excel, PowerPoint and OpenDocument files, and comments starting with `@clio` become agent instructions.
- **Capture in the UI** (`web/src/components/clio/document-workspace.tsx`, `document-pdf-viewer.tsx`). Selecting text in Markdown or HTML produces a `text-quote` with `exact` only, without prefix, suffix or offsets. Selecting text in a PDF produces a `pdf-quad` with one normalized rectangle per line. Nothing produces a dragged region, a `dom` anchor or a `source-map` anchor. A "Review selection" button opens a dialog, and sent reviews are listed in a separate Reviews tab. Nothing is drawn in the document.
- **The LaTeX preview.** The `latex` profile previews as a source code block. The compiled PDF is not shown beside it.
- **HTML preview.** `HtmlPreview` renders authored HTML in an opaque sandboxed frame (`desktop-html-frame.tsx`) with a channel-scoped `postMessage` protocol for theme, mount and height. The frame cannot be read from the host.
- **Chat annotations.** `web/src/lib/composer-annotations.ts` defines `text-quote` (a run of an agent answer), `data-zone-quote` (a chart, map or table zone) and `region-capture` (a dragged screenshot of an A2UI surface, from `a2ui-region-capture.tsx`). They travel as message text, not as stored anchors.
- **Office editing.** The ONLYOFFICE and Collabora embedded editors are still on `main`, `develop` and the base, although the September document decision marked them for deletion. They only start when `CLIO_ONLYOFFICE_URL` or `CLIO_COLLABORA_URL` points at a running server, so a normal install carries the code without using it. In clio-agent they span `gact/documents/editors.py` (WOPI locks, access tokens, ONLYOFFICE JWT, endpoint health), `gact/documents/editor_callbacks.py`, the editor-session, ONLYOFFICE and Collabora WOPI routes in `gact/routes/documents.py`, `EditorProvider` and the editor-session models in `gact/documents/models.py`, the `embedded_editors` field in `profiles.py`, the capability entry in `gact/routes/system.py`, the client SDK in `client/client.py` and `client/types.py`, three variables in `docs/ENVIRONMENT.md`, and their tests in `tests/test_gact/test_document_artifacts.py`. In gact-tui they span `onlyoffice-editor.tsx`, the launch and open-menu paths in `document-launch.ts` and `document-open-menu.tsx`, the editor branch of `document-workspace.tsx`, the editor types in `packages/core/src/v3/document-domain.ts` and `document-repository.ts`, `contract/SPEC.md`, and their tests and fixtures. Working copies, desktop-app opening, the LibreOffice rendition and native comment ingestion do not depend on them.

## Design: removing the embedded Office editors

**What goes.** Every piece listed under Office editing in the current state: the editor module and its callbacks, the editor-session, ONLYOFFICE and WOPI routes, `EditorProvider`, `embedded_editors`, the editor capability entry, the client SDK methods and types, the three environment variables, the frontend editor component and launch paths, the editor types in `packages/core`, the contract text, and their tests and fixtures. `EditorProvider` loses `onlyoffice` and `collabora`; if `native` is its only remaining value, the type itself is deleted.

**What stays.** Office and OpenDocument files keep their profiles, their LibreOffice PDF rendition as the in-app view, their native comment ingestion with the `@clio` trigger, working copies and opening in the desktop's own application, with stable saves becoming immutable revisions as today. In the viewer they take text and region comments on the rendition like any PDF (below).

**Contract.** The capability advertisement and `contract/SPEC.md` drop the editor entries in the same change, and the client SDK and gact-tui stop reading them. Nothing keeps a disabled or hidden editor path.

## Design: bundled LaTeX builds

**Install.** Tectonic becomes a pinned download in the post-install provisioning, next to the GitHub CLI: one version, a SHA-256 per platform (Linux x86_64 and aarch64, macOS, Windows), verified on every start like `installed_github_cli`. The package bundle URL is pinned to a versioned bundle, so a paper builds the same way next month. The package cache lives under the clio-agent cache root and fills as papers need packages. The base install grows by the Tectonic binary only.

**Network.** Builds run in the shell sandbox with network limited to the pinned bundle host, not `NET_DENY` and not open. When a package cannot be fetched, the build result carries `missing_package` with the package name and a plain message: the package could not be downloaded, so the build needs network access the first time this package is used. The agent reports it as written. There is no retry loop and no fallback to pandoc for `.tex`; the pandoc branch for LaTeX is deleted.

**Projects.** A build takes an entry file and the folder it belongs to. The entry is the `.tex` file with `\documentclass` that the user or agent names; when a folder has exactly one such file it is the default. The build copies the folder's source files (`.tex`, `.bib`, `.bst`, `.cls`, `.sty`, images, data read by `\input`) into the build directory, bounded by size and count, and records the SHA-256 of every file that the log shows was read. A rendition is bound to that set of hashes, so editing a section file or the bibliography rebuilds the paper.

**Diagnostics.** The build parses Tectonic's log into typed entries: `error` (with file and line), `undefined_reference`, `undefined_citation`, `missing_file`, `missing_package`, `overfull_box` (with page and amount) and `font_warning`. It returns the PDF, the page count and these entries. A build with errors or undefined references is reported as failed even when Tectonic exits cleanly, as the skill's review rules already require.

**Bibliography.** BibTeX works inside Tectonic. Papers using biblatex with biber need a biber binary whose version matches the bundle's biblatex. Phase 0 checks whether shipping it is a small, pinned addition; if it is not, `biber` papers report `unsupported_bibliography_backend` and the agent offers to switch the paper to BibTeX.

**Tools,** in the `latex` plugin, implemented by clio-agent:

- `latex_build(entry, venue=None)` builds and returns the PDF artifact, page count, diagnostics and, with a venue, the venue checks.
- `latex_outline(entry)` returns the structure below.
- `latex_locate(entry, page, x, y)` and `latex_locate(entry, file, line)` map between PDF positions and source lines.

The `author-latex-pdf` skill is rewritten around these tools, and `render_latex.py` is deleted.

## Design: paper templates

**Templates are plugin data.** The `latex` plugin ships a `templates/` folder. Each template is a directory with a `template.yaml` and starting source: the class, options, a skeleton `main.tex`, a `.bib`, and the venue's rules.

| Template | Class and options | Ships first |
| --- | --- | --- |
| `ieee-conference` | `IEEEtran`, `conference` | Yes |
| `ieee-journal` | `IEEEtran`, `journal` | Yes |
| `acm-sigconf` | `acmart`, `sigconf` (and `review`, `anonymous` when asked) | Yes |
| `acm-journal` | `acmart`, `acmsmall` | Yes |
| `springer-lncs` | `llncs` | If Phase 0 finds it in the bundle |
| `elsevier` | `elsarticle` | If Phase 0 finds it in the bundle |

Adding a venue is adding a folder. Nothing in clio-agent names a venue.

**Venue rules.** `template.yaml` declares what the build can check: page limit, and whether references count against it; double-blind (no author names, no acknowledgments section, no self-identifying URLs in the source); required and forbidden packages; and paper size. `latex_build(entry, venue=...)` reports each rule as passed or failed with the place it failed. The agent picks the venue from the paper's class and options unless told otherwise. Rules that need a person's judgment, such as whether a citation is self-identifying, are reported as things to check, not as failures.

**Skill.** `start-a-paper` in the `latex` plugin tells the agent how to begin from a template, where sections go, and to build early and after each change. A short fragment (`when: {tools: [latex_build]}`) says that templates and venue checks exist.

## Design: source and PDF mapping

**SyncTeX.** Every build runs with SyncTeX on. clio-agent parses the `.synctex.gz` output into an index from page boxes to source file and line, and back. The index is stored next to the rendition and keyed to the same set of source hashes. Phase 0 picks between a small parser in clio-agent and a maintained library, and records it.

**The outline.** `latex_outline` walks the entry file and its inputs and returns a tree of typed nodes: `section` (any level), `figure`, `table`, `equation`, `algorithm`, `listing`, `abstract`, `bibliography`, plus `label` and `citation` attached to their nodes. Each node carries its source file and line range and, through SyncTeX, the pages and rectangles it occupies in the PDF. A citation node carries its key and whether the `.bib` resolves it. This plays the role that slide and shape ids play for PowerPoint: the agent and the UI can name "Figure 3" or "Section 4.2" and get both its source and its place on the page.

**What the agent sees.** A review on a LaTeX paper arrives with its anchor already resolved: the source file and lines, the outline node it falls in, and the selected text. A comment on Figure 3 tells the agent to edit the figure environment in `sections/eval.tex` lines 40 to 58, not to guess from the PDF.

## Design: one comment system

**Three modes.** Every viewer offers the modes its format supports, chosen from a small dropdown beside the comment control. Unsupported modes are not shown.

| Mode | How it works | Formats | Anchor stored |
| --- | --- | --- | --- |
| Text | Select text as usual | Markdown, LaTeX source, PDF, HTML, agent answers | `text-quote` with exact, prefix, suffix and offsets; on a PDF also `pdf-quad` rectangles per line |
| Region | Drag a rectangle | PDF pages, images, A2UI surfaces, HTML | `pdf-quad` with no text on a PDF; a normalized rectangle plus a cropped PNG elsewhere |
| Element | Hover to outline, click to pick | HTML | `dom` with a selector, tag, a text excerpt and the element's rectangle |

Text stays the default. Region exists because figures, plots, equations and scanned pages have no useful text to select.

**Mapping each anchor to source.** On a compiled LaTeX PDF, both text and region comments add a `source-map` anchor (file, line range, outline node) from the SyncTeX index at the moment the comment is made, so the stored comment says what it pointed at in the source of that exact version. On LaTeX source, text selection is a `source-map` anchor directly. On HTML, the element picker generates the selector in the frame, preferring `id`, then stable data attributes, then the shortest unique path.

**The element picker.** The HTML frame's host document gains a picker script that speaks the existing channel protocol: `clio.html-preview.pick.start`, `pick.hover` and `pick.selected`, the last carrying the selector, tag, text excerpt, truncated `outerHTML` and the rectangle. The frame stays opaque and the host never reads its DOM. The same script is what the in-app browser will reuse.

**The comment box.** One component everywhere, opened next to the selection: a text field, and a "Send to agent" checkbox that is on by default. Unchecked, the comment is stored as a `human-note` and the agent is not asked to act. Sent, it is dispatched as today, bound to the exact version and hash.

**Batching.** Several comments made in a row are collected as a pending review with a "Send N comments" control, and go to the agent as one turn with all their anchors. A single comment can still be sent immediately. Reviewing a paper with fifteen comments then costs one turn, not fifteen.

**What the agent receives.** The server resolves every anchor before dispatch and renders it as a readable block, one header per document and one numbered entry per comment. A batch is one turn with all its entries. `_review_prompt` and its JSON dump are replaced by this renderer; the structured `artifact_review` part stays for the UI and the thread. Each entry carries what the agent needs to act without decoding coordinates:

- the mode, the selected text or element, and the comment;
- the text before and after a text selection, which is also what re-anchoring uses;
- the source location from the mapping when the document has a source (file, lines, outline node);
- for a region, the cropped region at 2x as an image part, referenced from the entry;
- for an image produced by a script in the workspace, a "Produced by" line naming that script, so the agent fixes the code and not the pixels.

A text comment on a compiled LaTeX paper:

```
Review of paper/main.pdf, built from paper/main.tex
(artifact art_8f2c, version 7, sha256 3b9e...a41c). Edit the source, not the PDF.

Comment 1 (text)
  Selected: "we observe a 3.2x speedup over the baseline"
  Before:   "...when running on 512 nodes, "
  After:    " once the cache is warm."
  PDF:      page 6, spanning 2 lines
  Source:   sections/eval.tex, lines 112 to 113, in Section 5.2 "Scaling"
  Comment:  Say which baseline. Fig. 7 uses tuned stripes, not Lustre defaults.
```

An element comment on an HTML artifact. The `outerHTML` excerpt is truncated to a fixed length so a picked container cannot flood the context:

```
Review of dashboard.html (artifact art_19ab, version 3, sha256 77c0...e512).

Comment 1 (element)
  Element:  <button id="run-sim">, selector main > section.controls > #run-sim
  Text:     "Run simulation"
  HTML:     <button id="run-sim" class="btn primary" disabled>Run simulation</button>
  Box:      412 x 36 px at (96, 540) in a 1280 px wide view
  Comment:  Enable this once a mesh is loaded, and say what it runs.
```

A region comment on an image:

```
Review of figs/throughput.png (artifact art_5d07, version 2, sha256 c41f...09bd).

Comment 1 (region)
  Region:   x 8% to 52%, y 11% to 39% of the image (attached as image 1)
  Comment:  The legend covers the 1024-node bars. Move it outside the plot.

[image part: the cropped region at 2x]
```

A region on a figure in a compiled LaTeX PDF has the same shape plus a Source line naming the figure, its lines and its `\includegraphics` and `\label`.

**Replies.** The agent needs no tool to answer a comment. When the turn a review started finishes, its final message is stored as the reply on every thread in that review, and the version the turn produced (from the artifact change feed) is linked from each thread. A turn that produced no new version links none, and the thread shows the reply alone.

**Threads in the document.** Comments are drawn where they point: highlights for text, outlines for regions and elements, and a margin marker per thread. A thread holds the comment, the agent's reply and a link to the version the agent made. The separate Reviews tab is replaced by this layer and a list of threads in the side panel.

**New versions.** When the agent makes a new version, each open thread is re-anchored: a text anchor by exact text and context, an element by its selector and text, a LaTeX anchor through the new SyncTeX index and outline node. A thread that re-anchors cleanly moves with the document and is marked as moved; one that does not is shown as outdated against the version it was made on. Nothing is carried to a new version without being marked, as the anchor rules already require.

**Chat annotations.** "Add to chat" on an agent answer, data-zone references and A2UI region captures move onto the same anchor model and the same comment box. They stay attachments to the next message instead of stored reviews, because a transcript is not an artifact. Their separate type definitions are deleted.

**Office formats.** Word, Excel and PowerPoint keep their native comments. In the viewer, their LibreOffice PDF rendition takes text and region comments like any PDF, mapped back to the file by quote and structure.

## Design: the LaTeX workspace

- **Split view.** Source on the left with a file tree of the project, compiled PDF on the right. The latest successful build stays visible while a new one runs.
- **Click-through.** Clicking a place in the PDF opens the source at its line; placing the cursor in the source highlights the box it produced in the PDF. Both use the server's SyncTeX index through `latex_locate`.
- **Build problems.** Diagnostics appear as icons in the source gutter and on the PDF page they affect, with detail on hover. Venue checks appear the same way, with a summary icon in the toolbar. No text boxes.
- **Outline.** The outline is a navigator in the side panel: click a section or figure to scroll both panes to it.
- **Rebuilds.** A new source version rebuilds automatically, through the same idempotent rendition request the preview already uses.
- **Comments.** The comment layer above works on both panes, so a comment made on the PDF shows up at its lines in the source and the other way round.

## Defaults for the open questions

Take these and record each in the ledger.

| Question | Default |
| --- | --- |
| Tectonic version and bundle | Latest stable release and its matching versioned bundle, pinned |
| Network during builds | Only the bundle host; `missing_package` when unreachable |
| Project root | The folder of the entry file; the entry is the one file with `\documentclass`, or the one named |
| Send to agent | On by default; off stores a human note |
| Batching | Comments queue as a pending review; one comment can be sent immediately |
| Re-anchoring | Move and mark when it re-anchors cleanly, otherwise outdated |
| Editing LaTeX source in the UI | Read-only source with comments; the agent makes the edits. Typing in the source pane belongs to the document editing rework |

## Phase 0: inventory and decisions

No production code changes in this phase.

1. Pick the Tectonic release and bundle, record per-platform URLs and SHA-256, and confirm `--synctex` output on the four platforms the installer targets.
2. Build the IEEE and ACM sample papers from their official template packages with Tectonic and list every package fetched, the cache size afterwards, and any class that fails. Check whether `llncs` and `elsarticle` are in the bundle.
3. Decide biber: try a matched biber build next to Tectonic and record size and result.
4. Confirm how the shell sandbox can allow one host, and record the net policy decision.
5. Pick the SyncTeX parser.
6. List every place that defines or captures a selection or annotation in gact-tui, so Phase 4 deletes all the duplicates.
7. Confirm the editor inventory under Office editing against the base, including the marketplace and any install or Docker files, so Phase 1 removes everything in one change.
8. Write `docs/design/latex-and-comments-ledger.md` with the megachange ledger's columns, decisions L1 onward and the scenarios below. Commit, push, open draft PRs, and continue.

## Phase 1: remove the embedded editors

1. Delete the editor code, routes, models, capability entry, client SDK methods and types, environment variables and tests in clio-agent.
2. Delete the editor component, launch and open-menu paths, the editor branch of `document-workspace.tsx`, the `packages/core` types, the contract text, and their tests and fixtures in gact-tui.
3. Run the document tests in both repositories, then open a `.docx`, a `.pptx` and an `.odt` in the restarted stack and confirm each shows its rendition and offers only desktop-app opening.

## Phase 2: builds and templates

1. Add Tectonic to post-install provisioning with the pinned download and verification.
2. Replace the LaTeX branch of `_convert_to_pdf` with the project build: folder copy, entry detection, hash set, sandbox with the bundle host allowed, typed diagnostics. Delete the pandoc fallback for `.tex`.
3. Add `latex_build` to the `latex` plugin. Rewrite `author-latex-pdf` around it and delete `render_latex.py`.
4. Add the templates, `template.yaml` rules, venue checks, `start-a-paper` and the fragment.

## Phase 3: mapping

1. Run builds with SyncTeX, parse the index, and store it with the rendition.
2. Add `latex_outline` and `latex_locate`.
3. Resolve LaTeX review anchors on the server before dispatch, so the review part carries source file, lines and outline node.
4. Update `anchor-semantics.md` with the resolved `source-map` fields.

## Phase 4: the comment model

1. Extend `DocumentAnchor` for region (rectangle and crop) and complete `text-quote` capture with prefix, suffix and offsets.
2. Add pending reviews and batch dispatch to the review route and store, with one agent turn per batch.
3. Replace `_review_prompt` with the review renderer above: resolved anchors, before and after text, source locations, "Produced by" lines, and region crops as image parts.
4. Add thread replies: when a review's turn finishes, store its final message and the produced version on each of its threads.
5. Add re-anchoring on new versions with `moved` and `outdated` states.
6. Move composer annotations onto `DocumentAnchor` and delete their separate types.

## Phase 5: UI in gact-tui

1. The comment box and the mode dropdown, shared by every viewer and the transcript.
2. Region mode on PDF pages, images and HTML; reuse `a2ui-region-capture` for A2UI surfaces instead of writing a second drag tool.
3. The element picker script in the HTML host document and the `pick.*` messages.
4. The in-document comment layer and thread list. Delete the Review selection dialog and the Reviews tab.
5. The LaTeX workspace: split view, file tree, click-through, gutter diagnostics, venue checks, outline navigator.

Verify each change as the megachange plan describes: restart the stack, run `node scripts/cloud/ui_check.mjs` with a `--steps` script at 1440 by 900 and 390 by 844, read every screenshot, and check that `consoleErrors` and `failedRequests` are empty.

## Phase 6: qualification

Run focused tests and `ruff` on touched paths while working. Then run these against a freshly restarted stack (`claude_code` provider), and record each in the ledger with commands, handles and the screenshots read.

| ID | Scenario | Passes when |
| --- | --- | --- |
| L1 | Fresh install, then build an IEEE conference paper with `\input` sections, a `.bib` and two figures | The PDF builds with citations resolved; the cache holds only the packages fetched |
| L2 | The same for an ACM `sigconf` paper with `review,anonymous` | It builds, and the double-blind check flags an author name planted in a section file |
| L3 | A paper one page over the venue limit | The page-limit check fails and names the page count and the limit |
| L4 | Build with the bundle host blocked and a package not yet cached | The result is `missing_package` with the package name, and the agent says network is needed |
| L5 | An undefined reference and an undefined citation | The build is reported failed with both, each linked to file and line |
| L6 | Edit only the `.bib` | The paper rebuilds and the new rendition's hash set includes the `.bib` |
| L7 | Click a paragraph in the PDF, then place the cursor on a line in the source | The source opens at the right line; the PDF highlights the right box |
| L8 | A text comment on a PDF sentence of a LaTeX paper, sent to the agent | The agent receives file, lines and outline node, edits the source, and the thread shows its reply and the new version |
| L9 | A region comment around a figure | The anchor resolves to the figure environment, and the agent edits that environment |
| L10 | An element comment on a button in an HTML artifact | The stored selector matches exactly one element, and the agent's edit changes that element |
| L11 | Three comments queued and sent as one review | One agent turn carries all three, rendered as numbered entries with no raw anchor JSON in the model's input; when the turn ends, each thread shows the agent's reply and the new version |
| L18 | A region comment on a plot produced by a workspace script | The model's input has the crop as an image part and a Produced by line, and the agent edits the script |
| L12 | A comment with "Send to agent" off | It is stored as a human note and no turn starts |
| L13 | The agent edits the paper after L8 to L9 | Threads that still match are marked moved; one whose text was rewritten is outdated |
| L14 | "Add to chat" on an agent answer and a region capture on an A2UI chart | Both use the shared comment box and attach to the next message |
| L15 | A paper using `minted` | The build reports shell escape as unsupported for that package |
| L16 | Open a `.docx` with a native `@clio` comment, then comment on its rendition | The view is the LibreOffice rendition with no embedded editor offered; the native comment becomes a review; the rendition comment reaches the agent mapped to the right paragraph |
| L17 | Search both repositories for the editor names, routes and variables | Nothing remains outside the ledger and this plan |

The cloud session cannot qualify the Tauri desktop or Windows and macOS installs; mark those rows of L1 blocked and run L1 on Linux. Nothing else may be blocked. A failing scenario is fixed, not deferred.

## Done

1. Every phase is implemented; the ONLYOFFICE and Collabora integrations, `render_latex.py`, the pandoc path for `.tex`, the duplicate annotation types, the review dialog and the Reviews tab are gone.
2. Focused tests pass, and `ruff check` and `ruff format --check` are clean on touched paths.
3. Scenarios L1 to L18 pass against a freshly restarted stack, each shown in the transcript with commands, handles and the screenshots read.
4. The ledger has every row passed, apart from the platform rows above.
5. Branches are pushed in clio-agent, gact-tui and clio-agent-marketplace, the submodule pins point at the pushed commits, and the draft PRs carry a summary and the evidence.

## Working conventions

- Branch `claude/latex-document-comments` in clio-agent, gact-tui and clio-agent-marketplace. Open draft PRs against the base branch.
- Small Conventional Commits per step, with test evidence in each PR description.
- Keep runtime state in `.cloud-run/`. Stop the stack when you finish a work block.
