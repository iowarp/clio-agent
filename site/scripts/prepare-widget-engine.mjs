import { copyFile, mkdir, readFile, stat, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { widgets, widgetUses, widgetFieldHelp } from '../src/data/widget-library.mjs';

const checkout = resolve(process.argv[2] ?? '../external/gact-tui');
const web = resolve(checkout, 'web');
const source = resolve('widget-engine');
const backup = resolve(web, '.clio-docs-original');
await mkdir(backup, { recursive: true });

// Apply to the saved original so preparing twice cannot nest patches.
async function original(relative) {
  const saved = resolve(backup, relative.replaceAll('/', '__'));
  if (!(await stat(saved).catch(() => null))) await copyFile(resolve(web, relative), saved);
  return readFile(saved, 'utf8');
}

const config = await original('vite.config.ts');
const anchor = "gallery: fileURLToPath(new URL('./widget-preview.html', import.meta.url)),";
const addition = "widgetDocs: fileURLToPath(new URL('./widget-docs.html', import.meta.url)),";
const galleryOnly = "? { gallery: fileURLToPath(new URL('./widget-preview.html', import.meta.url)) }";
if (!config.includes(anchor)) throw new Error('Widget gallery Vite entry not found.');
await writeFile(resolve(web, 'vite.config.ts'), config.includes(galleryOnly)
  ? config.replace(galleryOnly, `? { ${anchor} ${addition} }`)
  : config.replace(anchor, `${anchor}\n        ${addition}`));

const repository = await original('src/hooks/use-repository.ts');
const galleryRoute = "window.location.pathname.endsWith('/widget-preview.html')";
if (!repository.includes(galleryRoute)) throw new Error('Gallery repository route not found.');
await writeFile(resolve(web, 'src/hooks/use-repository.ts'), repository
  .replace("import { createGalleryRepository }", "import { createDocsRepository } from '@/docs-repository'\nimport { createGalleryRepository }")
  .replace('return window.location.pathname', "if (document.querySelector('[data-clio-widget-live]')) return createDocsRepository(createGalleryRepository(repository))\n    return window.location.pathname"));

const preview = await original('src/widget-preview.tsx');
const rootMount = "createRoot(document.getElementById('root')!).render(";
if (!preview.includes(rootMount)) throw new Error('Standalone gallery mount not found.');
await writeFile(resolve(web, 'src/widget-preview.tsx'), preview
  .replace('function HurricaneShowcase()', 'export function HurricaneShowcase()')
  .replace('function IntroShowcase()', 'export function IntroShowcase()')
  .replace(rootMount, "const docsRoot = document.getElementById('root');\nif (docsRoot) createRoot(docsRoot).render("));
await original('src/gallery-skill-dialog.tsx');
// Use the production components with their shared sizing and toolbar fixes.
// Vega's fit autosize includes legends; subtracting their width a second time
// leaves an empty strip beside every chart.
const chartPath = 'src/components/clio/use-chart-view.ts';
const chart = await original(chartPath);
await writeFile(resolve(web, chartPath), chart.replace(/\/\/ Vega lays legends outside the plot width\.[\s\S]*?width: Math\.max\(220, measuredWidth - \(colorField \|\| seriesLegend\.visible \? 112 : 0\)\),/u, 'width: Math.max(220, measuredWidth),'));
const meshPath = 'src/components/clio/a2ui-mesh-viewport.tsx';
const mesh = await original(meshPath);
await writeFile(resolve(web, meshPath), mesh.replace(/          \{boxButton\(\)\}\r?\n          \{zoomButtons\(\)\}\r?\n          <SurfaceToolbar capabilities=\{toolbarCapabilities\} floating=\{false\} \/>/u, '          <div className="flex shrink-0 items-center gap-0.5">\n            {boxButton()}\n            {zoomButtons()}\n            <SurfaceToolbar capabilities={toolbarCapabilities} floating={false} />\n          </div>'));
const galleryPath = 'src/widget-gallery.tsx';
const gallery = await original(galleryPath);
await writeFile(resolve(web, galleryPath), gallery.replace('>Contract</button>', '>Skill</button>'));
for (const file of ['widget-docs.tsx', 'docs-repository.ts', 'simulation-example.tsx', 'gallery-skill-dialog.tsx']) {
  await copyFile(resolve(source, file), resolve(web, 'src', file));
}
await copyFile(resolve(source, 'widget-docs.html'), resolve(web, 'widget-docs.html'));
await writeFile(resolve(web, 'src/docs-widget-guidance.ts'), `export const WIDGET_GUIDANCE: Record<string, {label: string; purpose: string; fields: Record<string, string>}> = ${JSON.stringify(Object.fromEntries(widgets.map((widget) => [widget.name, { label: widget.label, purpose: widgetUses[widget.name], fields: widgetFieldHelp[widget.name] ?? {} }])))};\n`);
console.log(`Prepared the native docs widget entry in ${checkout}`);
