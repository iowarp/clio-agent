// Memory page check for scripts/cloud/ui_check.mjs: switches, tables, a document,
// its history and the review inbox.
//   node scripts/cloud/ui_check.mjs --label memory --steps scripts/cloud/checks/memory_page.mjs
// Set MEMORY_DOC to a document name (user/<category>/<slug>) to open that one.
export default async function memoryPage(page, shot) {
  const base = new URL(page.url()).origin;
  await page.goto(`${base}/settings/memory`, { waitUntil: "networkidle" });
  await page.getByRole("heading", { name: /^Memory/, level: 1 }).waitFor({ timeout: 30000 });
  await page.waitForTimeout(1500);
  await shot("page");
  await page.getByText("Review", { exact: false }).first().scrollIntoViewIfNeeded();
  await shot("inbox");
  const doc = process.env.MEMORY_DOC;
  if (doc) {
    await page.goto(`${base}/settings/memory?doc=${encodeURIComponent(doc)}`, {
      waitUntil: "networkidle",
    });
    await page.getByRole("dialog").waitFor({ timeout: 15000 });
    await page.waitForTimeout(800);
    await shot("document");
    await page.keyboard.press("Escape");
    await page.getByRole("button", { name: new RegExp(`Actions for ${doc.split("/").pop()}`) }).click();
    await page.getByRole("menuitem", { name: "History" }).click();
    await page.getByRole("dialog").waitFor({ timeout: 15000 });
    await page.waitForTimeout(800);
    await shot("history");
    await page.keyboard.press("Escape");
  }
  await page.getByRole("button", { name: "Import" }).click();
  await page.getByRole("dialog").waitFor({ timeout: 15000 });
  await page.getByRole("tab", { name: "From another assistant" }).click();
  await page.waitForTimeout(800);
  await shot("import");
  await page.keyboard.press("Escape");
  await page.goto(`${base}/settings/runtime`, { waitUntil: "networkidle" });
  await page.waitForTimeout(1500);
  await shot("execution-history");
}
