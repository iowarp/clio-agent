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
