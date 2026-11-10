// Phase 2 check: the file tools' rows in a finished conversation.
// Opens the most recent conversation, scrolls to its start, expands each group of
// tool calls and the read and search rows inside, and captures the ranged read
// (numbered from the range's first line) and the search results.
export default async function (page, shot) {
  await page.getByText("New conversation", { exact: true }).first().click();
  await page.waitForTimeout(2500);
  await page.mouse.move(1300, 850);
  await page.evaluate(() => {
    for (const el of document.querySelectorAll("*")) {
      if (el.scrollHeight > el.clientHeight + 10 && el.scrollTop > 0) el.scrollTop = 0;
    }
  });
  await page.waitForTimeout(1200);
  const groups = page.getByRole("button", { name: /^Read files/ });
  const count = await groups.count();
  for (let i = 0; i < count; i += 1) {
    await groups.nth(i).click().catch(() => {});
    await page.waitForTimeout(500);
  }
  await shot("groups");
  const rows = page.locator('[data-slot="activity-row"]');
  for (let i = 0; i < Math.min(await rows.count(), 4); i += 1) {
    await rows.nth(i).scrollIntoViewIfNeeded();
    await rows.nth(i).click().catch(() => {});
    await page.waitForTimeout(700);
    await shot(`row-${i}`);
  }
}
