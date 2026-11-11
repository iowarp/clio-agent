// Open a paper from the conversation and shoot its workspace (optionally its PDF tab).
//   CAP_WS=ws_... CAP_SID=sess_... CAP_OPEN="<paper card title>" [CAP_TAB=PDF] \
//     node scripts/cloud/ui_check.mjs --steps scripts/cloud/checks/paper_open.mjs --label paper
const UI = process.env.CLIO_UI_URL || "http://127.0.0.1:5173";

export default async function paperOpen(page, shot) {
  const { CAP_WS: ws, CAP_SID: sid, CAP_OPEN: open, CAP_TAB: tab } = process.env;
  await page.goto(`${UI}/workspaces/${ws}/sessions/${sid}`, { waitUntil: "load" });
  await page.waitForTimeout(6000);
  await page.getByText(open, { exact: false }).last().click();
  await page.locator('[data-slot="latex-workspace"]').waitFor({ timeout: 30000 });
  if (tab) {
    await page.getByRole("tab", { name: tab, exact: true }).locator("visible=true").first()
      .click().catch(() => {});
  }
  await page.waitForTimeout(5000);
  const zoomGroups = await page.locator('[data-slot="viewer-zoom-controls"]').locator("visible=true").count();
  console.log(JSON.stringify({ zoomGroups }));
  await shot("paper");
  await page.getByRole("button", { name: /^Build log/ }).locator("visible=true").first().click();
  await page.waitForTimeout(800);
  await shot("log");
}
