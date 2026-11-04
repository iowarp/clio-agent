// Browser check: send a plain request (CHECK_ASK), answer each permission question the
// way a person would (CHECK_DURATION, default "This session"), then show the
// conversation, the nested browser calls and the shared tab. CHECK_DENY_HOST names a host
// whose question is denied instead. CHECK_SETTLE_MS bounds the turn (default 7 minutes).
export default async function (page, shot) {
  const ask = process.env.CHECK_ASK;
  const duration = process.env.CHECK_DURATION || "This session";
  const denyHost = process.env.CHECK_DENY_HOST || "";
  const settle = Number(process.env.CHECK_SETTLE_MS || 420000);
  if (ask) {
    const composer = page.locator('textarea, [contenteditable="true"]').first();
    await composer.click();
    await page.keyboard.type(ask);
    await page.keyboard.press("Enter");
  }
  const deadline = Date.now() + settle;
  let answered = 0;
  while (Date.now() < deadline) {
    const done = page.getByText("Done", { exact: true }).last();
    if (await done.isVisible().catch(() => false)) break;
    const allow = page.getByRole("button", {
      name: /^(Allow once|Allow for session|Allow for workspace|Allow for the rest of this script)$/,
    });
    if (await allow.count()) {
      const card = allow.first().locator("xpath=ancestor::*[@data-slot][1]");
      await allow.first().scrollIntoViewIfNeeded().catch(() => {});
      await shot(`question-${answered + 1}`);
      const text = (await page.locator("body").innerText()).slice(-4000);
      if (denyHost && text.includes(denyHost) && text.includes("Open a website")) {
        await page.getByRole("button", { name: /^Deny$/ }).first().click();
      } else {
        const picker = page.getByRole("combobox", { name: "Permission duration" }).first();
        if (await picker.count()) {
          await picker.click();
          await page.getByRole("option", { name: duration }).click().catch(() => {});
        }
        await page
          .getByRole("button", {
            name: /^(Allow once|Allow for session|Allow for workspace|Allow for the rest of this script)$/,
          })
          .first()
          .click();
      }
      void card;
      answered += 1;
      await page.waitForTimeout(1500);
      continue;
    }
    await page.waitForTimeout(1000);
  }
  await page.waitForTimeout(1500);
  await shot("answered");
  // The nested browser calls under their run_code row.
  const groups = page.getByRole("button", { name: /^Activity: / });
  for (let i = 0; i < (await groups.count()); i += 1) {
    const group = groups.nth(i);
    if ((await group.getAttribute("aria-expanded")) === "false") {
      await group.click().catch(() => {});
      await page.waitForTimeout(300);
    }
  }
  const row = page.getByRole("button", { name: /Run code/ }).last();
  if (await row.count()) {
    await row.scrollIntoViewIfNeeded().catch(() => {});
    await row.click().catch(() => {});
    await page.waitForTimeout(1200);
    const nested = page.locator('[data-slot="nested-tool-calls"]').last();
    await nested.scrollIntoViewIfNeeded().catch(() => {});
    await shot("nested-calls");
  }
  // The shared tab, opened from the Work rail's Browser pages.
  const work = page.getByRole("button", { name: /^(Work|Session and work|Work rail)/ }).first();
  if (await work.count()) await work.click().catch(() => {});
  await page.waitForTimeout(1000);
  await shot("work-rail");
  const pageRow = page
    .getByRole("region", { name: /Work rail/ })
    .getByRole("button")
    .filter({ hasText: /fixture|Browser|localhost/ })
    .first();
  if (await pageRow.count()) {
    await pageRow.click();
    await page.waitForTimeout(4000);
    await shot("browser-surface");
  }
  console.error(JSON.stringify({ answered }));
}
