// Helpers for step files run by scripts/cloud/ui_check.mjs (the browser surface).
//
// A step file:
//   import * as ui from '../../.claude/skills/verifier-clio/scripts/ui-steps.mjs';
//   export default async (page, shot) => {
//     await ui.openSession(page, 'sess_...');
//     await ui.expandUsedTools(page);
//     await shot('transcript');
//   };
//
// Run it at both widths and read every screenshot:
//   node scripts/cloud/ui_check.mjs --steps FILE --label NAME
//   node scripts/cloud/ui_check.mjs --steps FILE --label NAME-phone --width 390 --height 844

export const UI = process.env.CLIO_UI_URL || 'http://127.0.0.1:5173';

/** Open a conversation and wait for its transcript to settle. */
export async function openSession(page, sid, workspace = 'ws_default') {
  await page.goto(`${UI}/workspaces/${workspace}/sessions/${sid}`, { waitUntil: 'networkidle' });
  await page.waitForTimeout(4000);
}

/** Open a Settings page by its id from web/src/lib/application-destinations.ts (usage, tools = MCP tools, plugins, providers, models, fleet, permissions, ...). */
export async function openSettings(page, section) {
  await page.goto(`${UI}/settings/${section}`, { waitUntil: 'networkidle' });
  await page.waitForTimeout(2500);
}

/** Expand every collapsed "Used tools" group in the transcript. */
export async function expandUsedTools(page) {
  const groups = page.getByRole('button', { name: /Used tools/ });
  const n = await groups.count();
  for (let i = 0; i < n; i += 1) await groups.nth(i).click().catch(() => {});
  await page.waitForTimeout(1200);
}

/**
 * Open the right-hand panel and add a tab from its + menu ("Observability",
 * "Work", "File explorer", "Session artifacts", "Conversation agent", ...).
 * The panel toggle and the + sit at the top right; `?panel=` does not open it.
 */
export async function openPanelTab(page, name) {
  const { width } = page.viewportSize();
  await page.mouse.click(width - 24, 19);
  await page.waitForTimeout(1200);
  await page.mouse.click(width - 56, 19);
  await page.waitForTimeout(800);
  await page.getByRole('menuitem', { name: new RegExp(name) }).first().click();
  await page.waitForTimeout(2500);
}

/** In Observability: open Background tasks and reveal finished work. */
export async function showCompletedWork(page) {
  await page.getByRole('button', { name: /Background tasks/ }).first().click().catch(() => {});
  await page.waitForTimeout(1000);
  await page.getByRole('button', { name: /Show completed work/ }).first().click().catch(() => {});
  await page.waitForTimeout(2000);
}

/** Type into the composer and send, as a person would. */
export async function send(page, text) {
  const composer = page.locator('div[contenteditable="true"][role="combobox"], div[contenteditable="true"][role="textbox"]').first();
  await composer.click();
  await composer.fill(text);
  await page.getByRole('button', { name: /^(Send|Submit)$/ }).first().click();
}

/** Wait until the page shows `text` (string or RegExp), or fail with what it showed. */
export async function expectText(page, text, timeout = 30000) {
  await page.getByText(text).first().waitFor({ timeout });
}
