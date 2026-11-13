// clio.mcp-app.v1 in a surface beside a chart bound to the same /selection/time path
// (plan AQ27, AQ28, AQ29). Needs the channel fixture saved as the connection
// "timeline" (scripts/cloud/checks/apps/a2ui_channel.sh does that).
//
// Without CHECK_SESSION_PATH it opens a new conversation and sends a tool-directed
// request for the surface (mechanism only; not evidence of agent judgement). With
// CHECK_SESSION_PATH (/workspaces/<ws>/sessions/<sid>) it opens that conversation.
// Then:
//   AQ27  move the view's slider to step 5: the chart's selection follows, and the
//         channel calls no tool (nothing is sent back to the view);
//   AQ28  choose time 9 from the chart's own "Select a time by keyboard" menu: the
//         channel calls set_time once with 9 and the view shows 9;
//   AQ29  press the view's burst button (sixty reports in one second): at most ten
//         data-model writes, and the last value wins.
// Channel counters come from the component's data-* attributes; the fixture logs every
// set_time call to .cloud-run/channel_app.log, which a2ui_channel.sh prints.
import { allowPending, api, openConversation, send, waitIdle } from "./_lib.mjs";

const PROMPT =
  'Use create_a2ui_surface to make surface timeline_dash. Its root is a Column with two children. First, a clio.mcp-app.v1 component with id app, server timeline, tool open_timeline, arguments {"steps": 24}, state [{"key": "time", "path": "/selection/time", "tool": "set_time"}] and height 360. Second, a clio.chart.v1 with id chart, title "Value by time step", preset scatter, xField time, yField value, entityField time, selection {"path": "/selection/time"}, selectionField time, and inline data rows for time 0 to 23 where value is time*time/10. Do nothing else and reply with one short sentence.';

const result = { log: [] };

async function counters(page) {
  const row = page.locator('[data-mcp-app-channel="time"]').first();
  return {
    reports: Number((await row.getAttribute("data-reports")) ?? 0),
    writes: Number((await row.getAttribute("data-writes")) ?? 0),
    toolCalls: Number((await row.getAttribute("data-tool-calls")) ?? 0),
    lastWritten: await row.getAttribute("data-last-written"),
  };
}

/** The view's own document: the frame inside the app's sandbox frame. */
function viewFrame(page) {
  return page
    .frames()
    .find((frame) => frame.parentFrame() && /\/mcp-apps\/[^/]+\/sandbox/.test(frame.parentFrame().url()));
}

/** Wait for `check` while answering the conversation's permission prompts. */
async function until(page, sessionId, check, label, seconds = 120) {
  const deadline = Date.now() + seconds * 1000;
  while (Date.now() < deadline) {
    await allowPending(sessionId, result.log);
    if (await check().catch(() => false)) return;
    await page.waitForTimeout(500);
  }
  throw new Error(`timed out waiting for ${label}`);
}

async function viewState(page) {
  const frame = viewFrame(page);
  return frame.evaluate(() => ({
    step: document.getElementById("step")?.textContent,
    status: document.getElementById("status")?.textContent,
    hostSets: document.getElementById("counts")?.dataset.hostSets,
    reports: document.getElementById("counts")?.dataset.reports,
  }));
}

export default async function (page, shot) {
  let sessionId;
  if (process.env.CHECK_SESSION_PATH) {
    sessionId = process.env.CHECK_SESSION_PATH.split("/").at(-1);
    await page.goto(new URL(process.env.CHECK_SESSION_PATH, page.url()).toString());
    await page.waitForTimeout(4000);
  } else {
    const { session } = await openConversation(page, "a2ui-channel");
    sessionId = session.id;
    await send(page, PROMPT);
    await waitIdle(page, sessionId, result.log);
  }
  result.session = sessionId;

  const app = page.locator("[data-mcp-app-component]").first();
  await until(
    page,
    sessionId,
    () => page.locator("[data-mcp-app-channel]").first().isVisible(),
    "the app to open",
  );
  await app.scrollIntoViewIfNeeded();
  await until(
    page,
    sessionId,
    async () => (await viewState(page)).status === "Connected",
    "the view to connect",
  );
  await page.waitForTimeout(1000);
  await shot("surface-open");
  result.opened = { ...(await counters(page)), view: await viewState(page) };

  // AQ27: change time in the view.
  await viewFrame(page).evaluate(() => {
    const input = document.getElementById("slider");
    input.value = "5";
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await page.waitForTimeout(1500);
  result.aq27 = { ...(await counters(page)), view: await viewState(page) };
  await page.locator('[data-slot="a2ui-chart"]').first().scrollIntoViewIfNeeded();
  await shot("aq27-view-moved-chart-follows");

  // AQ28: change time from the chart's own selection menu.
  const chart = page.locator('[data-slot="a2ui-chart"]').first();
  await chart.hover();
  await chart.getByRole("button", { name: "More" }).first().click();
  await page.getByRole("menuitem", { name: /Select a time by keyboard/i }).first().hover();
  await page.getByRole("menuitemradio", { name: /^9$/ }).first().click();
  await page.keyboard.press("Escape").catch(() => {});
  await until(
    page,
    sessionId,
    async () => (await viewState(page)).hostSets === "1",
    "set_time to reach the view",
  );
  await page.waitForTimeout(1500);
  result.aq28 = { ...(await counters(page)), view: await viewState(page) };
  await app.scrollIntoViewIfNeeded();
  await shot("aq28-chart-drives-view");

  // AQ29: sixty reports in one second.
  const before = await counters(page);
  await viewFrame(page).evaluate(() => document.getElementById("burst").click());
  await until(
    page,
    sessionId,
    async () => /Burst done/.test((await viewState(page)).status ?? ""),
    "the burst",
    30,
  );
  await page.waitForTimeout(800);
  const after = await counters(page);
  result.aq29 = {
    reports: after.reports - before.reports,
    writes: after.writes - before.writes,
    toolCalls: after.toolCalls - before.toolCalls,
    lastWritten: after.lastWritten,
    view: await viewState(page),
  };
  await page.locator('[data-slot="a2ui-chart"]').first().scrollIntoViewIfNeeded();
  await shot("aq29-burst");
  result.permissionsPending = (await api("GET", `/v1/permissions?session_id=${sessionId}&status=pending`)).permissions?.length;
  console.log(`A2UI_CHANNEL_RESULT ${JSON.stringify(result)}`);
}
