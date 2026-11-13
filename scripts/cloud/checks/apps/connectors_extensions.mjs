// Check (AQ1, AQ2, AQ3, AQ4, AQ5, AQ11, AQ12): Settings > Extensions lists connectors
// first with their logos (or monograms) and their state in words, and each one's setup
// card shows one row per need.
//
// Environment, all optional:
//   CHECK_CONNECTORS  comma-separated connector names to open, one screenshot each
//   CHECK_OPEN        the name of the connector whose card the steps below act on
//   CHECK_ENABLE=1    press Enable on that card (every conversation)
//   CHECK_FILL        "Label=value;Label=value" rows to type, then Save
//   CHECK_SIGNIN=1    sign in from that card's sign-in row (the fixture's consent page)
//   CHECK_DISABLE=1   press Turn off on that card
//   CHECK_ADD_URL     add a server by URL through "Add by URL or command" (name CHECK_ADD_NAME)
// It prints one JSON line with the list's rows and the open card's state and rows.
const settle = (page, ms = 1500) => page.waitForTimeout(ms);

async function rows(page) {
  return page
    .locator('[role="group"][aria-label="Extensions"] > *')
    .evaluateAll((items) =>
      items
        .map((item) => item.innerText.replace(/\s+/gu, " ").trim())
        // The connector rows; the plugins after them are only counted.
        .filter((text) => text.includes("Connector")),
    );
}

async function openCard(page, name) {
  // A row opens its card on click; the eye button shows only on hover.
  await page
    .locator('[role="group"][aria-label="Extensions"]')
    .getByText(name, { exact: true })
    .first()
    .click();
  const card = page.locator('[data-slot="connector-setup-card"]');
  await card.waitFor({ timeout: 20000 });
  await settle(page);
  return card;
}

export default async function (page, shot) {
  await page.goto(new URL("/settings/plugins", page.url()).toString());
  await page.getByRole("heading", { name: "Extensions" }).first().waitFor({ timeout: 30000 });
  await page.locator('[data-slot="connector-logo"], [data-slot="monogram"]').first().waitFor({
    timeout: 30000,
  });
  await settle(page, 2500);
  await shot("extensions-list");
  const report = { list: await rows(page) };

  if (process.env.CHECK_ADD_URL) {
    await page.getByRole("button", { name: "Add options" }).click();
    await page.getByRole("menuitem", { name: "Add by URL or command" }).click();
    const dialog = page.getByRole("dialog");
    await dialog.getByLabel("Name", { exact: true }).fill(process.env.CHECK_ADD_NAME || "added-by-url");
    await dialog.getByLabel("MCP server URL", { exact: true }).fill(process.env.CHECK_ADD_URL);
    await shot("add-by-url");
    await dialog.getByRole("button", { name: /^Add$/u }).click();
    await settle(page, 4000);
    // A server that asks for a sign-in opens the sign-in window; close it to read the card.
    const signIn = page.getByRole("dialog");
    if (await signIn.count()) {
      await shot("add-by-url-sign-in");
      await page.keyboard.press("Escape");
      await settle(page);
    }
    report.listAfterAdd = await rows(page);
    await shot("extensions-list-after-add");
  }

  for (const name of (process.env.CHECK_CONNECTORS || "").split(",").filter(Boolean)) {
    await openCard(page, name.trim());
    await shot(`card-${name.trim().replace(/\W+/gu, "-").toLowerCase()}`);
  }

  const target = process.env.CHECK_OPEN;
  if (target) {
    const card = await openCard(page, target);
    await shot("card-open");
    if (process.env.CHECK_ENABLE) {
      await card.getByRole("button", { name: "Enable" }).click();
      await settle(page, 6000);
      await shot("card-enabled");
    }
    for (const pair of (process.env.CHECK_FILL || "").split(";").filter(Boolean)) {
      const [label, ...rest] = pair.split("=");
      await card.getByLabel(label.trim(), { exact: true }).fill(rest.join("="));
    }
    if (process.env.CHECK_FILL) {
      await shot("card-filled");
      await card.getByRole("button", { name: "Save" }).click();
      await card.getByRole("button", { name: "Save" }).waitFor({ timeout: 60000 });
      await settle(page, 4000);
      await shot("card-saved");
    }
    if (process.env.CHECK_SIGNIN) {
      const popup = page.context().waitForEvent("page", { timeout: 60000 });
      await card.getByRole("button", { name: /^Sign in/u }).first().click();
      const dialog = page.getByRole("dialog");
      await dialog.waitFor({ timeout: 20000 });
      await dialog.getByRole("button", { name: /^Sign in$/u }).click();
      const consent = await popup;
      await consent.waitForLoadState();
      await shot("consent-page");
      await consent.getByRole("link", { name: "Allow test connection" }).click();
      await dialog.getByText(/^Connected/u).waitFor({ timeout: 90000 });
      await shot("signed-in");
      await dialog.getByRole("button", { name: "Done" }).click();
      await settle(page, 6000);
      await shot("card-after-sign-in");
    }
    if (process.env.CHECK_DISABLE) {
      await card.getByRole("button", { name: "Turn off" }).click();
      await settle(page, 4000);
      await shot("card-turned-off");
    }
    report.card = {
      state: await card.locator('[data-slot="connector-state"]').innerText(),
      rows: await card.locator("[data-need]").evaluateAll((items) =>
        items.map((item) => ({
          key: item.getAttribute("data-need"),
          kind: item.getAttribute("data-need-kind"),
          value: item.querySelector("input")?.value ?? null,
          type: item.querySelector("input")?.type ?? null,
          text: item.innerText.replace(/\s+/gu, " ").trim(),
        })),
      ),
    };
    report.listAfter = await rows(page);
  }
  console.error(JSON.stringify(report));
}
