// Phase 5 check: a finished run_code call in the most recent conversation.
// Opens the conversation, scrolls to its start, expands the "Run code" row and
// captures the script, its output and the tool calls nested under it.
export default async function (page, shot) {
  await page.locator('[data-slot="session-row"], a[href*="/sessions/"]').first().click().catch(() => {});
  await page.waitForTimeout(2500);
  await page.evaluate(() => {
    for (const el of document.querySelectorAll("*")) {
      if (el.scrollHeight > el.clientHeight + 10 && el.scrollTop > 0) el.scrollTop = 0;
    }
  });
  const row = page.getByText("Run code", { exact: false }).first();
  await row.scrollIntoViewIfNeeded().catch(() => {});
  await row.click().catch(() => {});
  await page.waitForTimeout(1000);
  await shot("run-code-row");
  const nested = page.locator('[data-slot="nested-tool-calls"]').first();
  await nested.scrollIntoViewIfNeeded().catch(() => {});
  await shot("nested-calls");
}
