// A stream conversation driven as a person would: a list of requests, each answered and
// captured, with the stream reads the page makes while it waits.
//
// FEED_ROOT names a fresh workspace folder (inside CLIO_ALLOWED_ROOTS). FEED_TURNS is a JSON
// list of {"text": "...", "after_s": N, "label": "..."}: each text is sent in the
// conversation, every question is allowed for the session, the turn is waited for, then
// the page waits after_s seconds (counting the stream requests it sends meanwhile) and is
// captured as label. FEED_OPEN_STREAM names a stream to open in the side panel after the
// first turn (from the conversation's artifacts), captured live and after each later turn.
// It prints the calls the agent made, the stream requests per wait and the session id.
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
  const composer = page
    .locator('form:has(button[type="submit"]) textarea, form:has(button[type="submit"]) [contenteditable="true"]')
    .last();
  await composer.click();
  await page.keyboard.type(text);
  await page.keyboard.press("Enter");
}

export default async function (page, shot) {
  const root = process.env.FEED_ROOT;
  const turns = JSON.parse(process.env.FEED_TURNS || "[]");
  const streamRequests = [];
  page.on("request", (request) => {
    if (request.url().includes("/v1/streams/")) streamRequests.push(request.url());
  });
  const log = [];
  const workspace = await api("POST", "/v1/workspaces", {
    name: path.basename(root),
    root_path: root,
  });
  const workspaceId = workspace.id || workspace.error?.details?.workspace_id;
  const session = await api("POST", "/v1/sessions", { workspace_id: workspaceId });
  await page.goto(new URL(`/workspaces/${workspaceId}/sessions/${session.id}`, page.url()).toString());
  await page.waitForTimeout(5000);
  const waits = [];
  for (const [index, turn] of turns.entries()) {
    await send(page, turn.text);
    const settled = await waitIdle(page, session.id, log, turn.minutes || 10);
    log.push(`turn ${index + 1} settled: ${settled}`);
    if (index === 0 && process.env.FEED_OPEN_STREAM) {
      const name = process.env.FEED_OPEN_STREAM;
      const link = page.getByRole("button", { name: new RegExp(`^${name}\\b`, "u") }).first();
      if (await link.count()) await link.click().catch(() => log.push("open stream: click failed"));
      else log.push(`open stream: no button named ${name}`);
      await page.waitForTimeout(2000);
    }
    const before = streamRequests.length;
    await page.waitForTimeout((turn.after_s || 0) * 1000);
    waits.push({ label: turn.label, streamRequests: streamRequests.length - before });
    await shot(turn.label || `turn-${index + 1}`);
  }
  const made = await calls(session.id);
  console.log(
    JSON.stringify(
      {
        session: session.id,
        workspace: workspaceId,
        calls: made.map((p) => p.tool_name),
        waits,
        log,
      },
      null,
      2,
    ),
  );
}
