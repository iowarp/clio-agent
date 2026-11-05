// #1905 live check on Linux: a plain request to copy a workspace file outside the fence.
//
// BOUNDARY_ROOT names a fresh workspace folder (inside CLIO_ALLOWED_ROOTS) holding
// notes.txt; BOUNDARY_OUTSIDE a folder outside the sandbox territory. The step asks,
// plainly, for the copy; answers each permission request as a person approving it would
// (a folder request: allow for the workspace; anything else: allow for the session);
// waits for the turn; captures the transcript with its activity opened; and prints the
// calls (with any access_paths), whether a result carried a boundary diagnosis, and
// whether the copy exists. With BOUNDARY_UI_GRANT=1 it does not answer folder requests:
// it clicks the "Allow <folder>" control on the blocked call instead, then asks again.
import fs from "node:fs";
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

async function answerPending(sessionId, log, { folders }) {
  const listed = await api("GET", `/v1/permissions?session_id=${sessionId}&status=pending`);
  for (const row of listed.permissions || listed.items || []) {
    const id = row.id || row.permission_id;
    if (!id) continue;
    const actions = row.actions || [];
    const folder = actions.includes("allow_workspace") && !actions.includes("allow_session");
    if (folder && !folders) continue; // left for the person's click in the UI case
    const action = folder ? "allow_workspace" : "allow_session";
    log.push(`${action}: ${row.summary || row.title || row.tool_name || id}`);
    await api("POST", `/v1/permissions/${id}`, { action });
  }
}

async function waitIdle(page, sessionId, log, minutes, options) {
  const until = Date.now() + minutes * 60_000;
  let quiet = 0;
  while (Date.now() < until) {
    await answerPending(sessionId, log, options);
    const session = await api("GET", `/v1/sessions/${sessionId}`);
    quiet = ["idle", "completed"].includes(session.status) ? quiet + 1 : 0;
    if (quiet >= 3) return true;
    await page.waitForTimeout(3000);
  }
  return false;
}

async function send(page, text) {
  const composer = page.locator('textarea[placeholder^="Ask"], [contenteditable="true"][aria-placeholder^="Ask"]').first();
  await composer.click();
  await page.keyboard.type(text);
  await page.keyboard.press("Enter");
}

export default async function (page, shot) {
  const root = process.env.BOUNDARY_ROOT;
  const outside = process.env.BOUNDARY_OUTSIDE;
  const viaUi = process.env.BOUNDARY_UI_GRANT === "1";
  const log = [];
  const workspace = await api("POST", "/v1/workspaces", { name: path.basename(root), root_path: root });
  const session = await api("POST", "/v1/sessions", { workspace_id: workspace.id });
  await page.goto(new URL(`/workspaces/${workspace.id}/sessions/${session.id}`, page.url()).toString());
  await page.waitForTimeout(5000);
  await send(
    page,
    (process.env.BOUNDARY_TEXT || "Please copy notes.txt from this folder to {outside} so I have it there too.").replaceAll(
      "{outside}",
      outside,
    ),
  );
  log.push(`turn settled: ${await waitIdle(page, session.id, log, 8, { folders: !viaUi })}`);
  await shot("reply");
  const activity = page.getByRole("button", { name: /used tools|ran commands/ });
  if (await activity.count()) await activity.first().click();
  await page.waitForTimeout(800);
  // Open the shell call rows so their results (and any sandbox notice) show.
  const runs = page.locator('[data-slot="tool-activity"]', { hasText: "Sandbox:" });
  for (let i = 0; i < (await runs.count()); i += 1) {
    await runs.nth(i).getByText("Run", { exact: true }).first().click();
  }
  await page.waitForTimeout(800);
  await shot("activity");
  const allow = page.getByRole("button", { name: /^Allow / });
  log.push(`allow controls: ${await allow.count()}`);
  if (viaUi && (await allow.count())) {
    await allow.first().scrollIntoViewIfNeeded();
    await shot("blocked-call");
    await allow.first().click();
    await page.waitForTimeout(1500);
    await shot("allowed");
    await send(page, "I allowed that folder. Please try the copy again.");
    log.push(`retry settled: ${await waitIdle(page, session.id, log, 6, { folders: false })}`);
    await shot("after-retry");
  }
  const listed = await api("GET", `/v1/sessions/${session.id}/messages`);
  const rows = Array.isArray(listed) ? listed : listed.messages || listed.items || [];
  const parts = rows.flatMap((m) => m.parts || []);
  const calls = parts
    .filter((p) => p.type === "tool_call")
    .map((p) => ({ tool: p.tool_name, access_paths: (p.input || {}).access_paths || null }));
  const boundaries = parts
    .filter((p) => p.type === "tool_result" && /write_denied|elevation_required/.test(JSON.stringify(p)))
    .map((p) => p.tool_name);
  console.log(
    JSON.stringify(
      {
        session: session.id,
        calls,
        resultsWithBoundary: boundaries,
        copied: fs.existsSync(path.join(outside, "notes.txt")),
        log,
      },
      null,
      2,
    ),
  );
}
