/** Validate every documented recipe against the actual pinned client dependency.
 * This is documentation/renderer qualification, not model acceptance or visual QA.
 */
import assert from 'node:assert/strict';
import { readFileSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { parseArgs } from 'node:util';
import { pathToFileURL } from 'node:url';

const { values } = parseArgs({ options: {
  'ui-root': { type: 'string' }, catalog: { type: 'string' }, out: { type: 'string' },
} });
if (!values['ui-root'] || !values.catalog) {
  throw new Error('Supply --ui-root and --catalog (the canonical workspace catalog.json).');
}
const dependency = path.resolve(values['ui-root'], 'web/node_modules/pele');
const { render, parse, detectType } = await import(pathToFileURL(path.join(dependency, 'dist/pele.js')));
const packageInfo = JSON.parse(readFileSync(path.join(dependency, 'package.json'), 'utf8'));
const catalog = JSON.parse(readFileSync(values.catalog, 'utf8'));
const guide = catalog.components['clio.mermaid.v1']['x-clio-authoring'];
assert.equal(packageInfo.version, guide.renderer.version, 'Guide and installed renderer differ');
const declared = [...readFileSync(path.join(dependency, 'dist/detect.d.ts'), 'utf8')
  .matchAll(/'([A-Za-z][A-Za-z0-9]*)'/g)].map(match => match[1]);
assert.deepEqual([...new Set(guide.families.map(f => f.family))].sort(), [...new Set(declared)].sort(),
  'The component guide must cover the entire pinned family inventory');
const examples = [
  ...guide.families.map(f => ({ name: f.family, source: f.example, family: f.family })),
  ...Object.entries(guide.notation_variants).map(([name, source]) => ({ name, source })),
  { name: 'source-encoding', source: guide.source_encoding.component_example.source },
];
const results = [];
for (const example of examples) {
  const model = parse(example.source);
  const family = detectType(example.source);
  if (example.family) assert.equal(family, example.family, example.name);
  const rendered = render(example.source, { maxWidth: 900, links: false, images: false });
  assert.ok(rendered.svg.includes('<svg'), example.name);
  // Indentation-only parsers can accept invented keywords as visible labels.
  // Check the intended tree, not just that the sample returns an SVG.
  if (example.name === 'ishikawa') {
    assert.equal(model.root.text, 'Missing records');
    assert.equal(model.root.children[0].text, 'Inputs');
    assert.deepEqual(model.root.children[0].children.map(n => n.text), ['Late arrival', 'Invalid format']);
  }
  results.push({ name: example.name, family, svg_bytes: rendered.svg.length });
}
const receipt = { renderer: packageInfo.version, families: guide.families.length, results };
if (values.out) writeFileSync(values.out, JSON.stringify(receipt, null, 2));
console.log(`${guide.families.length} families, ${results.length} recipes parsed and rendered`);
