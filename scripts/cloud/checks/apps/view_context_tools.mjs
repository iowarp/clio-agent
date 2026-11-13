// Apps check (ctx-tools stream): a view's context reaches the agent and its own tools
// are offered and routed to it (AQ18, AQ20, AQ21, AQ22).
//
// Needs the fixture board server saved as the MCP connection "board", always loaded:
//   tests/fixtures/mcp_apps/view_tools_server.py
// Run:
//   node scripts/cloud/ui_check.mjs --agent http://127.0.0.1:$AGENT_PORT \
//     --ui http://127.0.0.1:$UI_PORT/ --steps scripts/cloud/checks/apps/view_context_tools.mjs \
//     --label view-tools [--width 390 --height 844]
//
// Each prompt is the person's plain request; CHECK_PHASES (comma list, default all)
// picks the phases: open, context, tool, changed, closed.
//   open     "Open the cooling loop readings board." then the person selects Pump B
//   context  "What am I looking at?" (AQ18)
//   tool     "Highlight the outlet temperature on the board." (AQ20)
//   changed  "Add a note on the board: check pump B bearings." then "Make the board's
//            highlights orange." (AQ22: the new tool is offered, the removed one is gone)
//   closed   closes the view the way closing its tab does (DELETE on the app), then asks
//            for a highlight again (AQ21)
// The session id and the app instance are printed for the ledger.

const agent = process.env.CHECK_AGENT || `http://127.0.0.1:${process.env.AGENT_PORT || 8787}`;
const phases = new Set((process.env.CHECK_PHASES || "open,context,tool,changed,closed").split(","));
const settleMs = Number(process.env.CHECK_SETTLE_MS || 240000);

async function sendAndSettle(page, text) {
  const composer = page.locator('textarea, [contenteditable="true"]').first();
  const posted = page
    .waitForRequest((r) => r.method() === "POST" && /\/v1\/sessions\/[^/]+\/messages$/.test(r.url()), {
      timeout: 180000,
    })
    .catch(() => null);
  await composer.waitFor({ state: "visible", timeout: 180000 });
  await composer.click({ timeout: 120000 });
  await page.keyboard.type(text);
  await page.keyboard.press("Enter");
  const request = await posted;
  const sid = request ? new URL(request.url()).pathname.split("/")[3] : "";
  if (!sid) throw new Error(`the UI posted no message for: ${text}`);
  const deadline = Date.now() + settleMs;
  await page.waitForTimeout(2000);
  while (Date.now() < deadline) {
    // The person approves the board's server tools for this conversation when asked.
    const allow = page.getByRole("button", { name: /This session/ }).first();
    if (await allow.isVisible().catch(() => false)) await allow.click().catch(() => {});
    const state = await fetch(`${agent}/v1/sessions/${sid}`).then((r) => r.json()).catch(() => ({}));
    if (state.status === "idle" || state.status === "error") break;
    await page.waitForTimeout(1500);
  }
  await page.waitForTimeout(2000);
  return sid;
}

function board(page) {
  return page.frameLocator("iframe[data-mcp-app-iframe]").last().frameLocator("iframe").first();
}

async function showBoard(page) {
  const frame = page.locator("iframe[data-mcp-app-iframe]").last();
  await frame.waitFor({ timeout: 60000 });
  await frame.scrollIntoViewIfNeeded().catch(() => {});
  await board(page).getByRole("button", { name: /Pump B vibration/ }).waitFor({ timeout: 30000 });
}

export default async function (page, shot) {
  let sid = "";
  if (phases.has("open")) {
    sid = await sendAndSettle(page, "Open the cooling loop readings board.");
    await showBoard(page);
    await board(page).getByRole("button", { name: /Pump B vibration/ }).click();
    await page.waitForTimeout(2500); // the view reports its context after the click
    await shot("board-selected");
  }
  if (phases.has("context")) {
    sid = await sendAndSettle(page, "What am I looking at?");
    await shot("what-am-i-looking-at");
  }
  if (phases.has("tool")) {
    sid = await sendAndSettle(page, "Highlight the outlet temperature on the board.");
    await showBoard(page);
    await shot("board-highlighted");
  }
  if (phases.has("changed")) {
    sid = await sendAndSettle(page, "Add a note on the board: check pump B bearings.");
    await showBoard(page);
    await shot("board-note");
    sid = await sendAndSettle(page, "Make the board's highlights orange.");
    await shot("board-colour-request");
  }
  if (phases.has("closed")) {
    const apps = await page.evaluate(() =>
      [...document.querySelectorAll("[data-mcp-app]")].map((n) => n.getAttribute("data-mcp-app")),
    );
    const appId = apps[apps.length - 1];
    const transcript = await fetch(`${agent}/v1/sessions/${sid}/messages`).then((r) => r.json());
    const part = transcript.messages
      .flatMap((m) => m.parts || [])
      .find((p) => p.type === "mcp_app" && p.app_instance_id === appId);
    if (!part) throw new Error("no mcp_app part for the open board");
    const closed = await fetch(
      `${agent}/v1/sessions/${sid}/mcp-apps/${appId}?data_ref=${encodeURIComponent(part.data_ref)}`,
      { method: "DELETE" },
    );
    console.log(`closed app ${appId}: HTTP ${closed.status}`);
    await page.waitForTimeout(1500);
    sid = await sendAndSettle(page, "Highlight the inlet temperature on the board.");
    await shot("closed-board-request");
  }
  console.log(`session ${sid}`);
}
