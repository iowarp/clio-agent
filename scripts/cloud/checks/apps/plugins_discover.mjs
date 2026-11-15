// PL-Q1: every catalog entry is shown as what it is (PL-D1), in Extensions (Yours: All,
// Plugins, Connectors) and in Discover. A plugin that bundles a connector (ParaView,
// Home Assistant) reads "Plugin" and what it brings ("1 skill, 1 MCP server (13
// tools)"); a connector reads "Connector" and "1 MCP server (8 tools)"; both say what
// they still need and their state in words. Opens PL_OPEN's pane (default "Home
// Assistant"). Prints one JSON line with every list's rows.
const settle = (page, ms = 1500) => page.waitForTimeout(ms);
const words = (text) => text.replace(/\s+/gu, " ").trim();

async function listRows(page, label) {
  const group = page.locator(`[role="group"][aria-label="${label}"]`);
  if (!(await group.count())) return [];
  // Discover's catalog is cards inside domain groups (DC-D1); other lists are rows.
  const cards = group.first().locator('[data-slot="discover-card"]');
  return ((await cards.count()) ? cards : group.first().locator(":scope > *")).evaluateAll(
    (items) => items.map((item) => item.innerText.replace(/\s+/gu, " ").trim()),
  );
}

async function filter(page, name) {
  const radio = page.getByRole("radio", { name: new RegExp(`^${name}`, "u") }).first();
  if ((await radio.count()) && (await radio.isVisible())) await radio.click();
  else {
    // On a phone the other types sit behind "More types".
    await page.getByRole("button", { name: "More types" }).first().click();
    await page.getByRole("menuitemradio", { name: new RegExp(`^${name}`, "u") }).first().click();
  }
  await settle(page);
}

export default async function (page, shot) {
  const open = process.env.PL_OPEN || "Home Assistant";
  const report = {};
  const settings = page.getByRole("link", { name: "Settings" }).first();
  const wide = (page.viewportSize()?.width ?? 1440) > 700;
  if (wide) await settings.click();
  // On a phone the sidebar is closed: go to Extensions by its address.
  else await page.goto(new URL("/settings/plugins", page.url()).toString());
  await settle(page);
  const list = page.locator('[role="group"][aria-label="Extensions"]');
  const shown = await list
    .first()
    .waitFor({ timeout: 15000 })
    .then(() => true, () => false);
  if (!shown && !(await page.getByRole("heading", { name: "Extensions" }).count())) {
    await page.getByRole("link", { name: /^Extensions/u }).first().click();
  }
  await page.locator('[role="group"][aria-label="Extensions"]').waitFor({ timeout: 30000 });
  await settle(page, 3000);
  report.yoursAll = (await listRows(page, "Extensions")).slice(0, 12);
  await shot("yours-all");

  await filter(page, "Plugins");
  report.yoursPlugins = (await listRows(page, "Extensions")).slice(0, 6);
  await shot("yours-plugins");
  await filter(page, "Connectors");
  report.yoursConnectors = (await listRows(page, "Extensions")).slice(0, 10);
  await shot("yours-connectors");

  await page.getByRole("radio", { name: "Discover" }).first().click();
  await settle(page, 2500);
  report.discoverConnectors = await listRows(page, "Catalog connectors");
  await shot("discover-connectors");
  await filter(page, "Plugins");
  report.discoverPlugins = await listRows(page, "Catalog plugins");
  await shot("discover-plugins");
  await filter(page, "All");
  report.discoverAll = await listRows(page, "Catalog plugins and connectors");
  report.suggested = await listRows(page, "Suggested extensions");
  await shot("discover-all");

  await page
    .locator('[data-slot="discover-card-open"]', { hasText: new RegExp(`^${open}$`, "u") })
    .first()
    .click();
  const card = page.locator('[data-slot="connector-setup-card"]');
  await card.waitFor({ timeout: 20000 });
  await settle(page);
  report.pane = words(await card.innerText());
  await shot("pane");
  console.log(JSON.stringify(report));
}
