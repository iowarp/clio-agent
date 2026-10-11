/** Capture production UI over retained real-model acceptance, without route fixtures. */
import { createRequire } from 'node:module';
import { readFile, mkdir, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const require = createRequire(path.join(root, 'external/gact-tui/web/package.json'));
const { chromium, expect } = require('@playwright/test');
const [statePath, out, name, mode] = process.argv.slice(2);
if (!statePath || !out || !name) throw new Error('Expected state JSON, output directory and capture name');
const state = JSON.parse(await readFile(statePath, 'utf8'));
const endpoint = process.env.CLIO_CAPTURE_ENDPOINT ?? 'http://127.0.0.1:17963';
const ui = process.env.CLIO_CAPTURE_UI ?? 'http://127.0.0.1:5189';
await mkdir(path.join(out, 'originals'), { recursive: true });
const browser = await chromium.launch({ headless: true });
let ownedSession;
try {
  const context = await browser.newContext({ viewport: { width: 1440, height: mode === 'question' ? 1000 : 760 }, deviceScaleFactor: 1 });
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.addInitScript(endpoint => {
    localStorage.setItem('clio.recent-connections', JSON.stringify([endpoint]));
    localStorage.setItem('theme', 'light');
  }, endpoint);
  let sid = Object.values(state.sessions)[0];
  if (mode === 'commands') {
    const response = await context.request.post(`${endpoint}/v1/sessions`, { data: { title: 'Plan a bounded loop', workspace_id: state.workspace } });
    expect(response.ok()).toBe(true);
    sid = (await response.json()).id;
    ownedSession = sid;
  }
  await page.goto(`${ui}/workspaces/${state.workspace}/sessions/${sid}`);
  const editor = page.getByRole('combobox', { name: /^Ask .+ to investigate/ });
  await expect(editor).toBeVisible({ timeout: 45000 });
  if (mode === 'commands') {
    for (const command of [
      '/goal --max-iters=3 --max-hours=1 Three fictional checkpoint reports have been saved.',
      '/loop 1h --max-iters=3 --max-hours=1 Save the next numbered fictional checkpoint report. Reschedule with loop_wakeup if another report is needed.',
    ]) {
      const result = page.waitForResponse(response => response.url().includes(`/sessions/${sid}/commands/`) && response.request().method() === 'POST');
      await editor.fill(command);
      await editor.press('Enter');
      expect((await result).ok()).toBe(true);
    }
    const response = await context.request.get(`${endpoint}/v1/sessions/${sid}`);
    const session = await response.json();
    expect(session.metadata.goal.max_wallclock_s).toBe(3600);
    expect(session.metadata.loop.max_iters).toBe(3);
    expect(session.metadata.loop.active).toBe(true);
  }
  await page.getByRole('button', { name: /^Evidence layout:/ }).click();
  await page.getByRole('button', { name: 'Open Work view', exact: true }).click();
  const work = page.getByRole('tabpanel', { name: 'Work', exact: true });
  await expect(work).toBeVisible();
  await expect(work.getByRole('heading', { name: /^Goals:/ })).toBeVisible();
  for (const kind of ['goal', 'loop']) {
    const previous = work.getByRole('button', { name: new RegExp(`^Previous \\d+ ${kind} records$`) });
    if (await previous.count()) await previous.click();
  }
  const divider = page.getByRole('separator', { name: 'Resize workspace canvas' });
  const bounds = await divider.boundingBox();
  if (bounds) {
    await page.mouse.move(bounds.x + bounds.width / 2, bounds.y + bounds.height / 2);
    await page.mouse.down();
    await page.mouse.move(900, bounds.y + bounds.height / 2, { steps: 20 });
    await page.mouse.up();
  }
  if (mode === 'work') {
    for (const title of ['Todos', 'Schedules']) {
      await work.getByRole('button', { name: new RegExp(`^${title}:`) }).click();
    }
  }
  await page.getByRole('log', { name: 'Conversation' }).evaluate(element => element.scrollTo({ top: element.scrollHeight, behavior: 'instant' }));
  await page.mouse.move(1400, 730);
  await page.screenshot({ path: path.join(out, 'originals', name + '.png') });
  const clip = mode === 'work' ? { x: 900, y: 0, width: 540, height: 710 }
    : mode === 'question' ? { x: 248, y: 0, width: 652, height: 980 }
    : { x: 248, y: 0, width: 1192, height: 740 };
  await page.screenshot({ path: path.join(out, name + '.png'), clip });
  await writeFile(path.join(out, name + '.json'), JSON.stringify({ url: page.url(), sid, captured_at: new Date().toISOString(), errors }, null, 2));
  expect(errors).toEqual([]);
  console.log(`Captured ${name}: ${sid}`);
} finally {
  try {
    if (ownedSession) {
      const response = await fetch(`${endpoint}/v1/sessions/${ownedSession}/commands/loop`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ input: 'stop' }) });
      if (!response.ok) throw new Error(`Cleanup failed: ${response.status}`);
    }
  } finally {
    await browser.close();
  }
}
