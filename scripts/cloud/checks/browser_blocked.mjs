// W20 check: after the agent opened a page that redirects itself a few seconds later
// (CHECK_ASK, answered by browser_shared_tab.mjs), wait for the redirect, open the page
// from the Work rail, capture the blocked-host notice, allow the host there and capture
// the page after it loads.
import shared from "./browser_shared_tab.mjs";

export default async function (page, shot) {
  await shared(page, shot);
  await page.waitForTimeout(Number(process.env.CHECK_WAIT_MS || 8000));
  const row = page.getByRole("button", { name: /^Open / }).filter({
    has: page.getByText("a.fixture.localhost", { exact: true }),
  });
  const layout = page.getByRole("button", { name: /^Evidence layout/ }).first();
  for (let i = 0; i < 4 && !(await row.first().isVisible().catch(() => false)); i += 1) {
    await layout.click();
    await page.waitForTimeout(1000);
  }
  await row.first().click();
  const surface = page.locator('[data-slot="browser-surface"]').first();
  await surface.waitFor({ timeout: 20000 });
  const hide = page.getByRole("button", { name: /^Hide work rail/ }).first();
  if (await hide.count()) await hide.click().catch(() => {});
  const grow = page.getByRole("button", { name: /^Maximize canvas/ }).first();
  if (await grow.count()) await grow.click().catch(() => {});
  await page.waitForTimeout(2000);
  await shot("blocked-notice");
  const allow = page.getByRole("button", { name: /^Allow b\.fixture\.localhost/ }).first();
  await allow.click();
  await page.waitForTimeout(4000);
  await shot("allowed");
  const address = await page.getByRole("textbox", { name: "Address" }).inputValue().catch(() => "");
  console.error(JSON.stringify({ addressAfterAllow: address }));
}
