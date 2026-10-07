# Editing a template with `python-docx`

Use the prepared Python interpreter. Inspect the document first; this reference
covers native body paragraphs and tables, not complete support for floating
objects, tracked changes or fields.

## Reuse the actual styles

Inspect the styles on representative existing paragraphs and tables. Reuse those
style objects when adding comparable content:

```python
from docx import Document

document = Document(input_path)
for paragraph in document.paragraphs:
    print(paragraph.text[:100], paragraph.style.name, paragraph.style.style_id)
for table in document.tables:
    print(table.style.name if table.style is not None else None)

# Choose a heading that has the level and appearance needed for this section.
reference_heading = next(p for p in document.paragraphs if p.text == heading_text)
anchor = next(p for p in document.paragraphs if p.text == insertion_anchor_text)
heading = anchor.insert_paragraph_before(new_heading_text)
heading.style = reference_heading.style
heading.paragraph_format.keep_with_next = True
```

An imported file may define `Heading 1` differently from the built-in internal
name that `python-docx` looks up. Assigning the existing style object avoids that
name translation. Do not fall back to bold body text when a heading lookup fails.
Some documents have no named table style and instead format cells directly;
inspect the cells rather than assuming `Table Grid` exists.

## Keep inserted content in document order

`document.add_table()` appends a table. Inserting a heading before an existing
paragraph does not place that appended table after the heading. Move the new
table explicitly when the section belongs before an anchor:

```python
table = document.add_table(rows=1, cols=len(column_labels))
reference_table = document.tables[0]  # Select a suitable existing table.
if reference_table.style is not None:
    table.style = reference_table.style
heading._p.addnext(table._tbl)
for cell, label in zip(table.rows[0].cells, column_labels, strict=True):
    cell.text = label
```

This example uses the package's XML elements for a scoped insertion. Preserve
the final section-properties element and existing relationships. To move several
paragraphs, capture their elements before moving any of them, then advance an
insertion cursor after each `addnext()`; repeated insertion after the same element
reverses the order. XML properties such as `table._tbl.tblPr` are read-only
accessors: use explicit element replacement when required, not property assignment.

Set table widths to the available text area. With fixed layout, set the grid
column widths and each cell width consistently. Keep readable body-sized text,
repeat a header row on continuation pages and keep each short row intact. Inspect
the resulting page overview for splits, misplaced headings and excess blank space.

Run formatting-sensitive edits on a new output path. Reopen the DOCX, render a
fresh PDF, and actually view the overview images. Successful XML editing or PDF
conversion does not establish that the page layout is good.
