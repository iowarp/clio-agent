// Friction polish (FP-D6): a chart's own capture inside a dashboard says it marks that
// chart, and its controls offer the whole dashboard; the frame menu names the whole-dashboard
// row.
//
//   FP_SESSION  /workspaces/<w>/sessions/<s> holding the dashboard's card
//   FP_TITLE    the dashboard's title on its card
// Prints FP_CAPTURE with the chart button's name, the controls' title before and after
// "Mark across the whole dashboard", and the frame menu's row.
export default async function (page, shot) {
  const out = {};
  await page.goto(new URL(process.env.FP_SESSION, page.url()).toString());
  await page.waitForTimeout(6000);
  await page.getByText(process.env.FP_TITLE, { exact: true }).last().click();
  const sidebar = page.getByRole("button", { name: "Toggle Sidebar" }).first();
  if (await sidebar.count()) await sidebar.click().catch(() => undefined);
  const maximize = page.getByRole("button", { name: "Maximize canvas" }).first();
  if (await maximize.count()) await maximize.click().catch(() => undefined);
  await page.waitForTimeout(15000);
  const chartButton = page.getByRole("button", { name: "Mark regions in this chart" }).first();
  await chartButton.hover({ force: true }).catch(() => undefined);
  out.chartButton = await chartButton.getAttribute("aria-label");
  await chartButton.click({ force: true });
  await page.waitForTimeout(1500);
  const controls = page.locator('[data-slot="comment-controls"]');
  out.before = (await controls.getAttribute("aria-label")) || "";
  await shot("chart-capture");
  await page.getByRole("button", { name: "Mark across the whole dashboard" }).click();
  await page.waitForTimeout(1500);
  out.after = (await controls.getAttribute("aria-label")) || "";
  await shot("whole-dashboard");
  await controls.getByRole("button", { name: /close/i }).last().click().catch(() => undefined);
  await page.getByRole("button", { name: "File actions" }).filter({ visible: true }).last().click().catch(() => undefined);
  await page.waitForTimeout(800);
  out.menuRow = await page
    .getByRole("menuitem", { name: /Mark regions across the dashboard/ })
    .first()
    .innerText()
    .catch(() => "");
  await shot("frame-menu");
  console.log(`FP_CAPTURE ${JSON.stringify(out)}`);
}
