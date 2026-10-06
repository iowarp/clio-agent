/** Six editable slides demonstrating a coherent theme and varied composition. */
import { mkdir, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import pptxgen from 'pptxgenjs';

const output = resolve(process.argv[2] ?? 'reference-output');
await mkdir(output, { recursive: true });
const data = [
  { station: 'A', count: 12, baseline: 18, followup: 16 },
  { station: 'B', count: 12, baseline: 17, followup: 24 },
  { station: 'C', count: 12, baseline: 18, followup: 19 },
];
await writeFile(resolve(output, 'reference-source.json'), JSON.stringify({
  provenance: 'Fictional sample for layout demonstration; not a real field trial.',
  units: 'arbitrary units', observations: data,
}, null, 2));
const deck = new pptxgen();
deck.layout = 'LAYOUT_WIDE';
deck.title = 'Sensor field review — illustrative reference';
deck.subject = 'Fictional sample: themes, evidence, editable charts and notes';
deck.theme = { headFontFace: 'Arial', bodyFontFace: 'Arial', lang: 'en-US' };
const c = { ink: '202124', accent: '244C62', muted: '60666D', rule: 'D7DDE1' };
let slideNumber = 0;
function text(slide, content, x, y, w, h, extra = {}) {
  slide.addText(content, { x, y, w, h, fontFace: 'Arial', fontSize: 22,
    color: c.ink, margin: 0, valign: 'top', ...extra });
}
function page(title, notes) {
  const slide = deck.addSlide();
  slide.background = { color: 'FFFFFF' };
  text(slide, title, .65, .5, 12, 1.15, { fontSize: 32, bold: true });
  text(slide, 'ILLUSTRATIVE DATA · reference-source.json · arbitrary units', .65, 7.02, 11.2, .2,
    { fontSize: 10, color: c.muted });
  text(slide, String(++slideNumber), 12, 7.02, .6, .2, { fontSize: 10, color: c.muted, align: 'right' });
  slide.addNotes(notes + '\nSource: reference-source.json. All data are fictional; no real trial or statistical inference is represented.');
  return slide;
}
function table(slide, rows, y, widths) {
  slide.addTable(rows, { x: .65, y, w: 12, colW: widths, rowH: .62, fontSize: 20,
    color: c.ink, margin: .13, border: { type: 'solid', pt: .5, color: c.rule } });
}
let slide = page('Sensor field review', 'Open with the descriptive finding, then distinguish observation from explanation. This is a six-slide reference for composition, not a fixed-length template.');
text(slide, 'An observed difference is a reason to inspect the data.', .65, 2.05, 10.7, 1.65,
  { fontSize: 38, bold: true, color: c.accent });
text(slide, 'Three stations · twelve observations each\nCalibration and sampling context remain unknown.', .65, 4.55, 10.7, 1.3,
  { fontSize: 24 });

slide = page('The summary retains counts and baseline context', 'The native table is editable. Explain the equal counts without claiming equivalent sampling or independent replication.');
table(slide, [['Station', 'Observations', 'Baseline mean', 'Follow-up mean'],
  ...data.map((d) => [d.station, d.count, d.baseline, d.followup])], 1.8, [2, 3, 3.5, 3.5]);
text(slide, 'Counts describe the sample. They do not establish comparable conditions.', .65, 5.2, 11.9, .9,
  { fontSize: 24, color: c.accent });

slide = page('Station B has the highest follow-up mean', 'The chart is native PowerPoint content with editable values and axis labels. The chart contains no error bars because the source has no variance estimates.');
slide.addChart(deck.ChartType.bar, [{ name: 'Follow-up mean', labels: data.map((d) => 'Station '+d.station),
  values: data.map((d) => d.followup) }], {
  x: .65, y: 1.6, w: 8.1, h: 4.75, catAxisLabelFontSize: 18, valAxisLabelFontSize: 16,
  chartColors: [c.accent], barDir: 'col', showLegend: false, showValue: true, dataLabelFormatCode: '0',
  dataLabelPosition: 'outEnd', dataLabelColor: c.ink, dataLabelFontSize: 18,
  valAxisMinVal: 0, valAxisMaxVal: 30, valAxisMajorUnit: 10,
  showCatName: false, showTitle: false, showBorder: false,
  valAxisTitle: 'Mean reading (arbitrary units)', showValAxisTitle: true,
  valAxisTitleFontSize: 16, catAxisLineShow: false,
});
text(slide, '24', 9.25, 2, 3, .95, { fontSize: 54, color: c.accent, bold: true });
text(slide, 'arbitrary units\nStation B follow-up mean', 9.25, 3.15, 3.1, 1.4, { fontSize: 22 });
text(slide, 'Descriptive ordering; no significance test.', 9.25, 5.0, 3.1, 1, { fontSize: 20, color: c.muted });

slide = page('The baseline comparison remains exploratory', 'Differences are computed directly from the source summaries. Individual paired values and variation are unavailable, so this is not a treatment-effect estimate.');
table(slide, [['Station', 'Baseline', 'Follow-up', 'Change'], ...data.map((d) =>
  [d.station, d.baseline, d.followup, (d.followup-d.baseline>0?'+':'')+(d.followup-d.baseline)])], 1.75,
  [2.5, 3, 3, 3.5]);
text(slide, 'Change = follow-up minus baseline, in arbitrary units.', .65, 4.85, 11.9, .55, { fontSize: 22 });
text(slide, 'Variation and sampling context are needed before interpreting the difference.', .65, 5.7, 11.9, .8,
  { color: c.accent, fontSize: 24 });

slide = page('Supported observations and missing evidence', 'Keep the important limitations on the slide. Notes can expand the reasoning but must not hide a caveat that changes the conclusion.');
text(slide, 'Supported by the summaries', .65, 1.75, 5.7, .6, { bold: true, color: c.accent, fontSize: 24 });
text(slide, 'The recorded counts are equal.\n\nStation B has the highest follow-up mean.\n\nThe baseline differences can be checked.', .65, 2.55, 5.6, 3.5);
text(slide, 'Still unknown', 7.05, 1.75, 5.6, .6, { bold: true, color: c.accent, fontSize: 24 });
text(slide, 'Calibration and measurement conditions.\n\nIndividual variation and pairing.\n\nReasons for the observed difference.', 7.05, 2.55, 5.6, 3.5);

slide = page('Verify measurements before interpreting differences', 'Close with the decision and the evidence required to support it. Adapt the sequence to the actual task rather than copying sample actions.');
const steps = [
  ['1', 'Check calibration', 'Recover instrument records and assess comparability.'],
  ['2', 'Recover source observations', 'Inspect timestamps, pairing and variation.'],
  ['3', 'Review sampling conditions', 'Assess alternative explanations before inference.'],
];
steps.forEach(([number, title, detail], i) => {
  const y = 1.65 + i*1.55;
  text(slide, number, .65, y, .8, .6, { fontSize: 34, color: c.accent, bold: true });
  text(slide, title, 1.8, y, 10.8, .5, { fontSize: 24, bold: true });
  text(slide, detail, 1.8, y+.6, 10.8, .65, { fontSize: 21, color: c.muted });
});
await deck.writeFile({ fileName: resolve(output, 'reference-deck.pptx') });
console.log(resolve(output, 'reference-deck.pptx'));
