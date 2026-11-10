// Check: a web link in an answer opens in the shared browser on a plain click, and the
// page lays out at the panel's width. CHECK_SESSION_PATH names a conversation whose
// answer links CHECK_LINK_HOST (default arxiv.org).
export default async function (page, shot) {
  await page.goto(new URL(process.env.CHECK_SESSION_PATH, page.url()).toString());
  await page.waitForTimeout(4000);
  const host = process.env.CHECK_LINK_HOST || "arxiv.org";
  const link = page.locator(`a[href*="${host}"]`).last();
  await link.scrollIntoViewIfNeeded();
  const title = await link.getAttribute("title");
  await link.click();
  const surface = page.locator('[data-slot="browser-surface"]').first();
  await surface.waitFor({ timeout: 20000 });
  await page.locator('[data-slot="browser-live-view"] img').first().waitFor({ timeout: 30000 });
  await page.waitForTimeout(5000);
  await shot("link-in-panel");
  const address = await page.getByRole("textbox", { name: "Address" }).inputValue().catch(() => "");
  const view = await page.locator('[data-slot="browser-live-view"]').first().boundingBox();
  console.error(JSON.stringify({ title, address, view }));
}
