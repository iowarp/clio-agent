// Phase 6 check: the Work rail's Plan section. CHECK_SESSION_PATH names the
// conversation (/workspaces/<id>/sessions/<id>); without it the most recent one opens.
// Captures the rendered plan, then the editor after Edit plan (nothing is saved).
export default async function (page, shot) {
  const path = process.env.CHECK_SESSION_PATH;
  if (path) {
    await page.goto(new URL(path, page.url()).toString());
  } else {
    await page.locator('[data-slot="session-row"], a[href*="/sessions/"]').first().click().catch(() => {});
  }
  await page.waitForTimeout(3000);
  const plan = page.getByRole("region", { name: "Plan" });
  if (!(await plan.isVisible().catch(() => false))) {
    await page.getByRole("button", { name: /^Evidence layout/ }).first().click().catch(() => {});
    await page.waitForTimeout(1500);
  }
  await plan.scrollIntoViewIfNeeded().catch(() => {});
  await shot("work-plan");
  await plan.getByRole("button", { name: "Edit plan" }).click().catch(() => {});
  await page.waitForTimeout(600);
  await shot("plan-editing");
}
