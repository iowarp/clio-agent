// Example steps file for scripts/cloud/ui_check.mjs: open the Runs view.
//   node scripts/cloud/ui_check.mjs --label runs --steps scripts/cloud/checks/runs_view.mjs
// Copy this shape for the view you change: act on the page, then shot("name").
export default async function runsView(page, shot) {
  const visibleRuns = () => page.getByText("Runs", { exact: true }).locator("visible=true");
  if ((await visibleRuns().count()) === 0) {
    // Narrow layouts collapse the sidebar; its toggle is the first header button.
    await page.locator("header button, button").first().click();
    await page.waitForTimeout(500);
    await shot("sidebar");
  }
  await visibleRuns().first().click();
  await page.waitForTimeout(2000);
  await shot("runs");
}
