# JavaScript slides with PptxGenJS

Use the command and package version from `prepare_execution_runtime`. The managed
stack currently locks `pptxgenjs` 4.0.1. Save the builder in its returned
JavaScript workspace and execute it with `javascript.script_argv`. PptxGenJS
creates new decks; it does not import an existing presentation for editing.

Read `examples/create-reference-deck.mjs` for a complete runnable reference:
six themed slides with an editable native chart, tables, source captions and
speaker notes. Copy the builder into the prepared JavaScript workspace and run
it with an absolute output-directory argument. It writes a PPTX and explicitly
fictional source data without changing user files. Render and inspect it before
using it as a visual reference. Its slide count and fictional content are not a
template for every task.

Slide coordinates and dimensions use inches; font sizes use points. `LAYOUT_WIDE`
is approximately 13.33 × 7.5 inches. Set the layout and font theme explicitly,
then use shared colours and margins. A master is useful for common footer or
background objects, but should not force every slide into the same composition.

This runnable library example shows a theme, editable text and table, and notes.
Its one-slide content is a capability check, not a finished presentation.

```javascript
import pptxgen from 'pptxgenjs';

const deck = new pptxgen();
deck.layout = 'LAYOUT_WIDE';
deck.title = 'Evidence review';
deck.subject = 'Source-supported meeting presentation';
deck.theme = { headFontFace: 'Arial', bodyFontFace: 'Arial', lang: 'en-US' };
const colours = { ink: '202124', accent: '244C62', muted: '60666D', paper: 'FFFFFF' };
const slide = deck.addSlide();
slide.background = { color: colours.paper };
slide.addText('What the evidence establishes', {
  x: 0.65, y: 0.5, w: 12.0, h: 0.65, fontSize: 32,
  bold: true, color: colours.ink, margin: 0, breakLine: false,
});
slide.addText('Replace this example with a specific, source-supported finding.', {
  x: 0.65, y: 1.45, w: 11.9, h: 0.8, fontSize: 22,
  color: colours.ink, margin: 0, valign: 'top',
});
slide.addTable([
  [{ text: 'Evidence', options: { bold: true } }, { text: 'Limitation', options: { bold: true } }],
  ['Source identifier and observation', 'Material uncertainty from the source'],
], {
  x: 0.65, y: 2.65, w: 12.0, colW: [6, 6], rowH: 0.65,
  fontFace: 'Arial', fontSize: 20, color: colours.ink,
  margin: 0.14, border: { type: 'solid', color: 'D7DDE1', pt: 0.6 },
});
slide.addText('Source: identify the supplied evidence; preserve its units.', {
  x: 0.65, y: 6.7, w: 11.8, h: 0.3, fontSize: 12,
  color: colours.muted, margin: 0,
});
slide.addNotes('Explain the finding, source, uncertainty and presenter context here.');
await deck.writeFile({ fileName: process.argv[2] ?? 'evidence-review.pptx' });
```

For a source figure, use `deck.imageSizingContain(path, x, y, w, h)` and pass
its result to `slide.addImage({ path, ...sizing })`. This preserves aspect ratio
inside the allotted box. Use `imageSizingCrop` only for an intentional crop;
do not crop chart axes, legends or evidence to decorate the layout.

For bullets, use native text objects and their `bullet` options rather than a
literal bullet glyph. Text bounds do not guarantee rendered fit. Avoid automatic
shrink-to-fit as the first response to overflow: shorten or split the content
before sacrificing legibility. Use notes for references and supporting context,
and keep essential limitations visible on the slide.

Official references:
- https://gitbrent.github.io/PptxGenJS/docs/usage-pres-options.html
- https://gitbrent.github.io/PptxGenJS/docs/api-images/
- https://gitbrent.github.io/PptxGenJS/docs/speaker-notes/
