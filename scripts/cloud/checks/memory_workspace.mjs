// Toggle a workspace's "Use account memory" from its editor and save.
//   MEMORY_WORKSPACE=memory-lab node scripts/cloud/ui_check.mjs --label memory-ws \
//     --steps scripts/cloud/checks/memory_workspace.mjs
export default async function memoryWorkspace(page, shot) {
  const name = process.env.MEMORY_WORKSPACE;
  const actions = page.getByRole("button", { name: `Workspace actions for ${name}` });
  await page.getByText(name, { exact: true }).first().hover();
  await actions.click();
  await page.getByRole("menuitem", { name: "Edit workspace" }).click();
  const dialog = page.getByRole("dialog");
  await dialog.waitFor({ timeout: 15000 });
  const toggle = dialog.getByRole("switch", { name: "Use account memory" });
  await toggle.waitFor({ timeout: 15000 });
  await shot("before");
  await toggle.click();
  await shot("toggled");
  await dialog.getByRole("button", { name: "Save workspace" }).click();
  await dialog.waitFor({ state: "hidden", timeout: 15000 });
  await shot("saved");
}
