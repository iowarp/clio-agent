// The compact notice a memory write leaves in the conversation, and its link.
//   node scripts/cloud/ui_check.mjs --label memory-notice --send "Remember ..." \
//     --steps scripts/cloud/checks/memory_notice.mjs
export default async function memoryNotice(page, shot) {
  if (process.env.MEMORY_SESSION) {
    const base = new URL(page.url()).origin;
    await page.goto(`${base}/workspaces/ws_default/sessions/${process.env.MEMORY_SESSION}`, {
      waitUntil: "networkidle",
    });
  }
  // A finished turn folds its activity; its label says memory changed.
  const fold = page.getByRole("button", { name: /updated memory/i }).last();
  await fold.waitFor({ timeout: 30000 });
  await fold.scrollIntoViewIfNeeded();
  await shot("folded");
  await fold.click();
  await page.waitForTimeout(600);
  await shot("expanded");
  // The row's details carry the link to the document on the memory page.
  await page.getByText("Memory updated").last().click();
  await page.waitForTimeout(800);
  const link = page.getByRole("link").filter({ hasText: /./ }).and(
    page.locator('a[href*="/settings/memory?doc="]'),
  );
  await link.first().waitFor({ timeout: 30000 });
  await link.first().scrollIntoViewIfNeeded();
  await shot("notice");
  const href = await link.first().getAttribute("href");
  await link.first().click();
  await page.getByRole("dialog").waitFor({ timeout: 20000 });
  await page.waitForTimeout(800);
  await shot("opened");
  console.error(`notice link: ${href}`);
}
