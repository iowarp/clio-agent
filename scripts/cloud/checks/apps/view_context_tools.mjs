// Apps check (ctx-tools stream): a view's context reaches the agent and its own tools
// are offered and routed to it, with the view in a workbench tab (AQ18, AQ20, AQ21, AQ22).
//
// Needs the fixture board server saved as the MCP connection "board", always loaded:
//   tests/fixtures/mcp_apps/view_tools_server.py
// Run on a fresh stack:
//   node scripts/cloud/ui_check.mjs --agent http://127.0.0.1:$AGENT_PORT \
//     --ui http://127.0.0.1:$UI_PORT/ --steps scripts/cloud/checks/apps/view_context_tools.mjs \
//     --label view-tools [--width 390 --height 844, with CHECK_PLACEMENT=inline]
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
import { send, waitIdle } from "./_lib.mjs";

const phases = new Set((process.env.CHECK_PHASES || "open,context,tool,changed,closed").split(","));
const settleMs = Number(process.env.CHECK_SETTLE_MS || 300000);

/** Type a plain request, allow the board's server tool for the session, wait for idle. */
async function ask(page, text) {
  const allowed = [];
  await send(page, text);
  await page.waitForURL(/\/sessions\//, { timeout: 180000 });
  const sid = sessionIdFrom(page);
  await page.waitForTimeout(5000);
  const settled = await waitIdle(page, sid, allowed, settleMs / 60000);
  log(`asked "${text}" in`, { sid, settled, allowed });
  return sid;
}

/** Desktop hero: navigation collapsed, the canvas widened beside the agent's tool call. */
async function hero(page, shot) {
  const toggle = page.getByRole("button", { name: "Toggle Sidebar" }).first();
  if (await toggle.isVisible().catch(() => false)) await toggle.click();
  await page.waitForTimeout(800);
  const handle = page.getByRole("separator", { name: "Resize workspace canvas" }).first();
  const box = await handle.boundingBox().catch(() => null);
  if (box) {
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    await page.mouse.down();
    await page.mouse.move(560, box.y + box.height / 2, { steps: 12 });
    await page.mouse.up();
  }
  const group = page.getByRole("button", { name: /^Expand Called/ }).last();
  if (await group.isVisible().catch(() => false)) await group.click().catch(() => {});
  await page.waitForTimeout(1500);
  await shot("hero-board-in-tab-agent-highlight");
}

async function boardPart(sid) {
  const { body } = await json(`/v1/sessions/${sid}/messages`);
  const parts = (body?.messages || []).flatMap((m) => m.parts || []);
  return parts.filter((p) => p.type === "mcp_app").at(-1);
}

export default async function (page, shot) {
  let sid = "";
  // CHECK_PLACEMENT=inline keeps the view in its card (at phone width the canvas covers
  // the conversation, so the person works with the card there).
  const placement = process.env.CHECK_PLACEMENT || "tab";
  const tab = () => viewIn(page, placement);
  const showTab = async () => {
    if (placement === "inline") {
      await page.locator("[data-mcp-app-iframe]").last().scrollIntoViewIfNeeded().catch(() => {});
    }
    await page.waitForTimeout(1500);
  };
  if (phases.has("open")) {
    sid = await ask(page, "Open the cooling loop readings board.");
    const inline = viewIn(page, "inline");
    await inline.getByRole("button", { name: /Pump B vibration/ }).waitFor({ timeout: 60000 });
    if (placement === "tab") {
      await page.getByRole("button", { name: "Open in tab" }).first().click();
      await page.locator("[data-mcp-app-tab]").first().waitFor({ timeout: 30000 });
    }
    await showTab();
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
    if (process.env.CHECK_DUMP_BUTTONS) {
      log("url", page.url());
      log(
        "buttons",
        await page.evaluate(() =>
          [...document.querySelectorAll("button")]
            .filter((b) => b.offsetParent)
            .map((b) => (b.getAttribute("aria-label") || b.textContent || "").trim().slice(0, 40)),
        ),
      );
    }
  }
  if (phases.has("context")) {
    sid = await ask(page, "What am I looking at?");
    await shot("what-am-i-looking-at");
  }
  if (phases.has("tool")) {
    sid = await ask(page, "Highlight the outlet temperature on the board.");
    await showTab();
    await expectText(tab().locator("#activity"), /Highlighted Outlet temperature/, 30000);
    await shot("board-highlighted");
    if (placement === "tab" && (page.viewportSize()?.width ?? 0) >= 1200) await hero(page, shot);
  }
  if (phases.has("changed")) {
    sid = await ask(page, "Add a note on the board: check pump B bearings.");
    await showTab();
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
