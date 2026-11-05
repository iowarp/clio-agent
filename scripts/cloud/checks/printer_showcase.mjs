// The printer case end to end (issue #1908): a plain request, answered as a person would.
//
// PRINTER_ROOT names a fresh workspace folder (inside CLIO_ALLOWED_ROOTS); PRINTER_URL
// the fixture's status endpoint (scripts/cloud/fixtures/printer_status.py). The step
// opens a new conversation there, sends "Keep an eye on my 3D printer and show me how
// it's doing", allows each question for the session, waits for the turn to finish,
// opens the published dashboard, and captures it twice to show it updating. It then
// asks how the printer is doing and, once the print ends, captures the stopped view.
// It prints the calls the agent made and the stream requests the page sent.
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

async function calls(sessionId) {
  const listed = await api("GET", `/v1/sessions/${sessionId}/messages`);
  const rows = Array.isArray(listed) ? listed : listed.messages || listed.items || [];
  return rows.flatMap((m) => (m.parts || []).filter((p) => p.type === "tool_call"));
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
  // The conversation's composer: the editable field in the form with the Send button.
  const composer = page
    .locator('form:has(button[type="submit"]) textarea, form:has(button[type="submit"]) [contenteditable="true"]')
    .last();
  await composer.click();
  await page.keyboard.type(text);
  await page.keyboard.press("Enter");
}

export default async function (page, shot) {
  const root = process.env.PRINTER_ROOT;
  const url = process.env.PRINTER_URL || "http://127.0.0.1:8799/status";
  const streamRequests = [];
  page.on("request", (request) => {
    if (request.url().includes("/v1/streams/")) streamRequests.push(request.url());
  });
  const log = [];
  const workspace = await api("POST", "/v1/workspaces", {
    name: path.basename(root),
    root_path: root,
  });
  const session = await api("POST", "/v1/sessions", { workspace_id: workspace.id });
  await page.goto(new URL(`/workspaces/${workspace.id}/sessions/${session.id}`, page.url()).toString());
  await page.waitForTimeout(5000);
  await send(page, `Keep an eye on my 3D printer and show me how it's doing. Its status is at ${url}.`);
  const settled = await waitIdle(page, session.id, log, 12);
  log.push(`first turn settled: ${settled}`);
  await shot("reply");

  // The agent's view of the stream is in the conversation; it should update by itself.
  await page.waitForTimeout(6000);
  await shot("dashboard");
  const before = streamRequests.length;
  await page.waitForTimeout(10000);
  await shot("dashboard-10s-later");
  log.push(`stream requests in 10 s with the dashboard open: ${streamRequests.length - before}`);

  await send(page, "How is it doing?");
  await waitIdle(page, session.id, log, 5);
  await shot("how-is-it-doing");

  // After the print ends the script ends, its stream stops and the view says so.
  const waitDone = Number(process.env.PRINTER_WAIT_DONE_S || 0);
  if (waitDone > 0) {
    await page.waitForTimeout(waitDone * 1000);
    await shot("after-the-print");
  }
  const made = await calls(session.id);
  console.log(
    JSON.stringify(
      {
        session: session.id,
        workspace: workspace.id,
        calls: made.map((p) => p.tool_name),
        streamRequests: streamRequests.length,
        log,
      },
      null,
      2,
    ),
  );
}
