// Friction polish (FP-D4): connectors are findable by name in Settings, the Agent scope is
// named as an agent, and the Connectors filter's Discover lists connectors.
//
// Steps: Settings; the nav as it opens; search "connectors"; the result opened; the
// Connectors filter (Yours); Discover under it. Prints one JSON line with the nav's group
// headings, the item's label, the search hits and the Discover headings.
const settle = (page, ms = 1500) => page.waitForTimeout(ms);

export default async function (page, shot) {
  const out = {};
  const settings = page.getByRole("link", { name: "Settings" }).first();
  // On a phone the navigation is a drawer behind its toggle.
  if (!(await settings.isVisible().catch(() => false))) {
    await page.getByRole("button", { name: "Toggle Sidebar" }).first().click();
    await settle(page, 1000);
  }
  await settings.click();
  await settle(page, 2500);
  const nav = page.locator("nav").filter({ hasText: "Appearance" }).first();
  out.groups = await nav
    .locator("section")
    .evaluateAll((sections) => sections.map((s) => s.getAttribute("aria-label")));
  out.extensionsItem = await nav
    .getByRole("link", { name: /Extensions/ })
    .first()
    .innerText()
    .catch(() => "");
  await shot("settings-nav");
  await page.getByRole("dialog").getByText("Search", { exact: true }).first().click();
  await settle(page, 800);
  await page.keyboard.type("connectors");
  await settle(page, 2000);
  out.searchHits = (await page.getByRole("dialog").last().innerText().catch(() => ""))
    .replace(/\s+/gu, " ")
    .slice(0, 400);
  await shot("search-connectors");
  await page.keyboard.press("Enter");
  await settle(page, 3000);
  await page.locator('[data-slot="connector-logo"], [data-slot="monogram"]').first().waitFor({ timeout: 30000 });
  await page.getByRole("radio", { name: /^Connectors/ }).or(page.getByRole("button", { name: /^Connectors/ })).first().click();
  await settle(page, 2000);
  out.yoursText = (await page.locator("main, [role='main']").first().innerText().catch(() => ""))
    .replace(/\s+/gu, " ")
    .slice(0, 600);
  out.yoursOffersHomeServer = out.yoursText.includes("MCP servers you add yourself");
  await shot("connectors-yours");
  await page.getByRole("radio", { name: /^Discover/ }).or(page.getByRole("button", { name: /^Discover/ })).first().click();
  await settle(page, 4000);
  out.discoverLabels = await page
    .locator('[data-slot="settings-group-label"], h3, [role="group"][aria-label]')
    .evaluateAll((items) =>
      items.map((item) => item.getAttribute("aria-label") || item.innerText.trim()).filter(Boolean),
    );
  await shot("connectors-discover");
  console.log(`FP_SETTINGS ${JSON.stringify(out)}`);
}
