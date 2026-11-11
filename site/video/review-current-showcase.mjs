/** Capture fictional files through the current, unmodified product UI. */
import { chromium } from '../node_modules/@playwright/test/index.mjs';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';

const output = resolve(process.env.CLIO_SHOWCASE_EVIDENCE ?? 'out/current-showcase');
const scene = process.env.CLIO_CAPTURE_SCENE ?? 'dashboard';
const sizes = { dashboard: [1440, 1280], data: [1120, 420], lineage: [1120, 420], report: [1120, 520] };
if (!sizes[scene]) throw new Error('Unknown capture scene');
await mkdir(output, { recursive: true });
const runtime = JSON.parse(await readFile(resolve(process.env.CLIO_SHOWCASE_RUNTIME ?? resolve(output, 'runtime'), 'runtime.json'), 'utf8'));
if (!runtime.synthetic_only) throw new Error('Website captures require a synthetic-only service');
const ui = `http://127.0.0.1:${runtime.ui_port}`;
const browser = await chromium.launch({ channel: 'chrome', headless: true });
const [width, height] = sizes[scene];
const context = await browser.newContext({ viewport: { width, height }, deviceScaleFactor: 1, colorScheme: 'light' });
await context.addInitScript(() => {
  localStorage.setItem('clio.appearance.v1', JSON.stringify({ theme: 'light', motion: 'reduced' }));
});
const page = await context.newPage();
const errors = [];
page.on('pageerror', (error) => errors.push(error.message));
try {
  const example = runtime.example;
  await page.goto(`${ui}/?intent=connect`);
  await page.getByLabel('Connection address').fill(`http://127.0.0.1:${runtime.backend_port}`);
  await page.getByRole('button', { name: /^Connect/ }).click();
  await page.waitForURL((url) => !url.search.includes('intent=connect'), { timeout: 60000 });
  await page.goto(`${ui}/workspaces/${example.workspace_id}/sessions/${example.session_id}`);
  await page.evaluate(() => document.fonts.ready);
  const unprotected = page.getByRole('button', { name: 'Continue without protection' });
  if (await unprotected.waitFor({ timeout: 30000 }).then(() => true, () => false)) await unprotected.click();
  await page.getByRole('button', { name: 'Open workspace canvas', exact: true }).click();
  await page.getByRole('button', { name: 'Artifacts', exact: true }).first().click();
  await page.getByText('bike-stations.csv').first().waitFor();
  if (scene !== 'data') {
    await page.getByText(scene === 'dashboard' ? 'Morning availability' : 'morning-findings.md').first().click();
  }
  await page.getByRole('button', { name: 'Maximize canvas', exact: true }).click();
  if (width >= 1440 && await page.getByRole('button', { name: 'New conversation', exact: true }).isVisible()) {
    await page.getByRole('button', { name: 'Toggle Sidebar' }).first().click();
  }
  if (scene === 'lineage') {
    await page.getByRole('tab', { name: 'Lineage', exact: true }).click();
    await page.getByRole('button', { name: 'bike-stations.csv, v1', exact: true }).waitFor();
    if (await page.locator('button[aria-label$=", v1"], button[aria-label$=", ran"]').count() !== 9) {
      throw new Error('Expected all three declared evidence branches and their report join');
    }
  } else if (scene === 'dashboard') {
    await page.locator('.vega-embed canvas, .vega-embed svg').first().waitFor();
    await page.waitForFunction(() => {
      const surfaces = [...document.querySelectorAll('[data-slot="a2ui-map-surface"]')];
      return surfaces.length > 0 && surfaces.every((surface) =>
        surface.__clioMap?.loaded() && surface.__clioMap?.areTilesLoaded()
          && !surface.__clioMapFailure,
      );
    }, null, {timeout:60000});
  } else if (scene === 'report') {
    await page.getByRole('heading', {name:'Where bikes leave and accumulate'}).waitFor();
  }
  await page.waitForTimeout(2000);
  const body = await page.locator('body').innerText();
  if (/opal/i.test(body)) throw new Error('Private research context must never enter a website capture');
  if (errors.length) throw new Error(errors.join('\n'));
  await page.screenshot({path:resolve(output, `${scene}.png`),animations:'disabled'});
  await writeFile(resolve(output, `${scene}.txt`), body);
  console.log(JSON.stringify({output,scene,errors}));
} finally {
  await browser.close();
}