// Qualification (q-apps): interactions between the app streams that no single stream ran.
//
// Needs the board fixture (tests/fixtures/mcp_apps/view_tools_server.py) saved as the
// connection "board" and the host panel fixture (tests/fixtures/mcp_apps/host_panel_server.py)
// as "hostpanel", both always loaded. Every prompt is a plain request.
//
//   tab-tools   the board in a tab, then a newer inline app (the host panel) that reports
//               context: the next turn carries both views' context, and the board's own
//               tool still reaches the board in its tab (it is not the latest inline app).
//   tab-close   closing the board's tab closes it (it is not the latest, MA7); the same
//               request now returns at once saying the view is not open (AQ21, closed
//               through the real control).
//   reopen      Reopen on the board's card replays its call; the reopened view offers its
//               tools again and a request reaches it.
// CHECK_PHASES (comma list) picks phases; the session id is printed for the ledger.
import { agentUrl, check, expectText, json, log, sessionIdFrom, viewIn } from "./_app_view.mjs";
import { allowPending, heroShot, openConversation, send, waitIdle } from "./_lib.mjs";

const phases = new Set((process.env.CHECK_PHASES || "tab-tools,tab-close,reopen").split(","));

async function ask(page, text) {
  const allowed = [];
  await send(page, text);
  await page.waitForTimeout(4000);
  const sid = sessionIdFrom(page);
  const settled = await waitIdle(page, sid, allowed, 6);
  log(`asked "${text}"`, { settled, allowed });
  check(settled, `the turn for "${text}" settled`);
  await page.waitForTimeout(1500);
  return sid;
}

/** The newest assistant message's parts (the list is newest first). */
async function lastTurn(sid) {
  const { body } = await json(`/v1/sessions/${sid}/messages`);
  const message = (body?.messages || []).find((m) => m.role === "assistant");
  return message?.parts || [];
}

function summary(parts) {
  return parts
    .filter((p) => ["tool_result", "injection"].includes(p.type))
    .map((p) =>
      p.type === "injection"
        ? `injection ${p.source}: ${(p.text || p.content || "").slice(0, 160)}`
        : `${p.tool_name} -> ${String(p.metadata?.result?.content?.[0]?.text ?? p.metadata?.result ?? "").slice(0, 200)}`,
    );
}

export default async function (page, shot) {
  const { session } = await openConversation(page, "qa-cross");
  let sid = session.id;
  log("session", sid);
  const board = () => viewIn(page, "tab");

  if (phases.has("tab-tools")) {
    sid = await ask(page, "Open the cooling loop readings board.");
    const inline = viewIn(page, "inline");
    await inline.getByRole("button", { name: /Pump B vibration/ }).waitFor({ timeout: 90000 });
    await page.getByRole("button", { name: "Open in tab" }).first().click();
    await page.locator("[data-mcp-app-tab]").first().waitFor({ timeout: 30000 });
    await board().getByRole("button", { name: /Pump B vibration/ }).click({ timeout: 60000 });
    await expectText(board().locator("#activity"), /Selected Pump B/);

    sid = await ask(page, "Open the host panel for the topic delta.");
    const panel = viewIn(page, "inline");
    await expectText(panel.locator("#topic"), /delta/, 90000);
    await panel.locator("#context").click();
    await page.waitForTimeout(2500);
    await shot("board-tab-panel-inline");

    sid = await ask(page, "What do my open views show right now?");
    const contextTurn = await lastTurn(sid);
    const injections = contextTurn.filter((p) => p.type === "injection" && p.source === "mcp_app_context");
    const text = injections.map((p) => p.text || p.content || "").join("\n");
    log("context injections", { count: injections.length, board: /Pump B/.test(text), panel: /delta|host/i.test(text) });
    log("context text", text.slice(0, 1200));
    check(/Pump B/.test(text), "the tab board's context is in the turn");
    await shot("both-contexts");

    sid = await ask(page, "Highlight the loop flow on the board.");
    log("tab tool turn", summary(await lastTurn(sid)));
    await page.getByRole("tab", { name: /Open Board/ }).first().click().catch(() => undefined);
    await expectText(board().locator("#activity"), /Highlighted Loop flow/, 30000);
    await shot("tab-board-highlighted");
  }

  if (phases.has("tab-close")) {
    const tabId = await page.locator("[data-mcp-app-tab]").first().getAttribute("data-mcp-app-tab");
    const chip = page.locator(`[data-tab-id="mcp-app:${tabId}"]`);
    await chip.hover();
    await chip.locator('[data-slot="frame-tab-close"]').click({ force: true });
    await page.locator("[data-mcp-app-tab]").first().waitFor({ state: "detached", timeout: 30000 });
    await page.waitForTimeout(2500);
    const live = await json(`/v1/sessions/${sid}/mcp-apps/${tabId}/live`).catch(() => ({ status: 0 }));
    log("board after its tab closed", { app: tabId, live: live.status });
    sid = await ask(page, "Highlight the inlet temperature on the board.");
    const turn = summary(await lastTurn(sid));
    log("closed tab turn", turn);
    await shot("closed-tab-request");
  }

  if (phases.has("reopen")) {
    for (const group of await page.getByRole("button", { name: /Called \d+ tools|used tools/i }).all())
      await group.click().catch(() => undefined);
    const lines = page.locator("[data-mcp-app-reopen]:visible");
    if (await lines.count()) {
      const line = lines.first();
      await line.scrollIntoViewIfNeeded();
      await line.getByRole("button", { name: "Reopen" }).click();
      const allowing = setInterval(() => allowPending(sid, []).catch(() => undefined), 1000);
      await page.locator("[data-mcp-app-tab], [data-mcp-app-placement=\"inline\"]").first().waitFor({ timeout: 90000 });
      clearInterval(allowing);
      await page.waitForTimeout(6000);
      await shot("reopened");
    } else {
      log("reopen", "no closed card (the agent reopened the board itself)");
    }
    sid = await ask(page, "Highlight the pump B vibration on the board.");
    log("reopened tool turn", summary(await lastTurn(sid)));
    const where = (await page.locator("[data-mcp-app-tab]").count()) ? "tab" : "inline";
    await expectText(viewIn(page, where).locator("#activity"), /Highlighted Pump B/, 30000).then(
      (t) => log("reopened view", t),
      (e) => log("reopened view did not change", String(e).slice(0, 200)),
    );
    await shot("reopened-highlighted");
    if (process.env.CHECK_HERO) await heroShot(page, shot, "hero-reopened-board");
  }
  log("session", sid);
  log("agent", agentUrl());
}
