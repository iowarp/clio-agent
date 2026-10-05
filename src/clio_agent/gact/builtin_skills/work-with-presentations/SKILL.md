---
name: work-with-presentations
description: Create, edit and inspect editable PowerPoint presentations, including slide content, images, notes and rendered layout review.
---

# Work with presentations

Call `prepare_execution_runtime` and use its execution-host commands and library
inventory. Execute Python builders with `python_argv` (uv selects the prepared
Python), and JavaScript builders with `javascript.script_argv` (pnpm executes
managed Node). Create slides with `python-pptx` in the prepared Python environment,
or `pptxgenjs` when JavaScript is ready. Store JavaScript builders in the returned
JavaScript workspace so both CommonJS and ES module imports resolve. A working
Node executable alone does not make packages available to scripts stored elsewhere.
Use the supplied pnpm command for additional task dependencies in a separate
task-owned project; retain that project's manifest and lockfile.

Inspect an existing deck with `prepare_document(path, action="inspect")`, then
read its content derivative in bounded sections. Use `pages="1-10"` for a deck
over the page limit. The result includes slide numbers, shapes, groups, text,
table cells, shape bounds and speaker notes. Read/render a reference before
editing it. Choose its dimensions, typography and layout conventions unless the
user requests a redesign. Preserve slide order, notes, hyperlinks, charts and
other objects outside the requested edit.

For creation, organize the intended narrative first and decide what each slide
needs the reader to understand. Keep text, charts and tables editable. Use images
for actual image content; do not flatten whole slides into pictures as a shortcut.
Set slide dimensions and font choices explicitly. Prefer shared layout constants
and inspect text-box bounds. A PowerPoint object's declared bounds do not prove
that its text fits; wrapping depends on the renderer and installed fonts.

For a small edit, change the relevant runs or shapes with `python-pptx`. A
`pptxgenjs` builder creates new decks; it does not provide a general importer for
editing an existing deck. Unsupported objects need scoped OOXML edits or a
preservation-aware tool. Reopen the generated file and check slide count, content,
notes and the objects the task required before layout review.

Call `prepare_document(output_path, action="render")` to obtain a LibreOffice PDF
and slide PNGs. For a long deck, render it in explicit page ranges. Review each
slide with `view_image`, or read the rendition with `view_pdf` when available.
Check overlap, cropping, text fit, contrast, image aspect ratio and chart labels.
Regenerate previews after fixing the source. PDF conversion may differ from
PowerPoint, so distinguish local rendered review from acceptance in PowerPoint.

Clio uses bundled LibreOffice or provisions a private verified copy when rendering.
If provisioning fails or the model cannot receive images, report that precise
review limitation. Successful generation, shape-bound checks and text extraction
do not establish visual quality. Designate the requested editable `.pptx` with
`create_artifact(path=output_path, kind="report", pdf_preview=true)` and link it.
This registers the editable deck and a version-bound PDF artifact for Clio's
viewer. Check `pdf_previews` for failures; a saved preview is not visual review.
A separately registered review PDF does not bind to the editable deck. Keep
`pdf_preview=true` on the source call, or on its batch item, when a preview is
requested; the publication workflow creates that relationship.
