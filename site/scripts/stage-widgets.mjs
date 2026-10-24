import { cp, mkdir, mkdtemp, readFile, readdir, rm, stat, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { resolve, sep } from 'node:path';
import { createThermalSpecimen } from './thermal-specimen.mjs';
import { spawnSync } from 'node:child_process';

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
await writeFile(resolve(target, 'gallery', 'thermal-specimen.glb'), createThermalSpecimen(await readFile(resolve(target, 'gallery', 'load-specimen.glb'))));
// The published gallery shows the presentation skill and the standard agent's
// composed prompt from this site checkout, not the development snapshot
// bundled for the standalone gact-tui preview.
const presentationSkill = resolve('..', 'external', 'clio-agent-marketplace', 'plugins', 'visuals', 'skills', 'present-interactive-analysis', 'SKILL.md');
if (!(await stat(presentationSkill).catch(() => null))?.isFile()) {
  throw new Error(`Presentation skill missing: ${presentationSkill}`);
}
await mkdir(resolve(target, 'gallery-skills'), { recursive: true });
await cp(presentationSkill, resolve(target, 'gallery-skills', 'present-interactive-analysis.md'));
// Resolve the standard agent against a throwaway plugin store so the staged
// prompt reflects the shipped marketplace, never a developer's installs.
const store = await mkdtemp(resolve(tmpdir(), 'clio-site-store-'));
const composed = spawnSync('uv', ['run', '--project', '..', 'clio-agent', 'plugin', 'resolve', 'clio/standard', '--prompt'], {
  encoding: 'utf8',
  env: { ...process.env, HOME: store, XDG_CONFIG_HOME: resolve(store, 'config'), XDG_DATA_HOME: resolve(store, 'data'), XDG_CACHE_HOME: resolve(store, 'cache') },
  stdio: ['ignore', 'pipe', 'inherit'],
});
await rm(store, { recursive: true, force: true });
if (composed.status !== 0 || !composed.stdout.trim()) throw new Error('Composing the standard agent prompt failed.');
await writeFile(resolve(target, 'gallery-skills', 'standard-agent.md'), composed.stdout);
const skills = spawnSync('uv', ['run', '--no-project', 'python', 'scripts/render-agent-component-skills.py', resolve(source, '..', 'src/test-fixtures/a2ui/v0_9_1/catalogs'), resolve(target, 'gallery-skills/component-skills.json')], { stdio: 'inherit' });
if (skills.status !== 0) throw new Error('The production agent skill resolver failed.');
console.log(`Staged the CLIO widget gallery in ${target}`);
