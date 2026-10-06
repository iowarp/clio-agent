# Website review, 2026-10-06

The homepage now explains CLIO in two short lines, puts a readable product
showcase near the top, and links directly to setup, data, documents and evidence
guides. Figures, documents and evidence use real OPAL review captures. The five
existing workflow examples retain their text, screenshots and guide links in
keyboard-accessible expandable rows.

The docs landing page uses the same task links. Inactive nested sidebar groups
start closed; the current guide's ancestors open automatically. The article and
guide index have a more compact type scale. Account settings documentation now
matches the global sign-in page; attaching and managing datasets stays in a
workspace.

Image captions are visible below each capture. The full-size dialog fits the
viewport, keeps Close accessible, restores focus on Escape, and provides a link
to the full-resolution image for closer inspection on small screens.

## Review and validation

Reviewed the actual built site at `http://127.0.0.1:5197`, including the homepage,
expanded workflow, docs landing page, current source guide, complete image
dialogs, and the linked OPAL report example. Desktop and phone captures were
inspected in light and dark themes. Review covers browser presentation; it does
not establish native Desktop acceptance or public-site deployment.

Passed locally, sequentially:

- `pnpm build`: 72 pages and all internal links valid.
- `pnpm check`: zero errors and warnings; five existing hints.
- `pnpm exec playwright test e2e/showcase.spec.ts -g 'product showcase' --workers=1`: keyboard tab selection, native expandable examples, viewport-contained dialogs, focus restoration and the worked-example link at 1440 px and 390 px.
- `pnpm exec playwright test e2e/showcase.spec.ts -g 'docs reveal' --workers=1`: current-group disclosure, guide navigation, account/dataset semantics and screenshot retrieval.
- `pnpm exec playwright test e2e/navigation.spec.ts -g 'mobile docs index remains complete and usable at 390px' --workers=1`: complete mobile guide navigation and page index.

The wider browser, video, unit and backend suites run in GitHub CI. No full
suite or parallel test batch was run locally.

Untouched captures and checked SHA-256 checksums are archived at
`D:/Libraries/Videos/clio_recordings/2026-10-06-site-ergonomics`. Private connection
files and browser profiles are excluded. The OPAL source files are unchanged.

## Document-guide refresh

The general file guide now explains platform workflows with fictional sensor
references. The OPAL report and briefing have their own worked-example page,
linked from the homepage and the guide. Four new continuous recordings show
the current full-height reader, Word revision, corrected PowerPoint revision
and saved OPAL review. The homepage's document capture shows the current compact
toolbar beside the completed report review. Original and revised editable files,
reviewed PDFs, overviews and runnable builders are downloadable from the example.

The Word and PowerPoint skills keep library mechanics separate from design
guidance. New Python editing references address the observed style-lookup,
document-order and text-run formatting failures. They were exercised against
the actual reference files: native Word headings and tables remain in order;
PowerPoint run properties, six slides, chart and notes survive a save and reopen.
The corrected model revision retains one native chart and two editable tables.
Overview review is required; individual-page inspection remains discretionary.

Current local validation used sequential commands and one worker:

- Site build: 73 pages; every internal link valid. The final build used a 768 MiB Node heap cap and one image worker.
- Site type check: zero errors and warnings; five existing hints.
- A single backend case resolves all shipped Office skills without user directories.
- A single FFmpeg regression checks black-level preservation for full- and limited-range recordings. The editor uses input metadata and emits limited-range H.264.
- The focused showcase browser case covers desktop/mobile keyboard choices, figure and report dialogs, the separate worked-example route, four revised-file downloads and the general guide's three current videos.

Each final encoded movie was played completely at normal speed in the actual
built page and fullscreen. Decoded-frame callbacks cover the full timeline;
half-second contact sheets retain consecutive frames for visual review. Review
caught a trailing Word reload, captions carrying into the next slide action and
a homepage capture taken after its document pane closed. The final cuts, captions
and source capture correct those defects. A Windows buffer error during a font
request was retained and the affected example-page review repeated after checking
the resource; it was not dismissed as a successful page check.

The refreshed recordings use browser UI at
`ce62046333fc4583f4c23c4a4a935ed4c4755cf7` (UI PR #547). Local native Desktop
visual acceptance remains pending after automatic approval review blocked its
isolated launch. The browser proof and successful Desktop builds do not replace
that acceptance. Recording provenance and edit instructions are in
`site/video/edits/files-20261006/README.md`; originals, corrected files, playback
evidence, rejected attempts and verified hashes are archived in
`D:/Libraries/Videos/clio_recordings/2026-10-06-files-video-refresh`.
