// After a turn, open the answer's memory popover (what the answer took from memory).
//   node scripts/cloud/ui_check.mjs --label memory-used --send "..." --steps scripts/cloud/checks/memory_used.mjs
export default async function memoryUsed(page, shot) {
  const trigger = page.getByRole("button", { name: /^Used \d+ memory document/ }).last();
  await trigger.waitFor({ timeout: 30000 });
  await trigger.scrollIntoViewIfNeeded();
  await trigger.click();
  await page.waitForTimeout(800);
  await shot("popover");
}
