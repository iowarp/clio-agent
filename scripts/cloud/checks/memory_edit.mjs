// Correct a remembered fact on the memory page, the way a person would.
//   MEMORY_DOC=user/topics/frontier-project MEMORY_FROM=CSC456 MEMORY_TO=CSC789 \
//     node scripts/cloud/ui_check.mjs --label memory-edit --steps scripts/cloud/checks/memory_edit.mjs
export default async function memoryEdit(page, shot) {
  const base = new URL(page.url()).origin;
  const doc = process.env.MEMORY_DOC;
  const from = process.env.MEMORY_FROM;
  const to = process.env.MEMORY_TO;
  await page.goto(`${base}/settings/memory?doc=${encodeURIComponent(doc)}`, {
    waitUntil: "networkidle",
  });
  const dialog = page.getByRole("dialog");
  await dialog.waitFor({ timeout: 20000 });
  await dialog.getByText(from, { exact: false }).first().waitFor({ timeout: 20000 });
  await shot("before");
  await dialog.getByRole("button", { name: new RegExp(`Correct .*${from}`) }).click();
  const input = dialog.getByRole("textbox", { name: "Corrected fact" });
  const current = await input.inputValue();
  await input.fill(current.replace(from, to));
  await shot("editing");
  await dialog.getByRole("button", { name: "Save correction" }).click();
  await dialog.getByText(to, { exact: false }).first().waitFor({ timeout: 20000 });
  await page.waitForTimeout(800);
  await shot("after");
}
