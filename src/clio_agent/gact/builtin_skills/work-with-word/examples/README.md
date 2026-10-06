# Build and review the report reference

The example contains fictional station summaries in arbitrary units. It
demonstrates connected report prose, a chart derived from those summaries, native
headings and tables, captions and page numbering. It is not a real experiment.

1. Prepare the execution runtime and confirm JavaScript is ready.
2. Load `create-reference-report.mjs` through this skill's file argument and save
   it in the returned JavaScript workspace, where `docx` and `sharp` resolve.
3. Execute it with the returned `javascript.script_argv`, passing a task-owned
   output directory. It writes `reference-report.docx`, `reference-chart.png` and
   `reference-source.json` there.
4. Reopen the DOCX, then call `prepare_document` with `action="render"` on it.
   Inspect its contact sheet and both individual page images. Fonts and renderer
   changes can change pagination; the checked reference renders as two pages.
5. Fix the native builder, regenerate and repeat if the layout needs work. Publish
   the final editable DOCX with its source-bound PDF preview.

Use `references/docx-js.md` for library mechanics and `references/report-design.md`
for content and composition. Adapt them to the actual task; do not merely replace
the example's title and leave fictional findings in a user report.
