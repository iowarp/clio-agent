// LZ phase 2: Discover lists every server of the pinned clio-kit, grouped by domain, a
// plugin that bundles one once (as the plugin) and every other one once (as a connector).
//
//   LZ_SEARCH  a search term to type after the full list is read (default "hdf")
//   LZ_ENABLE  the name of an entry to open and enable from Discover (default: none)
//
// Prints one JSON line: the groups with their rows, the search's rows, and the enabled
// entry's card before and after.
const settle = (page, ms = 1500) => page.waitForTimeout(ms);
const words = (text) => (text || "").replace(/\s+/gu, " ").trim();

async function groups(page) {
  return page.locator('[data-slot="discover-group"]').evaluateAll((nodes) =>
    nodes.map((node) => ({
      group: node.querySelector("p")?.innerText.trim(),
      rows: [...node.querySelectorAll('[role="group"] > *')].map((row) =>
        row.innerText.replace(/\s+/gu, " ").trim(),
      ),
    })),
  );
}

export default async function (page, shot) {
  const report = {};
  const wide = (page.viewportSize()?.width ?? 1440) > 700;
  if (wide) await page.getByRole("link", { name: "Settings" }).first().click();
  else await page.goto(new URL("/settings/plugins", page.url()).toString());
  await settle(page);
  const listed = await page
    .locator('[role="group"][aria-label="Extensions"]')
    .first()
    .waitFor({ timeout: 15000 })
    .then(() => true, () => false);
  if (!listed) await page.getByRole("link", { name: /^Extensions/u }).first().click();
  await page.locator('[role="group"][aria-label="Extensions"]').waitFor({ timeout: 30000 });
  await settle(page, 2500);
  report.yours = await page
    .locator('[role="group"][aria-label="Extensions"] > *')
    .evaluateAll((rows) => rows.map((row) => row.innerText.replace(/\s+/gu, " ").trim().slice(0, 90)));
  await shot("yours");
  await page.locator('[role="radio"]:visible', { hasText: /^Discover$/u }).first().click();
  await page.locator('[data-slot="discover-group"]').first().waitFor({ timeout: 30000 });
  await settle(page, 2500);
  report.groups = await groups(page);
  report.count = report.groups.reduce((sum, group) => sum + group.rows.length, 0);
  await shot("discover");
  const viewport = page.locator('[data-slot="discover-group"]').nth(2);
  if (await viewport.count()) {
    await viewport.scrollIntoViewIfNeeded();
    await settle(page);
    await shot("discover-more");
  }
  const later = page.locator('[data-slot="discover-group"]').last();
  await later.scrollIntoViewIfNeeded();
  await settle(page);
  await shot("discover-end");

  const term = process.env.LZ_SEARCH ?? "hdf";
  if (term) {
    await page.locator("button[data-extension-search]:visible").first().click();
    const search = page.locator("input:visible").and(page.getByPlaceholder(/Search/u)).first();
    await search.fill(term);
    await settle(page, 2000);
    report.search = await groups(page);
    await shot("search");
    await search.fill("");
    await settle(page, 1500);
  }

  const name = process.env.LZ_ENABLE;
  if (name) {
    const row = page.locator('[data-slot="discover-group"]').getByText(name, { exact: true }).first();
    await row.scrollIntoViewIfNeeded();
    await row.click();
    const card = page.locator('[data-slot="connector-setup-card"]');
    await card.waitFor({ timeout: 20000 });
    await settle(page, 2000);
    report.before = words(await card.innerText()).slice(0, 500);
    await shot("enable-card");
    const started = Date.now();
    await card.getByRole("button", { name: "Enable" }).first().click();
    await settle(page, 3000);
    report.during = words(await card.innerText()).slice(0, 300);
    await shot("enabling");
    let after = "";
    while (Date.now() - started < 300000) {
      after = words(await card.innerText());
      if (/Ready, \d+ tools|Can.t be reached|Needs /u.test(after)) break;
      await settle(page, 2000);
    }
    report.after = after.slice(0, 500);
    report.seconds = Math.round((Date.now() - started) / 1000);
    await shot("enabled");
  }
  console.log(JSON.stringify(report));
}
