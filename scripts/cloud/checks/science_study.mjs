// A study from a plain request on the science agent (CK17): its views and its Science log.
//
// STUDY_ROOT names a fresh workspace folder (inside CLIO_ALLOWED_ROOTS); STUDY_TEXT the
// request (a plain one, naming no tools or steps); STUDY_AGENT the agent (science). The
// step opens a conversation there, sends the request, allows each question for the
// session, waits for the turn to finish, then opens the Science log from the canvas menu
// and captures its list and, when the study has views, its Views mode. It prints the
// calls the agent made, the study's entries by type and its views.
import path from "node:path";

const AGENT = process.env.AGENT_URL || "http://127.0.0.1:8787";

async function api(method, route, body) {
  const response = await fetch(AGENT + route, {
    method,
    headers: { "content-type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await response.text();
  return text ? JSON.parse(text) : {};
}

async function allowPending(sessionId, log) {
  const listed = await api("GET", `/v1/permissions?session_id=${sessionId}&status=pending`);
  for (const row of listed.permissions || listed.items || []) {
    const id = row.id || row.permission_id;
    if (!id) continue;
    log.push(`allowed: ${row.summary || row.title || row.tool_name || id}`);
    await api("POST", `/v1/permissions/${id}`, { action: "allow_session" });
  }
}

async function waitIdle(page, sessionId, log, minutes) {
  const until = Date.now() + minutes * 60_000;
  let quiet = 0;
  while (Date.now() < until) {
    await allowPending(sessionId, log);
    const session = await api("GET", `/v1/sessions/${sessionId}`);
    quiet = ["idle", "completed"].includes(session.status) ? quiet + 1 : 0;
    if (quiet >= 3) return true;
    await page.waitForTimeout(4000);
  }
  return false;
}

async function send(page, text) {
  const composer = page
    .locator('form:has(button[type="submit"]) textarea, form:has(button[type="submit"]) [contenteditable="true"]')
    .last();
  await composer.click();
  await page.keyboard.type(text);
  await page.keyboard.press("Enter");
}

export default async function (page, shot) {
  const root = process.env.STUDY_ROOT;
  const text = process.env.STUDY_TEXT;
  const log = [];
  const workspace = await api("POST", "/v1/workspaces", { name: path.basename(root), root_path: root });
  const session = await api("POST", "/v1/sessions", {
    workspace_id: workspace.id,
    agent: { id: process.env.STUDY_AGENT || "science" },
  });
  await page.goto(new URL(`/workspaces/${workspace.id}/sessions/${session.id}`, page.url()).toString());
  await page.waitForTimeout(5000);
  await send(page, text);
  log.push(`turn settled: ${await waitIdle(page, session.id, log, 25)}`);
  await shot("reply");

  const study = await api("GET", `/v1/sessions/${session.id}/science-log`);
  const openCanvas = page.getByRole("button", { name: "Open workspace canvas" });
  if (await openCanvas.count()) await openCanvas.first().click();
  await page.waitForTimeout(1500);
  await page.getByTitle("Open a canvas tab").click();
  const item = page.getByRole("menuitem", { name: "Science log" });
  if (await item.count()) {
    await item.click();
    await page.waitForTimeout(4000);
    await shot("science-log");
    const views = page.getByRole("radio", { name: "Views" });
    if (await views.count()) {
      await views.first().click();
      await page.waitForTimeout(6000);
      await shot("science-log-views");
    }
  } else {
    log.push("no Science log in the canvas menu");
    await page.keyboard.press("Escape");
  }
  const listed = await api("GET", `/v1/sessions/${session.id}/messages`);
  const rows = Array.isArray(listed) ? listed : listed.messages || listed.items || [];
  const calls = rows.flatMap((m) => (m.parts || []).filter((p) => p.type === "tool_call"));
  const entries = {};
  for (const entry of study.entries || []) entries[entry.type] = (entries[entry.type] || 0) + 1;
  console.log(
    JSON.stringify(
      {
        session: session.id,
        workspace: workspace.id,
        calls: calls.map((p) => p.tool_name),
        skills: calls.filter((p) => p.tool_name === "load_skill").map((p) => (p.input || {}).name),
        entries,
        views: (study.views || []).map((v) => ({ title: v.title, kind: v.kind, dashboard: v.dashboard_id })),
        log,
      },
      null,
      2,
    ),
  );
}
