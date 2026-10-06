import { cp, mkdir, readFile, readdir, rm, stat, writeFile } from 'node:fs/promises';
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
// The published gallery shows the skill from this site checkout, not the
// development snapshot bundled for the standalone gact-tui preview.
const presentationSkill = resolve('..', 'src', 'clio_agent', 'gact', 'builtin_skills', 'present-interactive-analysis', 'SKILL.md');
if (!(await stat(presentationSkill).catch(() => null))?.isFile()) {
  throw new Error(`Presentation skill missing: ${presentationSkill}`);
}
await mkdir(resolve(target, 'gallery-skills'), { recursive: true });
await cp(presentationSkill, resolve(target, 'gallery-skills', 'present-interactive-analysis.md'));
const marketplaceAgent = resolve('..', 'external', 'clio-agent-marketplace', 'base-agent');
if (!(await stat(resolve(marketplaceAgent, 'AGENT.md')).catch(() => null))?.isFile()) {
  throw new Error(`Standard agent missing: ${marketplaceAgent}`);
}
const stagedAgent = resolve(target, 'gallery-skills', 'base-agent');
await mkdir(resolve(stagedAgent, 'experts'), { recursive: true });
await cp(resolve(marketplaceAgent, 'AGENT.md'), resolve(stagedAgent, 'AGENT.md'));
await cp(resolve(marketplaceAgent, 'experts', 'base.md'), resolve(stagedAgent, 'experts', 'base.md'));
const skills = spawnSync('uv', ['run', '--no-project', 'python', 'scripts/render-agent-component-skills.py', resolve(source, '..', 'src/test-fixtures/a2ui/v0_9_1/catalogs'), resolve(target, 'gallery-skills/component-skills.json')], { stdio: 'inherit' });
if (skills.status !== 0) throw new Error('The production agent skill resolver failed.');
console.log(`Staged the CLIO widget gallery in ${target}`);
