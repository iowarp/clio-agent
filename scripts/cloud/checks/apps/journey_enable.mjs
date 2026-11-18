// First-run journey: a person turns on a connector the way the Connectors page says,
// starting from the first screen, and every click is counted and shot.
//
//   JOURNEY_CONNECTOR  the connector's name as listed (default "ParaView")
//   JOURNEY_FILTER=1   choose "Connectors" above the list first, as the page suggests
//
// Clicks: Settings, Extensions (when Settings opens elsewhere), the Connectors filter
// (optional), the connector's row, Enable. It prints one JSON line with the clicks, the
// card's text before and after, and the row's state as the list shows it.
const settle = (page, ms = 1500) => page.waitForTimeout(ms);

export default async function (page, shot) {
  const name = process.env.JOURNEY_CONNECTOR || "ParaView";
  const clicks = [];
  const click = async (locator, what) => {
    await locator.first().click({ timeout: 20000 });
    clicks.push(what);
    await settle(page);
  };
  await click(page.getByRole("link", { name: "Settings" }), "Settings (sidebar footer)");
  await shot("settings");
  const heading = page.getByRole("heading", { name: "Extensions" });
  if (!(await heading.count())) {
    await click(page.getByRole("link", { name: /^Extensions/ }), "Extensions (settings nav)");
  }
  await page.locator('[data-slot="connector-logo"], [data-slot="monogram"]').first().waitFor({ timeout: 30000 });
  await settle(page, 2500);
  await shot("extensions");
  if (process.env.JOURNEY_FILTER === "1") {
    await click(page.getByRole("radio", { name: /^Connectors/ }).or(page.getByRole("button", { name: /^Connectors/ })), "Connectors filter");
    await shot("connectors-filter");
  }
  await click(
    page.locator('[role="group"][aria-label="Extensions"]').getByText(name, { exact: true }),
    `${name} row`,
  );
  const card = page.locator('[data-slot="connector-setup-card"]');
  await card.waitFor({ timeout: 20000 });
  await settle(page);
  await shot("card");
  const before = (await card.innerText()).replace(/\s+/g, " ");
  await click(card.getByRole("button", { name: "Enable" }), "Enable");
  const started = Date.now();
  let after = "";
  while (Date.now() - started < 240000) {
    after = (await card.innerText()).replace(/\s+/g, " ");
    if (/Ready, \d+ tools|Can.t be reached|Needs /u.test(after)) break;
    await settle(page, 2000);
  }
  await shot("enabled");
  console.log(JSON.stringify({ clicks, seconds: Math.round((Date.now() - started) / 1000), before, after }));
}
