# JavaScript Word creation with `docx`

Use the version and command returned by `prepare_execution_runtime`. The managed
stack currently locks `docx` 9.8.1. Store the `.mjs` builder in its returned
JavaScript workspace; a script outside that directory may not resolve packages.
This library creates DOCX packages. Use a preservation-aware edit path for an
existing file rather than reconstructing it from extracted text.

Read `examples/create-reference-report.mjs` for a complete runnable reference:
natural pagination, native headings and tables, page-number fields, captions
and a source-derived figure. Copy the builder into the prepared JavaScript
workspace and run it with an absolute output-directory argument. It writes a
DOCX, a PNG and explicitly fictional source data; it does not change user files.
Render the DOCX to review the example's design. Adapt its structure to the task
rather than treating its sample content or page count as a requirement.

Page dimensions and margins use twentieths of a point (twips): 1 inch = 1440.
Run font sizes use half-points: 11 pt = 22. Paragraph spacing is in twips.
Set page and style defaults once; express headings and captions through styles.
The native page field belongs in a `TextRun`, rather than literal text saying
"page 1". Table widths must fit the page's text area.

This runnable example demonstrates structure, fields and a native table. Its
short sample content is a library check, not a completed report. Replace the
content with developed, source-supported material and add the required figures.

```javascript
import { writeFile } from 'node:fs/promises';
import {
  Document, Footer, HeadingLevel, Packer, PageNumber, Paragraph,
  Table, TableCell, TableRow, TextRun, WidthType,
} from 'docx';

const cell = (text) => new TableCell({
  children: [new Paragraph({ children: [new TextRun(text)] })],
});
const document = new Document({
  title: 'Evidence review',
  styles: {
    default: {
      document: {
        run: { font: 'Arial', size: 22, color: '202124' },
        paragraph: { spacing: { after: 120 }, widowControl: true },
      },
    },
    paragraphStyles: [
      {
        id: 'Title', name: 'Title', basedOn: 'Normal', next: 'Normal',
        run: { size: 40, bold: true, color: '202124' },
        paragraph: { spacing: { after: 240 }, keepNext: true },
      },
      {
        id: 'Heading1', name: 'Heading 1', basedOn: 'Normal', next: 'Normal',
        run: { size: 30, bold: true, color: '244C62' },
        paragraph: { spacing: { before: 240, after: 120 }, keepNext: true },
      },
    ],
  },
  sections: [{
    properties: {
      page: {
        size: { width: 12240, height: 15840 }, // US Letter; use A4 if appropriate.
        margin: { top: 1080, right: 1080, bottom: 1080, left: 1080 },
      },
    },
    footers: {
      default: new Footer({ children: [new Paragraph({
        alignment: 'right',
        children: [new TextRun({ children: ['Page ', PageNumber.CURRENT] })],
      })] }),
    },
    children: [
      new Paragraph({ text: 'Evidence review', heading: HeadingLevel.TITLE }),
      new Paragraph('Summarize the actual finding and its principal limitation here.'),
      new Paragraph({ text: 'Recorded evidence', heading: HeadingLevel.HEADING_1 }),
      new Paragraph('Explain the observation, source and implication in connected prose.'),
      new Table({
        width: { size: 100, type: WidthType.PERCENTAGE },
        columnWidths: [5040, 5040],
        rows: [
          new TableRow({ tableHeader: true, children: [cell('Source'), cell('Status')] }),
          new TableRow({ children: [cell('Source identifier'), cell('Recorded limitation')] }),
        ],
      }),
    ],
  }],
});
await writeFile(process.argv[2] ?? 'evidence-review.docx', await Packer.toBuffer(document));
```

For images, use `ImageRun` with an explicit image `type`, source bytes and
proportional `transformation` dimensions in pixels. Put the image in a paragraph
with `keepNext: true` and follow it with a native caption paragraph. Use the
source's aspect ratio; a successful save does not establish that labels are
readable. Render and review the complete document through the main skill's
workflow.

Primary library reference: https://github.com/dolanmiu/docx/tree/master/demo
