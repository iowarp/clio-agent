---
name: work-with-pdfs
description: Read, inspect, and verify existing PDFs through structured text and rendered-page evidence. Use for PDF questions and review, not for creating a new PDF report.
---

# Work with PDFs

Use this skill when a PDF must be read, reviewed, compared, or used as evidence.
Do not treat extracted text as evidence about layout, figures, equations, scans,
or handwriting.

## Resolve evidence, not accessibility

Every PDF supplied to the turn has a usable path on the connected agent:

- A workspace `@` reference supplies its verified workspace path and revision.
- An uploaded attachment supplies an independent working copy beneath the
  active workspace's `.clio/inputs/` directory. Its immutable custody original
  remains available to CLIO's conversion pipeline.

Use the supplied workspace path for shell, Docling, Poppler, OCR, Python, and
other file-based tools. Never use or modify a private custody path. Do not ask
whether the PDF is accessible or ask the user to upload it again merely to
obtain a filesystem path.

For an uploaded attachment, inspect its resource record when structured
conversion could help. If conversion is queued or processing, call
`workspace_resource_wait` once with the supplied task id. When conversion is
complete, use `workspace_resource_structure`, `workspace_resource_search`, and
bounded `workspace_resource_read` calls for searchable text, tables, and
document structure.

For either source, use the workspace path and local workflow when no usable
conversion exists or when the question depends on geometry, layout, figures,
equations, scans, handwriting, or other visual evidence.

## Local conversion and page rendering

Resolve this skill's directory as `SKILL_ROOT`, then run:

```text
uv run --no-project --with "docling>=2.0" --with "pymupdf>=1.24" python "SKILL_ROOT/scripts/prepare_pdf.py" "INPUT.pdf" "OUTPUT_DIR"
```

This uses uv's shared cached environment. Do not create a `.venv` inside the
installed skill or blueprint directory.

For a drawing, scan, or other explicitly visual question, skip the slower text
conversion and render pages immediately:

```text
uv run --no-project --with "pymupdf>=1.24" python "SKILL_ROOT/scripts/prepare_pdf.py" "INPUT.pdf" "OUTPUT_DIR" --visual-only
```

The helper performs two independent operations:

- Docling exports Markdown plus its structured JSON document.
- PyMuPDF renders bounded page PNGs and writes a manifest recording every
  output and any partial failure.

Use `--max-pages`, `--max-bytes`, and `--dpi` to keep large documents bounded.
The default limits are intentionally conservative. Read `manifest.json` before
claiming either conversion succeeded.

Prefer the Markdown/JSON outputs for text, headings, tables, and document
structure. Search or read only the relevant sections instead of putting an
entire large conversion into the model context.

## Visual evidence boundary

An image-capable CLIO model has a `view_image` tool. Prefer the Markdown/JSON
conversion when it answers the question faithfully. When converted text is
missing or insufficient—for example, scans, plots, equations, handwriting, or
layout-dependent questions—render the relevant PDF pages with the helper and
call `view_image` on each relevant page PNG. That tool attaches the verified
workspace image pixels to the next model step; a successful shell render alone
does not count as visual inspection.

If `view_image` is absent, the active model has no evidenced image capability.
Use only available text/OCR/structured evidence and state the limitation. Never
infer visual facts from filenames, dimensions, metadata, or successful
rendering alone, and do not ask the user to reattach pages merely to compensate
for a missing agent-side image inspection path.

For scanned or image-only PDFs, treat a weak or empty text conversion as a
signal to use the rendered-page path. For mixed PDFs, combine bounded textual
evidence with visual inspection of only the pages whose layout or figures
matter.

## Engineering drawings and diagrams

Questions about dimensions, feature counts, geometry, callout-to-feature
associations, or relative size are always layout-dependent. Render the relevant
page and call `view_image` before answering, even when text extraction contains
the requested number. Extracted labels without their positions do not show
which feature they dimension. A completed render is not evidence until
`view_image` has returned the page pixels to the model.

Do not infer drawing units from decimal style, likely engineering convention,
software metadata, filename, or the magnitude of dimensions. Report units only
when a visible note, title-block field, or explicit callout states them. If no
unit is specified, say so plainly and keep numerical dimensions unitless.

For large-format drawings or small, dense callouts, inspect the full page first,
then create and inspect high-resolution crops of every relevant view or callout
region. Do not claim a complete list of dimensions or radii from a page preview
whose labels are not all legible.

## Evidence and completion

- Cite page numbers and section/table/figure labels when the source exposes
  them.
- Separate source text, observed visual content, OCR output, and inference.
- Do not call a PDF fully reviewed until the relevant textual content and every
  visually material page have been checked through a representation the model
  actually received.
- Preserve the original PDF. Write derivatives to a separate output directory.
