// Open an exported Helene conversation on its own (file://, nothing else running for it) and
// capture what a colleague sees: the conversation, the saved results and the dashboard,
// with the ParaView view's recording where the live view cannot open.
//
//   OFFLINE_HTML=/abs/path/export.html node scripts/cloud/ui_check.mjs \
//     --steps scripts/cloud/checks/apps/helene_offline.mjs --label offline --width 1440 --height 900
export default async function (page, shot) {
  const file = process.env.OFFLINE_HTML;
  if (!file) throw new Error("OFFLINE_HTML is required");
  const outside = [];
  page.on("request", (request) => {
    const url = request.url();
    if (/^https?:/.test(url)) outside.push(url);
  });
  await page.goto(`file://${file}`, { waitUntil: "domcontentloaded", timeout: 180000 });
  await page.getByRole("tab", { name: "Results", exact: true }).waitFor({ timeout: 180000 });
  await page.waitForTimeout(8000);
  await shot("offline-conversation");
  const title = await page.getByRole("heading", { level: 1 }).filter({ visible: true }).first().textContent();
  const count = await page.getByText(/Transcript.*\d+ messages/).filter({ visible: true }).first().textContent().catch(() => "");
  console.log("OFFLINE_TITLE", title, "|", count);
  // The recording of each ParaView view stands in for the live view.
  const recordings = page.getByText(/this is its recording/);
  console.log("OFFLINE_RECORDINGS_IN_CONVERSATION", await recordings.count());
  await page.getByRole("tab", { name: "Results", exact: true }).click();
  await page.waitForTimeout(6000);
  await shot("offline-results");
  const videos = page.locator("video");
  console.log("OFFLINE_VIDEOS", await videos.count());
  if (await videos.count()) {
    await videos.first().scrollIntoViewIfNeeded();
    await page.waitForTimeout(3000);
    await shot("offline-recording");
  }
  console.log("OFFLINE_OUTSIDE_REQUESTS", JSON.stringify(outside.filter((u) => !u.includes("127.0.0.1")).slice(0, 10)));
}
