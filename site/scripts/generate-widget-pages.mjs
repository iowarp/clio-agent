import { mkdir, readFile, readdir, rm, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { widgets, widgetFieldHelp } from '../src/data/widget-library.mjs';

const source = resolve(process.argv[2] ?? '../external/gact-tui/web/src/test-fixtures/a2ui/v0_9_1/catalogs/clio-workspace/v1/catalog.json');
const catalog = JSON.parse(await readFile(source, 'utf8'));
const target = resolve('src/content/docs/docs/widgets');
await mkdir(target, { recursive: true });

const missing = widgets.filter((item) => !catalog.components[item.name]);
const extra = Object.keys(catalog.components).filter((name) => !widgets.some((item) => item.name === name));
if (missing.length || extra.length) throw new Error(`Widget docs do not match the catalog. Missing: ${missing.map((item) => item.name)}. Extra: ${extra}`);

for (const file of await readdir(target)) {
  if (file.endsWith('.mdx') && !['linked-data.mdx', 'interaction.mdx', 'components.mdx'].includes(file)) await rm(resolve(target, file));
}
const reference = {};
for (const widget of widgets) {
  const schema = catalog.components[widget.name];
  const parts = schema.allOf ?? [schema];
  const properties = Object.assign({}, ...parts.map((part) => part.properties ?? {}));
  const required = new Set(parts.flatMap((part) => part.required ?? []));
  reference[widget.name] = {
    description: schema.description ?? '',
    properties: Object.entries(properties).filter(([name]) => name !== 'component').map(([name, definition]) => ({
      name,
      required: required.has(name),
      description: definition.description ?? widgetFieldHelp[widget.name]?.[name] ?? '',
      type: definition.type ?? (definition.enum ? definition.enum.map(String).join(' | ') : definition.$ref ? 'value' : 'object'),
    })),
  };
  const description = String(schema.description ?? '').replaceAll('"', '\\"');
  const page = `---\ntitle: "${widget.label}"\ndescription: "${description}"\ntableOfContents: false\n---\n\nimport WidgetDetail from '../../../../components/WidgetDetail.astro';\n\n<WidgetDetail name="${widget.name}" />\n`;
  await writeFile(resolve(target, `${widget.slug}.mdx`), page);
}
await writeFile(resolve('src/data/widget-reference.json'), `${JSON.stringify(reference, null, 2)}\n`);
console.log(`Generated ${widgets.length} native widget reference pages from the component catalog.`);
