// AQ48: an A2UI surface holding the ParaView view (clio.mcp-app.v1) and a chart of a
// field over time (clio.chart.v1), both bound to /selection/time.
//
// Needs the paraview connector pointed where PV_SERIES lives. Without
// CHECK_SESSION_PATH it opens a new conversation and sends PV_SURFACE_PROMPT (default:
// a tool-directed request naming the components, so mechanism only, not evidence of
// agent judgement). Then:
//   1. scrub the chart: choose time PV_CHART_TIME from the chart's own keyboard menu;
//      the channel calls set_timestep once and the view's bar shows that step;
//   2. move the view's own time slider to step PV_VIEW_INDEX; the view reports the new
//      time and the channel writes it to /selection/time, so the chart's selection moves.
import { allowPending, api, openConversation, send, waitIdle } from "./_lib.mjs";

const series = process.env.PV_SERIES || "/home/researcher/data/pulse_series.pvd";
const chartTime = process.env.PV_CHART_TIME || "0.8";
const viewIndex = Number(process.env.PV_VIEW_INDEX || 3);
// Mean temperature of the pulse series at each step (numpy, from make_datasets.py).
const ROWS = [
  [0.0, 300.705], [0.1, 300.805], [0.2, 300.864], [0.3, 300.864], [0.4, 300.808], [0.5, 300.716],
  [0.6, 300.624], [0.7, 300.567], [0.8, 300.567], [0.9, 300.624], [1.0, 300.714], [1.1, 300.797],
].map(([time, temperature]) => ({ time, temperature }));
const PROMPT =
  process.env.PV_SURFACE_PROMPT ||
  `Use create_a2ui_surface to make surface pulse_dash. Its root is a Column with two children. First, a clio.mcp-app.v1 component with id view, server paraview, tool open_view, arguments {"path": "${series}"}, state [{"key": "time", "path": "/selection/time", "tool": "set_timestep"}] and height 460. Second, a clio.chart.v1 with id chart, title "Mean temperature by time", preset line, xField time, yField temperature, selection {"path": "/selection/time"}, selectionField time, and these inline data rows: ${JSON.stringify(ROWS)}. Do nothing else and reply with one short sentence.`;

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

const trame = (page) => page.frames().find((f) => f.url().includes("/service/"));

async function bar(page) {
  const frame = trame(page);
  if (!frame) return "";
  const text = await frame.locator(".clio-bar").innerText({ timeout: 3000 }).catch(() => "");
  return text.replace(/\s+/g, " ").trim();
}

async function until(page, sessionId, check, label, seconds = 180) {
  const deadline = Date.now() + seconds * 1000;
  while (Date.now() < deadline) {
    await allowPending(sessionId, result.log);
    if (await check().catch(() => false)) return true;
    await page.waitForTimeout(500);
  }
  result.log.push(`timed out: ${label}`);
  return false;
}

async function chartSelection(page) {
  const chart = page.locator('[data-slot="a2ui-chart"]').first();
  return chart.evaluate((el) => {
    const marked = el.querySelectorAll('[aria-selected="true"], [data-selected="true"]');
    return [...marked].map((m) => m.getAttribute("aria-label") || m.textContent).filter(Boolean).slice(0, 4);
  });
}

export default async function (page, shot) {
  let sessionId;
  if (process.env.CHECK_SESSION_PATH) {
    sessionId = process.env.CHECK_SESSION_PATH.split("/").at(-1);
    await page.goto(new URL(process.env.CHECK_SESSION_PATH, page.url()).toString());
    await page.waitForTimeout(4000);
  } else {
    const { session } = await openConversation(page, "pv-surface");
    sessionId = session.id;
    await send(page, PROMPT);
    await waitIdle(page, sessionId, result.log, 15);
  }
  result.session = sessionId;
  await until(page, sessionId, () => page.locator("[data-mcp-app-channel]").first().isVisible(), "the app");
  await page.locator("[data-mcp-app-component]").first().scrollIntoViewIfNeeded();
  await until(page, sessionId, async () => Boolean(trame(page)), "the ParaView view", 300);
  await trame(page).locator(".clio-view canvas").first().waitFor({ timeout: 120000 });
  await until(page, sessionId, async () => /Step \d+ of 12/.test(await bar(page)), "the time bar", 120);
  await page.waitForTimeout(3000);
  result.opened = { ...(await counters(page)), bar: await bar(page) };
  await shot("surface-open");

  // 1. Scrub the chart: the view's timestep follows.
  const before = await counters(page);
  const chart = page.locator('[data-slot="a2ui-chart"]').first();
  await chart.scrollIntoViewIfNeeded();
  await chart.hover();
  await chart.getByRole("button", { name: "More" }).first().click();
  await page.getByRole("menuitem", { name: /Select a time by keyboard/i }).first().hover();
  const escaped = chartTime.replace(".", "\\.");
  await page.getByRole("menuitemradio", { name: new RegExp(`^${escaped}$`) }).first().click();
  await page.keyboard.press("Escape").catch(() => {});
  const wanted = Math.round(Number(chartTime) * 10) + 1;
  await until(page, sessionId, async () => (await bar(page)).includes(`Step ${wanted} of 12`), "the view to follow", 90);
  await page.waitForTimeout(2000);
  const after = await counters(page);
  result.chartToView = {
    chosen: chartTime,
    toolCalls: after.toolCalls - before.toolCalls,
    bar: await bar(page),
    counters: after,
  };
  await page.locator("[data-mcp-app-component]").first().scrollIntoViewIfNeeded();
  await shot("chart-scrubbed-view-follows");

  // 2. Change time in the view: the chart's selection moves.
  const before2 = await counters(page);
  await trame(page)
    .locator('.clio-bar input[type=range]')
    .evaluate((el, value) => {
      el.value = String(value);
      el.dispatchEvent(new Event("change", { bubbles: true }));
    }, viewIndex);
  const expected = (viewIndex / 10).toString();
  await until(page, sessionId, async () => (await counters(page)).lastWritten?.includes(expected), "the chart to move", 60);
  await page.waitForTimeout(1500);
  const after2 = await counters(page);
  result.viewToChart = {
    index: viewIndex,
    writes: after2.writes - before2.writes,
    toolCalls: after2.toolCalls - before2.toolCalls,
    lastWritten: after2.lastWritten,
    bar: await bar(page),
    chartSelection: await chartSelection(page).catch(() => []),
  };
  await chart.scrollIntoViewIfNeeded();
  await shot("view-moved-chart-follows");
  result.permissionsPending = (await api("GET", `/v1/permissions?session_id=${sessionId}&status=pending`)).permissions?.length;
  console.log(`PV_SURFACE_RESULT ${JSON.stringify(result)}`);
}
