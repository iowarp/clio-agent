// Discover cards (apps ledger, "Discover cards (DC)"): Extensions, Discover shows the
// catalog as cards by domain, each with its action in sight. Run with --theme light|dark,
// at 1440x900 and 390x844 (--touch).
//
//   DC_SHOWALL  "1": open the first long group's "Show all (n)" and shoot it
//   DC_SEARCH   a term to type in the search, shot, then cleared
//   DC_ENABLE   the name of a card with no needs to enable from its "+"
//   DC_SETUP    the name of a card with needs: its "+" opens the setup pane, filled from
//               DC_ADDRESS and DC_TOKEN, saved, then enabled from the card's "+"
//
// Prints one JSON line: the groups (heading, cards shown, Show all), the search's cards,
// and each enabled card's words before, during and after. The token is never printed.
const settle = (page, ms = 1500) => page.waitForTimeout(ms);
const words = (text) => (text || "").replace(/\s+/gu, " ").trim();

async function groups(page) {
  return page.locator('[data-slot="discover-group"]').evaluateAll((nodes) =>
    nodes.map((node) => ({
      group: node.querySelector("h3")?.innerText.trim(),
      cards: node.querySelectorAll('[data-slot="discover-card"]').length,
      showAll: node.querySelector('[data-slot="discover-show-all"]')?.innerText.trim() ?? null,
      states: [...node.querySelectorAll('[data-slot="discover-card"]')].map(
        (card) => `${card.dataset.connector}:${card.dataset.state}`,
      ),
    })),
  );
}

const cardOf = (page, name) =>
  page.locator('[data-slot="discover-card"]').filter({
    has: page.locator('[data-slot="discover-card-open"]', { hasText: new RegExp(`^${name}$`, "u") }),
  });

/** Enable from the card's "+", shooting the progress, until the card reads Enabled. */
async function enableFromCard(page, shot, name, label) {
  const card = cardOf(page, name).first();
  await card.scrollIntoViewIfNeeded();
  const before = words(await card.innerText());
  const started = Date.now();
  await card.getByRole("button", { name: "Enable" }).click();
  await settle(page, 1200);
  const during = words(await card.innerText());
  await shot(`${label}-progress`);
  let after = "";
  while (Date.now() - started < 300000) {
    after = words(await card.innerText());
    const state = await card.getAttribute("data-state");
    if (state === "enabled" || state === "state") break;
    await settle(page, 2000);
  }
  await settle(page, 800);
  await shot(`${label}-enabled`);
  return {
    before: before.slice(0, 300),
    during: during.slice(0, 300),
    after: after.slice(0, 300),
    state: await card.getAttribute("data-state"),
    seconds: Math.round((Date.now() - started) / 1000),
  };
}

export default async function (page, rawShot) {
  const env = process.env;
  const shot = async (name) => {
    const { width, height } = page.viewportSize();
    await page.mouse.move(width - 6, Math.round(height * 0.55)).catch(() => undefined);
    await settle(page, 600);
    return rawShot(name);
  };
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
  await page.locator('[role="radio"]:visible', { hasText: /^Discover$/u }).first().click();
  await page.locator('[data-slot="discover-card"]').first().waitFor({ timeout: 30000 });
  await settle(page, 3000);
  report.groups = await groups(page);
  await shot("discover");
  const third = page.locator('[data-slot="discover-group"]').nth(2);
  if (await third.count()) {
    await third.evaluate((el) => el.scrollIntoView({ block: "start" }));
    await settle(page);
    await shot("discover-more");
  }

  if (env.DC_SHOWALL === "1") {
    const link = page.locator('[data-slot="discover-show-all"]').first();
    const group = page.locator('[data-slot="discover-group"]').filter({ has: link }).first();
    await group.evaluate((el) => el.scrollIntoView({ block: "start" }));
    report.showAllLink = words(await link.innerText());
    await link.click();
    await settle(page);
    report.showAll = {
      group: words(await group.locator("h3").innerText()),
      cards: await group.locator('[data-slot="discover-card"]').count(),
      link: words(await link.innerText()),
    };
    await shot("show-all");
    await link.click();
    await settle(page);
  }

  if (env.DC_SEARCH) {
    // The search field is always shown (DC-D9).
    const search = page.locator("input[data-extension-search]:visible").first();
    await search.fill(env.DC_SEARCH);
    await settle(page, 2000);
    report.search = await groups(page);
    await page.evaluate(() => window.scrollTo(0, 0));
    await shot("search");
    await search.fill("");
    await settle(page, 1500);
  }

  if (env.DC_ENABLE) report.enable = await enableFromCard(page, shot, env.DC_ENABLE, "enable");

  if (env.DC_SETUP) {
    const card = cardOf(page, env.DC_SETUP).first();
    await card.scrollIntoViewIfNeeded();
    report.setupBefore = words(await card.innerText()).slice(0, 300);
    await card.getByRole("button", { name: "Enable" }).click();
    const pane = page.locator('[data-slot="connector-setup-card"]');
    await pane.waitFor({ timeout: 20000 });
    await settle(page, 1500);
    report.setupPane = words(await pane.innerText()).slice(0, 400);
    await shot("setup-pane");
    const fields = pane.locator("input:visible");
    if (env.DC_ADDRESS) await fields.nth(0).fill(env.DC_ADDRESS);
    if (env.DC_TOKEN) await fields.nth(1).fill(env.DC_TOKEN);
    await shot("setup-filled");
    await pane.getByRole("button", { name: /^Save$/u }).click();
    await page.waitForFunction(
      (id) => document.querySelector(`[data-slot="discover-card"][data-connector="${id}"]`)
        ?.dataset.state === "enable",
      await card.getAttribute("data-connector"),
      { timeout: 60000 },
    );
    await settle(page, 1000);
    // A phone shows the pane instead of the list: close it to see the cards again.
    if (!(await card.isVisible())) {
      await page.getByRole("button", { name: "Back to the list" }).first().click();
      await settle(page);
    }
    report.setup = await enableFromCard(page, shot, env.DC_SETUP, "setup");
  }
  console.log(JSON.stringify(report));
}
