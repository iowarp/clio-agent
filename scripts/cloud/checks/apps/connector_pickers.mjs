// Check (AQ10, CN9/CN10 *Amended*): where a connector's plugin is offered. It looks for
// CHECK_PLUGIN (default fixture-secret-skills) and the connector CHECK_CONNECTOR:
//   - the Extensions list, searched for the plugin (All and Plugins): never on its own,
//     the connector row stands for it;
//   - "Compose a plugin", its "Skills & plugins" choice: never (a plugin bundle does not
//     take a connector's plugin);
//   - the Agent tab's "Add plugin" menu of CHECK_SESSION (in CHECK_WORKSPACE): offered,
//     under Connectors by the connector's name, while the connector is enabled; left out
//     while it is off.
// The connector's card names it as "Brings plugin". Prints one JSON line.
const settle = (page, ms = 1500) => page.waitForTimeout(ms);
const text = async (locator) =>
  ((await locator.count()) ? await locator.first().innerText() : "").replace(/\s+/gu, " ");

export default async function (page, shot) {
  const plugin = process.env.CHECK_PLUGIN || "fixture-secret-skills";
  const word = plugin.split("-")[0];
  const report = {};

  await page.goto(new URL("/settings/plugins", page.url()).toString());
  const list = page.locator('[role="group"][aria-label="Extensions"]');
  await list.waitFor({ timeout: 30000 });
  await settle(page, 2500);
  await page.getByRole("button", { name: "Search extensions" }).click();
  await page.keyboard.type(word);
  await settle(page, 2000);
  await shot("extensions-search");
  report.extensionsAll = (await text(list)).includes(plugin);
  report.extensionsRows = await text(list);
  const pluginsTab = page.getByRole("radio", { name: /^Plugins/u }).or(page.getByRole("tab", { name: /^Plugins/u }));
  if (await pluginsTab.count()) {
    await pluginsTab.first().click();
    await settle(page, 1500);
    await shot("extensions-plugins-tab");
    report.extensionsPlugins = (await text(list)).includes(plugin);
  }

  // The connector's card names the plugin it brings.
  const allTab = page.getByRole("radio", { name: /^All/u }).or(page.getByRole("tab", { name: /^All/u }));
  if (await allTab.count()) await allTab.first().click();
  await settle(page, 1000);
  const connector = process.env.CHECK_CONNECTOR || "Fixture Secret";
  await list.getByText(connector, { exact: true }).first().click();
  const card = page.locator('[data-slot="connector-setup-card"]');
  await card.waitFor({ timeout: 20000 });
  await settle(page);
  report.cardBringsPlugin = (await text(card)).includes(`Brings plugin ${plugin}`);
  await shot("connector-card");

  // Compose a plugin: its "Skills & plugins" choice.
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "Add options" }).click();
  await page.getByRole("menuitem", { name: /Compose a plugin/u }).click();
  const dialog = page.getByRole("dialog").last();
  await dialog.waitFor({ timeout: 15000 });
  await settle(page);
  // The first "Choose…" is the "Skills & plugins" choice; its menu takes typing.
  await dialog.getByRole("button", { name: /^Choose/u }).first().click();
  await settle(page);
  await page.keyboard.type(word);
  await settle(page, 1500);
  await shot("compose-picker");
  report.compose = (await page.locator("body").innerText()).includes(plugin);
  await page.keyboard.press("Escape");
  await page.keyboard.press("Escape");

  // The Agent tab's "Add plugin" menu in a conversation.
  const session = process.env.CHECK_SESSION;
  if (session) {
    const workspace = process.env.CHECK_WORKSPACE || "ws_default";
    await page.goto(new URL(`/workspaces/${workspace}/sessions/${session}`, page.url()).toString());
    await settle(page, 3000);
    // The workbench keeps its open or closed state between visits: open it only if closed.
    const lists = page.getByRole("toolbar", { name: "Workbench lists" });
    if (!(await lists.count())) {
      await page.getByRole("button", { name: "Open workspace canvas" }).first().click();
      await settle(page, 1500);
    }
    await lists
      .getByRole("button", { name: /^Agent/u })
      .first()
      .click();
    await settle(page, 2000);
    await page.locator('[data-agent-use="tools"]').first().click();
    await settle(page, 2500);
    await page.getByRole("button", { name: /Add (package|plugin)/u }).first().click();
    await settle(page, 1500);
    await shot("agent-add-plugin");
    const group = page.getByRole("group", { name: "Connectors" });
    report.agentAddPlugin = (await text(group)).includes(connector);
    report.agentAddPluginText = (await text(group)).slice(0, 200);
    await page.keyboard.press("Escape");
  }
  console.error(JSON.stringify(report));
}
