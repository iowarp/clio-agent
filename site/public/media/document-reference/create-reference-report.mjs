/** A native report demonstrating structure and natural pagination. */
import { mkdir, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import sharp from 'sharp';
import { Document, Footer, HeadingLevel, ImageRun, Packer,
  PageNumber, Paragraph, Table, TableCell, TableRow, TextRun, WidthType } from 'docx';

const output = resolve(process.argv[2] ?? 'reference-output');
await mkdir(output, { recursive: true });
const observations = [
  { station: 'A', count: 12, baseline: 18, followup: 16 },
  { station: 'B', count: 12, baseline: 17, followup: 24 },
  { station: 'C', count: 12, baseline: 18, followup: 19 },
];
const chartSvg = `<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="460">
<rect width="1200" height="460" fill="white"/>
<text x="80" y="45" font-family="Arial" font-size="27" fill="#202124">Follow-up mean by station · illustrative data</text>
${[0, 10, 20, 30].map((v) => `<line x1="110" y1="${365-v*9}" x2="1120" y2="${365-v*9}" stroke="#D7DDE1"/><text x="82" y="${372-v*9}" font-family="Arial" font-size="21" text-anchor="end">${v}</text>`).join('')}
${observations.map((d, i) => `<rect x="${220+i*310}" y="${365-d.followup*9}" width="140" height="${d.followup*9}" fill="#244C62"/><text x="${290+i*310}" y="${350-d.followup*9}" font-family="Arial" font-size="24" text-anchor="middle">${d.followup}</text><text x="${290+i*310}" y="402" font-family="Arial" font-size="24" text-anchor="middle">Station ${d.station}</text>`).join('')}
<text x="80" y="447" font-family="Arial" font-size="20" fill="#60666D">Mean sensor reading (arbitrary units); n = 12 per station; no inference test.</text></svg>`;
const image = await sharp(Buffer.from(chartSvg)).png().toBuffer();
await writeFile(resolve(output, 'reference-chart.png'), image);
await writeFile(resolve(output, 'reference-source.json'), JSON.stringify({
  provenance: 'Fictional sample for layout demonstration; not a real field trial.',
  units: 'arbitrary units', observations,
}, null, 2));

const p = (text, extra = {}) => new Paragraph({ text, ...extra });
const heading = (text) => p(text, { heading: HeadingLevel.HEADING_1 });
const cell = (text, header = false) => new TableCell({ children: [new Paragraph({
  children: [new TextRun({ text: String(text), bold: header })],
})] });
const table = (headers, rows, widths) => new Table({
  width: { size: 100, type: WidthType.PERCENTAGE }, columnWidths: widths,
  rows: [new TableRow({ tableHeader: true, children: headers.map((v) => cell(v, true)) }),
    ...rows.map((row) => new TableRow({ cantSplit: true, children: row.map((v) => cell(v)) }))],
});
const document = new Document({
  title: 'Sensor field review — illustrative report',
  description: 'Fictional example demonstrating editable report structure and page review.',
  styles: {
    default: { document: { run: { font: 'Arial', size: 22, color: '202124' },
      paragraph: { spacing: { after: 150, line: 270 }, widowControl: true } } },
    paragraphStyles: [
      { id: 'Title', name: 'Title', basedOn: 'Normal', next: 'Normal',
        run: { size: 42, bold: true }, paragraph: { spacing: { after: 240 }, keepNext: true } },
      { id: 'Heading1', name: 'Heading 1', basedOn: 'Normal', next: 'Normal',
        run: { size: 29, bold: true, color: '244C62' },
        paragraph: { spacing: { before: 260, after: 140 }, keepNext: true } },
      { id: 'Caption', name: 'Caption', basedOn: 'Normal', next: 'Normal',
        run: { size: 19, color: '60666D' }, paragraph: { spacing: { after: 180 } } },
    ],
  },
  sections: [{
    properties: { page: { size: { width: 12240, height: 15840 },
      margin: { top: 1080, right: 1080, bottom: 1080, left: 1080 } } },
    footers: { default: new Footer({ children: [new Paragraph({ alignment: 'right',
      children: [new TextRun({ text: 'Illustrative reference · ', size: 18 }),
        new TextRun({ children: ['Page ', PageNumber.CURRENT], size: 18 })],
    })] }) },
    children: [
      p('Sensor field review', { heading: HeadingLevel.TITLE }),
      p('Illustrative reference report · fictional data for a layout example'),
      heading('Executive summary'),
      p('Station B has the highest follow-up mean in this fictional sample: 24 arbitrary units, compared with 19 at Station C and 16 at Station A. These are descriptive summaries. They do not establish a treatment effect, statistical significance or sensor reliability.'),
      p('The useful next decision is to verify calibration and recover the underlying paired observations before interpreting the differences. Equal observation counts make the summary easier to compare, but do not establish equivalent sampling conditions.'),
      heading('Evidence at a glance'),
      new Paragraph({ keepNext: true, children: [new ImageRun({ type: 'png', data: image,
        transformation: { width: 624, height: 239 } })] }),
      p('Figure 1. Follow-up means from reference-source.json. Units are arbitrary; all observations are fictional. No confidence intervals or significance tests are available.', { style: 'Caption' }),
      heading('Recorded observations and method'),
      p('The source records three stations, twelve observations per station and baseline and follow-up means. The table retains every recorded summary value, including the baseline, so the reader can inspect the arithmetic rather than infer it from the chart.'),
      table(['Station', 'n', 'Baseline mean', 'Follow-up mean', 'Change'], observations.map((d) =>
        [d.station, d.count, d.baseline, d.followup, d.followup-d.baseline]), [1700, 900, 2500, 2500, 2480]),
      p('Table 1. Means and differences in arbitrary units. Change = follow-up mean minus baseline mean.', { style: 'Caption' }),
      heading('Interpretation'),
      p('Station B increases by seven units between the two summaries; Station C increases by one and Station A decreases by two. This ordering is supported by the supplied values. The source lacks individual observations, variation estimates and environmental context, so these differences should remain exploratory.'),
      p('A common axis in Figure 1 allows direct comparison of the follow-up means. The full table supplies the baseline context. Neither view supplies the missing sampling or calibration evidence; the document retains that limitation rather than using visual emphasis as a substitute for inference.'),
      heading('Limitations that affect the decision'),
      p('No calibration records, timestamps or individual measurements accompany the summaries. The reference cannot establish whether the stations were sampled under comparable conditions. The twelve observations per station are a recorded count, not a claim of independent replication.'),
      p('The source also lacks a variance estimate. Confidence intervals and hypothesis tests would therefore be invented if added here. The report separates the observed ordering from explanations that need further evidence.'),
      heading('Recommended next steps'),
      table(['Action', 'Evidence needed', 'Purpose'], [
        ['Verify calibration', 'Instrument records', 'Check whether readings are comparable'],
        ['Recover paired observations', 'Timestamped source rows', 'Inspect spread and pairing'],
        ['Review sampling conditions', 'Protocol and environment', 'Assess alternative explanations'],
      ], [3300, 3200, 3580]),
      heading('Source and reproducibility'),
      p('reference-source.json contains the fictional input summaries. The builder computes the differences and the chart from those same values. The DOCX preserves native headings, paragraphs, a page-number field and editable tables; only the source chart is an image.'),
      p('The report uses natural pagination, a coherent style hierarchy and repeatable table headers. Review the PDF, individual page images and contact sheet before reusing its design. Adapt the structure to the actual task; sample content and page count are not requirements.'),
    ],
  }],
});
await writeFile(resolve(output, 'reference-report.docx'), await Packer.toBuffer(document));
console.log(resolve(output, 'reference-report.docx'));
