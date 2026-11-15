// Shared helpers for the app host checks (MA1-MA7, MA15-MA17).
// The view sits two frames deep: CLIO's sandbox frame, then the view's own frame.
import { openConversation, send, waitIdle } from "./_lib.mjs";

export { openConversation };

export const agentUrl = () =>
  process.env.CHECK_AGENT || `http://127.0.0.1:${process.env.AGENT_PORT || 8787}`;

export function sessionIdFrom(page) {
  const match = new URL(page.url()).pathname.match(/\/sessions\/([^/]+)/);
  if (!match) throw new Error(`no session in ${page.url()}`);
  return decodeURIComponent(match[1]);
}

/** The view inside the frame at `placement` (inline, tab or surface). */
export function viewIn(page, placement) {
  return page
    .frameLocator(`[data-mcp-app-placement="${placement}"] [data-mcp-app-iframe]`)
    .frameLocator("iframe");
}

export async function frameCount(page) {
  return page.locator("[data-mcp-app-iframe]").count();
}

export async function expectText(locator, pattern, timeout = 30000) {
  const deadline = Date.now() + timeout;
  let last = "";
  while (Date.now() < deadline) {
    last = (await locator.textContent({ timeout: 2000 }).catch(() => "")) ?? "";
    if (pattern.test(last)) return last;
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  throw new Error(`expected ${pattern} but saw ${JSON.stringify(last)}`);
}

export function check(condition, message) {
  if (!condition) throw new Error(`check failed: ${message}`);
}

export async function json(path, init) {
  const response = await fetch(`${agentUrl()}${path}`, init);
  return { status: response.status, body: await response.json().catch(() => null) };
}

/** Send a plain request through the composer and wait for the turn to settle. */
export async function sendAndSettle(page, text) {
  const sid = sessionIdFrom(page);
  const log = [];
  await send(page, text);
  await page.waitForTimeout(3000);
  const settled = await waitIdle(page, sid, log, 6);
  for (const line of log) console.error(`[check] ${line}`);
  if (!settled) throw new Error(`the turn for ${JSON.stringify(text)} did not settle`);
  await page.waitForTimeout(1500);
}

export function log(label, value) {
  console.error(`[check] ${label}: ${typeof value === "string" ? value : JSON.stringify(value)}`);
}
