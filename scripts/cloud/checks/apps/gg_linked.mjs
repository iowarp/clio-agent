// Geo globe (GG), the headline: a dashboard holding the globe (`clio.mcp-app.v1`, geo's
// show_on_globe) beside a table of stations, linked both ways through one shared
// selection. Opens the conversation's dashboard as a hero (navigation collapsed, canvas
// maximized), then a person:
//   1. looks at it (before);
//   2. clicks a table row: the globe turns to that station and highlights it;
//   3. clicks another station's dot on the globe: its table row is highlighted.
// Each step checks what the globe and the table show, not just the pixels.
//
//   GG_SESSION  "<workspace id>/<session id>" holding the dashboard
//   GG_TITLE    the dashboard's title on its card (found from the transcript when omitted)
//   GG_ROW      the station whose table row is clicked (default ELBE)
//   GG_DOT      the station whose dot is clicked on the globe (default SNRS)
import { api } from "./_lib.mjs";

const env = process.env;
const out = { checks: {}, log: [] };
const note = (label, value) => {
  out.log.push({ label, value });
  console.error(`[gg] ${label}: ${typeof value === "string" ? value : JSON.stringify(value)}`);
};

async function dashboardTitle(session) {
  const listed = await api("GET", `/v1/sessions/${session}/messages`);
  const rows = Array.isArray(listed) ? listed : listed.messages || listed.items || [];
  const parts = rows.flatMap((m) => m.parts || []);
  return parts.filter((p) => /a2ui|surface|dashboard|artifact/.test(p.type) && p.title).at(-1)?.title || "";
}

async function findGlobe(page) {
  for (let i = 0; i < 120; i++) {
    for (const frame of page.frames()) {
      if (frame === page.mainFrame()) continue;
      const ready = await frame.evaluate(() => !!(window.geoGlobe && window.geoGlobe.viewer)).catch(() => false);
      if (ready) return frame;
    }
    await page.waitForTimeout(1000);
  }
  throw new Error("no globe view frame");
}

async function settle(frame, page, ms = 2500) {
  await frame
    .waitForFunction(() => {
      const v = window.geoGlobe.viewer;
      v.scene.requestRender();
      return v.scene.globe.tilesLoaded;
    }, null, { timeout: 60000, polling: 500 })
    .catch(() => note("tiles", "still loading after 60 s"));
  await page.waitForTimeout(ms);
}

const globeState = (frame) =>
  frame.evaluate(() => {
    const v = window.geoGlobe.viewer;
    const c = v.camera.positionCartographic;
    return {
      selected: window.geoGlobe.selected,
      camera: { lon: (c.longitude * 180) / Math.PI, lat: (c.latitude * 180) / Math.PI, height_km: c.height / 1000 },
    };
  });

const selectedRows = (page) => page.locator('tr[aria-selected="true"]').allInnerTexts();

async function heroOn(page) {
  const navOpen = await page.getByRole("button", { name: "New conversation" }).first().isVisible().catch(() => false);
  if (navOpen) {
    const toggle = page.locator('[data-slot="sidebar-trigger"]').or(page.getByRole("button", { name: "Toggle Sidebar" }))
      .filter({ visible: true }).first();
    if (await toggle.count()) await toggle.click().catch(() => undefined);
  }
  const maximize = page.getByRole("button", { name: "Maximize canvas" }).first();
  if (await maximize.isVisible().catch(() => false)) await maximize.click();
}

export default async function (page, shot) {
  const [workspace, session] = env.GG_SESSION.split("/");
  const rowStation = env.GG_ROW || "ELBE";
  const dotStation = env.GG_DOT || "SNRS";
  await page.goto(new URL(`/workspaces/${workspace}/sessions/${session}`, page.url()).toString());
  await page.waitForTimeout(8000);
  const title = env.GG_TITLE || (await dashboardTitle(session));
  note("dashboard", title);
  await page.getByText(title, { exact: true }).last().click({ timeout: 20000 });
  await page.locator("[data-mcp-app-component]").filter({ visible: true }).first().waitFor({ timeout: 120000 });
  await heroOn(page);
  const frame = await findGlobe(page);
  await frame.evaluate(() => window.geoGlobe.ready);
  await settle(frame, page, 4000);
  out.checks.before = { globe: await globeState(frame), rows: await selectedRows(page) };
  await shot("1-before");

  // 2. A table row.
  // The last match: an earlier answer in the transcript can hold a table of the same stations.
  const row = page.locator("tr").filter({ hasText: rowStation }).filter({ visible: true }).last();
  await row.scrollIntoViewIfNeeded();
  await row.click();
  await page.waitForTimeout(1500);
  await settle(frame, page, 2500);
  const afterRow = await globeState(frame);
  out.checks.afterRow = { globe: afterRow, rows: await selectedRows(page) };
  out.checks.rowSelectsGlobe = afterRow.selected.some((s) => s.id === rowStation);
  await shot("2-after-row-click");

  // 3. A dot on the globe.
  const where = await frame.evaluate((id) => {
    const rec = window.geoGlobe.find(id);
    const v = window.geoGlobe.viewer;
    const pos = rec.entities[0].position.getValue(Cesium.JulianDate.now());
    const toWindow = Cesium.SceneTransforms.worldToWindowCoordinates || Cesium.SceneTransforms.wgs84ToWindowCoordinates;
    const p = toWindow(v.scene, pos);
    return p ? { x: p.x, y: p.y } : null;
  }, dotStation);
  const box = await (await frame.frameElement()).boundingBox();
  note("dot at", { where, box });
  if (where && box) {
    await page.mouse.move(box.x + where.x, box.y + where.y);
    await page.waitForTimeout(500);
    await page.mouse.click(box.x + where.x, box.y + where.y);
  }
  await page.waitForTimeout(2000);
  await settle(frame, page, 1500);
  const rows = await selectedRows(page);
  out.checks.afterDot = { globe: await globeState(frame), rows };
  out.checks.dotSelectsRow = rows.some((text) => text.includes(dotStation));
  await shot("3-after-dot-click");
  console.log(`GG_RESULT ${JSON.stringify(out)}`);
}
