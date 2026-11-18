// The startup tour's "Bring in tools" step (apps ledger, DC-D8): Settings, General, Replay
// tutorial; Next through the steps to the one after "Explore the results"; the example
// offer card (Geo) spotlighted in the practice conversation. Run with --theme light|dark,
// at 1440x900 and 390x844 (--touch).
//
// Prints one JSON line: the step titles passed, the step's title and body, the card's
// words, whether it is inert and its logo loaded.
const settle = (page, ms = 1200) => page.waitForTimeout(ms);
const words = (text) => (text || "").replace(/\s+/gu, " ").trim();

export default async function (page, shot) {
  const report = { titles: [] };
  const wide = (page.viewportSize()?.width ?? 1440) > 700;
  if (wide) {
    await page.getByRole("link", { name: "Settings" }).first().click();
    await settle(page);
    await page.getByRole("link", { name: /^General/u }).first().click().catch(() => undefined);
  } else {
    await page.goto(new URL("/settings/general", page.url()).toString());
  }
  await settle(page, 2000);
  await page.getByRole("button", { name: "Replay", exact: true }).filter({ visible: true }).first().click();
  const dialog = page.locator("[data-tour-card]").last();
  await dialog.waitFor({ timeout: 30000 });
  for (let i = 0; i < 30; i += 1) {
    await settle(page, 1500);
    const title = words(await dialog.locator("h2, h3").first().innerText().catch(() => ""));
    report.titles.push(title);
    if (title === "Explore the results") await shot("explore");
    if (title === "Bring in tools") break;
    await dialog.getByRole("button", { name: "Next" }).click();
  }
  const card = page.locator('[data-slot="tour-offer"]');
  await card.waitFor({ timeout: 20000 });
  await settle(page, 2500);
  report.step = words(await dialog.innerText()).slice(0, 400);
  report.card = words(await card.innerText()).slice(0, 400);
  report.inert = await card.evaluate((el) => el.inert === true);
  report.logo = await card
    .locator('[data-slot="connector-logo"]')
    .first()
    .evaluate((el) => el.tagName === "IMG" && el.complete && el.naturalWidth > 0)
    .catch(() => false);
  await shot("bring-in-tools");
  // The next step drops the example again.
  await dialog.getByRole("button", { name: "Next" }).click();
  await settle(page, 1500);
  report.next = words(await dialog.locator("h2, h3").first().innerText().catch(() => ""));
  report.cardAfter = await card.count();
  await page.keyboard.press("Escape");
  console.log(JSON.stringify(report));
}
