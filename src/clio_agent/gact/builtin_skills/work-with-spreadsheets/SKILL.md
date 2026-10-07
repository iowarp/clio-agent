---
name: work-with-spreadsheets
description: Create, edit and analyze Excel workbooks and CSV/TSV tables, preserving formulas and checking calculated values and rendered sheets.
---

# Work with spreadsheets

Call `prepare_execution_runtime` and use the prepared Python environment, which
contains `openpyxl`. Execute scripts with `python_argv` (uv selects the prepared
Python). Use its reported host paths; a repository's `uv run` may
select another environment. Use the returned output directory for intermediate
files; save requested deliverables outside `.tmp` in the active workspace. Keep
additional dependencies in task-owned environments.

Inspect a workbook with `prepare_document(path, action="inspect")`. For large
files specify `sheet` and `cell_range`, such as `sheet="Budget"` and
`cell_range="A1:H50"`. Read the returned content JSON in bounded sections. It
reports formulas separately from cached values and detects external links.
The helper refuses excessive cell ranges rather than silently truncating them.
CSV/TSV inspection preserves row boundaries; these formats carry no workbook
styles, multiple sheets or formula caches.

When editing, load with `data_only=False`, and use `keep_vba=True` for `.xlsm`.
Never save a workbook loaded with `data_only=True` when formulas must survive.
Inspect input cells, formulas, merged ranges, tables, named ranges and existing
formatting before changes. Write a merged cell through its top-left anchor.
Preserve formulas, macros, external links and unsupported objects outside scope;
re-saving with a library can alter features it does not support. Keep the original.

For a new workbook, name sheets and units clearly, use appropriate number formats,
and separate editable inputs from formulas. Use formulas for outputs that should
update with inputs. Test representative results against independently calculated
expected values. Formatting a cell as a percentage does not change its value:
store `0.15` for fifteen percent. Quote cross-sheet names containing spaces.

`openpyxl` writes formulas but does not evaluate them. After changing formula
inputs or formulas, call `prepare_document(output_path, action="recalculate")`.
This creates a new XLSX copy using an isolated LibreOffice profile, preserves the
input file and checks every formula cache plus error cells. Read `status`,
`formula_count`, `issue_count`, and the bounded issue list. A failed manifest is
not a checked workbook. Reopen the returned recalculated path to confirm formulas
and expected values. Copy those checked bytes to the requested final workspace
destination outside `.tmp` before publishing. Do not resave that copy with
`openpyxl`, which clears the calculated caches.

Recalculation evaluates the workbook but does not prove its formulas are correct.
Compare representative totals and edge cases with the task's intended calculation.
An error-free result can still reference the wrong cells. External-linked files
are refused by automatic recalculation because conversion can lose their cached
values and relationships. Macro-enabled files are also refused by this helper;
use a preservation-aware application workflow instead of converting them silently.

Clio uses bundled LibreOffice or provisions a private verified copy for rendering
and recalculation. Check the result before claiming either succeeded. When layout
matters, set appropriate print areas and
page settings, render with `prepare_document(path, action="render")`, and inspect
the material sheet pages through `view_image` or `view_pdf`. Rendering reflects
print layout, not the entire interactive workbook. If the converter or visual tool
is unavailable, state what was actually checked and retain the editable workbook.

Designate the final XLSX with
`create_artifact(path=output_path, kind="report", pdf_preview=true)`.
Name the published artifact in the final response; Clio shows its artifact card.
Do not format local filesystem paths as Markdown links.
This registers the editable workbook and its version-bound PDF print
preview for Clio's viewer. Check `pdf_previews` for failures. For CSV/TSV, register
the table directly. Keep provenance for supplied data and label introduced assumptions.
A separately registered review PDF does not bind to the editable workbook. Keep
`pdf_preview=true` on the source call, or on its batch item, when a print preview
is requested; the publication workflow creates that relationship.
Publish and name the requested editable workbook. Keep PDFs made only for review
or display as derivatives; do not separately register or list them as deliverables.
Publish a separate PDF only when requested. In `used`, cite the actual source
documents or data, not the review PDF derived from this output.
