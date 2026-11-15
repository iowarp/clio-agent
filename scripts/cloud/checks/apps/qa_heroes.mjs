// Hero screenshots (q-apps) of each kind of view at 1440x900: the left navigation collapsed and
// the workbench maximized, so the view fills the frame. One fresh conversation per view.
//
// Needs the fixtures saved as in docs/design/sdk/mcp-apps-ledger.md, Stack fixtures (hostpanel,
// board, livecounter, timeline). CHECK_HEROES (comma list, default all) picks the views:
//   panel    the host panel in a tab (AQ13)
//   board    the readings board in a tab with a highlight the agent set (AQ20)
//   counter  the live counter in a tab, served through CLIO (AQ30)
//   surface  clio.mcp-app.v1 beside a chart on one time path (AQ27), the conversation widened
import { expectText, log, viewIn } from "./_app_view.mjs";
import { heroShot, openConversation, send, waitIdle } from "./_lib.mjs";

const heroes = new Set((process.env.CHECK_HEROES || "panel,board,counter,surface").split(","));

const SURFACE =
  'Use create_a2ui_surface to make surface timeline_dash. Its root is a Column with two children. First, a clio.mcp-app.v1 component with id app, server timeline, tool open_timeline, arguments {"steps": 24}, state [{"key": "time", "path": "/selection/time", "tool": "set_time"}] and height 360. Second, a clio.chart.v1 with id chart, title "Value by time step", preset scatter, xField time, yField value, entityField time, selection {"path": "/selection/time"}, selectionField time, and inline data rows for time 0 to 23 where value is time*time/10. Do nothing else and reply with one short sentence.';

async function ask(page, session, text) {
  await send(page, text);
  await page.waitForTimeout(4000);
  const settled = await waitIdle(page, session.id, [], 6);
  log(`asked "${text.slice(0, 60)}"`, { session: session.id, settled });
}

async function inTab(page) {
  // A view may ask for full screen itself; otherwise the person opens it in a tab.
  const deadline = Date.now() + 120000;
  while (!(await page.locator("[data-mcp-app-tab]").count())) {
    if (Date.now() > deadline) throw new Error("the view did not reach a tab");
    const open = page.getByRole("button", { name: "Open in tab" }).first();
    if (await open.isVisible().catch(() => false)) await open.click().catch(() => undefined);
    await page.waitForTimeout(2000);
  }
}

export default async function (page, shot) {
  if (heroes.has("panel")) {
    const { session } = await openConversation(page, "qa-heroes");
    await ask(page, session, "Open the host panel for the topic hero.");
    await expectText(viewIn(page, "inline").locator("#topic"), /hero/, 90000);
    await viewIn(page, "inline").locator("#fullscreen").click();
    await page.locator("[data-mcp-app-tab]").first().waitFor({ timeout: 60000 });
    await heroShot(page, shot, "hero-host-panel-tab");
  }
  if (heroes.has("board")) {
    const { session } = await openConversation(page, "qa-heroes");
    await ask(page, session, "Open the cooling loop readings board.");
    await inTab(page);
    await viewIn(page, "tab").getByRole("button", { name: /Pump B vibration/ }).click({ timeout: 60000 });
    await ask(page, session, "Highlight the loop flow on the board.");
    await shot("board-tab-highlighted");
    // Maximizing remounts the tab, so the view replays and the highlight is not kept;
    // the person selects a reading in the maximized view for the hero shot.
    await heroShot(page, shot, "hero-board-tab", async () => {
      await viewIn(page, "tab").getByRole("button", { name: /Pump B vibration/ }).click({ timeout: 60000 });
    });
  }
  if (heroes.has("counter")) {
    const { session } = await openConversation(page, "qa-heroes");
    await ask(page, session, "Show me the live counter.");
    await inTab(page);
    await page.waitForTimeout(4000);
    await heroShot(page, shot, "hero-live-counter-tab");
  }
  if (heroes.has("surface")) {
    const { session } = await openConversation(page, "qa-heroes");
    await ask(page, session, SURFACE);
    const app = page.locator("[data-mcp-app-component]").first();
    await app.waitFor({ timeout: 90000 }).catch(() => undefined);
    await app.scrollIntoViewIfNeeded().catch(() => undefined);
    await heroShot(page, shot, "hero-surface-app-and-chart");
  }
}
