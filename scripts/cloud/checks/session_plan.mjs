// Phase 6 check: the Work rail's Plan section for the most recent conversation.
export default async function (page, shot) {
  await page.locator('[data-slot="session-row"], a[href*="/sessions/"]').first().click().catch(() => {});
  await page.waitForTimeout(2500);
  await page.getByRole("button", { name: /^Work|Session work/ }).first().click().catch(() => {});
  await page.waitForTimeout(1500);
  const plan = page.getByRole("region", { name: "Plan" });
  await plan.scrollIntoViewIfNeeded().catch(() => {});
  await shot("work-plan");
}
