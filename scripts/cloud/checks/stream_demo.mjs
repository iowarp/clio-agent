// A recorded demo of a live stream conversation (run with ui_check.mjs --video).
//
// DEMO_ROOT names a fresh workspace folder (inside CLIO_ALLOWED_ROOTS). DEMO_TURNS is a JSON
// list of {"text": "...", "minutes": N, "after_s": N}: each text is typed into the
// conversation as a person would, every question is allowed for the session, the turn is
// waited for, then the page stays on screen after_s seconds (the stream keeps updating).
// A turn with "wait_closed": true is sent only once the conversation's streams have closed
// (at most 8 minutes); "scroll": true then scrolls the canvas down through a dashboard and
// back, so every chart is seen.
// The left navigation is closed and the canvas opened to DEMO_CANVAS of the width (0.5),
// so a stream or dashboard the agent opens fills the right half. It prints the session id.
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

async function allowPending(sessionId) {
  const listed = await api("GET", `/v1/permissions?session_id=${sessionId}&status=pending`);
  for (const row of listed.permissions || listed.items || []) {
    const id = row.id || row.permission_id;
    if (id) await api("POST", `/v1/permissions/${id}`, { action: "allow_session" });
  }
}

async function waitIdle(page, sessionId, minutes) {
  const until = Date.now() + minutes * 60_000;
  let quiet = 0;
  while (Date.now() < until) {
    await allowPending(sessionId);
    const session = await api("GET", `/v1/sessions/${sessionId}`);
    quiet = ["idle", "completed"].includes(session.status) ? quiet + 1 : 0;
    if (quiet >= 3) return true;
    await page.waitForTimeout(2000);
  }
  return false;
}

async function streamsClosed(sessionId) {
  const listed = await api("GET", `/v1/sessions/${sessionId}/artifacts`);
  const streams = (listed.artifacts || []).filter((a) => a.kind === "stream");
  if (!streams.length) return false;
  for (const record of streams) {
    const id = record.versions?.at(-1)?.producer?.stream_id;
    const stream = id ? await api("GET", `/v1/streams/${id}`) : {};
    if (stream.state !== "closed") return false;
  }
  return true;
}

async function scrollCanvas(page) {
  // Scroll the canvas's own scroll container (the wheel over a chart is the chart's), down
  // through a dashboard in three steps and back to the top.
  const viewport = page.viewportSize();
  if (!viewport) return;
  const at = { x: viewport.width - 220, y: viewport.height * 0.6 };
  for (const [fraction, pause] of [[0.35, 9000], [0.7, 9000], [1, 9000], [0, 3000]]) {
    await page.evaluate(
      ({ x, y, fraction }) => {
        let node = document.elementFromPoint(x, y);
        while (node && !(node.scrollHeight > node.clientHeight + 4 && /(auto|scroll)/.test(getComputedStyle(node).overflowY)))
          node = node.parentElement;
        if (node) node.scrollTo({ top: (node.scrollHeight - node.clientHeight) * fraction, behavior: "smooth" });
      },
      { ...at, fraction },
    );
    await page.waitForTimeout(pause);
  }
}

export default async function (page, shot) {
  const root = process.env.DEMO_ROOT;
  const turns = JSON.parse(process.env.DEMO_TURNS || "[]");
  const share = Number(process.env.DEMO_CANVAS || 0.5);
  const workspace = await api("POST", "/v1/workspaces", { name: path.basename(root), root_path: root });
  const workspaceId = workspace.id || workspace.error?.details?.workspace_id;
  const session = await api("POST", "/v1/sessions", { workspace_id: workspaceId });
  await page.goto(new URL(`/workspaces/${workspaceId}/sessions/${session.id}`, page.url()).toString());
  await page.waitForTimeout(4000);

  // The left navigation closed; the canvas open at `share` of the width.
  await page.locator('[data-slot="sidebar-trigger"]').first().click();
  await page.waitForTimeout(800);
  const open = page.getByRole("button", { name: "Open workspace canvas" });
  if (await open.count()) await open.first().click();
  await page.waitForTimeout(1200);
  const handle = page.getByRole("separator", { name: "Resize workspace canvas" }).first();
  const box = await handle.boundingBox();
  const viewport = page.viewportSize();
  if (box && viewport) {
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    await page.mouse.down();
    await page.mouse.move(viewport.width * (1 - share), box.y + box.height / 2, { steps: 12 });
    await page.mouse.up();
  }
  await page.waitForTimeout(1500);
  await shot("layout");

  const composer = page
    .locator('form:has(button[type="submit"]) textarea, form:has(button[type="submit"]) [contenteditable="true"]')
    .last();
  for (const [index, turn] of turns.entries()) {
    if (turn.wait_closed) {
      const until = Date.now() + 8 * 60_000;
      while (Date.now() < until && !(await streamsClosed(session.id))) {
        await allowPending(session.id);
        await page.waitForTimeout(3000);
      }
      await page.waitForTimeout(4000);
    }
    await composer.click();
    await page.keyboard.type(turn.text, { delay: 18 });
    await page.waitForTimeout(600);
    await page.keyboard.press("Enter");
    await waitIdle(page, session.id, turn.minutes || 10);
    if (turn.scroll) await scrollCanvas(page);
    await page.waitForTimeout((turn.after_s || 0) * 1000);
    await shot(`turn-${index + 1}`);
  }
  console.log(JSON.stringify({ session: session.id, workspace: workspaceId }));
}
