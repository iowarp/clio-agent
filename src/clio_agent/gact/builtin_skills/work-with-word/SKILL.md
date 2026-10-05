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

For an existing `.docx`, call `prepare_document(path, action="inspect")` and read
the returned content derivative through bounded file reads. This extracts body
paragraphs and tables in order. It does not establish pagination, floating-object
placement, revision handling, comments, or header/footer layout. Render when those
features matter. Uploaded attachments have workspace working copies; use the
supplied copy path and preserve the original.

Create or edit with `python-docx` using the prepared interpreter. Prefer styles
and native headings, lists and tables so the result remains editable. Inspect
section dimensions and existing styles before modifying a template. Specify page
size, margins and table widths deliberately. Replace text in individual runs when
formatting must survive; assigning paragraph text replaces its runs. Preserve
images, hyperlinks and fields that the task does not change. Do not recreate an
existing document from extracted text to perform a small edit.

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

Inspect every material output page using `view_image`, or the rendition using
`view_pdf` when exposed. Check wrapping, table splits, image placement, glyphs and
headers/footers. After a change, regenerate the rendition and inspect the new
images. Extraction and rendering are distinct from visual review. If conversion
or visual tools are unavailable, report the concrete limitation and keep the
editable source rather than calling its layout verified.

Use `create_artifact(path=output_path, kind="report", pdf_preview=true)` to designate
the final editable file and a version-bound PDF preview as artifacts. The UI
requires this source-bound preview to open the Word artifact directly. A PDF from
`prepare_document` is a review derivative; separately registering that PDF does
not bind it to the editable artifact. Do not disable `pdf_preview` when a preview
was requested. In a batch, set `pdf_preview=true` on each editable Office item.
Clio's UI
opens the saved PDF when the source artifact is selected; users can still download
or edit the original. Check `pdf_previews` for conversion failures. A preview is
not visual review. Return the source link. Cite supplied sources and distinguish
user content from assumptions introduced while drafting.
