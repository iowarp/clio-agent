// Phase 5 check: a finished run_code call and the tool calls nested under it.
// CHECK_SESSION_PATH names the conversation (/workspaces/<id>/sessions/<id>); the
// step opens it, expands the "Run code" row and captures the script, its output and
// the nested calls with their count.
export default async function (page, shot) {
  const path = process.env.CHECK_SESSION_PATH;
  if (path) {
    await page.goto(new URL(path, page.url()).toString());
    await page.waitForTimeout(4000);
  }
  await shot("conversation");
  // Tool calls sit inside collapsed activity groups; open them so the row exists.
  await page.evaluate(() => window.scrollTo(0, 0));
  const scroller = page.locator('[data-slot="conversation-content"], main').first();
  await scroller.evaluate((el) => el.scrollTo?.(0, 0)).catch(() => {});
  const groups = page.getByRole("button", { name: /^Activity: / });
  for (let i = 0; i < (await groups.count()); i += 1) {
    const group = groups.nth(i);
    if ((await group.getAttribute("aria-expanded")) === "false") {
      await group.click().catch(() => {});
      await page.waitForTimeout(300);
    }
  }
  const row = page.getByRole("button", { name: /Run code/ }).first();
  await row.scrollIntoViewIfNeeded().catch(() => {});
  await row.click().catch(() => {});
  await page.waitForTimeout(1500);
  await shot("run-code-row");
  const nested = page.locator('[data-slot="nested-tool-calls"]').first();
  await nested.scrollIntoViewIfNeeded().catch(() => {});
  await shot("nested-calls");
  const all = nested.getByRole("button", { name: /^Show all/ }).first();
  if (await all.count()) {
    await all.click();
    await page.waitForTimeout(800);
    await shot("nested-all");
  }
}
