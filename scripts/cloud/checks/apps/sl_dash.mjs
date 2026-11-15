// Slicer plugin (SL): a dashboard pairing the slice view (clio.mcp-app.v1, the slicer's
// show_slice) with an A2UI chart of time per layer, linked by `layer`.
//
//   SL_SESSION   "<workspace id>/<session id>" holding the slice
//   SL_DASH_SEND the plain request for the dashboard (optional: without it the step opens
//                the dashboard the conversation already has)
//   SL_LAYER     the layer chosen from the chart's own keyboard menu (default 72)
//   SL_HERO=1    take the shots as heroes (navigation collapsed, canvas maximized)
//   SL_DASH_TITLE the dashboard card's title, a regular expression (default "layer view linked")
// Then a person:
//   1. moves the view's layer slider: the chart's selection follows (the channel writes
//      the view's reported `layer` into the shared selection);
//   2. picks a layer from the chart's "Select a layer by keyboard" menu: the channel
//      calls go_to_layer on the view, and the view shows that layer.
// Checks read the channel's data-* counters and the view's own reported state.
import { allowPending, api, send } from "./_lib.mjs";

const env = process.env;
const out = { log: [] };
const note = (label, value) => {
  out.log.push({ label, value });
  console.error(`[sl-dash] ${label}: ${typeof value === "string" ? value : JSON.stringify(value)}`);
};

async function settleTurn(page, session) {
  const log = [];
  const until = Date.now() + 12 * 60_000;
  let quiet = 0;
  while (Date.now() < until && quiet < 3) {
    await allowPending(session, log);
    const status = (await api("GET", `/v1/sessions/${session}`)).status;
    quiet = ["idle", "completed"].includes(status) ? quiet + 1 : 0;
    await page.waitForTimeout(3000);
  }
  if (log.length) note("permissions", log);
}

async function dashboardView(page, minutes = 3) {
  for (let i = 0; i < minutes * 60; i++) {
    for (const frame of page.frames()) {
      if (frame === page.mainFrame()) continue;
      const inSurface = await frame.frameElement()
        .then((el) => el.evaluate((node) => !!node.closest("[data-mcp-app-component]")))
        .catch(() => false);
      if (!inSurface) continue;
      for (const child of frame.childFrames()) {
        const ready = await child
          .evaluate(() => !!(window.slicerView && window.slicerView.state) && document.getElementById("status").hidden)
          .catch(() => false);
        if (ready) return child;
      }
    }
    await page.waitForTimeout(1000);
  }
  throw new Error("no slice view in the dashboard");
}

async function counters(page) {
  const row = page.locator('[data-mcp-app-channel="layer"]').first();
  if (!(await row.count())) return null;
  return {
    reports: Number((await row.getAttribute("data-reports")) ?? 0),
    writes: Number((await row.getAttribute("data-writes")) ?? 0),
    toolCalls: Number((await row.getAttribute("data-tool-calls")) ?? 0),
    lastWritten: await row.getAttribute("data-last-written"),
  };
}

async function heroOn(page) {
  const navOpen = await page.getByRole("button", { name: "New conversation" }).first().isVisible().catch(() => false);
  if (navOpen) {
    const toggle = page.locator('[data-slot="sidebar-trigger"]').or(page.getByRole("button", { name: "Toggle Sidebar" }))
      .filter({ visible: true }).first();
    if (await toggle.count()) await toggle.click().catch(() => undefined);
  }
  const maximize = page.getByRole("button", { name: "Maximize canvas" }).first();
  if (await maximize.count()) await maximize.click().catch(() => undefined);
  await page.waitForTimeout(6000);
}

export default async function (page, shot) {
  const [workspace, session] = env.SL_SESSION.split("/");
  await page.goto(new URL(`/workspaces/${workspace}/sessions/${session}`, page.url()).toString());
  await page.waitForTimeout(8000);
  if (env.SL_DASH_SEND) {
    await send(page, env.SL_DASH_SEND);
    await page.waitForTimeout(5000);
    await settleTurn(page, session);
    await page.waitForTimeout(4000);
    await shot("answered");
  }
  // Open the dashboard from its card in the transcript (its title), unless it is open.
  if (!(await page.locator("[data-mcp-app-component]").filter({ visible: true }).count())) {
    const title = env.SL_DASH_TITLE || "layer view linked";
    const card = page.getByText(new RegExp(title, "i")).filter({ visible: true }).last();
    await card.scrollIntoViewIfNeeded().catch(() => undefined);
    await card.click().catch(() => undefined);
    note("dashboard", `opened from its card (${title})`);
    await page.waitForTimeout(5000);
  }
  if (env.SL_HERO === "1") await heroOn(page);
  const chart = page.locator('[data-slot="a2ui-chart"]').filter({ visible: true }).first();
  await chart.scrollIntoViewIfNeeded().catch(() => undefined);
  let view = await dashboardView(page);
  await page.waitForTimeout(2000);
  note("opened", { channel: await counters(page), view: await view.evaluate(() => window.slicerView.state) });
  await shot("dashboard");

  // 1. The person moves the view's slider: the chart's selection follows.
  const slider = view.locator("#to");
  await slider.focus();
  await page.keyboard.press("Home");
  for (let i = 0; i < 20; i++) await page.keyboard.press("ArrowRight");
  await page.waitForTimeout(2000);
  note("slider", {
    channel: await counters(page),
    view: (await view.evaluate(() => window.slicerView.state)).layer,
    chartSelection: await chart.getAttribute("data-selected-count").catch(() => null),
  });
  await shot("view-moves-chart");

  // 2. The person picks a layer from the chart's own keyboard menu: the view follows.
  const wanted = env.SL_LAYER || "72";
  await chart.hover();
  await chart.getByRole("button", { name: "More" }).first().click();
  await page.getByRole("menuitem", { name: /Select a .* by keyboard/i }).first().hover();
  // A long menu: bring the item into view, then choose it as a keyboard user would.
  const item = page.getByRole("menuitemradio", { name: new RegExp(`^${wanted}$`) }).first();
  await item.evaluate((el) => el.scrollIntoView({ block: "center" }));
  await item.focus();
  await page.keyboard.press("Enter");
  await page.keyboard.press("Escape").catch(() => {});
  for (let i = 0; i < 30; i++) {
    view = await dashboardView(page, 1);
    if (String((await view.evaluate(() => window.slicerView.state)).layer) === wanted) break;
    await page.waitForTimeout(1000);
  }
  await page.waitForTimeout(1500);
  note("chart", {
    channel: await counters(page),
    view: await view.evaluate(() => ({ layer: window.slicerView.state.layer, z: window.slicerView.state.z_mm,
      text: document.getElementById("layer-text").textContent })),
  });
  await shot("chart-moves-view");
  console.log(`SL_DASH_RESULT ${JSON.stringify(out)}`);
}
