import { cp, mkdir, readdir, rm, stat } from 'node:fs/promises';
import { resolve, sep } from 'node:path';

const source = resolve(process.argv[2] ?? '');
const publicDirectory = resolve('public');
const target = resolve(publicDirectory, 'widgets');

if (!process.argv[2]) {
  throw new Error('Pass the standalone gact-tui web/dist directory to stage-widgets.');
}
if (!target.startsWith(`${publicDirectory}${sep}`)) {
  throw new Error('The widgets destination must stay inside site/public.');
}
if (!(await stat(resolve(source, 'widget-preview.html')).catch(() => null))?.isFile()) {
  throw new Error(`No widget-preview.html found in ${source}. Build the standalone gallery first.`);
}

await rm(target, { recursive: true, force: true });
await mkdir(target, { recursive: true });
for (const entry of await readdir(source)) {
  await cp(resolve(source, entry), resolve(target, entry), { recursive: true });
}
console.log(`Staged the CLIO widget gallery in ${target}`);
