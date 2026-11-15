// PL-Q3: Home Assistant is one plugin (ha-mcp plus its skills). Its pane asks for the
// address and the long-lived access token; typed as a person would, saved, then enabled.
//
//   HA_STEPS  "address=token|address=token" setups in order (the token typed into the
//             secret field; never printed). Each is saved, enabled when off, and its
//             state read in words.
// Prints one JSON line with the pane's state after each setup and the list row.
const settle = (page, ms = 1500) => page.waitForTimeout(ms);
const words = (text) => text.replace(/\s+/gu, " ").trim();

export default async function (page, shot) {
  await page.goto(new URL("/settings/plugins", page.url()).toString());
  const list = page.locator('[role="group"][aria-label="Extensions"]');
  await list.first().waitFor({ timeout: 30000 });
  await settle(page, 2500);
  // The plugin's row (its connector is listed once the connectors have loaded).
  const row = list.locator(":scope > *").filter({ hasText: "Home Assistant" }).filter({ hasText: "Plugin" });
  await row.first().waitFor({ timeout: 30000 });
  await row.first().getByText("Home Assistant", { exact: true }).click();
  const card = page.locator('[data-slot="connector-setup-card"]');
  await card.waitFor({ timeout: 20000 });
  await settle(page);
  const report = { steps: [] };
  const setups = (process.env.HA_STEPS || "").split("|").filter(Boolean);
  for (const [index, setup] of setups.entries()) {
    const [address, ...token] = setup.split("=");
    await card.getByLabel("Your Home Assistant address", { exact: true }).fill(address);
    await card.getByLabel("Long-lived access token", { exact: true }).fill(token.join("="));
    await shot(`filled-${index + 1}`);
    await card.getByRole("button", { name: "Save" }).click();
    // Saving restarts the server and asks it again ("Connecting…" until it answers).
    await card.getByText("Connecting…").waitFor({ state: "detached", timeout: 240000 }).catch(() => undefined);
    await settle(page, 3000);
    const enable = card.getByRole("button", { name: "Enable" });
    if (await enable.count()) await enable.click();
    const started = Date.now();
    let state = "";
    while (Date.now() - started < 240000) {
      state = words(await card.locator('[data-slot="connector-state"]').innerText());
      const detail = words(await card.innerText());
      if (/Ready|Can.t be reached|Needs /u.test(state) && !/Starting/u.test(detail)) break;
      await settle(page, 2000);
    }
    await settle(page, 1500);
    await shot(`state-${index + 1}`);
    const text = words(await card.innerText());
    report.steps.push({
      address,
      state,
      // The secret field shows only that a value is stored, never the value.
      tokenShown: text.includes(token.join("=")) && token.join("=").length > 4,
      pane: text.slice(0, 600),
    });
  }
  report.row = words(
    await list.locator(":scope > *").filter({ hasText: "Home Assistant" }).first().innerText(),
  );
  await shot("list");
  console.log(JSON.stringify(report));
}
