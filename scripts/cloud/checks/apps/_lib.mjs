// Shared helpers for the apps qualification steps (scripts/cloud/checks/apps/*.mjs).
//
// The agent address comes from AGENT_URL, else AGENT_PORT, as ui_check's --agent does.
// Steps open a fresh conversation in a workspace folder under .cloud-run/apps-work/,
// send plain requests through the real composer, answer permission prompts as a
// person allowing them for the session would, and wait for the turn to settle.
import { mkdirSync } from "node:fs";
import path from "node:path";

export const AGENT = process.env.AGENT_URL || `http://127.0.0.1:${process.env.AGENT_PORT || 8787}`;
export const ROOT = process.env.CLAUDE_PROJECT_DIR || process.cwd();

export async function api(method, route, body) {
  const response = await fetch(AGENT + route, {
    method,
    headers: { "content-type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await response.text();
  if (!response.ok) throw new Error(`${method} ${route}: HTTP ${response.status} ${text.slice(0, 300)}`);
  return text ? JSON.parse(text) : {};
}

/** A workspace folder under .cloud-run/apps-work/<name>, and a new conversation in it, opened in the UI. */
export async function openConversation(page, name) {
  const root = path.join(ROOT, ".cloud-run", "apps-work", name);
  mkdirSync(root, { recursive: true });
  const listed = await api("GET", "/v1/workspaces");
  const rows = Array.isArray(listed) ? listed : listed.workspaces || listed.items || [];
  const workspace =
    rows.find((w) => w.root_path === root) || (await api("POST", "/v1/workspaces", { name, root_path: root }));
  const session = await api("POST", "/v1/sessions", { workspace_id: workspace.id });
  await page.goto(new URL(`/workspaces/${workspace.id}/sessions/${session.id}`, page.url()).toString(), {
    waitUntil: "domcontentloaded",
    timeout: 120000,
  });
  await page.waitForTimeout(4000);
  return { root, workspace, session };
}

export async function allowPending(sessionId, log) {
  const listed = await api("GET", `/v1/permissions?session_id=${sessionId}&status=pending`);
  for (const row of listed.permissions || listed.items || []) {
    const id = row.id || row.permission_id;
    if (!id) continue;
    log.push(`allowed: ${row.summary || row.title || row.tool_name || id}`);
    await api("POST", `/v1/permissions/${id}`, { action: "allow_session" });
  }
}

export async function waitIdle(page, sessionId, log, minutes = 8) {
  const until = Date.now() + minutes * 60_000;
  let quiet = 0;
  while (Date.now() < until) {
    await allowPending(sessionId, log);
    const session = await api("GET", `/v1/sessions/${sessionId}`);
    quiet = ["idle", "completed"].includes(session.status) ? quiet + 1 : 0;
    if (quiet >= 3) return true;
    await page.waitForTimeout(3000);
  }
  return false;
}

/** Type a plain request into the conversation's composer and send it. */
export async function send(page, text) {
  // A new conversation's composer can take a while: its agent's servers start first.
  const composer = page.locator('textarea, [contenteditable="true"]').last();
  await composer.waitFor({ state: "visible", timeout: 120000 });
  await page.waitForFunction(
    (el) => el && !el.disabled && el.getAttribute("aria-disabled") !== "true",
    await composer.elementHandle(),
    { timeout: 120000 },
  );
  await composer.click();
  await page.keyboard.type(text);
  await page.keyboard.press("Enter");
}

/** The tool calls the conversation holds, as `server.tool` names with their status. */
export async function toolCalls(sessionId) {
  const listed = await api("GET", `/v1/sessions/${sessionId}/messages`);
  const rows = Array.isArray(listed) ? listed : listed.messages || listed.items || [];
  return rows.flatMap((m) =>
    (m.parts || [])
      .filter((p) => p.type === "tool_call" || p.type === "mcp_app")
      .map((p) => (p.type === "mcp_app" ? `app:${p.source_server}/${p.tool_name}` : `${p.tool_name}:${p.status || p.state?.status || ""}`)),
  );
}

/**
 * The MCP App frames on the page: every frame loaded from an app's sandbox route, with
 * its element's title and height. Found through the frame tree, so a frame inside a
 * shadow root counts too.
 */
export async function appFrames(page) {
  const rows = [];
  for (const frame of page.frames()) {
    if (!/\/mcp-apps\/[^/]+\/sandbox/.test(frame.url())) continue;
    const element = await frame.frameElement().catch(() => null);
    const box = element ? await element.boundingBox().catch(() => null) : null;
    const title = element ? await element.getAttribute("title").catch(() => "") : "";
    rows.push({ app: frame.url().split("/mcp-apps/")[1].split("/")[0], title, height: box?.height ?? 0, element });
  }
  return rows;
}

/** Scroll the last app frame into view and drop the element handles for printing. */
export async function showLastApp(page, frames) {
  const last = frames.at(-1);
  if (last?.element) await last.element.scrollIntoViewIfNeeded().catch(() => {});
  await page.waitForTimeout(3000);
  return frames.map(({ element, ...row }) => row);
}

/**
 * A "hero" screenshot of a view at desktop size: the left navigation collapsed and, when the
 * workbench shows a tab, the workbench maximized so the view fills the frame. Maximizing
 * remounts an App's panel (it replays), so take it last in a step.
 */
export async function heroShot(page, shot, name, before) {
  const sidebar = page.getByRole("button", { name: "Toggle Sidebar" }).first();
  const navOpen = await page.getByRole("button", { name: "New conversation" }).first().isVisible().catch(() => false);
  if (navOpen && (await sidebar.count())) await sidebar.click().catch(() => undefined);
  const maximize = page.getByRole("button", { name: "Maximize canvas" }).first();
  if (await page.locator("[data-mcp-app-tab]:visible").count()) {
    if (await maximize.count()) await maximize.click().catch(() => undefined);
  }
  await page.waitForTimeout(6000);
  if (before) await before();
  await page.waitForTimeout(1000);
  await shot(name);
  // Put the canvas back beside the conversation for whatever the step does next.
  const restore = page.getByRole("button", { name: "Restore canvas beside conversation" }).first();
  if (await restore.count()) await restore.click().catch(() => undefined);
}

/**
 * A view's own document: the frame inside CLIO's sandbox frame. CLIO writes the view into
 * that frame (MC-F4), so its address is the sandbox's, not about:srcdoc; an older CLIO
 * set srcdoc. Either way its parent is the sandbox document.
 */
export function isViewDocument(frame) {
  return /\/mcp-apps\/[^/]+\/sandbox/.test(frame.parentFrame()?.url() || "");
}
