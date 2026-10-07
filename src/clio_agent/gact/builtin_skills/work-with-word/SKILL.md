---
name: work-with-word
description: Create, edit and inspect Word documents and templates, preserving existing formatting and checking rendered pages.
---

# Work with Word documents

Call `prepare_execution_runtime` before standalone document work. Its paths,
packages, fonts and execution-host facts describe this session; use its prepared
`python_argv` command. A Python project may select a different environment when using
`uv run`. Keep artifact scripts and additional dependencies in task-owned paths.
For JavaScript creation, use `docx` only when the returned JavaScript status is
ready, place the importing script in the returned JavaScript workspace, and run
it with the returned `javascript.script_argv` (pnpm executes managed Node).

For a new report, or when adding or reflowing report sections, read
`references/report-design.md` for content and layout decisions. For template
edits with Python, read `references/python-docx-editing.md` for style reuse and
section insertion. When using JavaScript, read `references/docx-js.md` for the shipped
library's units, styles, page fields and native table example. Load these files
with this skill's returned ID and `file` argument; they are supporting guidance,
not a required template or a substitute for the user's evidence.

`examples/create-reference-report.mjs` is a runnable, fictional two-page report
with a chart, native table, headings and page numbers. Load it with the same
skill ID and `file` argument. Copy it into the prepared JavaScript workspace
before running; its sample data demonstrate composition, not user findings.
Keep builders in task-owned workspace files and execute them with a short
command. Split large writes rather than embedding a whole report builder in one
shell command.

For an existing `.docx`, call `prepare_document(path, action="inspect")` and read
the returned content derivative through bounded file reads. This extracts body
paragraphs and tables in order. It does not establish pagination, floating-object
placement, revision handling, comments, or header/footer layout. Render when those
features matter. Uploaded attachments have workspace working copies; use the
supplied copy path and preserve the original.

Create or edit with `python-docx` using the prepared interpreter. Prefer styles
and native headings, lists and tables so the result remains editable. Inspect
section dimensions and existing styles before modifying a template. Imported
documents may use custom heading and table styles: reuse the actual style objects
from comparable content rather than assuming a built-in name exists or matches.
Keep a new section's heading and content together in document order. Specify page
size, margins and table widths deliberately. Replace text in individual runs when
formatting must survive; assigning paragraph text replaces its runs. Preserve
images, hyperlinks and fields that the task does not change. Do not recreate an
existing document from extracted text to perform a small edit.

For a new report, develop the complete content from the supplied evidence before
formatting. Establish the reader, purpose, main conclusion and source limitations.
Choose the length from the material and the user's intended use. A report needs
connected explanation, supported findings and useful figures or tables where the
evidence calls for them. A short sample, outline or placeholder is not a completed
report. Never invent results to fill pages.

Use a coherent heading hierarchy, readable body text around 11–12 points and
deliberate paragraph spacing. Add page numbers to a multi-page report. Keep figure
captions with their figures, preserve image proportions and use sufficiently
detailed source images. Include units and source identifiers with quantitative
evidence. Repeat table headers across pages and prevent isolated headings and
large accidental blank gaps. A restrained report should get its visual quality
from typography, alignment and clear evidence rather than decorative boxes.

`python-docx` does not provide every OOXML feature. For tracked changes, complex
fields, native comments or unsupported objects, inspect the relevant package XML
and relationships and make scoped changes with `lxml` or `defusedxml`. Keep a copy
of the original and reopen the edited package. Do not claim revision or comment
preservation from a successful library save. Legacy `.doc` files need an explicit
LibreOffice conversion to a new `.docx`; tell the user when conversion affects
fidelity.

Write the requested output to a new workspace file. Reopen it and check the
requested text, tables, sections and images. Call
`prepare_document(output_path, action="render")`. LibreOffice must be available
on the execution host; Clio uses its bundled renderer or provisions a private
verified copy automatically. The runtime inventory reports its status and accepts
`CLIO_DOCUMENT_SOFFICE` for an installed portable executable. Rendering uses a
fresh profile and produces a separate PDF and page PNGs.

Close the loop before delivery: save the native source, convert it to PDF, render
page images, look at those images, fix problems in the source, and repeat until
the current output is readable and well composed. `prepare_document(...,
action="render")` returns both full page `images` and labelled `contact_sheets`
with up to six pages in a two-column, three-row grid. Inspect every sheet for
consistency and pagination. Based on what the overviews show, decide which
individual pages need closer inspection: small or unclear text, figure labels,
suspected table splits, wrapping, glyphs or header/footer problems. Use `view_image`,
or `view_pdf` when exposed, to resolve those questions. Opening every individual
page is not required when the overview provides enough evidence. After a change,
regenerate the rendition and review the new overviews, inspecting details as needed.
Resolve visible defects and uncertainties before publishing. Extraction and
rendering are distinct from visual review. If conversion
or visual tools are unavailable, report the concrete limitation and keep the
editable source rather than calling its layout verified.

Use `create_artifact(path=output_path, kind="report", pdf_preview=true)` to designate
the final editable artifact with a version-bound PDF preview. The UI
requires this source-bound preview to open the Word artifact directly. A PDF from
`prepare_document` is a review derivative; separately registering that PDF does
not bind it to the editable artifact. Do not disable `pdf_preview` when a preview
was requested. In a batch, set `pdf_preview=true` on each editable Office item.
Clio's UI
opens the saved PDF when the source artifact is selected; users can still download
or edit the original. Check `pdf_previews` for conversion failures. A preview is
not visual review. Name the published artifact in the final response; Clio shows
its artifact card. Do not format local filesystem paths as Markdown links.
Publish and name the requested editable document. Keep PDFs made only for review
or display as derivatives; do not separately register or list them as deliverables.
Publish a separate PDF only when the user asked for that output. In `used`, cite
the actual source documents or data, not the review PDF derived from this output.
Cite supplied sources and distinguish
user content from assumptions introduced while drafting.
