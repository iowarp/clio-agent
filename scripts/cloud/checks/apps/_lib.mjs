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

/** The MCP App frames on the page, by their accessible titles. */
export async function appFrames(page) {
  return page.locator("iframe[data-mcp-app-iframe]").evaluateAll((frames) =>
    frames.map((f) => ({ id: f.getAttribute("data-mcp-app-iframe"), title: f.title, height: f.clientHeight })),
  );
}
