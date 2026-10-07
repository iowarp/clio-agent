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

For a new deck, read `references/slide-design.md` for narrative, theme and layout
decisions. When using JavaScript, read `references/pptxgenjs.md` for the shipped
library's layout units, reusable theme, image sizing and speaker notes example.
For Python edits to an existing deck, read `references/python-pptx-editing.md`
to preserve text-run formatting, native objects and speaker notes.
Load these files with this skill's returned ID and `file` argument. Adapt the
guidance to the user's audience and evidence; do not copy the sample as a finished
presentation.

`examples/create-reference-deck.mjs` is a runnable, fictional six-slide deck
with a shared theme, editable chart and tables, varied layouts and speaker notes.
Load it with the same skill ID and `file` argument, then copy it into the prepared
JavaScript workspace before running. Its sample data are design examples, not
user findings. Keep builders in task-owned workspace files and execute them with
a short command; split large writes instead of embedding a whole deck builder
in one shell call.

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

For a new presentation, cover the substance needed by its audience and occasion.
Choose the slide count from that purpose and the supplied evidence. An outline,
two-slide sample or empty placeholder deck does not complete a request for a
meeting presentation. Give each slide a distinct purpose and write a clear title.
Use the actual evidence as the visual focus: a legible figure, comparison, table
or explanation. Retain source identifiers, units and caveats with findings, and
put supporting references and presenter context in speaker notes.

Use a consistent type and colour system, generous margins and varied compositions
suited to the content. Around 30–36 point titles and 18–24 point body text are
useful starting sizes for a widescreen meeting deck. Prefer a few readable points
and a meaningful visual to dense paragraphs or repeated default bullet layouts.
Keep a cover simple. Avoid generic slogans, blank slides and filler added merely
to reach a slide count. Review the slide overviews and revise weak composition as well as
clipping or overlap before publishing.

For a small edit, change the relevant text runs with `python-pptx`; assigning
`shape.text` or `paragraph.text` rebuilds the runs and can discard their font,
size and colour. Preserve the template's run formatting and inspect the changed
slide in the rendered overview. A
`pptxgenjs` builder creates new decks; it does not provide a general importer for
editing an existing deck. Unsupported objects need scoped OOXML edits or a
preservation-aware tool. Reopen the generated file and check slide count, content,
notes and the objects the task required before layout review.

Call `prepare_document(output_path, action="render")` to obtain a LibreOffice PDF
and slide PNGs. For a long deck, render it in explicit page ranges. Close the loop
before delivery: save the native deck, convert it to PDF, render slide images,
look at them, fix the native source and repeat. The render returns `contact_sheets`
with up to six slides in a two-column, three-row grid alongside full `images`.
Inspect every sheet for theme and compositional consistency. Based on what the
overviews show, choose individual slides for closer review when text or chart
labels are too small to assess, or overlap, cropping, text fit, contrast or image
proportions are unclear. Use `view_image`, or `view_pdf` when available, to resolve
those questions. Opening every individual slide is not required when the overview
provides enough evidence. Regenerate and review the overviews after fixing the
source, inspecting details as needed. Resolve visible defects and uncertainties
before publishing. PDF conversion may differ from
PowerPoint, so distinguish local rendered review from acceptance in PowerPoint.

Clio uses bundled LibreOffice or provisions a private verified copy when rendering.
If provisioning fails or the model cannot receive images, report that precise
review limitation. Successful generation, shape-bound checks and text extraction
do not establish visual quality. Designate the requested editable `.pptx` with
`create_artifact(path=output_path, kind="report", pdf_preview=true)`.
Name the published artifact in the final response; Clio shows its artifact card.
Do not format local filesystem paths as Markdown links.
This registers the editable deck and a version-bound PDF artifact for Clio's
viewer. Before claiming a preview is available, confirm a successful entry in
the publication result's `pdf_previews`; correct a missing or failed binding.
A saved preview is not visual review.
A separately registered review PDF does not bind to the editable deck. Keep
`pdf_preview=true` on the source call, or on its batch item, when a preview is
requested; the publication workflow creates that relationship.
