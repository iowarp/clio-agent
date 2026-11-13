// Check (AQ10): a connector's following plugin joins and leaves a running conversation
// with the connector, shown by the plugin-change notice in the Agent tab, and is never
// offered by the Agent tab's plugin picker.
//   CHECK_SESSION    the running conversation (sess_...), CHECK_WORKSPACE its workspace
//   CHECK_CONNECTOR  the connector's name in Extensions
//   CHECK_PLUGIN     the following plugin's id, which the picker must not offer
//   CHECK_TOGGLE     "Turn off" or "Enable": the card button pressed before looking (optional)
const settle = (page, ms = 1500) => page.waitForTimeout(ms);

export default async function (page, shot) {
  const session = process.env.CHECK_SESSION;
  const workspace = process.env.CHECK_WORKSPACE || "ws_default";
  const name = process.env.CHECK_CONNECTOR;
  const plugin = process.env.CHECK_PLUGIN;
  await page.goto(new URL("/settings/plugins", page.url()).toString());
  await page
    .locator('[role="group"][aria-label="Extensions"]')
    .getByText(name, { exact: true })
    .first()
    .click();
  const card = page.locator('[data-slot="connector-setup-card"]');
  await card.waitFor({ timeout: 20000 });
  if (process.env.CHECK_TOGGLE) {
    await card.getByRole("button", { name: process.env.CHECK_TOGGLE, exact: true }).click();
    await settle(page, 6000);
  }
  await shot("connector-card");

  await page.goto(new URL(`/workspaces/${workspace}/sessions/${session}`, page.url()).toString());
  await settle(page, 3000);
  // A waiting tool permission in the conversation is allowed once so the turn ends.
  const permission = page.locator('[data-slot="ask-card"][data-interaction-kind="permission"]');
  await permission.waitFor({ timeout: 8000 }).catch(() => undefined);
  if (await permission.count())
    await permission
      .getByRole("button", { name: "Allow once" })
      .click({ timeout: 5000 })
      .catch(() => undefined);
  const canvas = page.getByRole("button", { name: "Open workspace canvas" });
  if (await canvas.count()) await canvas.first().click();
  await settle(page, 1500);
  const agent = page
    .getByRole("toolbar", { name: "Workbench lists" })
    .getByRole("button", { name: /^Agent/u });
  await agent.first().click();
  await settle(page, 2000);
  // "What it can use" opens the plugin tree, which holds the notice and the picker.
  await page.locator('[data-agent-use="tools"]').first().click();
  await settle(page, 3000);
  const notice = page.getByRole("region", { name: "Plugin changes waiting for compaction" });
  await notice.waitFor({ timeout: 30000 }).catch(() => undefined);
  await shot("agent-tab");
  const report = {
    notice: (await notice.count()) ? (await notice.innerText()).replace(/\s+/gu, " ") : null,
  };
  const add = page.getByRole("button", { name: /Add (package|plugin)/u }).first();
  if (await add.count()) {
    await add.click();
    await settle(page, 1500);
    await shot("plugin-picker");
    const menu = page.locator('[role="menu"], [role="listbox"], [role="dialog"]').last();
    const text = (await menu.innerText().catch(() => "")).replace(/\s+/gu, " ");
    report.pickerOffersFollower = text.includes(plugin);
    report.pickerSample = text.slice(0, 300);
    await page.keyboard.press("Escape");
  }
  console.error(JSON.stringify(report));
}
