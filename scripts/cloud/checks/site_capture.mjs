// Site captures from an existing conversation: open it, optionally scroll to some text,
// click a control, or type into the composer, then take one shot.
//   CAP_WS=ws_... CAP_SID=sess_... [CAP_FIND="You started explore"] [CAP_CLICK="Prompt define_term"]
//   [CAP_TYPE="/wtfp:"] [CAP_SETTLE=8000] [CAP_WAIT=1500] node scripts/cloud/ui_check.mjs \
//     --steps scripts/cloud/checks/site_capture.mjs --label site-x [--width 390 --height 844 --touch]
// The sidebar is collapsed first, so the shot shows the conversation at its working width.

const UI = process.env.CLIO_UI_URL || "http://127.0.0.1:5173";

export default async function siteCapture(page, shot) {
  const { CAP_WS: ws, CAP_SID: sid, CAP_FIND: find, CAP_CLICK: click, CAP_TYPE: type } = process.env;
  if (!ws || !sid) throw new Error("CAP_WS and CAP_SID are required");
  await page.goto(`${UI}/workspaces/${ws}/sessions/${sid}`, { waitUntil: "load" });
  await page.waitForTimeout(5000);
  if (await page.getByText("Workspaces", { exact: true }).first().isVisible().catch(() => false)) {
    await page.locator('[data-sidebar="trigger"]').first().click().catch(() => {});
    await page.waitForTimeout(600);
  }
  // Long conversations render Markdown progressively; let it settle before the shot.
  await page.waitForTimeout(Number(process.env.CAP_SETTLE || 0));
  if (find) {
    const target = page.getByText(find, { exact: false }).first();
    await target.scrollIntoViewIfNeeded().catch(() => {});
    await page.evaluate(() => window.scrollBy(0, 0));
    await page.waitForTimeout(600);
  }
  if (click) {
    await page.getByText(click, { exact: false }).first().click().catch(() => {});
    await page.waitForTimeout(800);
  }
  if (type) {
    await page.locator('[contenteditable="true"]').first().click();
    await page.keyboard.type(type);
  }
  await page.waitForTimeout(Number(process.env.CAP_WAIT || 1500));
  await shot("capture");
}
