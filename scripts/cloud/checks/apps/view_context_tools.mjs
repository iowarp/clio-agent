// Apps check (ctx-tools stream): a view's context reaches the agent and its own tools
// are offered and routed to it, with the view in a workbench tab (AQ18, AQ20, AQ21, AQ22).
//
// Needs the fixture board server saved as the MCP connection "board", always loaded:
//   tests/fixtures/mcp_apps/view_tools_server.py
// Run on a fresh stack:
//   node scripts/cloud/ui_check.mjs --agent http://127.0.0.1:$AGENT_PORT \
//     --ui http://127.0.0.1:$UI_PORT/ --steps scripts/cloud/checks/apps/view_context_tools.mjs \
//     --label view-tools [--width 390 --height 844]
//
// Every prompt is the person's plain request. CHECK_PHASES (comma list, default all)
// picks the phases:
//   open     "Open the cooling loop readings board.", Open in tab, the person selects Pump B
//   context  "What am I looking at?" (AQ18)
//   tool     "Highlight the outlet temperature on the board." (AQ20)
//   changed  "Add a note on the board: check pump B bearings." then "Make the board's
//            highlights orange." (AQ22: the new tool is offered, the removed one is gone)
//   closed   closes the app the way closing its last frame does (DELETE on the app), then
//            "Highlight the inlet temperature on the board." (AQ21)
// The session id and app instance are printed for the ledger.
import { agentUrl, check, expectText, json, log, sessionIdFrom, viewIn } from "./_app_view.mjs";

const phases = new Set((process.env.CHECK_PHASES || "open,context,tool,changed,closed").split(","));
const settleMs = Number(process.env.CHECK_SETTLE_MS || 300000);

/** Type a plain request, approve the board's server tool when asked, wait for idle. */
async function ask(page, text) {
  const composer = page.locator('textarea, [contenteditable="true"]').first();
  await composer.waitFor({ state: "visible", timeout: 180000 });
  await composer.click({ timeout: 120000 });
  await page.keyboard.type(text);
  await page.keyboard.press("Enter");
  await page.waitForURL(/\/sessions\//, { timeout: 180000 });
  const sid = sessionIdFrom(page);
  await page.waitForTimeout(3000);
  const deadline = Date.now() + settleMs;
  // On a loaded machine the turn can take a while to start: idle counts only after the
  // session was seen busy (or after 90 s without ever seeing it busy).
  const started = Date.now();
  let seenBusy = false;
  while (Date.now() < deadline) {
    const allow = page.getByRole("button", { name: /This session/ }).first();
    if (await allow.isVisible().catch(() => false)) await allow.click().catch(() => {});
    const { body } = await json(`/v1/sessions/${sid}`);
    const settled = body?.status === "idle" || body?.status === "error";
    if (!settled) seenBusy = true;
    if (settled && (seenBusy || Date.now() - started > 90000)) break;
    await page.waitForTimeout(1500);
  }
  await page.waitForTimeout(2000);
  log(`asked "${text}" in`, sid);
  return sid;
}

async function boardPart(sid) {
  const { body } = await json(`/v1/sessions/${sid}/messages`);
  const parts = (body?.messages || []).flatMap((m) => m.parts || []);
  return parts.filter((p) => p.type === "mcp_app").at(-1);
}

export default async function (page, shot) {
  let sid = "";
  const tab = () => viewIn(page, "tab");
  if (phases.has("open")) {
    sid = await ask(page, "Open the cooling loop readings board.");
    const inline = viewIn(page, "inline");
    await inline.getByRole("button", { name: /Pump B vibration/ }).waitFor({ timeout: 60000 });
    await page.getByRole("button", { name: "Open in tab" }).first().click();
    await page.locator("[data-mcp-app-tab]").first().waitFor({ timeout: 30000 });
    await tab().getByRole("button", { name: /Pump B vibration/ }).click({ timeout: 60000 });
    await expectText(tab().locator("#activity"), /Selected Pump B/);
    await page.waitForTimeout(2500); // the view's report reaches the session record
    const part = await boardPart(sid);
    log("app instance", part?.app_instance_id);
    const { body } = await json(`/v1/sessions/${sid}`);
    const stored = body?.metadata?.mcp_app_context?.[part?.app_instance_id];
    log("stored context", stored?.context?.content?.[0]?.text);
    check(/selected: Pump B/.test(stored?.context?.content?.[0]?.text || ""), "context stored");
    await shot("board-in-tab-selected");
  }
  if (phases.has("context")) {
    sid = await ask(page, "What am I looking at?");
    await shot("what-am-i-looking-at");
  }
  if (phases.has("tool")) {
    sid = await ask(page, "Highlight the outlet temperature on the board.");
    await expectText(tab().locator("#activity"), /Highlighted Outlet temperature/, 30000);
    await shot("board-highlighted");
  }
  if (phases.has("changed")) {
    sid = await ask(page, "Add a note on the board: check pump B bearings.");
    await expectText(tab().locator("#notes"), /bearings/i, 30000);
    await shot("board-note");
    sid = await ask(page, "Make the board's highlights orange.");
    await shot("board-colour-request");
  }
  if (phases.has("closed")) {
    const part = await boardPart(sid);
    const closed = await fetch(
      `${agentUrl()}/v1/sessions/${sid}/mcp-apps/${part.app_instance_id}?data_ref=${encodeURIComponent(part.data_ref)}`,
      { method: "DELETE" },
    );
    log("closed app", { app: part.app_instance_id, status: closed.status });
    await page.waitForTimeout(1500);
    sid = await ask(page, "Highlight the inlet temperature on the board.");
    await shot("closed-board-request");
  }
  log("session", sid);
}
