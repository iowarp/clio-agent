// Browser check: open a conversation (CHECK_SESSION_PATH) that has a browser page, show
// the Work rail's Browser pages, open the page in the workbench and capture the shared
// tab with its URL bar, status icons and live frames. CHECK_CLICK_TEXT, when set, clicks
// that text on the live frame (the person acting) and captures the page after it.
export default async function (page, shot) {
  const path = process.env.CHECK_SESSION_PATH;
  if (path) {
    await page.goto(new URL(path, page.url()).toString());
    await page.waitForTimeout(4000);
  }
  const host = process.env.CHECK_HOST || "a.fixture.localhost";
  const layout = page.getByRole("button", { name: /^Evidence layout/ }).first();
  const row = page
    .getByRole("button", { name: /^Open / })
    .filter({ has: page.getByText(host, { exact: true }) })
    .first();
  for (let i = 0; i < 4 && !(await row.isVisible().catch(() => false)); i += 1) {
    await layout.click();
    await page.waitForTimeout(1000);
  }
  await shot("browser-pages");
  await row.click();
  const surface = page.locator('[data-slot="browser-surface"]').first();
  await surface.waitFor({ timeout: 20000 });
  const hide = page.getByRole("button", { name: /^Hide work rail/ }).first();
  if (await hide.count()) await hide.click().catch(() => {});
  if (process.env.CHECK_MAXIMIZE !== "0") {
    const grow = page.getByRole("button", { name: /^(Maximize|Expand) (canvas|workbench)/ }).first();
    if (await grow.count()) await grow.click().catch(() => {});
  }
  await page.locator('[data-slot="browser-live-view"] img').first().waitFor({ timeout: 20000 });
  await page.waitForTimeout(1500);
  await shot("browser-surface");
  const click = process.env.CHECK_CLICK_TEXT;
  if (click) {
    const view = page.locator('[data-slot="browser-live-view"]').first();
    const box = await view.boundingBox();
    if (box) {
      await page.mouse.click(box.x + box.width * 0.1, box.y + box.height * 0.1);
      await page.waitForTimeout(1500);
      await shot("after-click");
    }
  }
}
