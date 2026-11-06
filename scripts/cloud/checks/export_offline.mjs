// Export a session as HTML from the UI and read it offline (#85: a stream view keeps its
// state; published dashboards are saved views).
//
// EXPORT_WORKSPACE and EXPORT_SESSION name an existing conversation. EXPORT_TEXT, when
// set, is sent first (for example a plain request for a dashboard), each question allowed
// for the session, and the turn awaited. The step then exports the conversation from its
// actions menu (Export session › Download HTML), opens the file offline, captures the
// conversation and the Saved results tab, and prints what the offline page says about the
// stream (Closed / Captured) and how many dashboards it shows.
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

async function waitIdle(page, sessionId, log, minutes) {
  const until = Date.now() + minutes * 60_000;
  let quiet = 0;
  while (Date.now() < until) {
    const listed = await api("GET", `/v1/permissions?session_id=${sessionId}&status=pending`);
    for (const row of listed.permissions || listed.items || []) {
      const id = row.id || row.permission_id;
      if (!id) continue;
      log.push(`allowed: ${row.summary || row.title || row.tool_name || id}`);
      await api("POST", `/v1/permissions/${id}`, { action: "allow_session" });
    }
    const session = await api("GET", `/v1/sessions/${sessionId}`);
    quiet = ["idle", "completed"].includes(session.status) ? quiet + 1 : 0;
    if (quiet >= 3) return true;
    await page.waitForTimeout(4000);
  }
  return false;
}

export default async function (page, shot) {
  const workspace = process.env.EXPORT_WORKSPACE;
  const sessionId = process.env.EXPORT_SESSION;
  const out = process.env.EXPORT_DIR || "/tmp";
  const log = [];
  await page.goto(new URL(`/workspaces/${workspace}/sessions/${sessionId}`, page.url()).toString());
  await page.waitForTimeout(5000);
  if (process.env.EXPORT_TEXT) {
    const editable = page.locator('[contenteditable="true"]').last();
    const field = (await editable.isVisible().catch(() => false))
      ? editable
      : page.locator('form:has(button[type="submit"]) textarea').first();
    await field.click();
    await page.keyboard.type(process.env.EXPORT_TEXT);
    await page.keyboard.press("Enter");
    log.push(`turn settled: ${await waitIdle(page, sessionId, log, 12)}`);
    const dashboards = await api("GET", `/v1/sessions/${sessionId}/dashboards`);
    log.push(`published dashboards: ${Array.isArray(dashboards) ? dashboards.length : 0}`);
    await shot("before-export");
  }
  await page.getByRole("button", { name: /^Actions for / }).first().click();
  await page.getByRole("menuitem", { name: /Export session/ }).click();
  const download = page.waitForEvent("download", { timeout: 180_000 });
  await page.getByRole("menuitem", { name: /^Download HTML/ }).click();
  const file = path.join(out, `export-${sessionId}.html`);
  await (await download).saveAs(file);
  log.push(`exported: ${file}`);

  const offline = page; // the same page, now reading the file with no server behind it
  const errors = [];
  offline.on("console", (message) => message.type() === "error" && errors.push(message.text()));
  await offline.goto(`file://${file}`);
  await offline.waitForTimeout(8000);
  await offline.screenshot({ path: file.replace(/\.html$/, "-conversation.png"), fullPage: false });
  const body = await offline.locator("body").innerText();
  log.push(`closed note: ${/Closed at .*showing its last entries/.test(body)}`);
  log.push(`captured note: ${/Captured at .* while live/.test(body)}`);
  const results = offline.getByRole("tab", { name: /Results/ });
  if (await results.count()) {
    await results.first().click();
    await offline.waitForTimeout(6000);
    await offline.screenshot({ path: file.replace(/\.html$/, "-results.png"), fullPage: false });
    log.push(`dashboards shown: ${await offline.locator('section[aria-label^="Dashboard: "]').count()}`);
    log.push(`no views recorded: ${(await offline.locator("body").innerText()).includes("No interactive views were recorded")}`);
  }
  log.push(`offline console errors: ${errors.length}${errors.length ? ` (${errors[0].slice(0, 160)})` : ""}`);
  console.log(JSON.stringify({ session: sessionId, log }, null, 2));
}
