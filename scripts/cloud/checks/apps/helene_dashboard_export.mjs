// Export the Helene walkthrough's latest dashboard as one HTML file from its viewer
// (Download options > HTML dashboard), as a person sharing it would, then open the file on
// its own and capture it.
//
//   WS=<workspace> SID=<session> EXPORT_OUT=.cloud-run/exports node scripts/cloud/ui_check.mjs \
//     --steps scripts/cloud/checks/apps/helene_dashboard_export.mjs --label dash-export
import { mkdirSync } from "node:fs";
import path from "node:path";

export default async function (page, shot) {
  const out = path.resolve(process.env.EXPORT_OUT || ".cloud-run/exports");
  mkdirSync(out, { recursive: true });
  await page.goto(new URL(`/workspaces/${process.env.WS}/sessions/${process.env.SID}`, page.url()).toString(), {
    waitUntil: "domcontentloaded",
    timeout: 120000,
  });
  await page.locator('textarea, [contenteditable="true"]').last().waitFor({ timeout: 180000 });
  await page.waitForTimeout(5000);
  await page.getByRole("button", { name: /^Open Hurricane Helene, 24-27 Sep 2024/ }).last().click();
  await page.waitForTimeout(8000);
  await page.getByRole("button", { name: "Download options" }).first().click();
  await page.waitForTimeout(800);
  await shot("dashboard-export-menu");
  const download = page.waitForEvent("download", { timeout: 300000 });
  await page.getByRole("menuitem", { name: /HTML dashboard/ }).click();
  const file = await download;
  const target = path.join(out, file.suggestedFilename() || "dashboard.html");
  await file.saveAs(target);
  console.log(`dashboard export: ${target}`);
  const outside = [];
  page.on("request", (request) => {
    if (/^https?:/.test(request.url())) outside.push(request.url());
  });
  await page.goto(`file://${target}`, { waitUntil: "domcontentloaded", timeout: 120000 });
  await page.waitForTimeout(10000);
  await shot("dashboard-offline-top");
  const video = page.locator("video").first();
  console.log("DASHBOARD_VIDEOS", await page.locator("video").count());
  if (await video.count()) {
    await video.scrollIntoViewIfNeeded();
    await page.waitForTimeout(3000);
    await shot("dashboard-offline-recording");
  }
  console.log("DASHBOARD_OUTSIDE_REQUESTS", JSON.stringify(outside.slice(0, 10)));
}
