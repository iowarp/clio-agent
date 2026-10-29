// Delete a memory document from the memory page, through its confirmation.
//   MEMORY_SLUG=frontier-project node scripts/cloud/ui_check.mjs --label memory-delete \
//     --steps scripts/cloud/checks/memory_delete.mjs
export default async function memoryDelete(page, shot) {
  const base = new URL(page.url()).origin;
  const slug = process.env.MEMORY_SLUG;
  await page.goto(`${base}/settings/memory`, { waitUntil: "networkidle" });
  const actions = page.getByRole("button", { name: `Actions for ${slug}` });
  await actions.waitFor({ timeout: 30000 });
  await actions.scrollIntoViewIfNeeded();
  await actions.click();
  await page.getByRole("menuitem", { name: "Delete" }).click();
  const confirm = page.getByRole("alertdialog");
  await confirm.waitFor({ timeout: 10000 });
  await page.waitForTimeout(700); // let the open animation settle
  await shot("confirm");
  await confirm.getByRole("button", { name: "Delete" }).click();
  await page.getByText(`Deleted ${slug}`).waitFor({ timeout: 15000 });
  await actions.waitFor({ state: "detached", timeout: 15000 });
  await shot("deleted");
}
