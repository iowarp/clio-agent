// A background script's links to its task in Work, from a plain request.
//
// LINKS_ROOT names a fresh workspace folder (inside CLIO_ALLOWED_ROOTS); PRINTER_URL the
// fixture's status endpoint. The step asks, plainly, to watch the printer and say when
// the print is half done and when it finishes; allows each question for the session;
// waits for the first turn; follows the launch row's "Show the task in Work" link; then
// waits for the script's message, opens it and follows its link; and finally captures
// the whole transcript after the script has ended, to check that no stray rows appear
// after the turn. It prints the calls, the links found and the injections recorded.
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
    .first();
  await composer.click();
  await page.keyboard.type(text);
  await page.keyboard.press("Enter");
}

async function messages(sessionId) {
  const listed = await api("GET", `/v1/sessions/${sessionId}/messages`);
  return Array.isArray(listed) ? listed : listed.messages || listed.items || [];
}

export default async function (page, shot) {
  const root = process.env.LINKS_ROOT;
  const url = process.env.PRINTER_URL || "http://127.0.0.1:8799/status";
  const log = [];
  const workspace = await api("POST", "/v1/workspaces", { name: path.basename(root), root_path: root });
  const session = await api("POST", "/v1/sessions", { workspace_id: workspace.id });
  await page.goto(new URL(`/workspaces/${workspace.id}/sessions/${session.id}`, page.url()).toString());
  await page.waitForTimeout(5000);
  await send(
    page,
    process.env.LINKS_TEXT ||
      `Watch my 3D printer and tell me when the print is half done and when it finishes. Its status is at ${url}.`,
  );
  log.push(`first turn settled: ${await waitIdle(page, session.id, log, 10)}`);
  await shot("reply");

  // The turn's activity is one collapsed row; open it, then the script's row.
  const activity = page.getByRole("button", { name: /used tools/ });
  if (await activity.count()) await activity.first().click();
  await page.waitForTimeout(800);
  const scriptRow = page.getByRole("button", { name: /background|Script|script/ });
  log.push(`script rows: ${await scriptRow.count()}`);
  const launch = page.getByRole("button", { name: "Show the task in Work" });
  if (!(await launch.count()) && (await scriptRow.count())) {
    await scriptRow.first().click();
    await page.waitForTimeout(800);
  }
  log.push(`launch links: ${await launch.count()}`);
  if (await launch.count()) {
    await launch.first().scrollIntoViewIfNeeded();
    await shot("launch-row");
    await launch.first().click();
    await page.waitForTimeout(2500);
    await shot("work-from-launch");
    log.push(`assignment regions open: ${await page.getByRole("region", { name: /^Assignment:/ }).count()}`);
  }

  // The script's messages wake the conversation; wait for one, then read it.
  const until = Date.now() + 8 * 60_000;
  let notified = false;
  while (!notified && Date.now() < until) {
    await allowPending(session.id, log);
    const rows = await messages(session.id);
    notified = JSON.stringify(rows).includes("clio.code.notify");
    if (!notified) await page.waitForTimeout(5000);
  }
  log.push(`notify recorded: ${notified}`);
  await waitIdle(page, session.id, log, 5);
  const group = page.getByRole("button", { name: /Received a task notice/ });
  if (await group.count()) {
    await group.first().click();
    await page.waitForTimeout(800);
  }
  const notice = page.getByRole("button", { name: /^Injection Message from / });
  log.push(`notify rows: ${await notice.count()}`);
  if (await notice.count()) {
    await notice.first().scrollIntoViewIfNeeded();
    await notice.first().click();
    await page.waitForTimeout(800);
    await shot("notify-open");
    const link = page.getByRole("button", { name: /^Show .* in Work$/ });
    log.push(`notify links: ${await link.count()}`);
    if (await link.count()) {
      await link.first().click();
      await page.waitForTimeout(2500);
      await shot("work-from-notify");
    }
  }

  // After the print the script ends; its completion wakes the conversation once more.
  await page.waitForTimeout(Number(process.env.LINKS_WAIT_DONE_S || 60) * 1000);
  await waitIdle(page, session.id, log, 5);
  // Live first (stray rows appeared only in the live view), then as reloaded.
  await page.keyboard.press("End");
  await shot("transcript-live");
  await page.reload();
  await page.waitForTimeout(5000);
  await shot("transcript-after-reload");
  const rows = await messages(session.id);
  const calls = rows.flatMap((m) => (m.parts || []).filter((p) => p.type === "tool_call"));
  console.log(
    JSON.stringify(
      {
        session: session.id,
        workspace: workspace.id,
        calls: calls.map((p) => p.tool_name),
        messages: rows.map((m) => `${m.role}:${(m.parts || []).map((p) => p.type).join(",")}`),
        log,
      },
      null,
      2,
    ),
  );
}
